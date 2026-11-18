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
* :func:`classify_ownerless` -- pure verdict for one worktree (eligible for a
  later sweep, or preserved, with a reason). It never calls :func:`cleanup`,
  never removes a directory and never deletes a branch. Worktrees are eligible
  only after :data:`OWNERLESS_MAX_AGE_S` (48 hours).

Stdlib-only (it must not import ``core.git.autonomy``, which imports it).
"""

from __future__ import annotations

import hashlib
import json
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
from typing import Any, Callable, Iterable, Iterator, Literal, Mapping, Optional, Sequence

from core.paths import state_root
from core.common.tempdirs import chmod_and_retry as _chmod_and_retry
from core.common.time import utc_now

logger = logging.getLogger("darkfac.git.ticket_workspace")

WORKTREES_DIRNAME = ".worktrees"
# Roots swept for dead directories, relative to the main checkout.
WORKTREE_ROOTS: tuple[Path, ...] = (Path(WORKTREES_DIRNAME), Path(".claude") / "worktrees")
TRASH_RELATIVE = Path(".factory") / "backups" / "trash"
CLEANUP_PENDING_RELATIVE = Path(".factory") / "runs" / "cleanup_pending"
TICKET_LEDGER_RELATIVE = ".factory/demands/demands.json"
TRASH_RETENTION_DAYS = 7
# Machine-state directories that live next to worktrees and must never be
# treated as dead worktrees (e.g. a DARKFAC_HARNESS_STATE_DIR pointed there).
RESERVED_DIR_NAMES = frozenset({"harness_state"})
# A directory touched more recently than this is never classified as an orphan:
# it may be a worktree being created right now.
ORPHAN_MIN_AGE_S = 3600.0
# `.worktrees/`, `/.factory/backups/` and `/.tmp/` are already in the repository
# .gitignore; a temporary or foreign clone may not have those rules yet, so the
# local exclude file is kept in sync too. `/.tmp/` is harness scratch (locks,
# timings) and must stay out of `git status` and `git add` (USR-429).
LOCAL_EXCLUDE_ENTRIES: tuple[str, ...] = ("/.worktrees/", "/.factory/backups/", "/.tmp/")
# How many scratch paths one `git update-index` removes (Windows command-line length).
_SCRATCH_INDEX_CHUNK = 50

DEFAULT_CLEANUP_RETRIES = 4
DEFAULT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_STAMP_PATTERN = re.compile(r"-(\d{8}T\d{6}Z)(?:-\d+)?$")
_GIT_TIMEOUT_S = 120.0

GitRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]
GitArgRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
SleepFn = Callable[[float], None]
NowFn = Callable[[], datetime]
RmtreeFn = Callable[[Path], None]
HolderProbe = Callable[[Path], Optional[Sequence[str]]]
CleanupOutcome = Literal["removed", "blocked_changes", "blocked_process", "error"]


def is_delivery_scratch(path: str) -> bool:
    """True only for the relative POSIX path ``.tmp`` or a path prefixed by ``.tmp/``.

    Backslashes are separators, so a Windows relative path is judged in POSIX
    form. Absolute paths, any other prefix (``src/.tmp``, ``.tmpfile``) and any
    ``..`` segment are product content.
    """
    text = str(path).replace("\\", "/")
    if not text or text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        return False
    if ".." in text.split("/"):
        return False
    return text == ".tmp" or text.startswith(".tmp/")


def without_delivery_scratch(paths: Iterable[str]) -> list[str]:
    """``paths`` minus delivery scratch, preserving order and the original spelling."""
    return [path for path in paths if not is_delivery_scratch(str(path))]


def drop_delivery_scratch_commands(ls_files_z: str, *, chunk: int = _SCRATCH_INDEX_CHUNK) -> list[list[str]]:
    """``git update-index --force-remove`` argv lists for scratch paths in ``ls_files_z``.

    ``ls_files_z`` is the NUL-separated output of ``git ls-files -z -- .tmp``.
    The commands take those paths out of the index and do not delete worktree bytes.
    """
    paths = [token.strip("\r\n") for token in ls_files_z.split("\0")]
    scratch = [path for path in paths if path and is_delivery_scratch(path)]
    commands: list[list[str]] = []
    for start in range(0, len(scratch), chunk):
        part = scratch[start : start + chunk]
        commands.append(["update-index", "--force-remove", "--", *part])
    return commands


def apply_drop_delivery_scratch(run: GitArgRunner) -> None:
    """Remove delivery scratch from the index ``run`` addresses. Worktree bytes stay.

    ``run`` executes one git argv list in the target repository and index.
    A non-zero exit raises ``RuntimeError``.
    """
    listed = run(["ls-files", "-z", "--", ".tmp"])
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout or "").strip()
        raise RuntimeError(f"Cannot list delivery scratch: {detail}")
    for command in drop_delivery_scratch_commands(listed.stdout or ""):
        removed = run(command)
        if removed.returncode != 0:
            detail = (removed.stderr or removed.stdout or "").strip()
            raise RuntimeError(f"Cannot drop delivery scratch from the index: {detail}")


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
    discard_path_hashes: Mapping[str, str] = field(default_factory=dict)


@dataclass
class CleanupResult:
    """Observable outcome of :func:`cleanup`.

    ``outcome`` is stable for callers that need to record why cleanup stopped:
    ``removed``, ``blocked_changes``, ``blocked_process``, or ``error``. ``method``
    retains the removal mechanism and older caller behavior.
    """

    path: Path
    removed: bool
    method: str  # absent | preserved | git | rmtree | trash | failed
    attempts: int = 0
    trash_path: Optional[Path] = None
    detail: str = ""
    errors: list[str] = field(default_factory=list)
    outcome: CleanupOutcome = "error"
    reason: str = ""


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
    """Append the scratch directories to ``<common-git-dir>/info/exclude`` if missing.

    Local to the machine (never tracked): keeps ``.worktrees/`` and ``.tmp/``
    out of ``git status`` / ``git add`` even in a clone whose ``.gitignore``
    does not have those rules yet.
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


