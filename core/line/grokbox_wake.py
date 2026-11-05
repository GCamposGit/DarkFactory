"""Pre-flight probe and wake-up email for the grokbox test worker (USR-310).

The grokbox (Tailscale ``100.77.130.5``) is the official gate's primary test
worker: it runs the full suite in ~2.5 min versus 23-40 min on the Notebook or
Desktop fallback (see ``core.harness.remote_dispatch.DEFAULT_WORKERS``). When
it falls over, today it only comes back once a human notices. This module:

1. Probes its ``/health`` during ``run_ticket``'s pre-flight (:func:`preflight_check`)
   and, if unreachable and explicitly authorized, emails the grokbot
   (:mod:`core.integrations.email_worker`) asking for it to be brought back online
   -- at most once every 30 minutes across worktrees and processes.
2. Before the official gate runs, gives a grokbox that was down at pre-flight a
   window (``DARKFAC_GROKBOX_WAKE_WAIT_SEC``, default 300s) to come back online
   (:func:`wait_if_down`), then lets the existing worker-selection fallback in
   ``core.harness.remote_dispatch`` take over exactly as it does today.

Every public function here is fail-open: a probe or email failure is logged and
swallowed, never raised, and never changes `run_ticket`'s outcome.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.harness.remote_dispatch import probe_health
from core.integrations.email_worker import send_grokbot_email
from core.paths import state_root
from core.usage.probe_deadline import run_with_deadline

logger = logging.getLogger("darkfac.line.grokbox_wake")

ENV_GROKBOX_URL = "DARKFAC_GROKBOX_URL"
DEFAULT_GROKBOX_URL = "http://100.77.130.5:8080"
ENV_WAKE_WAIT_SEC = "DARKFAC_GROKBOX_WAKE_WAIT_SEC"
DEFAULT_WAKE_WAIT_SEC = 300.0
#: Minimum spacing between authorized wake-up attempts (at most once per 30 min).
WAKE_EMAIL_THROTTLE_SEC = 1800.0
DEFAULT_WAIT_POLL_INTERVAL_SEC = 10.0
LOCK_WAIT_SEC = 10.0
LOCK_POLL_SEC = 0.05

STATE_SUBDIR = "grokbox_wake"
STATE_FILE_NAME = "state.json"

WAKE_EMAIL_SUBJECT = "DarkFac: grokbox fora do ar"
WAKE_EMAIL_TEXT = (
    "O grokbox (worker oficial do portao de testes, 100.77.130.5) nao respondeu ao "
    "/health durante o pre-flight de um ticket da DarkFac. Por favor deixe-o pronto: "
    "VM Online no Tailscale, worker do runbook docs/runbooks/grokbox_test_worker.md "
    "com o repo atualizado, e /health respondendo."
)


def grokbox_url() -> str:
    return os.environ.get(ENV_GROKBOX_URL, "").strip() or DEFAULT_GROKBOX_URL


def wake_wait_limit_s() -> float:
    raw = os.environ.get(ENV_WAKE_WAIT_SEC, "").strip()
    if not raw:
        return DEFAULT_WAKE_WAIT_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WAKE_WAIT_SEC
    return value if value >= 0 else DEFAULT_WAKE_WAIT_SEC


class _ThrottleStateError(RuntimeError):
    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(detail or cause)
        self.cause = cause


def _shared_state_root() -> Path:
    """Use the main checkout's state for linked worktrees; explicit roots stay isolated."""
    if os.environ.get("DARKFAC_STATE_ROOT"):
        return state_root()

    repository = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        )
        common_dir = Path(result.stdout.strip()).resolve()
        return common_dir.parent / ".factory"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise _ThrottleStateError("shared_state_root_unavailable", str(exc)) from exc


def _state_file() -> Path:
    directory = _shared_state_root() / STATE_SUBDIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _ThrottleStateError("state_write_failed", str(exc)) from exc
    return directory / STATE_FILE_NAME


@contextmanager
def _throttle_lock(path: Path) -> Iterator[None]:
    """Acquire a cross-process lock, failing closed if the lock cannot be obtained."""
    lock_path = path.with_suffix(".lock")
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise _ThrottleStateError("lock_unavailable", str(exc)) from exc

    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
        except OSError as exc:
            raise _ThrottleStateError("lock_unavailable", str(exc)) from exc
        deadline = time.monotonic() + LOCK_WAIT_SEC
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise _ThrottleStateError("lock_unavailable", str(exc)) from exc
                time.sleep(LOCK_POLL_SEC)
        try:
            yield
        finally:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.warning("Could not release grokbox wake state lock")
    finally:
        handle.close()


def _load_state() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state must be a JSON object")
        return data
    except Exception as exc:
        raise _ThrottleStateError("state_unreadable", str(exc)) from exc


def _save_state(data: dict[str, Any]) -> None:
    path = _state_file()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        raise _ThrottleStateError("state_write_failed", str(exc)) from exc


def probe_grokbox() -> bool:
    """True when the grokbox answers ``/health``; False on anything else, never raises."""
    try:
        return probe_health(grokbox_url()) is not None
    except Exception as exc:  # noqa: BLE001 - a probe failure must never bubble up
        logger.warning("Grokbox health probe raised: %s", exc)
        return False


