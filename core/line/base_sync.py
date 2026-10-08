"""Bring the run branch up to date with the base branch without losing the developer's edits (USR-165).

When the `development` stage waits on a red base (USR-153) the agent's edits stay UNCOMMITTED in the run
worktree. If somebody fixes the base meanwhile, validating those edits on top of the old base keeps
failing and only `integration` (a rebase) could unblock the run. `sync_with_base` runs when the wait ends:

1. `git fetch origin <default>`;
2. the uncommitted work (tracked, deleted and untracked files) is parked in a throwaway WIP commit;
3. `origin/<default>` is MERGED into the run branch (no history rewrite, so a plain push stays valid;
   the pushed diagnostics commits keep their SHAs);
4. the WIP commit is replayed with `cherry-pick --no-commit`, so the edits are back in the worktree
   (uncommitted) on top of the new base.

Conflicts are classified, never left half-done:

- the developer's edits clash with the new base -> `edit_conflict`: the worktree keeps the merged base and
  the conflict markers inside the developer's files (index cleaned), so the developer resolves them;
- the run branch's own commits clash with the base -> `branch_conflict`: the merge is aborted, the edits
  are restored exactly as they were, and the caller ends the stage with a structured cause.

Every git failure is "cannot tell" (`unavailable`) with the worktree restored, never an exception.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Literal, Optional

from core.line import workspace
from core.line.workspace import RunWorkspace, WorkspaceError
from core.projects.models import ProjectDescriptor
from core.projects.registry import normalize_repo_url

logger = logging.getLogger(__name__)

SyncStatus = Literal["up_to_date", "synced", "edit_conflict", "branch_conflict", "unavailable"]

BASE_SYNC_CONFLICT_CAUSE = "base_sync_conflict"
"""`cause_code` of the stage when the run branch itself cannot absorb the base; refs are `base_sync_conflict:<path>`."""

_WIP_MESSAGE = "wip: park uncommitted edits while syncing with the base (USR-165)"


@dataclass(frozen=True)
class SyncResult:
    status: SyncStatus
    base_ref: str
    conflicts: tuple[str, ...] = ()
    detail: str = ""


def conflict_refs(conflicts: tuple[str, ...] | list[str], *, limit: int = 20) -> list[str]:
    """`base_sync_conflict:<path>` evidence refs (bounded, like the other structured causes)."""
    refs = [f"{BASE_SYNC_CONFLICT_CAUSE}:{path}" for path in list(conflicts)[:limit]]
    if len(conflicts) > limit:
        refs.append(f"{BASE_SYNC_CONFLICT_CAUSE}:+{len(conflicts) - limit} more")
    return refs


def _git(ws: RunWorkspace, args: list[str], *, repo_url: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    return workspace._run_git(args, cwd=ws.path, repo_url=repo_url, check=False)


def _unmerged_paths(ws: RunWorkspace) -> tuple[str, ...]:
    proc = _git(ws, ["diff", "--name-only", "--diff-filter=U"])
    return tuple(line.strip() for line in (proc.stdout or "").splitlines() if line.strip())


def _replay_edits(ws: RunWorkspace, wip_sha: Optional[str]) -> tuple[bool, tuple[str, ...]]:
    """Re-apply the parked edits as uncommitted changes; `(clean, conflicting paths)`."""
    if wip_sha is None:
        return True, ()
    proc = _git(ws, ["cherry-pick", "--no-commit", wip_sha])
    conflicts = _unmerged_paths(ws) if proc.returncode != 0 else ()
    _git(ws, ["cherry-pick", "--quit"])  # drop any CHERRY_PICK_HEAD / sequencer state
    _git(ws, ["reset", "--quiet"])  # unstage: the edits are uncommitted again, markers stay in the files
    return proc.returncode == 0, conflicts


def sync_with_base(ws: RunWorkspace, project: ProjectDescriptor) -> SyncResult:
    """Merge `origin/<default_branch>` into the run branch keeping the uncommitted edits. Never raises."""
    base_ref = f"origin/{project.default_branch or 'main'}"
    parked: dict[str, str] = {}  # filled by `_sync` once the edits are parked: {"head": ..., "wip": ...}
    try:
        return _sync(ws, project, base_ref, parked)
    except WorkspaceError as exc:
        logger.warning("base sync for run %s failed: %s", ws.run_id, type(exc).__name__)
        if parked:  # best-effort rollback to the state we found, with the edits back in the worktree
            try:
                _git(ws, ["merge", "--abort"])
                _git(ws, ["reset", "--hard", parked["head"]])
                _replay_edits(ws, parked.get("wip"))
            except WorkspaceError:
                logger.warning("base sync rollback for run %s failed", ws.run_id)
        return SyncResult("unavailable", base_ref, detail=type(exc).__name__)


def _sync(ws: RunWorkspace, project: ProjectDescriptor, base_ref: str, parked: dict[str, str]) -> SyncResult:
    default_branch = project.default_branch or "main"
    repo_url = normalize_repo_url(project.repo_url) if project.repo_url else None

    fetch = _git(ws, ["fetch", "origin", default_branch], repo_url=repo_url)
    if fetch.returncode != 0:
        logger.warning("base sync for run %s: fetch of %s failed; using the last known ref", ws.run_id, base_ref)
    if not workspace._ref_exists(ws.path, base_ref):
        return SyncResult("unavailable", base_ref, detail="base ref not found")

    if _git(ws, ["merge-base", "--is-ancestor", base_ref, "HEAD"]).returncode == 0:
        return SyncResult("up_to_date", base_ref)

    identity = workspace._identity_args(ws.path)
    head_sha = workspace._rev_parse(ws.path, "HEAD")
    parked["head"] = head_sha

    wip_sha: Optional[str] = None
    dirty = _git(ws, ["status", "--porcelain", "--untracked-files=all"])
    if dirty.returncode != 0:
        return SyncResult("unavailable", base_ref, detail="git status failed")
    if (dirty.stdout or "").strip():
        add = _git(ws, ["add", "-A"])
        commit = _git(ws, [*identity, "commit", "--no-verify", "--quiet", "-m", _WIP_MESSAGE])
        if add.returncode != 0 or commit.returncode != 0:
            _git(ws, ["reset", "--quiet"])
            return SyncResult("unavailable", base_ref, detail="could not park the uncommitted edits")
        wip_sha = workspace._rev_parse(ws.path, "HEAD")
        parked["wip"] = wip_sha
        _git(ws, ["reset", "--hard", head_sha])  # the edits now live only in `wip_sha`

    merge = _git(ws, [*identity, "merge", "--no-edit", base_ref])
    if merge.returncode != 0:
        conflicts = _unmerged_paths(ws)
        _git(ws, ["merge", "--abort"])
        _git(ws, ["reset", "--hard", head_sha])
        _replay_edits(ws, wip_sha)
        detail = (merge.stderr or merge.stdout or "").strip()[:500]
        if not conflicts:
            return SyncResult("unavailable", base_ref, detail=detail or "merge failed")
        return SyncResult("branch_conflict", base_ref, conflicts, detail)

    clean, conflicts = _replay_edits(ws, wip_sha)
    if not clean:
        return SyncResult("edit_conflict", base_ref, conflicts or (".",))
    return SyncResult("synced", base_ref)