def _find_holders(path: Path) -> Optional[list[str]]:
    """Processes using the worktree, or ``None`` when inspection is inconclusive."""
    holders = probe_worktree_holders(path)
    return None if holders is None else list(holders)


def removal_hint(path: Path, main_root: Path) -> str:
    """Final-warning text; physical removal must be retried through :func:`cleanup`."""
    holders = _find_holders(path)
    if holders is None:
        who = "process state could not be inspected"
    elif holders:
        who = "processes using it: " + ", ".join(holders)
    else:
        who = "a process or filesystem handle may still be using it"
    return (
        f"{path} could not be removed ({who}). Resolve the holder or filesystem error, "
        "then retry removal through ticket_workspace.cleanup."
    )


def cleanup(
    path: Path,
    main_root: Path,
    *,
    discard_path_hashes: Optional[Mapping[str, str]] = None,
    sleep_fn: Optional[SleepFn] = None,
    retries: int = DEFAULT_CLEANUP_RETRIES,
    backoff_s: Sequence[float] = DEFAULT_BACKOFF_S,
    run_git: Optional[GitRunner] = None,
    rmtree_fn: Optional[RmtreeFn] = None,
    now_fn: Optional[NowFn] = None,
    holder_probe: Optional[HolderProbe] = None,
) -> CleanupResult:
    """Remove a worktree directory as reliably as the platform allows.

    1. non-forced ``git worktree remove`` up to ``retries`` times, sleeping
       ``backoff_s[i]`` between attempts (a transient Windows lock usually clears);
    2. a robust ``rmtree`` of whatever git left behind;
    3. when the directory is still there: RENAME it into the trash (never lose
       data, never leave a half-registered worktree) and ``git worktree prune``.

    Each destructive operation rechecks that ``path`` is a registered, unlocked,
    clean worktree and that no process is using it. Callers may provide SHA-256
    hashes for tracked generated inputs; a path is restored to HEAD only while its
    current bytes still match the caller's expected content. Unknown Git or process
    state preserves the worktree. This process cwd is moved out of ``path`` before
    those final checks (:func:`_outside_dir`).
    Never raises; ``CleanupResult.outcome`` records removal, dirty/process blocks,
    or an error. The Git remove is intentionally non-forced so a change created
    between inspection and removal is still rejected by Git.
    """
    try:
        with _outside_dir(path, main_root):
            result = _cleanup_outside(
                path, main_root, sleep_fn=sleep_fn, retries=retries, backoff_s=backoff_s,
                run_git=run_git, rmtree_fn=rmtree_fn, now_fn=now_fn, holder_probe=holder_probe,
                discard_path_hashes=discard_path_hashes or {},
            )
    except Exception as exc:  # noqa: BLE001 -- cleanup reports failures instead of leaking them
        detail = f"cleanup failed for {path}: {exc}"
        logger.exception(detail)
        result = CleanupResult(
            path=path,
            removed=False,
            method="failed",
            detail=detail,
            errors=[str(exc)],
            outcome="error",
            reason="cleanup_exception",
        )
    _sync_cleanup_pending(result, main_root)
    return result


def _cleanup_pending_record_path(path: Path, main_root: Path) -> Path:
    """Stable per-worktree record path; separate files avoid lost updates across cleanups."""
    key = hashlib.sha256(_norm(path).encode("utf-8", errors="surrogatepass")).hexdigest()
    return main_root / CLEANUP_PENDING_RELATIVE / f"{key}.json"


