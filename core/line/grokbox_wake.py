"""Pre-flight probe and wake-up email for the grokbox test worker (USR-310).

The grokbox (Tailscale ``100.77.130.5``) is the official gate's primary test
worker: it runs the full suite in ~2.5 min versus 23-40 min on the Notebook or
Desktop fallback (see ``core.harness.remote_dispatch.DEFAULT_WORKERS``). When
it falls over, today it only comes back once a human notices. This module:

1. Probes its ``/health`` during ``run_ticket``'s pre-flight (:func:`preflight_check`)
   and, if unreachable, emails the grokbot (:mod:`core.integrations.email_worker`)
   asking for it to be brought back online -- at most once every 30 minutes
   (state persisted on disk, since pre-flight and the gate run in separate
   processes across ``--resume-delivery``).
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
import time
from collections.abc import Callable
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
#: Minimum spacing between two wake-up emails (USR-310 acceptance criteria: at most once per 30 min).
WAKE_EMAIL_THROTTLE_SEC = 1800.0
DEFAULT_WAIT_POLL_INTERVAL_SEC = 10.0

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


def _state_file() -> Path:
    directory = state_root() / STATE_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / STATE_FILE_NAME


def _load_state() -> dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Could not read grokbox wake state (%s): %s", path, exc)
        return {}


def _save_state(data: dict[str, Any]) -> None:
    path = _state_file()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Could not persist grokbox wake state (%s): %s", path, exc)


def probe_grokbox() -> bool:
    """True when the grokbox answers ``/health``; False on anything else, never raises."""
    try:
        return probe_health(grokbox_url()) is not None
    except Exception as exc:  # noqa: BLE001 - a probe failure must never bubble up
        logger.warning("Grokbox health probe raised: %s", exc)
        return False


def preflight_check(
    *,
    probe: Callable[[], bool] = probe_grokbox,
    send_email: Callable[[str, str], dict[str, Any]] = send_grokbot_email,
    now: datetime | None = None,
) -> bool:
    """Probe the grokbox; if it is down, email the grokbot (throttled to once per
    30 min, state persisted on disk). Returns True when the grokbox was found down
    (so the caller can later wait for it before the gate via :func:`wait_if_down`).
    Never raises: a probe or email failure is logged and treated as "still down".
    """

    moment = now or datetime.now(UTC)
    state = _load_state()
    t_start = time.monotonic()

    try:
        healthy = bool(run_with_deadline("grokbox_probe", probe))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Grokbox pre-flight probe failed: %s", exc)
        healthy = False

    logger.info("wake preflight probe healthy=%s duration_s=%.3f", healthy, time.monotonic() - t_start)
    if healthy:
        state["down"] = False
        _save_state(state)
        return False

    state["down"] = True
    last_sent_raw = state.get("last_email_sent_at")
    should_send = True
    if last_sent_raw:
        try:
            last_sent = datetime.fromisoformat(last_sent_raw)
            should_send = (moment - last_sent).total_seconds() >= WAKE_EMAIL_THROTTLE_SEC
        except ValueError:
            should_send = True

    if should_send:
        try:
            t_mail = time.monotonic()
            result = run_with_deadline("grokbox_wake_email", lambda: send_email(WAKE_EMAIL_SUBJECT, WAKE_EMAIL_TEXT))
            logger.info("wake email duration_s=%.3f", time.monotonic() - t_mail)
            if isinstance(result, dict) and result.get("ok"):
                state["last_email_sent_at"] = moment.isoformat()
            else:
                logger.warning(
                    "Grokbot wake email not sent: %s",
                    result.get("error") if isinstance(result, dict) else result,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Grokbot wake email raised: %s", exc)
    else:
        logger.info("Grokbot wake email throttled (last sent %s)", last_sent_raw)

    _save_state(state)
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
