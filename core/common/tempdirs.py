"""Sandbox temp directories whose cleanup never raises from a finalizer (USR-457).

``tempfile.TemporaryDirectory`` removes its directory from a ``weakref.finalize``
callback. On Windows, a probe subprocess run under a restricted sandbox identity can
leave files whose ACL the current process cannot ``chmod``; when that failure surfaces
from the finalizer at interpreter shutdown it prints an unhandled "Exception ignored
in" trace instead of a normal, catchable error -- the WinError 5 seen cleaning up
``darkfac-quota-probe`` directories.

:func:`probe_sandbox` avoids the finalizer entirely: it creates the directory with
``tempfile.mkdtemp`` and removes it synchronously at the end of the ``with`` block, in
a ``finally`` so a raising body still gets cleanup. A removal failure is logged and
returned as a structured :class:`SandboxCleanup` rather than propagated.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("darkfac.common.tempdirs")


def chmod_and_retry(func: Callable[[str], Any], target: str, exc: BaseException) -> None:
    """``shutil.rmtree`` ``onexc`` hook: clear the read-only bit (git objects, Windows) and retry once.

    The single source of this hook; ``core.git.ticket_workspace.robust_rmtree`` reuses it
    instead of keeping its own copy.
    """
    if isinstance(exc, FileNotFoundError):
        return
    try:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    func(target)


def robust_rmtree(path: Path) -> None:
    """``shutil.rmtree`` that clears read-only attributes once; raises the last error on failure."""
    shutil.rmtree(path, onexc=chmod_and_retry)


@dataclass(frozen=True)
class SandboxCleanup:
    """Outcome of removing one sandbox directory created by :func:`probe_sandbox`."""

    path: Path
    ok: bool
    error_kind: str = ""
    winerror: Optional[int] = None
    detail: str = ""


def _within_system_tempdir(path: Path) -> bool:
    """True when ``path.resolve()`` is a strict descendant of the system tempdir."""
    try:
        resolved = path.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return temp_root in resolved.parents


def remove_sandbox_dir(path: Path) -> SandboxCleanup:
    """Remove ``path``; never raises.

    Refuses -- and reports, without touching the filesystem -- any ``path`` whose
    ``resolve()`` does not land inside ``tempfile.gettempdir()``: a sandbox helper must
    never be able to delete an external directory because of a bad prefix or caller
    bug. A removal failure (e.g. ``PermissionError``/``WinError 5`` from a
    restricted-ACL sandbox subprocess) is logged with the path and error code and
    reported via the returned result instead of propagating.
    """
    if not _within_system_tempdir(path):
        detail = f"refusing to remove {path}: outside {tempfile.gettempdir()}"
        logger.warning(detail)
        return SandboxCleanup(path=path, ok=False, error_kind="outside_tempdir", detail=detail)
    try:
        robust_rmtree(path)
    except FileNotFoundError:
        return SandboxCleanup(path=path, ok=True)
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        detail = f"could not remove sandbox {path}: {exc}"
        logger.warning("Sandbox cleanup failed for %s (winerror=%s): %s", path, winerror, exc)
        return SandboxCleanup(path=path, ok=False, error_kind=type(exc).__name__, winerror=winerror, detail=detail)
    return SandboxCleanup(path=path, ok=True)


@contextmanager
def probe_sandbox(prefix: str) -> Iterator[Path]:
    """A ``tempfile.mkdtemp`` directory removed synchronously when the block exits.

    Unlike ``tempfile.TemporaryDirectory`` there is no ``weakref.finalize`` callback:
    removal happens here, even when the body raises, and a failure is handled by
    :func:`remove_sandbox_dir` (logged, never raised) instead of surfacing later as an
    "Exception ignored in" trace at interpreter shutdown.
    """
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield directory
    finally:
        remove_sandbox_dir(directory)