def _sync_cleanup_pending(result: CleanupResult, main_root: Path) -> None:
    """Persist every surviving cleanup attempt, or clear its record after removal."""
    record_path = _cleanup_pending_record_path(result.path, main_root)
    try:
        if result.removed or not result.path.exists():
            record_path.unlink(missing_ok=True)
            return

        record_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "path": str(result.path.resolve()),
            "main_root": str(main_root.resolve()),
            "outcome": result.outcome,
            "reason": result.reason,
            "detail": result.detail,
            "updated_at": utc_now().isoformat(),
        }
        temporary = record_path.with_name(
            f"{record_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, record_path)
        finally:
            temporary.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 -- reporting cleanup must not introduce a new failure
        logger.error("Could not update pending cleanup record for %s: %s", result.path, exc)
        result.errors.append(f"cleanup_pending: {exc}")
        if result.detail:
            result.detail += f"; pending cleanup record failed: {exc}"
        else:
            result.detail = f"pending cleanup record failed: {exc}"


def list_cleanup_pending(main_root: Path) -> list[dict[str, Any]]:
    """Read the durable records for worktrees whose removal still needs attention."""
    records: list[dict[str, Any]] = []
    directory = main_root / CLEANUP_PENDING_RELATIVE
    if not directory.is_dir():
        return records
    for record_path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            logger.warning("Could not read pending cleanup record %s: %s", record_path, exc)
            continue
        if isinstance(payload, dict) and isinstance(payload.get("path"), str):
            records.append(payload)
        else:
            logger.warning("Ignoring malformed pending cleanup record %s", record_path)
    return records


def cleanup_pending_worktree_paths(main_root: Path) -> frozenset[str]:
    """Normalized paths with a durable cleanup record that must stay out of orphan sweeps."""
    return frozenset(
        _norm(record["path"])
        for record in list_cleanup_pending(main_root)
        if isinstance(record.get("path"), str)
    )


@dataclass(frozen=True)
class _CleanupSafetyIssue:
    outcome: CleanupOutcome
    reason: str
    detail: str


def _cleanup_safety_issue(
    path: Path,
    main_root: Path,
    runner: GitRunner,
    holder_probe: HolderProbe,
    discard_path_hashes: Mapping[str, str] | frozenset[str] = frozenset(),
) -> Optional[_CleanupSafetyIssue]:
    """Return why removal is unsafe, failing closed when ownership cannot be proved."""
    try:
        entries = list_worktrees(main_root, runner)
    except Exception as exc:  # noqa: BLE001 -- a failed probe must not authorize deletion
        return _CleanupSafetyIssue("error", "worktree_list_error", f"could not inspect registered worktrees: {exc}")
    if not entries:
        return _CleanupSafetyIssue(
            "error", "worktree_list_empty", "could not inspect registered worktrees; Git returned no worktrees"
        )

    target = _norm(path)
    primary = _norm(entries[0]["worktree"])
    if target == primary:
        return _CleanupSafetyIssue("error", "primary_checkout", "preserved because the path is the primary checkout")
    entry = next((item for item in entries if _norm(item.get("worktree", "")) == target), None)
    if entry is None:
        return _CleanupSafetyIssue("error", "not_registered", "preserved because the path is not a registered worktree")
    if "locked" in entry:
        return _CleanupSafetyIssue("error", "locked", "preserved because the worktree is locked")

    try:
        status = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    except Exception as exc:  # noqa: BLE001 -- unknown worktree state must be preserved
        return _CleanupSafetyIssue("error", "git_status_error", f"could not inspect worktree changes: {exc}")
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "git status failed").strip()
        return _CleanupSafetyIssue("error", "git_status_error", f"could not inspect worktree changes; preserved: {detail}")
    changes = _status_paths(status.stdout or "")
    discardable = {change.replace("\\", "/") for change in changes}
    if changes and not discardable.issubset(discard_path_hashes):
        return _CleanupSafetyIssue(
            "blocked_changes",
            "worktree_dirty",
            "preserved because the worktree has staged, tracked, or untracked changes: " + ", ".join(changes[:5]),
        )

    # Process inspection is the final check immediately before each destructive
    # call. Git probes above are read-only and establish registration and status.
    try:
        holders = holder_probe(path)
    except Exception as exc:  # noqa: BLE001 -- a broken probe must not look idle
        return _CleanupSafetyIssue("error", "process_probe_error", f"could not inspect live processes; preserved: {exc}")
    if holders is None:
        return _CleanupSafetyIssue(
            "error", "process_state_unknown", "could not inspect live processes; preserved because process state is unknown"
        )
    if holders:
        return _CleanupSafetyIssue(
            "blocked_process", "live_process", "preserved because processes are using it: " + ", ".join(holders)
        )
    return None


def _validated_discard_path_hashes(path_hashes: Mapping[str, str]) -> dict[str, str]:
    """Normalize generated paths and validate their expected SHA-256 content hashes."""
    normalized: dict[str, str] = {}
    for raw_path, expected_hash in path_hashes.items():
        value = str(raw_path).replace("\\", "/")
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or (len(value) >= 2 and value[1] == ":")
            or value.startswith(":(")
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"discard path must be a literal repository-relative path: {raw_path!r}")
        digest = str(expected_hash).casefold()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(f"discard path requires an expected SHA-256 hash: {raw_path!r}")
        normalized[value] = digest
    return normalized


