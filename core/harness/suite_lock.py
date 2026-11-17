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

Degraded mode (USR-463)
------------------------
A restricted identity (e.g. a sandboxed process group such as
``CodexSandboxUsers``) can read the canonical state directory but not write
to its pre-existing lock files -- their ACL grants write only to the account
that created them. The lock file is opened read-only, so this identity can
still acquire a free canonical lock without changing its metadata.
``resolve_state_dir()`` uses a writable per-identity fallback when the
canonical lock cannot be read or created. If the lock is readable but the
canonical sidecar cannot be written, ``SuiteLock.acquire()`` releases that
slot and switches to the same fallback. Degraded acquisition waits (best
effort, respecting the overall timeout) for any *live* canonical holder to
release first -- a dead or absent canonical holder never blocks it. Exclusion
between different identities' fallback directories is best effort, not
guaranteed: two different accounts each in their own degraded fallback do not
see each other's locks.
"""

from __future__ import annotations

import contextlib
import errno
import getpass
import json
import math
import os
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from core.harness.process_identity import proc_start_ticks, same_process

_DEFAULT_SLOTS = 1
_DEFAULT_TIMEOUT_SEC = 3600
_POLL_MIN_SEC = 0.5
_POLL_MAX_SEC = 5.0
_LOCK_PERMISSION_ATTEMPTS = 3
_LOCK_PERMISSION_RETRY_SEC = 0.05
_DEFAULT_STALL_AFTER_SEC = 600.0
_DEFAULT_STALL_WINDOW_SEC = 10.0
#: CPU seconds (whole holder process tree, over the sampling window) at or
#: below which the holder counts as idle.
_STALL_CPU_EPSILON_SEC = 0.25


class SuiteLockTimeout(RuntimeError):
    """Raised when no slot became free before the configured timeout.

    ``waited_sec`` (USR-178) is how long the caller waited before giving up, so
    the harness telemetry can still account for the queue time of a refused run.
    """

    waited_sec: float = 0.0


class SuiteLockUnwritable(RuntimeError):
    """Raised when lock state or its sidecar cannot be accessed, not when busy.

    This happens when the identity running the process has a restrictive ACL
    on the machine-global state directory (USR-463), e.g. a sandboxed process
    group (CodexSandboxUsers) granted only read/execute on files created by a
    different account. ``probe_free_slot`` treats it as "no slot observed
    free"; callers of ``acquire()``/``suite_lock()`` see degraded mode when a
    writable fallback exists instead of a raw ``PermissionError``.
    """

    def __init__(self, path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"[SUITE_LOCK] cannot access lock state at {path}: {reason}")


def _env_flag_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "off"


def _canonical_state_dir_path() -> Path:
    """Machine-global directory path, without creating it or checking access."""

    configured = os.environ.get("DARKFAC_HARNESS_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "DarkFac" / "harness"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "darkfac" / "harness"


def state_dir() -> Path:
    """Machine-global directory holding lock files and sidecar metadata.

    Raises ``SuiteLockUnwritable`` (not a raw ``PermissionError``) when the
    running identity cannot create the directory, e.g. a sandboxed account
    with a restrictive ACL on a directory created by a different account
    (USR-463). Callers that need to keep running under such an identity use
    ``resolve_state_dir()`` instead, which degrades gracefully rather than
    raising.
    """

    path = _canonical_state_dir_path()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise SuiteLockUnwritable(path, str(exc)) from exc
    return path


def _identity_name() -> str:
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var)
        if value:
            return value
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - identity lookup must never crash locking
        return "unknown"


def _fallback_state_dir() -> Path:
    """Per-identity writable directory used when the canonical one is not (USR-463).

    Rooted at ``DARKFAC_HARNESS_STATE_DIR`` when configured (so the fallback
    stays on the same configured volume as the canonical directory -- a fresh
    per-identity subdirectory there is typically still creatable even when
    pre-existing sibling files are not writable by this identity) or at
    ``tempfile.gettempdir()`` otherwise.
    """

    configured = os.environ.get("DARKFAC_HARNESS_STATE_DIR")
    base = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    return base / f"darkfac-harness-{_identity_name()}"


def _fallback_state_dir_candidates() -> tuple[Path, ...]:
    """Preferred per-identity fallback, followed by a writable temp location.

    A configured canonical directory may be readable but grant no permission
    to create children. In that case, putting its fallback underneath the same
    directory repeats the original ACL failure; the process temp directory is
    the independent last-resort location.
    """

    preferred = _fallback_state_dir()
    temporary = Path(tempfile.gettempdir()) / f"darkfac-harness-{_identity_name()}"
    return tuple(dict.fromkeys((preferred, temporary)))


def _resolve_fallback_state_dir(canonical: Path, reason: str) -> StateDirResolution:
    """Find a writable per-identity fallback or raise a structured error."""

    failures: list[str] = []
    candidates = _fallback_state_dir_candidates()
    for fallback in candidates:
        try:
            _probe_lock_access(fallback)
        except PermissionError as exc:
            failures.append(f"{fallback / 'suite-0.lock'}: {exc}")
            continue
        return StateDirResolution(path=fallback, degraded=True, reason=reason)

    last_fallback = candidates[-1]
    detail = "; ".join(failures)
    raise SuiteLockUnwritable(
        last_fallback / "suite-0.lock",
        f"fallbacks for {canonical / 'suite-0.lock'} are also inaccessible: {detail}",
    )


def _probe_lock_access(directory: Path) -> None:
    """Ensure the first lock can be opened without changing an existing file.

    A lock is an OS-level byte/file lock, so an existing lock file only needs
    read access. Creating a missing file is atomic; if another process wins
    that race, the exclusive create is discarded and the existing file is
    opened read-only.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "suite-0.lock"
    last_permission_error: PermissionError | None = None
    for attempt in range(_LOCK_PERMISSION_ATTEMPTS):
        try:
            handle = _open_lock_file(lock_path)
        except PermissionError as exc:
            last_permission_error = exc
            if attempt + 1 < _LOCK_PERMISSION_ATTEMPTS:
                _sleep(_LOCK_PERMISSION_RETRY_SEC)
            continue
        handle.close()
        return

    if last_permission_error is not None:
        raise last_permission_error
    raise FileNotFoundError(f"lock file kept disappearing while probing: {lock_path}")


