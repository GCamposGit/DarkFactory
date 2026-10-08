"""Per-ticket cancellation of a local ``run_ticket.py`` run (USR-166).

USR-152 made cancelling a *line* run kill the agent's process tree and block commit/push, through the
``core.line.cancellation`` scope (token + probe) that ``agent_cli._run_bounded`` already observes. The
headless launcher has no control store and no per-run worker, so nothing could reach the agent of a
ticket executed locally. This module closes that gap without a second mechanism: it only supplies the
*source* of the cancellation for the launcher and reuses the same scope, token and guards.

Sources (any of them cancels the run):

- a control file ``<state_root>/local_cancel/<TICKET>.cancel`` (portable: written by
  ``python run_ticket.py --cancel USR-XX`` or by hand, honoured by the launcher *and* by its delivery
  subprocess, which share the file system);
- a termination signal delivered to the launcher process (SIGINT, SIGTERM, and SIGBREAK on Windows).

``watch`` opens the ``run_scope`` whose probe is the control file, plus a daemon thread that re-checks
the probe every ``RUNNER_POLL_INTERVAL_S`` so the token is set while the agent runs and
``_run_bounded`` kills the process tree within about one second. Outside ``watch`` everything is a no-op.
"""

from __future__ import annotations

import json
import logging
import re
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.line import cancellation
from core.paths import state_root

logger = logging.getLogger(__name__)

CONTROL_DIRNAME = "local_cancel"
CONTROL_SUFFIX = ".cancel"
SIGNAL_REASON = "signal"
# Set by the launcher for its delivery subprocess: the child joins the parent's run and never owns the request.
ENV_PARENT = "DARKFAC_LOCAL_CANCEL_PARENT"
# Requests older than this process are leftovers of an earlier run (see ``watch``).
LAUNCHED_AT = time.time()
_SAFE_TICKET = re.compile(r"[^A-Za-z0-9_.-]")


def control_dir(root: Optional[Path] = None) -> Path:
    """Directory holding the per-ticket control files (inside the factory state root)."""
    return (Path(root) if root is not None else state_root()) / CONTROL_DIRNAME


def control_file(ticket_id: str, root: Optional[Path] = None) -> Path:
    """Control file of ``ticket_id`` (the id is sanitised: it can never escape the control directory)."""
    safe = _SAFE_TICKET.sub("_", ticket_id.strip()) or "_"
    return control_dir(root) / f"{safe}{CONTROL_SUFFIX}"


def request_cancel(
    ticket_id: str, reason: str = cancellation.CANCELLED_CAUSE, root: Optional[Path] = None
) -> Path:
    """Ask the run in progress for ``ticket_id`` to stop; returns the control file written."""
    path = control_file(ticket_id, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ticket_id": ticket_id,
        "reason": reason,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def cancel_requested(ticket_id: str, root: Optional[Path] = None) -> bool:
    """True while a cancel request for ``ticket_id`` exists."""
    try:
        return control_file(ticket_id, root).is_file()
    except OSError:
        return False


def read_request(ticket_id: str, root: Optional[Path] = None) -> dict[str, Any]:
    """Content of the request (empty when absent or unreadable; the file's existence is what counts)."""
    try:
        data = json.loads(control_file(ticket_id, root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def request_age_reference(ticket_id: str, root: Optional[Path] = None) -> Optional[float]:
    """Modification time of the request file, or None when absent."""
    try:
        return control_file(ticket_id, root).stat().st_mtime
    except OSError:
        return None


def clear_request(ticket_id: str, root: Optional[Path] = None) -> bool:
    """Remove the request (consumed, or stale from an older run). Returns True when a file was removed."""
    try:
        control_file(ticket_id, root).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("could not remove cancel request for %s: %s", ticket_id, exc)
        return False


def _termination_signals() -> list[int]:
    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


@contextmanager
def watch(
    ticket_id: str,
    run_id: str,
    *,
    fresh: bool = True,
    not_before: Optional[float] = None,
    poll_s: Optional[float] = None,
    install_signals: bool = True,
    root: Optional[Path] = None,
) -> Iterator[cancellation.CancelToken]:
    """Run the enclosed code inside a cancellation scope fed by the control file and signals.

    ``fresh`` marks the process that owns the run: it discards a stale request left by an earlier
    cancellation before starting and removes the request when it finishes (the run consumed it). A
    request is stale when written before ``not_before`` (epoch seconds; default: now), so the launcher
    passes its own start time and still honours a ``--cancel`` issued while it was running preflight. A
    delivery subprocess joining the same run passes ``fresh=False`` so it never erases the parent's
    request. When a scope is already active (in-process delivery), it is reused unchanged.
    """
    existing = cancellation.current_token()
    if existing is not None:
        yield existing
        return

    if fresh:
        written = request_age_reference(ticket_id, root)
        if written is not None and written <= (not_before if not_before is not None else time.time()):
            clear_request(ticket_id, root)

    def probe() -> bool:
        return cancel_requested(ticket_id, root)

    stop = threading.Event()
    previous_handlers: dict[int, Any] = {}
    with cancellation.run_scope(run_id, probe) as token:

        def poll() -> None:
            interval = poll_s if poll_s is not None else cancellation.RUNNER_POLL_INTERVAL_S
            while not stop.wait(interval):
                token.refresh()

        thread = threading.Thread(target=poll, name=f"local-cancel-{ticket_id}", daemon=True)
        thread.start()

        if install_signals and threading.current_thread() is threading.main_thread():
            def on_signal(signum: int, frame: Any) -> None:
                if token.is_cancelled():
                    # Second signal: the owner insists, fall back to the default behaviour.
                    previous = previous_handlers.get(signum, signal.SIG_DFL)
                    signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
                    if callable(previous):
                        previous(signum, frame)
                    else:
                        raise KeyboardInterrupt
                    return
                logger.warning("signal %s received: cancelling run %s", signum, run_id)
                token.cancel(SIGNAL_REASON)

            for signum in _termination_signals():
                try:
                    previous_handlers[signum] = signal.signal(signum, on_signal)
                except (ValueError, OSError):
                    continue
        try:
            yield token
        finally:
            stop.set()
            for signum, previous in previous_handlers.items():
                try:
                    signal.signal(signum, previous if previous is not None else signal.SIG_DFL)
                except (ValueError, OSError):
                    pass
            thread.join(timeout=2.0)
            if fresh:
                clear_request(ticket_id, root)