def _restore_discard_paths(
    path: Path,
    discard_path_hashes: Mapping[str, str],
    runner: GitRunner,
) -> Optional[_CleanupSafetyIssue]:
    """Restore generated files only while their bytes match the recorded hash."""
    if not discard_path_hashes:
        return None
    status = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout or "git status failed").strip()
        return _CleanupSafetyIssue("error", "git_status_error", f"could not inspect worktree changes; preserved: {detail}")
    changed = [item.replace("\\", "/") for item in _status_paths(status.stdout or "")]
    if not changed:
        return None
    if not set(changed).issubset(discard_path_hashes):
        return _CleanupSafetyIssue(
            "blocked_changes",
            "worktree_dirty",
            "preserved because the worktree has staged, tracked, or untracked changes: " + ", ".join(changed[:5]),
        )

    literal_paths = [f":(literal){item}" for item in changed]
    tracked = runner(["ls-files", "--error-unmatch", "--", *literal_paths], path)
    if tracked.returncode != 0:
        return _CleanupSafetyIssue(
            "blocked_changes",
            "discard_path_untracked",
            "preserved because an explicitly discardable path is not tracked: " + ", ".join(changed[:5]),
        )
    staged = runner(["diff", "--cached", "--quiet", "--", *literal_paths], path)
    if staged.returncode == 1:
        return _CleanupSafetyIssue(
            "blocked_changes",
            "discard_path_staged",
            "preserved because generated paths contain staged changes: " + ", ".join(changed[:5]),
        )
    if staged.returncode != 0:
        detail = (staged.stderr or staged.stdout or "could not inspect staged paths").strip()
        return _CleanupSafetyIssue(
            "error", "discard_path_index_error", f"could not inspect staged generated paths; preserved: {detail}"
        )
    for relative in changed:
        candidate = path / Path(relative)
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("not a regular file")
            actual_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError as exc:
            return _CleanupSafetyIssue(
                "blocked_changes",
                "discard_path_unverified",
                f"preserved because generated path {relative} could not be verified: {exc}",
            )
        if actual_hash != discard_path_hashes[relative]:
            return _CleanupSafetyIssue(
                "blocked_changes",
                "discard_path_changed",
                f"preserved because generated path {relative} no longer matches its recorded content",
            )
    restored = runner(
        ["restore", "--source=HEAD", "--staged", "--worktree", "--", *literal_paths], path
    )
    if restored.returncode != 0:
        detail = (restored.stderr or restored.stdout or "git restore failed").strip()
        return _CleanupSafetyIssue(
            "error", "discard_path_restore_failed", f"could not restore generated paths; preserved: {detail}"
        )
    status_after = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
    if status_after.returncode != 0 or status_after.stdout.strip():
        detail = (status_after.stderr or status_after.stdout or "worktree changed during cleanup").strip()
        return _CleanupSafetyIssue(
            "blocked_changes", "worktree_changed_during_cleanup", f"preserved because worktree changed: {detail}"
        )
    return None


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
    holder_probe: Optional[HolderProbe],
    discard_path_hashes: Mapping[str, str],
) -> CleanupResult:
    runner = run_git or _git
    sleeper = sleep_fn or time.sleep
    remove_tree = rmtree_fn or robust_rmtree
    inspect_holders = holder_probe if holder_probe is not None else _find_holders
    try:
        discardable = _validated_discard_path_hashes(discard_path_hashes)
    except ValueError as exc:
        return CleanupResult(
            path=path,
            removed=False,
            method="preserved",
            detail=str(exc),
            outcome="error",
            reason="invalid_discard_path",
        )
    result = CleanupResult(path=path, removed=False, method="failed")

    if not path.exists():
        _prune(main_root, runner)
        result.removed, result.method, result.outcome = True, "absent", "removed"
        return result

    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        issue = _cleanup_safety_issue(path, main_root, runner, inspect_holders, discardable)
        if issue:
            result.removed, result.method, result.attempts = False, "preserved", attempt - 1
            result.outcome, result.reason = issue.outcome, issue.reason
            result.detail = f"{path} {issue.detail}"
            logger.warning(result.detail)
            return result
        issue = _restore_discard_paths(path, discardable, runner)
        if issue:
            result.removed, result.method, result.attempts = False, "preserved", attempt - 1
            result.outcome, result.reason = issue.outcome, issue.reason
            result.detail = f"{path} {issue.detail}"
            logger.warning(result.detail)
            return result
        issue = _cleanup_safety_issue(path, main_root, runner, inspect_holders)
        if issue:
            result.removed, result.method, result.attempts = False, "preserved", attempt - 1
            result.outcome, result.reason = issue.outcome, issue.reason
            result.detail = f"{path} {issue.detail}"
            logger.warning(result.detail)
            return result
        result.attempts = attempt
        res = runner(["worktree", "remove", str(path)], main_root)
        if not path.exists():
            _prune(main_root, runner)
            result.removed, result.method, result.outcome = True, "git", "removed"
            return result
        message = (res.stderr or res.stdout or "").strip() or "directory still present after git worktree remove"
        result.errors.append(f"attempt {attempt}: {message}")
        logger.warning("worktree remove %s (attempt %s/%s): %s", path, attempt, attempts, message)
        if attempt < attempts:
            sleeper(backoff_s[min(attempt - 1, len(backoff_s) - 1)] if backoff_s else 0.0)

    # git gave up: delete what is left ourselves.
    try:
        if path.exists():
            issue = _cleanup_safety_issue(path, main_root, runner, inspect_holders)
            if issue:
                result.removed, result.method = False, "preserved"
                result.outcome, result.reason = issue.outcome, issue.reason
                result.detail = f"{path} {issue.detail}"
                logger.warning(result.detail)
                return result
            remove_tree(path)
    except OSError as exc:
        result.errors.append(f"rmtree: {exc}")
        logger.warning("rmtree %s failed: %s", path, exc)
    if not path.exists():
        _prune(main_root, runner)
        result.removed, result.method, result.outcome = True, "rmtree", "removed"
        return result

    # Still locked: rename out of the way, recoverable for TRASH_RETENTION_DAYS.
    try:
        issue = _cleanup_safety_issue(path, main_root, runner, inspect_holders)
        if issue:
            result.removed, result.method = False, "preserved"
            result.outcome, result.reason = issue.outcome, issue.reason
            result.detail = f"{path} {issue.detail}"
            logger.warning(result.detail)
            return result
        result.trash_path = trash_directory(path, main_root, now_fn=now_fn)
    except OSError as exc:
        result.errors.append(f"trash: {exc}")
        result.detail = f"could not remove or move {path}: {exc}. {removal_hint(path, main_root)}"
        result.reason = "trash_failed"
        logger.error("Could not remove or trash %s: %s", path, result.detail)
        return result
    _prune(main_root, runner)
    result.removed, result.method, result.outcome = True, "trash", "removed"
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


