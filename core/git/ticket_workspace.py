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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

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
    method: str  # absent | git | rmtree | trash | failed
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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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

    Never raises; ``CleanupResult.removed`` says whether ``path`` is gone.
    """
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
        result.detail = f"could not remove or move {path}: {exc}"
        logger.error("Could not remove or trash %s: %s", path, exc)
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


def purge_trash(
    main_root: Path,
    *,
    retention_days: float = TRASH_RETENTION_DAYS,
    now_fn: Optional[NowFn] = None,
    rmtree_fn: Optional[RmtreeFn] = None,
) -> TrashReport:
    """Delete trash entries older than ``retention_days``; younger ones stay recoverable."""
    report = TrashReport()
    trash = main_root / TRASH_RELATIVE
    if not trash.is_dir():
        return report
    now = (now_fn or utc_now)()
    remove_tree = rmtree_fn or robust_rmtree
    for entry in sorted(trash.iterdir(), key=lambda p: p.name):
        if _trash_age(entry, now) < timedelta(days=retention_days):
            report.kept.append(entry.name)
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                remove_tree(entry)
            else:
                entry.unlink()
            report.purged.append(entry.name)
        except OSError as exc:
            logger.warning("Could not purge trash entry %s: %s", entry, exc)
            report.failed.append(f"{entry.name}: {exc}")
    return report