def _open_lock_file(path: Path) -> object:
    """Open a lock file read-only, creating it exclusively only if absent.

    Existing files are never opened for writing or touched, so acquiring one
    does not change its metadata. Permission retries belong to the caller so
    acquisition can apply its bounded retry across both opening and OS locking.
    """

    for attempt in range(_LOCK_PERMISSION_ATTEMPTS):
        try:
            return path.open("rb")
        except FileNotFoundError:
            try:
                created = path.open("xb")
            except FileExistsError:
                # Another process created it after our read-only open failed.
                continue
            else:
                created.close()
                # Reopen read-only for the actual OS lock.
                continue
    raise FileNotFoundError(f"lock file kept disappearing while opening: {path}")


def _lock_is_contended(error: OSError) -> bool:
    """Recognize platform lock conflicts without hiding unrelated ACL errors."""

    if os.name == "nt":
        winerror = getattr(error, "winerror", None)
        if winerror in (32, 33):  # sharing violation / lock violation
            return True
        if winerror == 5:  # access denied, not a lock conflict
            return False
        return error.errno == errno.EACCES  # MS CRT uses EACCES for a busy range
    return error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)


@dataclass(frozen=True)
class StateDirResolution:
    """Result of :func:`resolve_state_dir` (USR-463).

    ``degraded`` is ``False`` and ``reason`` is ``None`` when the canonical
    machine-global lock is accessible to this identity -- the common case.
    When the canonical lock is not accessible, ``path`` is a per-identity
    fallback, ``degraded`` is ``True``, and ``reason`` explains why.
    """

    path: Path
    degraded: bool
    reason: str | None


def resolve_state_dir() -> StateDirResolution:
    """Canonical state directory, or a per-identity fallback (USR-463).

    Creates a missing directory or lock file. Existing lock files are only
    opened for reading. A permission problem in the canonical path switches to
    a per-identity fallback. If that fallback is also inaccessible, raise
    ``SuiteLockUnwritable`` rather than leaking a raw ``PermissionError`` from
    the later lock-file open.
    """

    canonical = _canonical_state_dir_path()
    try:
        _probe_lock_access(canonical)
    except PermissionError as exc:
        reason = f"sem acesso a {canonical / 'suite-0.lock'}: {exc}"
        return _resolve_fallback_state_dir(canonical, reason)
    return StateDirResolution(path=canonical, degraded=False, reason=None)


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
        ("CLAUDECODE", "claude-code"),  # census: wire-id (harness label, not a model id)
        ("CLAUDE_CODE_ENTRYPOINT", "claude-code"),  # census: wire-id (harness label, not a model id)
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
    """Attempt a non-blocking exclusive lock on ``path``; ``None`` if busy.

    Raises ``SuiteLockUnwritable`` when the file cannot be created or opened
    for read access because the running identity lacks permission
    (USR-463) -- a distinct condition from the lock being legitimately held
    by another process, which is reported by returning ``None``.
    """

    for attempt in range(_LOCK_PERMISSION_ATTEMPTS):
        try:
            handle = _open_lock_file(path)
        except PermissionError as exc:
            if attempt + 1 < _LOCK_PERMISSION_ATTEMPTS:
                _sleep(_LOCK_PERMISSION_RETRY_SEC)
                continue
            raise SuiteLockUnwritable(path, str(exc)) from exc
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            handle.close()
            if _lock_is_contended(exc):
                return None
            if isinstance(exc, PermissionError):
                if attempt + 1 < _LOCK_PERMISSION_ATTEMPTS:
                    _sleep(_LOCK_PERMISSION_RETRY_SEC)
                    continue
                raise SuiteLockUnwritable(path, str(exc)) from exc
            return None
        except Exception:
            handle.close()
            raise
    raise SuiteLockUnwritable(path, "permission denied while acquiring the OS lock")


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
        "start_ticks": proc_start_ticks(),
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