# ----------------------------------------------------------------------
# Ownerless worktrees (USR-500): classify only, never delete
# ----------------------------------------------------------------------

# The boundary itself is preserved; eligibility requires strictly older than 48h.
OWNERLESS_MAX_AGE_S = 48 * 60 * 60
# Exact repo-relative paths that are factory bookkeeping, not product work.
_OWNERLESS_LEDGER = ".factory/demands/demands.json"
_TERMINAL_TICKET_STATUSES = frozenset({"completed", "cancelled", "canceled"})


class TicketStatusUnreadable(RuntimeError):
    """The demands ledger could not be read and the queue has no row to trust."""


@dataclass(frozen=True)
class OwnerlessFacts:
    """Everything :func:`classify_ownerless` needs. Gathering them is a separate step.

    ``holders is None`` means process inspection was impossible (``psutil`` missing
    or broken): the worktree is preserved. An empty tuple means no holder was found.
    ``ticket_status is None`` with ``ticket_status_known`` means the ticket is absent.
    ``head_outside_base`` is consulted only when ``detached`` is true: commits that
    sit on a named branch stay reachable after the worktree directory is gone.
    """

    path: Path
    is_main: bool = False
    locked: bool = False
    index_locked: bool = False
    git_readable: bool = True
    detached: bool = False
    head_outside_base: bool = False
    changed_paths: tuple[str, ...] = ()
    status_readable: bool = True
    ticket_status: Optional[str] = None
    ticket_status_known: bool = True
    holders: Optional[tuple[str, ...]] = ()
    age_s: Optional[float] = None
    cleanup_pending: bool = False


@dataclass(frozen=True)
class OwnerlessVerdict:
    """Eligible worktrees are candidates for a later sweep. Nothing here was deleted."""

    path: Path
    eligible: bool
    reason: str
    detail: str = ""


def _preserved(path: Path, reason: str, detail: str = "") -> OwnerlessVerdict:
    return OwnerlessVerdict(path=path, eligible=False, reason=reason, detail=detail)


def worktree_named_stamp(name: str) -> Optional[datetime]:
    """UTC stamp encoded in a worktree directory name, or ``None`` when it is absent or invalid."""
    match = _STAMP_PATTERN.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def worktree_age_s(path: Path, *, now: Optional[datetime] = None) -> Optional[float]:
    """Age in seconds from the directory-name stamp.

    The directory's own ``st_mtime`` is used only when that stamp is absent or
    not a real UTC timestamp. Child mtimes are never consulted. ``None`` when
    there is no stamp and the directory itself cannot be stat'ed.
    """
    moment = now or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    stamped = worktree_named_stamp(path.name)
    if stamped is not None:
        return (moment - stamped).total_seconds()
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return (moment - mtime).total_seconds()


def worktree_ticket_id(path: Path, branch: str = "") -> Optional[str]:
    """Ticket slug from ``ticket/<id>`` or from the ``<slug>-<stamp>`` directory name.

    A branch named ``ticket/<slug>-<stamp>`` yields ``<slug>``. Detached HEAD
    falls back to the directory name. Anything else has no ticket.
    """
    label = branch.removeprefix("refs/heads/")
    if label.lower().startswith("ticket/"):
        slug = label.split("/", 1)[1]
        match = _STAMP_PATTERN.search(slug)
        if match:
            slug = slug[: match.start()]
        slug = slug.strip("-")
        return slug or None
    match = _STAMP_PATTERN.search(path.name)
    if match and match.start() > 0:
        return path.name[: match.start()].strip("-") or None
    return None


