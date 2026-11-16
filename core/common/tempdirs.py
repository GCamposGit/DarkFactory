"""Owned temporary sandboxes with explicit, synchronous cleanup (USR-457).

``tempfile.TemporaryDirectory`` delegates removal to ``weakref.finalize``. On
Windows, files left by a restricted probe subprocess can make that finalizer
raise ``PermissionError [WinError 5]`` during interpreter shutdown. A
``ProbeSandbox`` instead records the directory and each child process it owns,
stops those children, then attempts removal once before leaving its context.
Cleanup failures are logged and retained as :class:`SandboxCleanup` results;
there is no deferred finalizer.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger("darkfac.common.tempdirs")


class _Process(Protocol):
    """Minimal process surface needed by a sandbox owner."""

    def poll(self) -> Optional[int]: ...


ProcessStopper = Callable[[_Process], None]


def chmod_and_retry(func: Callable[[str], Any], target: str, exc: BaseException) -> None:
    """``shutil.rmtree`` hook: clear read-only bits and retry once.

    The single source of this hook; ``core.git.ticket_workspace.robust_rmtree``
    reuses it instead of keeping a second copy.
    """
    if isinstance(exc, FileNotFoundError):
        return
    try:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    func(target)


def robust_rmtree(path: Path) -> None:
    """Remove a tree after clearing read-only attributes once; propagate failures."""
    shutil.rmtree(path, onexc=chmod_and_retry)


@dataclass(frozen=True)
class SandboxCleanup:
    """Structured outcome of a sandbox's one explicit cleanup attempt."""

    path: Path
    ok: bool
    error_kind: str = ""
    winerror: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class _OwnedDirectory:
    path: Path
    owner_token: object
    device: int
    inode: int


_OWNERSHIP_LOCK = threading.RLock()
_OWNED_DIRECTORIES: dict[str, _OwnedDirectory] = {}


def _path_key(path: Path) -> str:
    """Canonical comparison key that handles relative, dot, and case aliases."""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _within_system_tempdir(path: Path) -> bool:
    """True only for a strict descendant of the current system temporary root."""
    try:
        resolved = os.path.normcase(os.path.abspath(os.fspath(path)))
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        common = os.path.commonpath((resolved, temp_root))
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved != temp_root and common == temp_root


def _cleanup_failure(path: Path, error_kind: str, detail: str, winerror: Optional[int] = None) -> SandboxCleanup:
    logger.warning(
        "Sandbox cleanup failed for %s (error_kind=%s winerror=%s): %s",
        path,
        error_kind,
        winerror,
        detail,
    )
    return SandboxCleanup(path=path, ok=False, error_kind=error_kind, winerror=winerror, detail=detail)


def remove_sandbox_dir(path: Path, *, owner: Optional["ProbeSandbox"] = None) -> SandboxCleanup:
    """Remove only the exact directory still owned by ``owner``.

    A path merely being under the system temp root is not proof of ownership.
    Shared, caller-provided, replaced, or external directories are left intact,
    including when the supplied spelling normalizes to an owned path.
    """
    candidate = Path(path)
    key = _path_key(candidate)
    with _OWNERSHIP_LOCK:
        record = _OWNED_DIRECTORIES.get(key)
    if record is None or owner is None or record.owner_token is not owner._owner_token:
        return _cleanup_failure(candidate, "unowned_directory", "directory is not registered to this sandbox")

    owned_path = record.path
    if not _within_system_tempdir(owned_path):
        _release_directory(key, record)
        return _cleanup_failure(
            owned_path,
            "outside_tempdir",
            f"refusing to remove path outside {tempfile.gettempdir()}",
        )

    try:
        current = owned_path.lstat()
    except FileNotFoundError:
        _release_directory(key, record)
        return SandboxCleanup(path=owned_path, ok=True)
    except OSError as exc:
        _release_directory(key, record)
        return _cleanup_failure(owned_path, type(exc).__name__, str(exc), getattr(exc, "winerror", None))

    identity_matches = (current.st_dev, current.st_ino) == (record.device, record.inode)
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode) or not identity_matches:
        _release_directory(key, record)
        return _cleanup_failure(owned_path, "ownership_changed", "registered directory identity changed")

    try:
        robust_rmtree(owned_path)
    except FileNotFoundError:
        result = SandboxCleanup(path=owned_path, ok=True)
    except OSError as exc:
        result = _cleanup_failure(
            owned_path,
            type(exc).__name__,
            f"could not remove sandbox {owned_path}: {exc}",
            getattr(exc, "winerror", None),
        )
    except Exception as exc:
        result = _cleanup_failure(
            owned_path,
            type(exc).__name__,
            f"could not remove sandbox {owned_path}: {exc}",
        )
    else:
        result = SandboxCleanup(path=owned_path, ok=True)
    finally:
        _release_directory(key, record)
    return result


