"""Cooperative run cancellation for the production line (USR-152).

`SQLiteControlStore.cancel_run` / `PostgresControlStore.cancel_run` (USR-123) only flip rows in the control
store: the run and its open jobs become `cancelled` (cause `owner_cancelled`) and the claim is released.
Nothing told the worker thread that is *inside* a stage handler, so the agent CLI it had spawned kept
running (and later committed and pushed on a cancelled run: run-6594bec531c2, 07/10/2026).

This module is the bridge between the store and the code that executes the agent:

- the worker opens a `run_scope(run_id, probe)` around a stage handler. `probe()` answers "is this run
  cancelled in the store?" and is polled by the lease-heartbeat thread, so a cancellation is noticed within
  one heartbeat interval (<= `CANCEL_POLL_INTERVAL_S`), far below the 60 s acceptance bound;
- `agent_cli._run_bounded` watches the scope's `CancelToken` while the agent runs and kills the whole
  process tree as soon as it is set;
- `workspace.commit` / `commit_paths` / `push` call `ensure_not_cancelled` first (re-verifying against the
  store through the probe), so a cancelled run never gets a new commit on `df/<run_id>`.

Outside a scope (launcher runs, tests, no store) every function is a no-op: nothing changes for them.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

CANCELLED_CAUSE = "owner_cancelled"

# Heartbeat/cancel-poll cadence of the worker; the acceptance criterion allows 60 s end to end.
CANCEL_POLL_INTERVAL_S = 10.0
# How often the bounded runner re-checks the token while the agent process is alive.
RUNNER_POLL_INTERVAL_S = 1.0


class RunCancelledError(RuntimeError):
    """Raised by a guard when the run was cancelled: the caller must stop and publish nothing."""

    def __init__(self, run_id: str, reason: str = CANCELLED_CAUSE) -> None:
        super().__init__(f"run {run_id} was cancelled ({reason}); refusing to continue")
        self.run_id = run_id
        self.reason = reason


class CancelToken:
    """Cancellation flag of one run, optionally backed by a store probe."""

    def __init__(self, run_id: str, probe: Optional[Callable[[], bool]] = None) -> None:
        self.run_id = run_id
        self._probe = probe
        self._event = threading.Event()
        self.reason: str = CANCELLED_CAUSE

    def cancel(self, reason: str = CANCELLED_CAUSE) -> None:
        """Mark the run cancelled (idempotent)."""
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    def is_cancelled(self) -> bool:
        """Cheap, store-free read of the flag."""
        return self._event.is_set()

    def refresh(self) -> bool:
        """Re-verify against the store (when a probe exists) and return the flag.

        A probe failure never cancels (fail open): a flaky database must not kill a healthy run; the next
        poll or guard tries again.
        """
        if self._event.is_set():
            return True
        if self._probe is None:
            return False
        try:
            cancelled = bool(self._probe())
        except Exception as exc:
            logger.warning("cancel probe failed for run %s: %s", self.run_id, exc)
            return False
        if cancelled:
            self.cancel()
        return cancelled


_REGISTRY: dict[str, CancelToken] = {}
_REGISTRY_LOCK = threading.Lock()
_CURRENT: contextvars.ContextVar[Optional[CancelToken]] = contextvars.ContextVar("df_cancel_token", default=None)


@contextmanager
def run_scope(run_id: str, probe: Optional[Callable[[], bool]] = None) -> Iterator[CancelToken]:
    """Register a `CancelToken` for `run_id` for the duration of a stage handler."""
    token = CancelToken(run_id, probe)
    with _REGISTRY_LOCK:
        _REGISTRY[run_id] = token
    reset = _CURRENT.set(token)
    try:
        yield token
    finally:
        _CURRENT.reset(reset)
        with _REGISTRY_LOCK:
            if _REGISTRY.get(run_id) is token:
                del _REGISTRY[run_id]


def current_token() -> Optional[CancelToken]:
    """Token of the scope the calling code runs in (None outside any scope)."""
    return _CURRENT.get()


def token_for(run_id: Optional[str]) -> Optional[CancelToken]:
    """Token registered for `run_id`, falling back to the calling scope's token."""
    if run_id:
        with _REGISTRY_LOCK:
            token = _REGISTRY.get(run_id)
        if token is not None:
            return token
    return _CURRENT.get()


def ensure_not_cancelled(run_id: Optional[str] = None) -> None:
    """Raise `RunCancelledError` when the run is cancelled; no-op outside a cancellation scope."""
    token = token_for(run_id)
    if token is not None and token.refresh():
        raise RunCancelledError(token.run_id, token.reason)


def store_probe(store: Any, run_id: str) -> Callable[[], bool]:
    """Build a probe answering "is `run_id` cancelled?" from `store.get_run_status`."""

    def _probe() -> bool:
        status = store.get_run_status(run_id)
        return bool(status) and str(status.get("status")) == "cancelled"

    return _probe