def _ownerless_rel(path: str) -> str:
    text = str(path).replace("\\", "/").strip().strip("\r")
    while text.startswith("./"):
        text = text[2:]
    return text


def substantive_worktree_changes(paths: Iterable[str]) -> list[str]:
    """Paths that count as product work.

    Delivery scratch (``.tmp`` and everything under it) and the demands ledger
    ``.factory/demands/demands.json`` do not. Order and the caller's spelling
    of the surviving paths are preserved.
    """
    kept: list[str] = []
    for path in paths:
        rel = _ownerless_rel(path)
        if is_delivery_scratch(rel) or rel == _OWNERLESS_LEDGER:
            continue
        kept.append(path)
    return kept


def _normalize_ticket_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    return str(status).strip().casefold()


def resolve_ticket_status(
    file_status: Optional[str],
    queued_status: Optional[str],
    *,
    file_readable: bool,
) -> Optional[str]:
    """Merge a ledger row with the control-store row.

    A terminal ledger status wins. Otherwise the queue wins. ``None`` means the
    ticket is absent. Raises :class:`TicketStatusUnreadable` when the ledger
    cannot be parsed and the queue has no row (fail closed: do not call that
    "absent").
    """
    file_norm = _normalize_ticket_status(file_status)
    queued_norm = _normalize_ticket_status(queued_status)
    if file_norm in _TERMINAL_TICKET_STATUSES:
        return file_norm
    if queued_norm is not None:
        return queued_norm
    if not file_readable:
        raise TicketStatusUnreadable("demands ledger unreadable and the queue has no row")
    return file_norm


def classify_ownerless(facts: OwnerlessFacts) -> OwnerlessVerdict:
    """Preserve or mark eligible. Does not touch the disk, git, or :func:`cleanup`.

    Ownership and age are established before reporting retryable local safety
    blockers. This lets the scheduled sweep send only ownerless, old worktrees
    to :func:`cleanup`, which records blocked attempts in ``cleanup_pending``.
    The main checkout, unknown/open tickets, young worktrees, and detached
    commits outside the base are always preserved.
    """
    path = facts.path
    if facts.cleanup_pending:
        return _preserved(path, "cleanup_pending", "registered as pending cleanup")
    if facts.is_main:
        return _preserved(path, "main_checkout", "primary checkout")
    if not facts.ticket_status_known:
        return _preserved(path, "ticket_status_unknown", "ticket status unreadable")
    status = _normalize_ticket_status(facts.ticket_status)
    if status is not None and status not in _TERMINAL_TICKET_STATUSES:
        return _preserved(path, "open_ticket", status)
    if facts.age_s is None:
        return _preserved(path, "age_unknown", "no stamp and no directory mtime")
    if facts.age_s <= OWNERLESS_MAX_AGE_S:
        return _preserved(path, "too_young", f"age {facts.age_s:.0f}s <= {OWNERLESS_MAX_AGE_S}s")
    if facts.detached and facts.head_outside_base:
        return _preserved(path, "detached_outside_base", "detached HEAD is not the base commit")
    if facts.holders is None:
        return _preserved(path, "psutil_unavailable", "psutil ImportError; cannot prove the worktree is idle")
    if facts.holders:
        return _preserved(path, "holder", ", ".join(facts.holders))
    if facts.locked:
        return _preserved(path, "locked", "git worktree lock")
    if facts.index_locked:
        return _preserved(path, "index_lock", "index.lock present")
    if not facts.git_readable:
        return _preserved(path, "git_unreadable", "git dir unreadable")
    if not facts.status_readable:
        return _preserved(path, "status_unreadable", "git status failed")
    changes = substantive_worktree_changes(facts.changed_paths)
    if changes:
        return _preserved(path, "dirty", ", ".join(changes[:5]))
    return OwnerlessVerdict(path=path, eligible=True, reason="ownerless", detail="no owner for more than 48h")


def probe_worktree_holders(path: Path) -> Optional[tuple[str, ...]]:
    """``name (pid)`` of processes using ``path`` as cwd or through an open file.

    ``None`` when ``psutil`` is unavailable or process enumeration fails;
    callers must preserve the worktree because its state is unknown. Windows
    commonly denies metadata access to unrelated protected processes, so an
    ``AccessDenied`` for an individual property is skipped. Open-file paths are
    inspected for processes whose cwd is the repository root or one of its
    parents; unrelated processes are excluded to avoid a machine-wide handle
    scan. Non-forced Git and filesystem removal still refuse a blocking open
    handle. An empty tuple means no holder was found among the relevant
    accessible process data. This does not remove anything.
    """
    try:
        import psutil  # optional
    except ImportError:
        logger.debug("psutil unavailable; preserving worktree %s", path)
        return None
    repository_root: Optional[Path] = None
    try:
        resolved_path = path.resolve()
        for candidate in (resolved_path, *resolved_path.parents):
            if (candidate / ".git").is_dir():
                repository_root = candidate
                break
    except OSError as exc:
        logger.debug("Could not resolve worktree path %s while inspecting process holders: %s", path, exc)
    found: list[str] = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                cwd = proc.cwd()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                cwd = None
            except Exception:  # noqa: BLE001 -- an unexpected per-process error must not look like "no holder"
                logger.warning("Could not inspect process paths for %s; preserving it", path)
                return None
            using_path = cwd is not None and _is_within(cwd, path)
            process_cwd_is_related = (
                cwd is not None
                and repository_root is not None
                and (_is_within(path, cwd) or _is_within(repository_root, cwd))
            )
            if not using_path and process_cwd_is_related:
                try:
                    using_path = any(_is_within(open_file.path, path) for open_file in proc.open_files())
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    continue
                except psutil.AccessDenied:
                    continue
                except Exception:  # noqa: BLE001 -- unexpected errors must not look like "no holder"
                    logger.warning("Could not inspect process paths for %s; preserving it", path)
                    return None
            if using_path:
                found.append(f"{proc.info.get('name')} (pid {proc.info.get('pid')})")
    except Exception:  # noqa: BLE001 -- cannot prove the worktree is idle
        logger.warning("Could not inspect processes for %s; preserving it", path)
        return None
    return tuple(found)