def preflight_check(
    *,
    allow_external_communication: bool = False,
    probe: Callable[[], bool] = probe_grokbox,
    send_email: Callable[[str, str], dict[str, Any]] = send_grokbot_email,
    now: datetime | None = None,
) -> bool:
    """Probe the grokbox; email the grokbot only when explicitly authorized.

    Authorized emails are throttled to once per 30 min, with state persisted on disk.
    Returns True when the grokbox was found down
    (so the caller can later wait for it before the gate via :func:`wait_if_down`).
    Never raises: a probe or email failure is logged and treated as "still down".
    """

    moment = now or datetime.now(UTC)
    t_start = time.monotonic()

    try:
        healthy = bool(run_with_deadline("grokbox_probe", probe))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Grokbox pre-flight probe failed: %s", exc)
        healthy = False

    logger.info("wake preflight probe healthy=%s duration_s=%.3f", healthy, time.monotonic() - t_start)
    if healthy:
        try:
            path = _state_file()
            with _throttle_lock(path):
                state = _load_state()
                state["down"] = False
                _save_state(state)
        except _ThrottleStateError as exc:
            logger.info(
                "provider=grokbox_wake_state outcome=suppressed cause=%s", exc.cause
            )
        return False

    if not allow_external_communication:
        logger.info(
            "provider=grokbox_wake_email outcome=suppressed "
            "cause=external_communication_not_authorized"
        )
        try:
            path = _state_file()
            with _throttle_lock(path):
                state = _load_state()
                state["down"] = True
                _save_state(state)
        except _ThrottleStateError as exc:
            logger.info(
                "provider=grokbox_wake_state outcome=suppressed cause=%s", exc.cause
            )
        return True

    try:
        path = _state_file()
        with _throttle_lock(path):
            state = _load_state()
            state["down"] = True
            last_sent_raw = state.get("last_email_sent_at")
            if last_sent_raw:
                try:
                    last_sent = datetime.fromisoformat(last_sent_raw)
                    if last_sent.tzinfo is None:
                        last_sent = last_sent.replace(tzinfo=UTC)
                    elapsed = (moment - last_sent).total_seconds()
                except (TypeError, ValueError) as exc:
                    raise _ThrottleStateError("state_unreadable", "invalid last_email_sent_at") from exc
                if elapsed < WAKE_EMAIL_THROTTLE_SEC:
                    _save_state(state)
                    logger.info(
                        "provider=grokbox_wake_email outcome=suppressed cause=throttle_active "
                        "last_email_sent_at=%s",
                        last_sent_raw,
                    )
                    return True

            # Reserve the window before the external side effect. A timeout or a
            # failed response may still mean the remote provider accepted the email.
            state["last_email_sent_at"] = moment.isoformat()
            _save_state(state)
            t_mail = time.monotonic()
            result = run_with_deadline(
                "grokbox_wake_email", lambda: send_email(WAKE_EMAIL_SUBJECT, WAKE_EMAIL_TEXT)
            )
            logger.info("wake email duration_s=%.3f", time.monotonic() - t_mail)
            if isinstance(result, dict) and result.get("ok"):
                logger.info("provider=grokbox_wake_email outcome=ok")
            else:
                logger.info(
                    "provider=grokbox_wake_email outcome=suppressed cause=email_send_failed"
                )
                logger.warning(
                    "Grokbot wake email not confirmed: %s",
                    result.get("error") if isinstance(result, dict) else result,
                )
    except _ThrottleStateError as exc:
        logger.info(
            "provider=grokbox_wake_email outcome=suppressed cause=%s", exc.cause
        )
    except Exception as exc:  # noqa: BLE001 - a send/probe issue must not stop the launcher
        logger.info(
            "provider=grokbox_wake_email outcome=suppressed cause=email_send_failed"
        )
        logger.warning("Grokbot wake email raised: %s", exc)
    return True


def wait_if_down(
    *,
    probe: Callable[[], bool] = probe_grokbox,
    sleep: Callable[[float], Any] = time.sleep,
    limit_s: float | None = None,
    poll_s: float = DEFAULT_WAIT_POLL_INTERVAL_SEC,
    announce: Callable[[str], Any] | None = None,
) -> bool:
    """Before the official gate: if :func:`preflight_check` found the grokbox down, poll
    its ``/health`` for up to ``limit_s`` (default ``DARKFAC_GROKBOX_WAKE_WAIT_SEC``).
    A no-op -- no wait, no probe -- when the grokbox was already up at pre-flight.

    Returns True when the grokbox answered healthy before the deadline (always False
    when there was nothing to wait for). Never raises; the existing worker-selection
    fallback in ``core.harness.remote_dispatch`` runs exactly as it does today
    regardless of the outcome here.
    """

    try:
        state = _load_state()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read grokbox wake state before the gate: %s", exc)
        return False
    if not state.get("down"):
        return False

    limit = wake_wait_limit_s() if limit_s is None else limit_s
    say = announce or (lambda _msg: None)
    waited = 0.0
    woke_up = False
    try:
        while waited < limit:
            try:
                if run_with_deadline("grokbox_probe", probe):
                    woke_up = True
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("Grokbox wait probe raised: %s", exc)
            step = min(poll_s, limit - waited)
            if step <= 0:
                break
            say(f"[GROKBOX] ainda fora do ar; aguardando (limite {limit:.0f}s, {waited:.0f}s decorridos)")
            sleep(step)
            waited += step
    finally:
        # The wait window (if any) has been consumed either way; the next pre-flight
        # probe re-establishes the flag from scratch instead of waiting again on a
        # later self-correction retry of the same gate run.
        state["down"] = False
        _save_state(state)

    return woke_up
