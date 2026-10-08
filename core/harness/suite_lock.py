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
``DARKFAC_SUITE_LOCK_STALL_AFTER_SEC``
    Seconds a waiter must have been waiting before it starts checking whether
    the holder is *stalled* (alive but idle), default 600 (USR-147).
``DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC``
    Length of the CPU sampling window used to call a holder stalled, default 10.
``DARKFAC_SUITE_LOCK_REAP_STALLED``
    Set to ``1`` to also terminate a holder judged stalled (default off: only
    a ``[SUITE_LOCK] holder appears stalled`` warning is printed).
``DARKFAC_SUITE_LOCK_HELD``
    Set to ``1`` by a holder for its child processes, so a nested pytest
    invocation (e.g. the harness runner shelling out to pytest) does not try
    to acquire the same machine-wide lock again and deadlock against itself.
"""

from __future__ import annotations

import contextlib
import json
import math
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
_DEFAULT_STALL_AFTER_SEC = 600.0
_DEFAULT_STALL_WINDOW_SEC = 10.0
#: CPU seconds (whole holder process tree, over the sampling window) at or
#: below which the holder counts as idle.
_STALL_CPU_EPSILON_SEC = 0.25


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


def _non_negative_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _stall_after_sec() -> float:
    return _non_negative_float_env("DARKFAC_SUITE_LOCK_STALL_AFTER_SEC", _DEFAULT_STALL_AFTER_SEC)


def _stall_window_sec() -> float:
    return _non_negative_float_env("DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC", _DEFAULT_STALL_WINDOW_SEC)


def _reap_stalled_enabled() -> bool:
    return os.environ.get("DARKFAC_SUITE_LOCK_REAP_STALLED", "").strip() == "1"


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
        ("GROK_AGENT", "grok"),
        ("GROK_SESSION_ID", "grok"),
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


def _sample_holder_tree(pid: int, started_at: float | None) -> dict[int, float] | None:
    """Accumulated CPU seconds of ``pid`` and all its descendants.

    ``None`` when the holder cannot be inspected (psutil unavailable, process
    gone, access denied) or when ``pid`` was recycled by an unrelated process
    (created after the sidecar was written, so it cannot be the lock holder).
    """

    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
        if started_at is not None and proc.create_time() > float(started_at) + 5.0:
            return None
        sample: dict[int, float] = {}
        for member in [proc, *proc.children(recursive=True)]:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                times = member.cpu_times()
                sample[member.pid] = float(times.user) + float(times.system)
        return sample if pid in sample else None
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None


def _reap_holder(pid: int) -> bool:
    """Terminate the holder and its descendants. Never touches this process or
    its ancestors. Returns ``True`` when a termination was attempted."""

    try:
        import psutil
    except ImportError:
        return False
    try:
        me = psutil.Process()
        if pid == me.pid or pid in {parent.pid for parent in me.parents()}:
            return False
        victim = psutil.Process(pid)
        victims = [*victim.children(recursive=True), victim]
        for member in victims:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                member.terminate()
        _, alive = psutil.wait_procs(victims, timeout=3.0)
        for member in alive:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                member.kill()
        psutil.wait_procs(alive, timeout=3.0)
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


class _StallWatch:
    """Detects a lock holder that is alive but idle (USR-147).

    The OS releases the lock when the holder dies, so the dangerous case is a
    live holder doing nothing for hours. A holder counts as *stalled* when,
    after the waiter has waited ``stall_after`` seconds, the accumulated CPU
    time of its whole process tree (the holder runner + pytest + xdist
    workers) advanced by no more than ``_STALL_CPU_EPSILON_SEC`` over a window
    of ``window`` seconds and no process joined or left the tree.
    """

    def __init__(self, *, stall_after: float, window: float, reap: bool) -> None:
        self._stall_after = stall_after
        self._window = window
        self._reap = reap
        self._wait_started = time.monotonic()
        self._baseline: dict[Path, tuple[int, float, dict[int, float]]] = {}
        self._warned: set[int] = set()

    def check(self, directory: Path, slots: int) -> None:
        if time.monotonic() - self._wait_started < self._stall_after:
            return
        for slot in range(slots):
            lock_path = directory / f"suite-{slot}.lock"
            if lock_path.exists():
                self._check_slot(lock_path)

    def _check_slot(self, lock_path: Path) -> None:
        info = _read_sidecar(lock_path)
        pid = info.get("pid") if info else None
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            self._baseline.pop(lock_path, None)
            return
        if str(info.get("hostname") or "").lower() != socket.gethostname().lower():
            return  # a sidecar from another host cannot be inspected
        now = time.monotonic()
        previous = self._baseline.get(lock_path)
        if previous is not None and previous[0] == pid and now - previous[1] < self._window:
            return
        sample = _sample_holder_tree(pid, info.get("started_at"))
        if sample is None:
            self._baseline.pop(lock_path, None)
            return
        self._baseline[lock_path] = (pid, now, sample)
        if previous is None or previous[0] != pid or set(previous[2]) != set(sample):
            return  # first look, new holder or processes joined/left: not idle
        cpu_delta = sum(sample[member] - previous[2][member] for member in sample)
        if cpu_delta > _STALL_CPU_EPSILON_SEC:
            return
        self._report(pid, info, cpu_delta=cpu_delta, window=now - previous[1], tree_size=len(sample))

    def _report(self, pid: int, info: dict, *, cpu_delta: float, window: float, tree_size: int) -> None:
        started_at = info.get("started_at")
        age = f"{max(time.time() - float(started_at), 0.0):.0f}s" if started_at else "unknown age"
        if pid not in self._warned:
            self._warned.add(pid)
            action = (
                "terminating it (DARKFAC_SUITE_LOCK_REAP_STALLED=1)"
                if self._reap
                else "not terminating it; set DARKFAC_SUITE_LOCK_REAP_STALLED=1 to reap stalled holders automatically"
            )
            print(
                f"[SUITE_LOCK] holder appears stalled: pid {pid} on {info.get('hostname', '?')} "
                f"held the lock for {age} and used {cpu_delta:.2f}s of CPU in {window:.0f}s "
                f"({tree_size} process(es) in its tree); {action}"
            )
        if self._reap and _reap_holder(pid):
            print(f"[SUITE_LOCK] terminated stalled holder pid {pid}; its slot is being released by the OS")
            self._baseline.clear()


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
        stall_watch = _StallWatch(
            stall_after=_stall_after_sec(),
            window=_stall_window_sec(),
            reap=_reap_stalled_enabled(),
        )
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
            try:
                stall_watch.check(directory, self._slots)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never break locking
                print(f"[SUITE_LOCK] stalled-holder check failed ({type(exc).__name__}: {exc})")
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