def _sidecar_owner_pid_if_live(lock_path: Path) -> int | None:
    """``pid`` of ``lock_path``'s sidecar owner when it is confirmably alive on
    this host (USR-463); ``None`` for an absent, unreadable, dead, or
    unverifiable (e.g. recorded on a different host) owner.

    Used to decide whether a degraded acquisition must wait for the canonical
    holder: only a *confirmed-live* holder blocks it, matching the rule that a
    dead or absent canonical lock never blocks the fallback.

    Without ``start_ticks`` (Windows/macOS), liveness falls back to comparing
    the live process' ``create_time()`` against the sidecar's ``started_at``
    with ``only_younger=True`` -- the same rule ``_sample_holder_tree`` uses --
    rather than the symmetric default tolerance: ``started_at`` is the moment
    the holder *acquired the lock*, which can be arbitrarily later than its own
    process start (e.g. a slow startup before reaching the lock), so only a pid
    created AFTER that moment indicates a reused pid.
    """

    info = _read_sidecar(lock_path)
    if not info:
        return None
    pid = info.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if str(info.get("hostname") or "").lower() != socket.gethostname().lower():
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None
    if same_process(proc, info.get("started_at"), info.get("start_ticks"), only_younger=True):
        return pid
    return None


def _canonical_owner_is_live(canonical_dir: Path, slots: int) -> bool:
    """True when any of the canonical directory's ``suite-N.lock`` sidecars
    names a process confirmed alive on this host (USR-463)."""

    for slot in range(slots):
        if _sidecar_owner_pid_if_live(canonical_dir / f"suite-{slot}.lock") is not None:
            return True
    return False


def _sample_holder_tree(
    pid: int, started_at: float | None, start_ticks: int | None = None
) -> dict[int, float] | None:
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
        if not same_process(proc, started_at, start_ticks, tolerance_sec=5.0, only_younger=True):
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


