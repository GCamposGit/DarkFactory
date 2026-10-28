"""Per-ticket Git worktrees and reliable cleanup (USR-69).

Root cause of the USR-60/USR-64 incident: ``run_ticket`` ran the agent in the
SHARED checkout and ``commit_ticket`` did ``git add -A`` there, so two
simultaneous sessions swept each other's files into one commit. On Windows the
cleanup half was just as fragile: ``git worktree remove`` fails on locked files
and leaves the directory behind (about 14 dead directories with thousands of
files each were found under ``.worktrees/``).

This module gives every ticket its own worktree and makes its removal reliable:

* :func:`create_workspace` -- fetch ``origin/<base>`` and add a dedicated
  worktree ``.worktrees/<ticket-id>-<timestamp>`` on branch ``ticket/<id>``.
  The name is unique per call and an existing directory is never reused.
* :func:`cleanup` -- ``git worktree remove`` with retry/backoff, then a robust
  ``rmtree`` (read-only files are chmod'ed), then, when the directory still
  cannot be deleted, a *rename* into the recoverable trash
  ``.factory/backups/trash/<name>-<timestamp>``; ``git worktree prune`` last.
* :func:`purge_trash` -- the next sweep deletes trash entries older than
  :data:`TRASH_RETENTION_DAYS` (7); until then a mistaken removal is recoverable.
* :func:`find_orphan_dirs` -- directories under ``.worktrees`` / ``.claude/worktrees``
  with no ``.git`` entry that ``git worktree list`` does not know. They are only
  ever MOVED to the trash (:func:`trash_directory`), never deleted directly.

Stdlib-only (it must not import ``core.git.autonomy``, which imports it).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from core.paths import state_root
from core.common.time import utc_now

logger = logging.getLogger("darkfac.git.ticket_workspace")

WORKTREES_DIRNAME = ".worktrees"
# Roots swept for dead directories, relative to the main checkout.
WORKTREE_ROOTS: tuple[Path, ...] = (Path(WORKTREES_DIRNAME), Path(".claude") / "worktrees")
TRASH_RELATIVE = Path(".factory") / "backups" / "trash"
TRASH_RETENTION_DAYS = 7
# Machine-state directories that live next to worktrees and must never be
# treated as dead worktrees (e.g. a DARKFAC_HARNESS_STATE_DIR pointed there).
RESERVED_DIR_NAMES = frozenset({"harness_state"})
# A directory touched more recently than this is never classified as an orphan:
# it may be a worktree being created right now.
ORPHAN_MIN_AGE_S = 3600.0
# `.worktrees/` is already in the repository .gitignore; temporary or foreign
# clones may not have it, so the local exclude file is kept in sync too.
LOCAL_EXCLUDE_ENTRIES: tuple[str, ...] = ("/.worktrees/", "/.factory/backups/")

DEFAULT_CLEANUP_RETRIES = 4
DEFAULT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP_PATTERN = re.compile(r"-(\d{8}T\d{6}Z)(?:-\d+)?$")
_GIT_TIMEOUT_S = 120.0

GitRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
SleepFn = Callable[[float], None]
NowFn = Callable[[], datetime]
RmtreeFn = Callable[[Path], None]


class WorkspaceError(RuntimeError):
    """A ticket worktree could not be created."""


@dataclass(frozen=True)
class TicketWorkspace:
    """A worktree dedicated to one ticket."""

    ticket_id: str
    path: Path
    branch: str
    main_root: Path
    base_ref: str
    base_sha: str


@dataclass
class CleanupResult:
    """Outcome of :func:`cleanup`; ``removed`` is True when the path is gone from the worktree tree."""

    path: Path
    removed: bool
    method: str  # absent | preserved | git | rmtree | trash | failed
    attempts: int = 0
    trash_path: Optional[Path] = None
    detail: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class TrashReport:
    """Outcome of :func:`purge_trash`."""

    purged: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# Git plumbing
# ----------------------------------------------------------------------


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def _git(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    """Run one git command as an argument list; never raises."""
    cmd = ["git", *args]
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
            check=False,
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"git timed out: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"OS error invoking git: {exc}")


def _norm(path: Path | str) -> str:
    """Comparable form of a path (resolved, case-folded on Windows)."""
    try:
        resolved = os.path.realpath(str(path))
    except OSError:
        resolved = str(path)
    return os.path.normcase(resolved)


def parse_worktree_list(porcelain: str) -> list[dict[str, str]]:
    """Parse ``git worktree list --porcelain``; the first entry is the main checkout."""
    entries: list[dict[str, str]] = []
    for block in porcelain.replace("\r\n", "\n").split("\n\n"):
        entry: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            entry[key] = value
        if entry.get("worktree"):
            entries.append(entry)
    return entries


def list_worktrees(cwd: Path, run_git: Optional[GitRunner] = None) -> list[dict[str, str]]:
    runner = run_git or _git
    res = runner(["worktree", "list", "--porcelain"], cwd)
    if res.returncode != 0:
        return []
    return parse_worktree_list(res.stdout)


def main_checkout_root(cwd: Path, run_git: Optional[GitRunner] = None) -> Path:
    """Root of the primary checkout of the repository that contains ``cwd``."""
    entries = list_worktrees(cwd, run_git)
    if not entries:
        raise WorkspaceError(f"{cwd} is not inside a git repository")
    return Path(entries[0]["worktree"])


def is_secondary_worktree(cwd: Path, run_git: Optional[GitRunner] = None) -> bool:
    """True when ``cwd`` lies inside a linked (non-primary) worktree."""
    runner = run_git or _git
    top = runner(["rev-parse", "--show-toplevel"], cwd)
    if top.returncode != 0 or not top.stdout.strip():
        return False
    return _norm(top.stdout.strip()) != _norm(main_checkout_root(cwd, run_git))


def ensure_local_excludes(main_root: Path, run_git: Optional[GitRunner] = None) -> None:
    """Append the worktree/trash directories to ``<common-git-dir>/info/exclude`` if missing.

    Local to the machine (never tracked): keeps ``.worktrees/`` out of
    ``git status`` / ``git add`` even in a clone whose ``.gitignore`` lacks it.
    """
    runner = run_git or _git
    res = runner(["rev-parse", "--path-format=absolute", "--git-common-dir"], main_root)
    if res.returncode != 0 or not res.stdout.strip():
        return
    exclude = Path(res.stdout.strip()) / "info" / "exclude"
    try:
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        present = {line.strip() for line in current.splitlines()}
        missing = [entry for entry in LOCAL_EXCLUDE_ENTRIES if entry not in present]
        if not missing:
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not current or current.endswith("\n") else "\n"
        exclude.write_text(current + prefix + "\n".join(missing) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not update %s: %s", exclude, exc)


def shared_checkout_dirty_paths(
    cwd: Path, ignore_prefixes: Sequence[str] = (), run_git: Optional[GitRunner] = None
) -> list[str]:
    """Uncommitted paths when ``cwd`` is the SHARED (primary) checkout, else ``[]``.

    ``ignore_prefixes`` are repository-relative POSIX prefixes of volatile
    runtime state that must not count as another ticket's work.
    """
    runner = run_git or _git
    if is_secondary_worktree(cwd, run_git):
        return []
    res = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd)
    if res.returncode != 0:
        return []
    paths: list[str] = []
    tokens = res.stdout.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2] != " ":
            continue
        status, path = token[:2], token[3:]
        if "R" in status or "C" in status:
            index += 1  # the rename source is the next NUL-separated token
        if any(path.startswith(prefix) for prefix in ignore_prefixes):
            continue
        paths.append(path)
    return paths


# ----------------------------------------------------------------------
# Creation
# ----------------------------------------------------------------------


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", text.strip().lower()).strip("-.")
    return cleaned or "ticket"


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(_STAMP_FORMAT)


def _ref_exists(ref: str, cwd: Path, run_git: GitRunner) -> bool:
    return run_git(["rev-parse", "--verify", "--quiet", ref], cwd).returncode == 0


def create_workspace(
    ticket_id: str,
    *,
    cwd: Path,
    base: str = "main",
    remote: str = "origin",
    fetch: bool = True,
    unique_branch: bool = False,
    now_fn: Optional[NowFn] = None,
    run_git: Optional[GitRunner] = None,
) -> TicketWorkspace:
    """Create a fresh worktree for ``ticket_id`` from ``<remote>/<base>``.

    Directory: ``<main checkout>/.worktrees/<id>-<UTC timestamp>`` (a numeric
    suffix is added when the name is taken: an existing directory is never
    reused). Branch: ``ticket/<id>``; when that name is already used locally or
    on the remote (a previous attempt) -- or when ``unique_branch`` is set, for
    labels that are not yet a ticket id -- the timestamp is appended.
    Raises :class:`WorkspaceError` (never returns a half-built workspace).
    """
    runner = run_git or _git
    now = (now_fn or utc_now)()
    main_root = main_checkout_root(cwd, runner)
    ensure_local_excludes(main_root, runner)

    if fetch:
        res = runner(["fetch", remote, base], main_root)
        if res.returncode != 0:
            raise WorkspaceError(f"fetch {remote} {base} failed: {res.stderr.strip()}")
    base_ref = f"{remote}/{base}"
    sha_res = runner(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], main_root)
    if sha_res.returncode != 0:
        raise WorkspaceError(f"cannot resolve {base_ref}: {sha_res.stderr.strip()}")
    base_sha = sha_res.stdout.strip()

    slug, stamp = _slug(ticket_id), _stamp(now)
    root_dir = main_root / WORKTREES_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    name, suffix = f"{slug}-{stamp}", 1
    while (root_dir / name).exists():
        suffix += 1
        name = f"{slug}-{stamp}-{suffix}"
    path = root_dir / name

    branch = f"ticket/{slug}"
    taken = _ref_exists(f"refs/heads/{branch}", main_root, runner) or _ref_exists(
        f"refs/remotes/{remote}/{branch}", main_root, runner
    )
    if unique_branch or taken:
        branch = f"ticket/{name}"

    add = runner(["worktree", "add", "--no-track", "-b", branch, str(path), base_ref], main_root)
    if add.returncode != 0:
        if path.exists():
            cleanup(path, main_root, sleep_fn=lambda _s: None, run_git=run_git, retries=1, now_fn=now_fn)
        raise WorkspaceError(f"git worktree add failed: {add.stderr.strip()}")
    logger.info("Created worktree %s on %s (base %s@%s)", path, branch, base_ref, base_sha[:12])
    return TicketWorkspace(
        ticket_id=ticket_id, path=path, branch=branch, main_root=main_root, base_ref=base_ref, base_sha=base_sha
    )


# ----------------------------------------------------------------------
# Removal
# ----------------------------------------------------------------------


def _chmod_and_retry(func: Callable[[str], Any], target: str, exc: BaseException) -> None:
    """``rmtree`` error hook: clear the read-only bit (git objects, Windows) and retry once."""
    if isinstance(exc, FileNotFoundError):
        return
    try:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass
    func(target)


def robust_rmtree(path: Path) -> None:
    """``shutil.rmtree`` that clears read-only attributes; raises the last error on failure."""
    shutil.rmtree(path, onexc=_chmod_and_retry)


def trash_directory(path: Path, main_root: Path, *, now_fn: Optional[NowFn] = None) -> Path:
    """Move ``path`` into ``.factory/backups/trash/<name>-<timestamp>`` (recoverable). Raises OSError."""
    stamp = _stamp((now_fn or utc_now)())
    trash = main_root / TRASH_RELATIVE
    trash.mkdir(parents=True, exist_ok=True)
    name, suffix = f"{path.name}-{stamp}", 1
    while (trash / name).exists():
        suffix += 1
        name = f"{path.name}-{stamp}-{suffix}"
    target = trash / name
    os.rename(path, target)
    logger.warning("Moved %s to recoverable trash %s", path, target)
    return target


def _prune(main_root: Path, runner: GitRunner) -> None:
    runner(["worktree", "prune"], main_root)


def _is_within(candidate: Path | str, root: Path | str) -> bool:
    """True when ``candidate`` is ``root`` or lives below it (resolved, case-folded on Windows)."""
    try:
        return os.path.commonpath([_norm(candidate), _norm(root)]) == _norm(root)
    except ValueError:  # different drives
        return False


@contextmanager
def _outside_dir(path: Path, fallback_root: Path) -> Iterator[None]:
    """Make sure the process cwd is NOT inside ``path`` while the body runs (USR-231).

    On Windows a process whose cwd is a directory holds a handle on it: ``git worktree
    remove``, ``rmtree`` and the trash rename all fail with WinError 32 against the
    worktree the caller itself is standing in. When the cwd is inside ``path`` (or no
    longer resolvable) it is moved to ``fallback_root`` (the main checkout); on exit the
    previous cwd is restored when it still exists, also if the body raised.
    """
    try:
        previous: Optional[Path] = Path.cwd()
    except OSError:
        previous = None
    moved = previous is None or _is_within(previous, path)
    if moved:
        try:
            os.chdir(fallback_root)
            logger.info("cwd moved from %s to %s before removing %s", previous, fallback_root, path)
        except OSError as exc:
            moved = False
            logger.warning("could not leave %s (chdir %s failed): %s", path, fallback_root, exc)
    try:
        yield
    finally:
        if moved and previous is not None and previous.exists():
            try:
                os.chdir(previous)
            except OSError as exc:
                logger.warning("could not restore cwd %s: %s", previous, exc)


def _find_holders(path: Path) -> list[str]:
    """Best-effort ``name (pid)`` of processes whose cwd is inside ``path`` (needs psutil; else empty)."""
    try:
        import psutil  # optional
    except ImportError:
        return []
    holders: list[str] = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if _is_within(proc.cwd(), path):
                    holders.append(f"{proc.info.get('name')} (pid {proc.info.get('pid')})")
            except Exception:  # noqa: BLE001 -- access denied / vanished process
                continue
    except Exception:  # noqa: BLE001
        return holders
    return holders


def removal_hint(path: Path, main_root: Path) -> str:
    """Final-warning text: who may hold ``path`` and the exact command to finish the cleanup."""
    holders = _find_holders(path)
    who = (
        "processes with cwd inside it: " + ", ".join(holders)
        if holders
        else "a process (shell, editor, antivirus, agent) still has a handle or cwd inside it"
    )
    return (
        f"{path} could not be removed ({who}). Close that process, then run: "
        f'git -C "{main_root}" worktree remove --force "{path}" ; '
        f'Remove-Item -LiteralPath "{path}" -Recurse -Force ; git -C "{main_root}" worktree prune'
    )


def cleanup(
    path: Path,
    main_root: Path,
    *,
    sleep_fn: Optional[SleepFn] = None,
    retries: int = DEFAULT_CLEANUP_RETRIES,
    backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
    run_git: Optional[GitRunner] = None,
    rmtree_fn: Optional[RmtreeFn] = None,
    now_fn: Optional[NowFn] = None,
) -> CleanupResult:
    """Remove a worktree directory as reliably as the platform allows.

    1. ``git worktree remove --force`` up to ``retries`` times, sleeping
       ``backoff_s[i]`` between attempts (a transient Windows lock usually clears);
    2. a robust ``rmtree`` of whatever git left behind;
    3. when the directory is still there: RENAME it into the trash (never lose
       data, never leave a half-registered worktree) and ``git worktree prune``.

    Live processes with cwd inside ``path`` preserve the checkout before any Git or
    filesystem mutation. Otherwise, this process cwd is moved out of ``path`` for the
    removal operation (:func:`_outside_dir`).
    Never raises; ``CleanupResult.removed`` says whether ``path`` is gone.
    """
    if path.exists():
        holders = _find_holders(path)
        if holders:
            result = CleanupResult(
                path=path,
                removed=False,
                method="preserved",
                detail=f"{path} is in use; preserved because processes have cwd inside it: "
                + ", ".join(holders),
            )
            logger.warning(result.detail)
            return result

    with _outside_dir(path, main_root):
        return _cleanup_outside(
            path, main_root, sleep_fn=sleep_fn, retries=retries, backoff_s=backoff_s,
            run_git=run_git, rmtree_fn=rmtree_fn, now_fn=now_fn,
        )


def _cleanup_outside(
    path: Path,
    main_root: Path,
    *,
    sleep_fn: Optional[SleepFn],
    retries: int,
    backoff_s: Sequence[float],
    run_git: Optional[GitRunner],
    rmtree_fn: Optional[RmtreeFn],
    now_fn: Optional[NowFn],
) -> CleanupResult:
    runner = run_git or _git
    sleeper = sleep_fn or time.sleep
    remove_tree = rmtree_fn or robust_rmtree
    result = CleanupResult(path=path, removed=False, method="failed")

    if not path.exists():
        _prune(main_root, runner)
        result.removed, result.method = True, "absent"
        return result

    # A locked worktree is in use by a running agent: git refuses to remove it and
    # the rmtree/trash fallbacks below must not be used to get around that.
    target = _norm(path)
    for entry in list_worktrees(main_root, run_git):
        if _norm(entry["worktree"]) == target and "locked" in entry:
            result.detail = f"{path} is a locked worktree (in use); left untouched"
            logger.warning(result.detail)
            return result

    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        result.attempts = attempt
        res = runner(["worktree", "remove", "--force", str(path)], main_root)
        if res.returncode == 0 and not path.exists():
            _prune(main_root, runner)
            result.removed, result.method = True, "git"
            return result
        message = (res.stderr or res.stdout or "").strip() or "directory still present after git worktree remove"
        result.errors.append(f"attempt {attempt}: {message}")
        logger.warning("worktree remove %s (attempt %s/%s): %s", path, attempt, attempts, message)
        if attempt < attempts:
            sleeper(backoff_s[min(attempt - 1, len(backoff_s) - 1)] if backoff_s else 0.0)

    # git gave up: delete what is left ourselves.
    try:
        if path.exists():
            remove_tree(path)
    except OSError as exc:
        result.errors.append(f"rmtree: {exc}")
        logger.warning("rmtree %s failed: %s", path, exc)
    if not path.exists():
        _prune(main_root, runner)
        result.removed, result.method = True, "rmtree"
        return result

    # Still locked: rename out of the way, recoverable for TRASH_RETENTION_DAYS.
    try:
        result.trash_path = trash_directory(path, main_root, now_fn=now_fn)
    except OSError as exc:
        result.errors.append(f"trash: {exc}")
        result.detail = f"could not remove or move {path}: {exc}. {removal_hint(path, main_root)}"
        logger.error("Could not remove or trash %s: %s", path, result.detail)
        return result
    _prune(main_root, runner)
    result.removed, result.method = True, "trash"
    result.detail = f"moved to {result.trash_path}"
    return result


# ----------------------------------------------------------------------
# Orphans and trash retention
# ----------------------------------------------------------------------


def latest_mtime(path: Path) -> float:
    """Newest mtime of ``path`` and its direct children (a cheap 'recently touched' probe)."""
    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    try:
        for child in path.iterdir():
            try:
                latest = max(latest, child.lstat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def find_orphan_dirs(
    main_root: Path,
    *,
    min_age_s: float = ORPHAN_MIN_AGE_S,
    now_fn: Optional[NowFn] = None,
    run_git: Optional[GitRunner] = None,
) -> tuple[list[Path], list[str]]:
    """Dead directories under the worktree roots, plus notes on entries left alone.

    Orphan = a directory with NO ``.git`` entry that ``git worktree list`` does
    not register, not modified within ``min_age_s`` and not a reserved state
    directory. A directory that has a ``.git`` entry but is unregistered (a
    broken worktree that may hold unpushed work) is reported, never touched.
    """
    registered = {_norm(entry["worktree"]) for entry in list_worktrees(main_root, run_git)}
    now = (now_fn or utc_now)().timestamp()
    orphans: list[Path] = []
    notes: list[str] = []
    for relative in WORKTREE_ROOTS:
        root_dir = main_root / relative
        if not root_dir.is_dir():
            continue
        for child in sorted(root_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name in RESERVED_DIR_NAMES:
                continue
            if _norm(child) in registered:
                continue
            if (child / ".git").exists():
                notes.append(f"{child} (has .git but is not a registered worktree; left alone)")
                continue
            if now - latest_mtime(child) < min_age_s:
                notes.append(f"{child} (modified recently; left alone)")
                continue
            orphans.append(child)
    return orphans, notes


def _trash_age(entry: Path, now: datetime) -> timedelta:
    """Age of a trash entry from the timestamp in its name (mtime as fallback)."""
    match = _STAMP_PATTERN.search(entry.name)
    if match:
        try:
            moved_at = datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=timezone.utc)
            return now - moved_at
        except ValueError:
            pass
    try:
        return now - datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return timedelta(0)


ReclaimFn = Callable[[Path], bool]

_WINERROR_ACCESS_DENIED = 5
_PURGE_MAX_PASSES = 3
_SUMMARY_MAX_NAMES = 5


def _is_access_denied(exc: BaseException) -> bool:
    """True for ``PermissionError`` / ``[WinError 5] Access is denied``."""
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == _WINERROR_ACCESS_DENIED


def _purge_tree(path: Path) -> list[tuple[str, OSError]]:
    """Delete as much of ``path`` as possible; return the ``(path, error)`` pairs that resisted.

    Each failing step first gets the read-only bit cleared and ONE retry
    (``shutil.rmtree`` ``onexc`` hook); a step that still fails is recorded and the walk goes on, so
    one blocked directory no longer stops its siblings from being deleted. When a directory was only
    unreadable because of the read-only bit, a further pass (at most ``_PURGE_MAX_PASSES``) picks up
    the contents that the first pass had to skip.
    """
    blocked: list[tuple[str, OSError]] = []

    def hook(func: Callable[[str], Any], target: str, exc: BaseException) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
        try:
            func(target)
        except FileNotFoundError:
            return
        except OSError as again:
            blocked.append((target, again))

    for _ in range(_PURGE_MAX_PASSES):
        blocked.clear()
        try:
            shutil.rmtree(path, onexc=hook)
        except FileNotFoundError:
            return []
        if not path.exists() or not blocked:
            break
    return list(blocked) if path.exists() else []


def _reclaim_windows_access(path: Path) -> bool:
    """Best effort: take ownership of ``path`` and grant the current user full control (Windows only).

    Needs an elevated token when the owner is another identity; without it both commands fail and
    this returns False (the caller then reports the entry as unremovable, once).
    """
    if sys.platform != "win32":
        return False
    import getpass

    user = getpass.getuser()
    commands = (
        ["takeown", "/F", str(path), "/R", "/D", "Y"],
        ["icacls", str(path), "/grant", f"{user}:(OI)(CI)F", "/T", "/C", "/Q"],
    )
    ok = True
    for cmd in commands:
        try:
            done = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_GIT_TIMEOUT_S, **_win_kwargs(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("Reclaim command %s failed to run: %s", cmd[0], exc)
            return False
        ok = ok and done.returncode == 0
    return ok


def _describe_failure(entry: Path, blocked: Sequence[tuple[str, OSError]], exc: Optional[OSError]) -> str:
    """Short, single-line reason an entry could not be removed (the first blocker is enough)."""
    if blocked:
        target, error = blocked[0]
        try:
            where = str(Path(target).relative_to(entry))
        except ValueError:
            where = target
        if _is_access_denied(error):
            return f"access denied (WinError 5) at {where}: protected ACL or foreign owner"
        return f"{error.strerror or error} at {where}"
    if exc is not None:
        return "access denied (WinError 5): protected ACL or foreign owner" if _is_access_denied(exc) else str(exc)
    return "entry still present after removal"


def purge_trash(
    main_root: Path,
    *,
    retention_days: float = TRASH_RETENTION_DAYS,
    now_fn: Optional[NowFn] = None,
    rmtree_fn: Optional[RmtreeFn] = None,
    reclaim_fn: Optional[ReclaimFn] = None,
) -> TrashReport:
    """Delete trash entries older than ``retention_days``; younger ones stay recoverable.

    Read-only files and directories are cleared and retried (see :func:`_purge_tree`). Failures are
    NOT logged one by one: the sweep emits ONE summary line per call (purged / kept / unremovable
    with the reason) so a stuck entry cannot bury useful messages (USR-224). The per-entry reasons
    are also in ``TrashReport.failed`` (``"<entry>: <reason>"``).

    Diagnosis of the recurring ``[WinError 5]`` (USR-224, evidence collected on the real trash,
    22 entries, read-only probes only): the read-only attribute was NOT the cause (0 files with
    ``FILE_ATTRIBUTE_READONLY`` in 31 entries). Exactly one directory per entry resists, the
    ``.pytest_cache`` at its root: ``icacls`` on it answers "Acesso negado", ``Get-Acl`` fails with
    "unauthorized operation" and ``CreateFileW`` with DELETE access returns error 5, while the
    entry itself and every other file open fine. ``pytest`` creates that directory with
    ``tempfile.mkdtemp`` (mode 0o700), which on Windows applies a PROTECTED DACL granting only
    SYSTEM, Administrators and OWNER RIGHTS (reproduced: ``icacls`` of a fresh ``mkdtemp`` shows
    just those three, no inheritance). Created by another identity (an elevated or sandboxed
    agent run), the owner is not the interactive user, whose token carries Administrators only as
    deny-only; neither DELETE on the object nor DELETE_CHILD on the parent is available, so even
    ``git worktree remove``/``rmtree`` fail and the worktree lands in the trash in the first place.
    No handle was open (sharing violations are WinError 32, not 5). Only an elevated
    ``takeown``/``icacls`` can fix those entries; ``reclaim_fn`` tries that when the process has the
    rights and is otherwise a harmless no-op.
    """
    report = TrashReport()
    trash = main_root / TRASH_RELATIVE
    if not trash.is_dir():
        return report
    now = (now_fn or utc_now)()
    reclaim = reclaim_fn or _reclaim_windows_access
    for entry in sorted(trash.iterdir(), key=lambda p: p.name):
        if _trash_age(entry, now) < timedelta(days=retention_days):
            report.kept.append(entry.name)
            continue
        blocked: list[tuple[str, OSError]] = []
        failure: Optional[OSError] = None
        try:
            if entry.is_dir() and not entry.is_symlink():
                if rmtree_fn is not None:
                    rmtree_fn(entry)
                else:
                    blocked = _purge_tree(entry)
                    denied = sorted({t for t, e in blocked if _is_access_denied(e)})
                    if denied and os.path.lexists(entry):
                        if any(reclaim(Path(target)) for target in denied):
                            blocked = _purge_tree(entry)
            else:
                try:
                    entry.unlink()
                except PermissionError:
                    os.chmod(entry, stat.S_IWRITE | stat.S_IREAD)
                    entry.unlink()
        except OSError as exc:
            failure = exc
        if not os.path.lexists(entry):
            report.purged.append(entry.name)
        else:
            report.failed.append(f"{entry.name}: {_describe_failure(entry, blocked, failure)}")
    _log_purge_summary(report)
    return report


def _log_purge_summary(report: TrashReport) -> None:
    """ONE line per sweep; unremovable entries are listed once, with the reason of each."""
    if not (report.purged or report.failed):
        return
    summary = f"Trash purge: {len(report.purged)} purged, {len(report.kept)} kept (recoverable)"
    if not report.failed:
        logger.info(summary)
        return
    shown = "; ".join(report.failed[:_SUMMARY_MAX_NAMES])
    extra = len(report.failed) - _SUMMARY_MAX_NAMES
    more = f"; +{extra} more (see TrashReport.failed)" if extra > 0 else ""
    logger.warning("%s, %d unremovable: %s%s", summary, len(report.failed), shown, more)


def find_ticket_worktree(
    ticket_id: str,
    main_root: Path,
    run_git: Optional[GitRunner] = None,
) -> Optional[Path]:
    """Find the most recent active worktree associated with a ticket ID (USR-116).

    Matches worktrees whose branch is ticket/<slug> or directory name contains <slug>.
    Returns the path of the most recently modified matching worktree, or None.
    """
    slug = _slug(ticket_id)
    runner = run_git or _git
    candidates: list[Path] = []
    seen: set[str] = set()

    for entry in list_worktrees(main_root, runner):
        wt_str = entry.get("worktree", "")
        if not wt_str:
            continue
        p = Path(wt_str).resolve()
        norm = _norm(p)
        if norm in seen:
            continue
        br = entry.get("branch", "").removeprefix("refs/heads/").lower()
        name = p.name.lower()
        if br == f"ticket/{slug}" or br.startswith(f"ticket/{slug}-") or name.startswith(f"{slug}-"):
            if p.is_dir():
                candidates.append(p)
                seen.add(norm)

    for root_rel in WORKTREE_ROOTS:
        root_dir = main_root / root_rel
        if root_dir.is_dir():
            for p in root_dir.glob(f"{slug}-*"):
                p_res = p.resolve()
                norm = _norm(p_res)
                if norm not in seen and p_res.is_dir():
                    candidates.append(p_res)
                    seen.add(norm)

    if not candidates:
        return None
    candidates.sort(key=latest_mtime, reverse=True)
    return candidates[0]


# ----------------------------------------------------------------------
# Reuse of a preserved worktree (USR-225)
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PreservedWorkspace:
    """A worktree an earlier attempt of the same ticket left behind WITH work in it."""

    workspace: TicketWorkspace
    changed_paths: tuple[str, ...]
    commits_ahead: int


@dataclass
class PreservedLookup:
    """Outcome of :func:`find_preserved_workspaces`.

    ``chosen`` is the worktree to resume (most recent one with work); ``others`` are the remaining ones
    with work (reported, never touched); ``notes`` explain every candidate that was left out.
    """

    chosen: Optional[PreservedWorkspace] = None
    others: list[PreservedWorkspace] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _status_paths(porcelain_z: str) -> list[str]:
    """Paths from ``git status --porcelain=v1 -z`` (rename sources are skipped, targets kept)."""
    paths: list[str] = []
    tokens = porcelain_z.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if len(token) < 4 or token[2] != " ":
            continue
        status, path = token[:2], token[3:]
        if "R" in status or "C" in status:
            index += 1
        paths.append(path)
    return list(dict.fromkeys(paths))


def find_preserved_workspaces(
    ticket_id: str,
    *,
    cwd: Path,
    base: str = "main",
    remote: str = "origin",
    fetch: bool = True,
    ignore_paths: Sequence[str] = (),
    run_git: Optional[GitRunner] = None,
) -> PreservedLookup:
    """Worktrees of ``ticket_id`` that an interrupted attempt preserved and that can be resumed (USR-225).

    A candidate is a worktree REGISTERED in git, living at ``<main>/.worktrees/<id>-<UTC stamp>`` (the
    names :func:`create_workspace` hands out) on branch ``ticket/<id>`` or ``ticket/<dir name>``, whose
    directory exists, that is neither ``git worktree lock``-ed / prunable nor holding a git ``index.lock``
    (another session is using it), and that has work: changed paths other than ``ignore_paths`` or commits
    ahead of ``<remote>/<base>``. The most recent one by name stamp is ``chosen``. Nothing is modified or
    deleted: worktrees of other tickets are never looked at.
    """
    runner = run_git or _git
    lookup = PreservedLookup()
    main_root = main_checkout_root(cwd, runner)
    if fetch:
        runner(["fetch", remote, base], main_root)  # best effort: create_workspace is the one that insists
    base_ref = f"{remote}/{base}"
    if not _ref_exists(f"{base_ref}^{{commit}}", main_root, runner):
        return lookup

    slug = _slug(ticket_id)
    name_pattern = re.compile(rf"^{re.escape(slug)}-(\d{{8}}T\d{{6}}Z)(?:-(\d+))?$")
    worktrees_dir = main_root / WORKTREES_DIRNAME
    ignored = set(ignore_paths)
    ranked: list[tuple[tuple[str, int], PreservedWorkspace]] = []

    for entry in list_worktrees(main_root, runner)[1:]:  # [0] is the primary checkout
        path = Path(entry["worktree"])
        match = name_pattern.match(path.name)
        if not match or _norm(path.parent) != _norm(worktrees_dir):
            continue
        branch = entry.get("branch", "").removeprefix("refs/heads/")
        if branch.lower() not in (f"ticket/{slug}", f"ticket/{path.name.lower()}"):
            lookup.notes.append(f"{path} (branch {branch or 'destacada'} nao e deste ticket; ignorada)")
            continue
        if not path.is_dir():
            lookup.notes.append(f"{path} (diretorio inexistente; ignorada)")
            continue
        if "locked" in entry or "prunable" in entry:
            lookup.notes.append(f"{path} (worktree bloqueada/prunable no git; ignorada)")
            continue
        git_dir = runner(["rev-parse", "--absolute-git-dir"], path)
        if git_dir.returncode != 0:
            lookup.notes.append(f"{path} (git ilegivel; ignorada)")
            continue
        if (Path(git_dir.stdout.strip()) / "index.lock").exists():
            lookup.notes.append(f"{path} (index.lock presente: em uso por outra sessao; ignorada)")
            continue
        status = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
        if status.returncode != 0:
            lookup.notes.append(f"{path} (git status falhou; ignorada)")
            continue
        changed = tuple(p for p in _status_paths(status.stdout) if p not in ignored)
        ahead_res = runner(["rev-list", "--count", f"{base_ref}..HEAD"], path)
        ahead = int(ahead_res.stdout.strip()) if ahead_res.returncode == 0 and ahead_res.stdout.strip().isdigit() else 0
        if not changed and ahead == 0:
            lookup.notes.append(f"{path} (sem alteracoes nem commits fora da base; ignorada)")
            continue
        base_sha_res = runner(["merge-base", base_ref, "HEAD"], path)
        base_sha = base_sha_res.stdout.strip() if base_sha_res.returncode == 0 else ""
        if not base_sha:
            base_sha = runner(["rev-parse", base_ref], main_root).stdout.strip()
        workspace = TicketWorkspace(
            ticket_id=ticket_id, path=path, branch=branch, main_root=main_root, base_ref=base_ref, base_sha=base_sha
        )
        ranked.append(
            ((match.group(1), int(match.group(2) or 1)), PreservedWorkspace(workspace, changed, ahead))
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked:
        lookup.chosen = ranked[0][1]
        lookup.others = [item[1] for item in ranked[1:]]
    return lookup

