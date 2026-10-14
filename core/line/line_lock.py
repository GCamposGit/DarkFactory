"""Exclusive line lock for ``run_ticket.py`` (USR-250).

The 'one session operates the line at a time' rule (V0 freeze) was only a convention: nothing
stopped a second ``run_ticket`` launch. On 2026-10-12 the SAME ticket (USR-239) was launched
twice seven minutes apart (09:08 and 09:17), each from its own worktree, and both ended up
contending for pytest, the Desktop test worker and RAM; the focal-test pre-check sat silent for
600 s in both runs and the first launch's official gate timed out at 3000 s.

This module makes that collision impossible: ``acquire()`` takes an exclusive, cross-process lock
file holding PID, ticket, host, start time and a heartbeat. It lives under ``core.paths.state_root()``
(the SHARED checkout's state directory -- the same root ``core.line.local_cancel`` already uses for
per-ticket control files), not inside any one ticket's worktree, so every invocation of the canonical
launcher (``python C:\\dev\\DarkFac\\run_ticket.py ...``), regardless of which worktree it operates on,
contends for the same file.

A second launch never waits: ``acquire()`` either succeeds immediately or raises ``LineLockHeld`` with
who holds it, since when, and how to cancel -- no worktree is created and no quota is spent finding
that out, because the lock is acquired before either of those happens.

A holder is recovered automatically (with a warning, never silently) when it is dead: either its PID no
longer exists (immediately), or its PID is alive but its heartbeat (refreshed every
``HEARTBEAT_INTERVAL_SEC`` while held) is older than ``HEARTBEAT_STALE_PID_ALIVE_SEC`` (PID reused). A
live PID with a heartbeat later than ``HEARTBEAT_STALE_SEC`` keeps the lock (a suspended notebook resumes
with a live run) and only logs a warning.
Recovery renames the stale file aside before deleting it, so two racing launches never both own the lock. The delivery subprocess ``run_ticket.py`` spawns for its own ticket
(``--resume-delivery --worktree ...``) is part of the SAME run, not a second launch: the parent marks its
environment with ``ENV_INHERITED`` (see ``mark_inherited``) so that child's own ``acquire()`` is a no-op
instead of a refusal, and its ``release()`` leaves the parent's lock file untouched.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from core.paths import state_root

logger = logging.getLogger(__name__)

LOCK_DIRNAME = "line_lock"
LOCK_FILENAME = "line.lock"

#: Set by a run on its delivery subprocess's environment: that child is part of THIS run, not a
#: second launch, so it must never try to acquire (or refuse itself) the lock its parent holds.
ENV_INHERITED = "DARKFAC_LINE_LOCK_HELD"

#: How often a held lock's heartbeat is refreshed on disk.
HEARTBEAT_INTERVAL_SEC = 15.0
#: A live holder whose heartbeat is older than this only triggers a "heartbeat atrasado" warning.
HEARTBEAT_STALE_SEC = 90.0
#: A holder whose PID still exists is only recovered after a MUCH longer silence: a suspended or
#: hibernated notebook resumes with an old heartbeat but a perfectly live run, and must not lose its
#: lock to a new launch. Past this window a live PID is assumed reused by the OS (Windows recycles PIDs).
HEARTBEAT_STALE_PID_ALIVE_SEC = 1800.0

RESUME_COMMAND_TEMPLATE = "python C:\\dev\\DarkFac\\run_ticket.py --cancel {ticket_id}"


@dataclass(frozen=True)
class LineLockInfo:
    """Snapshot of a held (or just-recovered) lock's sidecar content."""

    pid: int
    ticket_id: str
    hostname: str
    started_at: float
    heartbeat_at: float

    def started_at_display(self) -> str:
        try:
            return datetime.fromtimestamp(self.started_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (OverflowError, OSError, ValueError):
            return "horário desconhecido"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "ticket_id": self.ticket_id,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Optional["LineLockInfo"]:
        try:
            return cls(
                pid=int(data["pid"]),
                ticket_id=str(data.get("ticket_id") or "desconhecido"),
                hostname=str(data.get("hostname") or "desconhecido"),
                started_at=float(data["started_at"]),
                heartbeat_at=float(data.get("heartbeat_at", data["started_at"])),
            )
        except (KeyError, TypeError, ValueError):
            return None


class LineLockHeld(RuntimeError):
    """Another live ``run_ticket.py`` run already holds the line lock.

    The message is the full refusal text (who holds it, since when, how to cancel): callers print
    ``str(exc)`` directly rather than reformatting it.
    """

    def __init__(self, info: LineLockInfo) -> None:
        self.info = info
        since = info.started_at_display()
        cancel_cmd = RESUME_COMMAND_TEMPLATE.format(ticket_id=info.ticket_id)
        super().__init__(
            "[TRAVA DE LINHA] Já existe um run_ticket em andamento nesta máquina "
            f"(ticket {info.ticket_id}, pid {info.pid} em {info.hostname}, desde {since}). "
            "Esta tentativa foi recusada antes de criar worktree ou consumir cota.\n"
            f"Para cancelar esse lançamento: {cancel_cmd}\n"
            "Se o processo acima já estiver morto, a trava será recuperada automaticamente "
            "(com aviso) na próxima tentativa."
        )


def lock_dir(root: Optional[Path] = None) -> Path:
    """Directory holding the line lock file, inside the shared checkout's state root."""
    return (root if root is not None else state_root()) / LOCK_DIRNAME


def lock_path(root: Optional[Path] = None) -> Path:
    return lock_dir(root) / LOCK_FILENAME


def is_inherited() -> bool:
    """True when an ancestor process of THIS run already holds the line lock."""
    return os.environ.get(ENV_INHERITED, "").strip() == "1"


def mark_inherited(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Environment for a child process that is part of this same run (e.g. the delivery subprocess)."""
    result = dict(env) if env is not None else dict(os.environ)
    result[ENV_INHERITED] = "1"
    return result


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check; a PID this process cannot inspect is assumed alive (fail safe)."""
    if pid <= 0:
        return False
    try:
        import psutil

        try:
            return psutil.pid_exists(pid)
        except Exception:  # noqa: BLE001 - an inspection error must never be read as "dead"
            return True
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(path: Path) -> Optional[LineLockInfo]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return LineLockInfo.from_dict(data) if isinstance(data, dict) else None


def _is_stale(info: LineLockInfo, *, now: float) -> bool:
    """True when the holder is dead and its lock may be recovered.

    Stale when the PID no longer exists (immediately, regardless of heartbeat), OR when the PID is
    alive but the heartbeat is older than ``HEARTBEAT_STALE_PID_ALIVE_SEC`` (PID reused by the OS).
    A live PID with a merely late heartbeat (suspended notebook) is NOT stale;
    ``HEARTBEAT_STALE_SEC`` only triggers a "heartbeat atrasado" warning in that case.
    """
    if not _pid_alive(info.pid):
        return True
    return (now - info.heartbeat_at) > HEARTBEAT_STALE_PID_ALIVE_SEC


def _same_owner(a: Optional[LineLockInfo], b: Optional[LineLockInfo]) -> bool:
    if a is None or b is None:
        return a is b
    return a.pid == b.pid and a.started_at == b.started_at


def _recover_stale(path: Path, observed: Optional[LineLockInfo]) -> Optional[LineLockInfo]:
    """Remove the stale lock ``observed`` without ever deleting a lock someone else just created.

    A plain ``unlink`` races: two processes that both judged the same lock stale could both unlink,
    the second deleting the fresh lock the first had already created. Instead the file is atomically
    renamed to a unique name, re-read, and only deleted if it is still the SAME stale owner. If it is
    a different (live) owner, it is put back (when ``line.lock`` is free) and returned as the holder.

    Returns ``None`` when the stale lock is gone (caller may retry creating), else the live holder.
    """
    aside = path.with_name(f"{path.name}.stale-{os.getpid()}-{time.time_ns()}")
    try:
        os.replace(path, aside)
    except FileNotFoundError:
        return None  # someone else already cleared it
    except OSError:
        return _read_lock(path) or observed
    renamed = _read_lock(aside)
    if _same_owner(renamed, observed):
        with contextlib.suppress(OSError):
            aside.unlink()
        return None
    # We moved a lock that is not the one we judged stale: a new owner created it in between.
    if not path.exists():
        with contextlib.suppress(OSError):
            os.replace(aside, path)
    else:
        with contextlib.suppress(OSError):
            aside.unlink()
    return renamed or _read_lock(path) or observed


def _write_lock(path: Path, info: LineLockInfo) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(info.to_dict()), encoding="utf-8")
    os.replace(tmp, path)


class LineLock:
    """Context manager acquiring the exclusive, cross-process line lock (USR-250)."""

    def __init__(self, ticket_id: str, *, root: Optional[Path] = None) -> None:
        self._ticket_id = ticket_id
        self._path = lock_path(root)
        self._acquired = False
        self._inherited = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_heartbeat = threading.Event()

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def inherited(self) -> bool:
        return self._inherited

    def acquire(self) -> None:
        """Take the lock, recover a dead holder's, or raise ``LineLockHeld`` immediately.

        Never waits or retries against a live holder: a second launch must be refused on the spot,
        not queued, so it never pays for a worktree or an agent call it was always going to lose.
        """
        if self._acquired or self._inherited:
            return
        if is_inherited():
            self._inherited = True
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        info = LineLockInfo(
            pid=os.getpid(), ticket_id=self._ticket_id, hostname=socket.gethostname(),
            started_at=now, heartbeat_at=now,
        )
        # Two attempts: the first may find a stale lock and clear it; the second must then succeed.
        # A second collision (another process recreated it between our unlink and retry) is treated
        # as a live holder rather than retried forever -- fail closed, same as any other contention.
        for _ in range(2):
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                existing = _read_lock(self._path)
                if existing is not None and not _is_stale(existing, now=time.time()):
                    late = time.time() - existing.heartbeat_at
                    if late > HEARTBEAT_STALE_SEC:
                        logger.warning(
                            "[LINE_LOCK] heartbeat atrasado (%.0f s) do dono vivo (pid %s, ticket %s); "
                            "trava mantida", late, existing.pid, existing.ticket_id,
                        )
                    raise LineLockHeld(existing)
                logger.warning(
                    "[LINE_LOCK] recuperando trava órfã (pid %s, ticket %s, host %s): dono morto ou "
                    "heartbeat vencido",
                    existing.pid if existing else "?",
                    existing.ticket_id if existing else "?",
                    existing.hostname if existing else "?",
                )
                holder = _recover_stale(self._path, existing)
                if holder is not None:
                    raise LineLockHeld(holder)
                continue
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(info.to_dict()))
                self._acquired = True
                self._start_heartbeat(info)
                return

        existing = _read_lock(self._path)
        raise LineLockHeld(
            existing
            if existing is not None
            else LineLockInfo(pid=-1, ticket_id="desconhecido", hostname="desconhecido", started_at=now, heartbeat_at=now)
        )

    def _start_heartbeat(self, info: LineLockInfo) -> None:
        def beat() -> None:
            while not self._stop_heartbeat.wait(HEARTBEAT_INTERVAL_SEC):
                with contextlib.suppress(OSError):
                    _write_lock(
                        self._path,
                        LineLockInfo(
                            pid=info.pid, ticket_id=info.ticket_id, hostname=info.hostname,
                            started_at=info.started_at, heartbeat_at=time.time(),
                        ),
                    )

        self._stop_heartbeat.clear()
        thread = threading.Thread(target=beat, name="line-lock-heartbeat", daemon=True)
        thread.start()
        self._heartbeat_thread = thread

    def release(self) -> None:
        """Release on normal exit, failure or cancellation; a no-op for an inherited lock."""
        if self._inherited:
            self._inherited = False
            return
        if not self._acquired:
            return
        self._stop_heartbeat.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None
        with contextlib.suppress(FileNotFoundError, OSError):
            self._path.unlink()
        self._acquired = False

    def __enter__(self) -> "LineLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@contextlib.contextmanager
def guard(ticket_id: str, *, root: Optional[Path] = None) -> Iterator[LineLock]:
    """Acquire the line lock for ``ticket_id`` and release it on any exit, including an exception."""
    lock = LineLock(ticket_id, root=root)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


__all__ = [
    "ENV_INHERITED",
    "HEARTBEAT_INTERVAL_SEC",
    "HEARTBEAT_STALE_PID_ALIVE_SEC",
    "HEARTBEAT_STALE_SEC",
    "LineLock",
    "LineLockHeld",
    "LineLockInfo",
    "guard",
    "is_inherited",
    "lock_dir",
    "lock_path",
    "mark_inherited",
]