def _release_directory(key: str, record: _OwnedDirectory) -> None:
    with _OWNERSHIP_LOCK:
        if _OWNED_DIRECTORIES.get(key) is record:
            _OWNED_DIRECTORIES.pop(key, None)


class ProbeSandbox:
    """A temporary directory and child processes owned by one probe execution."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.path: Optional[Path] = None
        self.cleanup_result: Optional[SandboxCleanup] = None
        self._owner_token = object()
        self._processes: list[tuple[_Process, ProcessStopper]] = []
        self._entered = False
        self._cleanup_requested = False

    def __enter__(self) -> Path:
        if self._entered:
            raise RuntimeError("probe sandbox cannot be entered more than once")
        directory = Path(tempfile.mkdtemp(prefix=self.prefix))
        info = directory.lstat()
        record = _OwnedDirectory(directory, self._owner_token, info.st_dev, info.st_ino)
        key = _path_key(directory)
        with _OWNERSHIP_LOCK:
            if key in _OWNED_DIRECTORIES:
                raise RuntimeError("temporary directory path is already registered as owned")
            _OWNED_DIRECTORIES[key] = record
        self.path = directory
        self._entered = True
        return directory

    def register_process(self, process: _Process, stop: ProcessStopper) -> None:
        """Register a child that must be stopped and reaped before directory cleanup."""
        if not self._entered or self._cleanup_requested:
            raise RuntimeError("process registration requires an open probe sandbox")
        if any(owned is process for owned, _ in self._processes):
            raise ValueError("process is already registered to this probe sandbox")
        self._processes.append((process, stop))

    def close(self) -> SandboxCleanup:
        """Stop registered children and request directory cleanup at most once."""
        if self._cleanup_requested:
            assert self.cleanup_result is not None
            return self.cleanup_result
        self._cleanup_requested = True
        if self.path is None:
            self.cleanup_result = SandboxCleanup(Path(), ok=True)
            return self.cleanup_result

        failures: list[str] = []
        for process, stop in reversed(self._processes):
            try:
                stop(process)
                if process.poll() is None:
                    failures.append("registered child remained alive after its stop callback")
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
        if failures:
            key = _path_key(self.path)
            with _OWNERSHIP_LOCK:
                record = _OWNED_DIRECTORIES.get(key)
            if record is not None and record.owner_token is self._owner_token:
                _release_directory(key, record)
            self.cleanup_result = _cleanup_failure(
                self.path,
                "process_cleanup_failed",
                "; ".join(failures),
            )
            return self.cleanup_result

        self.cleanup_result = remove_sandbox_dir(self.path, owner=self)
        return self.cleanup_result

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False


def probe_sandbox(prefix: str) -> ProbeSandbox:
    """Create an explicit owner for a new temporary probe directory.

    ``with probe_sandbox(...) as path`` remains compatible with existing callers;
    callers that spawn children should retain the owner and register each process
    so ``close`` stops them before attempting removal.
    """
    return ProbeSandbox(prefix)