def _sleep(seconds: float) -> None:
    """Injectable sleeper (late-bound so patching ``time.sleep`` or ``_sleep`` both work)."""
    time.sleep(seconds)


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
        holder_ticks = info.get("start_ticks")
        if holder_ticks is None:
            sample = _sample_holder_tree(pid, info.get("started_at"))
        else:
            sample = _sample_holder_tree(pid, info.get("started_at"), holder_ticks)
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
        #: USR-178: seconds spent in ``acquire()`` waiting for a free slot
        #: (``None`` until the lock is acquired). The runner reports it in the
        #: per-run timing record, separate from the suite execution time.
        self.waited_sec: float | None = None
        #: USR-463: ``True`` once ``acquire()`` had to fall back to a
        #: per-identity directory because the canonical lock was inaccessible.
        self.degraded: bool = False
        #: USR-463: the directory the held lock actually lives in -- the
        #: canonical one, or the degraded fallback.
        self.state_dir_used: Path | None = None
        #: USR-463: why ``degraded`` is ``True``; ``None`` otherwise.
        self.degradation_reason: str | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        resolution = resolve_state_dir()
        self.degraded = resolution.degraded
        self.state_dir_used = resolution.path
        self.degradation_reason = resolution.reason
        directory = resolution.path
        wait_started = time.monotonic()
        deadline = wait_started + self._timeout_sec
        backoff = _POLL_MIN_SEC
        printed_wait = False
        stall_watch = _StallWatch(
            stall_after=_stall_after_sec(),
            window=_stall_window_sec(),
            reap=_reap_stalled_enabled(),
        )
        if resolution.degraded:
            canonical = _canonical_state_dir_path()
            print(
                f"[SUITE_LOCK] {resolution.reason}; usando estado degradado em {directory} "
                "(exclusao entre identidades e best-effort, sem garantia de exclusividade "
                "entre contas distintas)"
            )
            while _canonical_owner_is_live(canonical, self._slots):
                if time.monotonic() >= deadline:
                    timeout_error = SuiteLockTimeout(
                        f"[SUITE_LOCK] timed out after {self._timeout_sec:.0f}s waiting for the "
                        f"canonical lock owner at {canonical} to release before using the "
                        f"degraded state dir {directory}"
                    )
                    timeout_error.waited_sec = time.monotonic() - wait_started
                    raise timeout_error
                _sleep(backoff)
                backoff = min(backoff * 1.5, _POLL_MAX_SEC)
            backoff = _POLL_MIN_SEC
        while True:
            switched_to_fallback = False
            for slot in range(self._slots):
                lock_path = directory / f"suite-{slot}.lock"
                handle = None
                try:
                    handle = _try_lock_file(lock_path)
                    if handle is not None:
                        try:
                            _write_sidecar(lock_path)
                        except OSError as exc:
                            # The lock itself may be readable and lockable
                            # while the canonical state directory still denies
                            # metadata writes to this identity. Release the
                            # canonical slot before switching to the same
                            # per-identity fallback used for an unreadable lock.
                            _unlock_file(handle)
                            handle = None
                            sidecar_tmp = _sidecar_path(lock_path).with_suffix(".tmp")
                            raise SuiteLockUnwritable(sidecar_tmp, str(exc)) from exc
                except SuiteLockUnwritable as exc:
                    # The canonical ACL can change between the initial probe
                    # and acquisition, or can deny sidecar writes even though
                    # the lock is readable. Degrade here rather than leaking a
                    # structured error when a fallback remains usable.
                    if resolution.degraded:
                        raise
                    canonical = _canonical_state_dir_path()
                    reason = f"sem acesso a {exc.path}: {exc.reason}"
                    resolution = _resolve_fallback_state_dir(canonical, reason)
                    fallback = resolution.path
                    self.degraded = True
                    self.state_dir_used = fallback
                    self.degradation_reason = reason
                    directory = fallback
                    print(
                        f"[SUITE_LOCK] {reason}; usando estado degradado em {directory} "
                        "(exclusao entre identidades e best-effort, sem garantia de exclusividade "
                        "entre contas distintas)"
                    )
                    while _canonical_owner_is_live(canonical, self._slots):
                        if time.monotonic() >= deadline:
                            timeout_error = SuiteLockTimeout(
                                f"[SUITE_LOCK] timed out after {self._timeout_sec:.0f}s waiting for the "
                                f"canonical lock owner at {canonical} to release before using the "
                                f"degraded state dir {directory}"
                            )
                            timeout_error.waited_sec = time.monotonic() - wait_started
                            raise timeout_error
                        _sleep(backoff)
                        backoff = min(backoff * 1.5, _POLL_MAX_SEC)
                    backoff = _POLL_MIN_SEC
                    switched_to_fallback = True
                    break
                if handle is not None:
                    self._handle = _LockHandle(file=handle, path=lock_path, slot=slot)
                    self.waited_sec = time.monotonic() - wait_started
                    if printed_wait:
                        print(
                            f"[SUITE_LOCK] acquired slot {slot} after waiting {self.waited_sec:.1f}s "
                            "(queue time is not counted against any step timeout)"
                        )
                    return
            if switched_to_fallback:
                continue
            if time.monotonic() >= deadline:
                timeout_error = SuiteLockTimeout(
                    f"[SUITE_LOCK] timed out after {self._timeout_sec:.0f}s waiting for a free "
                    f"slot ({self._describe_first_busy(directory)})"
                )
                timeout_error.waited_sec = time.monotonic() - wait_started
                raise timeout_error
            if not printed_wait:
                print(f"[SUITE_LOCK] waiting: {self._describe_first_busy(directory)}")
                printed_wait = True
            try:
                stall_watch.check(directory, self._slots)
            except Exception as exc:  # noqa: BLE001 - diagnostics must never break locking
                print(f"[SUITE_LOCK] stalled-holder check failed ({type(exc).__name__}: {exc})")
            _sleep(backoff)
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


def probe_free_slot(*, slots: int | None = None) -> bool:
    """Try each configured suite slot briefly; return false when all are held.

    This is a capacity observation only. Callers still need to acquire a lock
    for their actual work, so the result can become stale immediately.

    Never raises for a permission problem (USR-463): a state directory or
    lock file that this identity cannot access is treated the same as "no slot
    observed free" rather than crashing the caller.
    """
    if is_disabled():
        return True
    if is_held_by_ancestor():
        return False
    count = _slot_count() if slots is None else max(1, slots)
    try:
        directory = state_dir()
    except SuiteLockUnwritable:
        return False
    handles: list[object] = []
    try:
        for slot in range(count):
            try:
                handle = _try_lock_file(directory / f"suite-{slot}.lock")
            except SuiteLockUnwritable:
                continue
            if handle is not None:
                handles.append(handle)
        return bool(handles)
    finally:
        for handle in handles:
            _unlock_file(handle)


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
