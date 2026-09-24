#!/usr/bin/env python3
"""Machine-wide suite lock so only one full test suite runs per host at a time.

Stdlib only. Uses ``msvcrt.locking`` on Windows and ``fcntl.flock`` on POSIX.
The operating system releases the lock automatically when the holding process
dies, so there is never a stale lock to clean up by hand.

The lock directory is machine-global (shared by every worktree/clone on the
host), not inside any repository, because the whole point is to prevent two
independent checkouts (or two harnesses: Claude Code, Codex, Grok,
Antigravity) from running the full suite on the same box at the same time.

Env vars
--------
``DARKFAC_HARNESS_STATE_DIR``
    Overrides the machine-global state directory.
``DARKFAC_SUITE_SLOTS``
    Number of concurrent suite runs allowed on this host (default 1).
``DARKFAC_SUITE_LOCK_TIMEOUT_SEC``
    Max seconds to wait for a free slot before failing closed (default 3600).
``DARKFAC_SUITE_LOCK``
    Set to ``off`` to disable locking entirely (opt-out escape hatch).
``DARKFAC_SUITE_LOCK_HELD``
    Set to ``1`` by a holder for its child processes, so a nested pytest
    invocation (e.g. the harness runner shelling out to pytest) does not try
    to acquire the same machine-wide lock again and deadlock against itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

_DEFAULT_SLOTS = 1
_DEFAULT_TIMEOUT_SEC = 3600
_POLL_MIN_SEC = 0.5
_POLL_MAX_SEC = 5.0


class SuiteLockTimeout(RuntimeError):
    """Raised when no slot became free before the configured timeout."""


def _env_flag_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "off"


def state_dir() -> Path:
    """Machine-global directory holding lock files and sidecar metadata."""

    configured = os.environ.get("DARKFAC_HARNESS_STATE_DIR")
    if configured:
        path = Path(configured).expanduser()
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        path = base / "DarkFac" / "harness"
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
        path = base / "darkfac" / "harness"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _slot_count() -> int:
    raw = os.environ.get("DARKFAC_SUITE_SLOTS", "").strip()
    if not raw:
        return _DEFAULT_SLOTS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_SLOTS
    return value if value > 0 else _DEFAULT_SLOTS


def _timeout_sec() -> float:
    raw = os.environ.get("DARKFAC_SUITE_LOCK_TIMEOUT_SEC", "").strip()
    if not raw:
        return float(_DEFAULT_TIMEOUT_SEC)
    try:
        value = float(raw)
    except ValueError:
        return float(_DEFAULT_TIMEOUT_SEC)
    return value if value > 0 else float(_DEFAULT_TIMEOUT_SEC)


def is_disabled() -> bool:
    return _env_flag_off("DARKFAC_SUITE_LOCK")


def is_held_by_ancestor() -> bool:
    """True when a parent process already holds the machine-wide lock."""

    return os.environ.get("DARKFAC_SUITE_LOCK_HELD", "").strip() == "1"


def _detect_harness() -> str | None:
    for env_name, label in (
        ("CLAUDECODE", "claude-code"),
        ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),
        ("CODEX_SANDBOX", "codex"),
        ("GROK_CLI", "grok"),
        ("ANTIGRAVITY_SESSION", "antigravity"),
    ):
        if os.environ.get(env_name):
            return label
    return None


@dataclass(frozen=True)
class _LockHandle:
    file: object
    path: Path
    slot: int


def _try_lock_file(path: Path) -> object | None:
    """Attempt a non-blocking exclusive lock on ``path``; ``None`` if busy."""

    path.touch(exist_ok=True)
    handle = open(path, "r+b")
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
        return handle
    except Exception:
        handle.close()
        raise


def _unlock_file(handle: object) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(Exception):
            handle.close()


def _sidecar_path(lock_path: Path) -> Path:
    return lock_path.with_suffix(".json")


def _write_sidecar(lock_path: Path) -> None:
    sidecar = _sidecar_path(lock_path)
    payload = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "cwd": str(Path.cwd()),
        "argv": sys.argv,
        "started_at": time.time(),
        "harness": _detect_harness(),
    }
    tmp_path = sidecar.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp_path, sidecar)


def _read_sidecar(lock_path: Path) -> dict | None:
    sidecar = _sidecar_path(lock_path)
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _describe_holder(lock_path: Path) -> str:
    info = _read_sidecar(lock_path)
    if not info:
        return f"held by another process (lock: {lock_path.name})"
    started_at = info.get("started_at")
    since = time.strftime("%H:%M:%S", time.localtime(started_at)) if started_at else "unknown time"
    return (
        f"held by pid {info.get('pid', '?')} on {info.get('hostname', '?')} "
        f"({info.get('cwd', '?')}) since {since}"
    )


class SuiteLock:
    """Context manager acquiring one of ``DARKFAC_SUITE_SLOTS`` machine-wide slots."""

    def __init__(self, *, timeout_sec: float | None = None, slots: int | None = None) -> None:
        self._timeout_sec = _timeout_sec() if timeout_sec is None else timeout_sec
        self._slots = _slot_count() if slots is None else slots
        self._handle: _LockHandle | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        directory = state_dir()
        deadline = time.monotonic() + self._timeout_sec
        backoff = _POLL_MIN_SEC
        printed_wait = False
        while True:
            for slot in range(self._slots):
                lock_path = directory / f"suite-{slot}.lock"
                handle = _try_lock_file(lock_path)
                if handle is not None:
                    _write_sidecar(lock_path)
                    self._handle = _LockHandle(file=handle, path=lock_path, slot=slot)
                    return
            if time.monotonic() >= deadline:
                sample = directory / "suite-0.lock"
                raise SuiteLockTimeout(
                    f"[SUITE_LOCK] timed out after {self._timeout_sec:.0f}s waiting for a free "
                    f"slot ({self._describe_first_busy(directory)})"
                )
            if not printed_wait:
                print(f"[SUITE_LOCK] waiting: {self._describe_first_busy(directory)}")
                printed_wait = True
            time.sleep(backoff)
            backoff = min(backoff * 1.5, _POLL_MAX_SEC)

    def _describe_first_busy(self, directory: Path) -> str:
        for slot in range(self._slots):
            lock_path = directory / f"suite-{slot}.lock"
            if lock_path.exists():
                return _describe_holder(lock_path)
        return "held by another process"

    def release(self) -> None:
        if self._handle is None:
            return
        handle, path = self._handle.file, self._handle.path
        _unlock_file(handle)
        with contextlib.suppress(OSError):
            _sidecar_path(path).unlink()
        self._handle = None

    def __enter__(self) -> "SuiteLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@contextlib.contextmanager
def suite_lock(*, timeout_sec: float | None = None, slots: int | None = None) -> Iterator[SuiteLock | None]:
    """Acquire the machine-wide suite lock unless disabled or already held.

    Yields ``None`` when locking is disabled (``DARKFAC_SUITE_LOCK=off``) or a
    parent process already holds it (``DARKFAC_SUITE_LOCK_HELD=1``); callers
    should treat ``None`` as "proceed, nothing to release."
    """

    if is_disabled() or is_held_by_ancestor():
        yield None
        return
    lock = SuiteLock(timeout_sec=timeout_sec, slots=slots)
    lock.acquire()
    previous = os.environ.get("DARKFAC_SUITE_LOCK_HELD")
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    try:
        yield lock
    finally:
        if previous is None:
            os.environ.pop("DARKFAC_SUITE_LOCK_HELD", None)
        else:
            os.environ["DARKFAC_SUITE_LOCK_HELD"] = previous
        lock.release()


def child_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for a child process that must not re-acquire this lock."""

    env = dict(base) if base is not None else dict(os.environ)
    env["DARKFAC_SUITE_LOCK_HELD"] = "1"
    return env