def _ledger_ticket_status(ledger: Path, ticket_id: str) -> tuple[Optional[str], bool]:
    """``(status, readable)``. A missing file is readable and has no row.

    Does not create the ledger.
    """
    if not ledger.exists():
        return None, True
    try:
        payload = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, False
    rows = payload.get("demands") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None, False
    wanted = ticket_id.casefold()
    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")).casefold() == wanted:
            status = row.get("status")
            return (None if status is None else str(status)), True
    return None, True


def _default_queue_status(ticket_id: str) -> Optional[str]:
    """Control-store status, or ``None`` when that store has no row for ``ticket_id``.

    An exception propagates so the caller preserves the worktree instead of
    treating a failed lookup as an absent ticket.
    """
    from core.demands import queue_store

    ticket = queue_store.get_ticket(ticket_id)
    if ticket is None:
        return None
    return str(getattr(ticket.status, "value", ticket.status))


def _lookup_ticket_status(
    ticket_id: Optional[str],
    *,
    main_root: Path,
    statuses: Optional[Mapping[str, str]],
    ledger_path: Optional[Path],
    queue_lookup: Optional[Callable[[str], Optional[str]]],
) -> tuple[Optional[str], bool]:
    """``(status or None when absent, known)``. ``statuses`` replaces ledger and queue."""
    if not ticket_id:
        return None, True
    if statuses is not None:
        folded = {str(key).casefold(): value for key, value in statuses.items()}
        if ticket_id.casefold() not in folded:
            return None, True
        return str(folded[ticket_id.casefold()]), True
    ledger = ledger_path if ledger_path is not None else main_root / ".factory" / "demands" / "demands.json"
    file_status, readable = _ledger_ticket_status(ledger, ticket_id)
    lookup = queue_lookup or _default_queue_status
    queued: Optional[str] = None
    for candidate in dict.fromkeys((ticket_id, ticket_id.upper())):
        try:
            found = lookup(candidate)
        except Exception as exc:  # noqa: BLE001 -- unreadable queue must not look like "absent"
            logger.warning("Ticket status lookup failed for %s: %s", candidate, exc)
            return None, False
        if found is not None:
            queued = found
            break
    try:
        return resolve_ticket_status(file_status, queued, file_readable=readable), True
    except TicketStatusUnreadable:
        return None, False


def _detached_outside_base(path: Path, entry: Mapping[str, str], runner: GitRunner, base_ref: str) -> bool:
    """True when a detached HEAD is not ``base_ref`` (or the base cannot be resolved)."""
    if "detached" not in entry:
        return False
    head = (entry.get("HEAD") or "").strip()
    if not head:
        rev = runner(["rev-parse", "HEAD"], path)
        if rev.returncode != 0 or not rev.stdout.strip():
            return True
        head = rev.stdout.strip()
    base = runner(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], path)
    if base.returncode != 0 or not base.stdout.strip():
        return True
    return head.lower() != base.stdout.strip().lower()


def _index_lock_state(path: Path, runner: GitRunner) -> tuple[bool, bool]:
    """``(index_locked, git_readable)`` from ``--absolute-git-dir`` / ``index.lock``."""
    res = runner(["rev-parse", "--absolute-git-dir"], path)
    if res.returncode != 0 or not res.stdout.strip():
        return False, False
    try:
        return (Path(res.stdout.strip()) / "index.lock").exists(), True
    except OSError:
        return False, False


def classify_ownerless_worktree(
    path: Path,
    *,
    cwd: Optional[Path] = None,
    main_root: Optional[Path] = None,
    entry: Optional[Mapping[str, str]] = None,
    is_main: Optional[bool] = None,
    now: Optional[datetime] = None,
    base: str = "main",
    remote: str = "origin",
    run_git: Optional[GitRunner] = None,
    statuses: Optional[Mapping[str, str]] = None,
    ledger_path: Optional[Path] = None,
    queue_lookup: Optional[Callable[[str], Optional[str]]] = None,
    holder_probe: Optional[Callable[[Path], Optional[tuple[str, ...]]]] = None,
    pending_paths: Optional[Iterable[str]] = None,
) -> OwnerlessVerdict:
    """Read one worktree and classify it. Does not call :func:`cleanup` or change git state.

    ``statuses``, when given, is the only ticket table consulted (a missing id is
    an absent ticket). Otherwise the main checkout ledger is read and not created,
    then the control-store queue. ``holder_probe`` defaults to
    :func:`probe_worktree_holders`.
    """
    moment = now or utc_now()
    runner = run_git or _git
    root = main_root
    if root is None:
        try:
            root = main_checkout_root(cwd or path, runner)
        except WorkspaceError as exc:
            return _preserved(path, "git_unreadable", str(exc))
    if pending_paths is None:
        try:
            pending_paths = cleanup_pending_worktree_paths(root)
        except Exception as exc:  # noqa: BLE001 -- unknown pending inventory must not authorize removal
            return _preserved(path, "cleanup_pending_inventory_error", str(exc))
    pending = {_norm(candidate) for candidate in pending_paths}
    cleanup_pending = _norm(path) in pending
    age = worktree_age_s(path, now=moment)
    if is_main is None:
        entries = list_worktrees(root, runner)
        if not entries:
            return _preserved(path, "git_unreadable", "git worktree list failed")
        is_main = _norm(path) == _norm(entries[0]["worktree"])
        if entry is None:
            target = _norm(path)
            entry = next((item for item in entries if _norm(item.get("worktree", "")) == target), None)
    if is_main:
        return classify_ownerless(
            OwnerlessFacts(path=path, is_main=True, cleanup_pending=cleanup_pending, age_s=age)
        )
    if entry is None:
        return _preserved(path, "unregistered", "path is not a registered worktree")
    locked = "locked" in entry
    index_locked, git_readable = _index_lock_state(path, runner)
    changed: tuple[str, ...] = ()
    status_readable = True
    if git_readable and not index_locked:
        status = runner(["status", "--porcelain=v1", "-z", "--untracked-files=all"], path)
        if status.returncode != 0:
            status_readable = False
        else:
            changed = tuple(_status_paths(status.stdout or ""))
    detached = "detached" in entry
    outside = _detached_outside_base(path, entry, runner, f"{remote}/{base}") if detached else False
    probe = holder_probe or probe_worktree_holders
    try:
        holders = probe(path)
    except Exception as exc:  # noqa: BLE001 -- a broken probe must not look like "no process"
        logger.warning("Holder probe failed for %s: %s", path, exc)
        holders = None
    ticket_status, ticket_known = _lookup_ticket_status(
        worktree_ticket_id(path, entry.get("branch", "")),
        main_root=root,
        statuses=statuses,
        ledger_path=ledger_path,
        queue_lookup=queue_lookup,
    )
    return classify_ownerless(
        OwnerlessFacts(
            path=path,
            is_main=False,
            cleanup_pending=cleanup_pending,
            locked=locked,
            index_locked=index_locked,
            git_readable=git_readable,
            detached=detached,
            head_outside_base=outside,
            changed_paths=changed,
            status_readable=status_readable,
            ticket_status=ticket_status,
            ticket_status_known=ticket_known,
            holders=holders,
            age_s=age,
        )
    )


def classify_ownerless_worktrees(
    cwd: Path,
    *,
    now: Optional[datetime] = None,
    base: str = "main",
    remote: str = "origin",
    run_git: Optional[GitRunner] = None,
    statuses: Optional[Mapping[str, str]] = None,
    ledger_path: Optional[Path] = None,
    queue_lookup: Optional[Callable[[str], Optional[str]]] = None,
    holder_probe: Optional[Callable[[Path], Optional[tuple[str, ...]]]] = None,
) -> list[OwnerlessVerdict]:
    """Classify every registered worktree, including the primary checkout.

    The primary checkout is always preserved. Nothing is removed.
    """
    runner = run_git or _git
    try:
        root = main_checkout_root(cwd, runner)
    except WorkspaceError:
        return [_preserved(cwd, "git_unreadable", "not inside a git repository")]
    entries = list_worktrees(root, runner)
    if not entries:
        return [_preserved(cwd, "git_unreadable", "git worktree list failed")]
    try:
        pending_paths = cleanup_pending_worktree_paths(root)
    except Exception as exc:  # noqa: BLE001 -- preserve all candidates if the inventory is uncertain
        return [
            _preserved(Path(item["worktree"]), "cleanup_pending_inventory_error", str(exc))
            for item in entries
        ]
    verdicts: list[OwnerlessVerdict] = []
    for index, item in enumerate(entries):
        verdicts.append(
            classify_ownerless_worktree(
                Path(item["worktree"]),
                main_root=root,
                entry=item,
                is_main=index == 0,
                now=now,
                base=base,
                remote=remote,
                run_git=runner,
                statuses=statuses,
                ledger_path=ledger_path,
                queue_lookup=queue_lookup,
                holder_probe=holder_probe,
                pending_paths=pending_paths,
            )
        )
    return verdicts

