#!/usr/bin/env python3
"""DarkFac Git Autonomy Engine (USR-57).

Empowers the Dark Factory with 100% autonomous Git operations:
- Atomic commits bound to demands/tickets with standardized metadata.
- Multi-environment synchronization between notebook, desktop worker, and cloud VPS.
- Automated status progression in the demand backlog upon green gate verification.
- Zero Human Touch post-Grill for internal factory development.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Literal, Optional, Sequence

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.demands.models import DemandOrigin, TAG_USER_DEMAND, UserTicket
from core.demands.id_allocator import title_collisions
from core.demands.merge import merge_demands_3way
from core.demands.store import DemandsStore
from core.git import main_ci_alert, queue_registration, secret_scan, ticket_workspace
from core.git.safe_show import safe_show
from core.git.state_guard import DEFAULT_PROTECTED_STATE_PATHS, check_staged_state_files
from core.line import cancellation
from core.git.ci_checks import (
    DEFAULT_MAIN_WORKFLOW,
    CiVerdict,
    MainStatus,
    check_main,
    ensure_green,
    is_ledger_only,
    pending_check_labels,
    sanitize,
)
from core.owner_actions.merge import merge_owner_actions_3way
from core.owner_actions.dedupe import OWNER_ACTIONS_RELATIVE, renumber_duplicate_owner_actions
from core.owner_actions.store import OwnerActionStore
from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType

logger = logging.getLogger("darkfac.git.autonomy")

_POLICY_BLOCK_MARKER = "base branch policy prohibits"

# Re-syncs (and CI re-runs) attempted when `gh pr merge` reports the PR as not mergeable (USR-238).
_MERGE_RESYNC_ATTEMPTS = 3
# Confirming the merge sha on origin/base: fetch can fail or lag right after GitHub reports MERGED.
_MERGE_CONFIRM_ATTEMPTS = 4
_MERGE_CONFIRM_DELAY_S = 5.0


def _is_unmergeable(text: str) -> bool:
    """True when a lower-cased `gh pr merge` output means main moved under the PR."""
    return "not mergeable" in text or "conflict" in text or "out of date" in text

_LEDGER_RELATIVE = ".factory/demands/demands.json"

PhaseCallback = Callable[[str, str, str, Optional[str]], None]


class GitSyncResult(BaseModel):
    """Normalized outcome of an environment sync operation."""

    ok: bool
    action: str = Field(description="up_to_date, merged_fast_forward, pushed, or error")
    local_sha: Optional[str] = None
    remote_sha: Optional[str] = None
    message: str = ""


class DeliveryReport(BaseModel):
    """Outcome of delivering a ticket branch (push, PR, merge, cleanup)."""

    ok: bool
    action: str = Field(
        description=(
            "merged, conflict, awaiting_commercial_acceptance, no_changes, staged_inspect_failed "
            "(the index could not be read for the secret scan; resumable), ci_pending "
            "(checks still pending after the wait), ci_failed (a check is red), base_red "
            "(every red check is already red on the base branch) or error; ci_pending, "
            "ci_failed and base_red never merge and leave the PR and branch open"
        )
    )
    pr_url: Optional[str] = None
    merge_sha: Optional[str] = None
    cleaned: bool = False
    branch: Optional[str] = None
    conflict_files: list[str] = Field(default_factory=list)
    message: str = ""


class SweepReport(BaseModel):
    """Outcome of sweeping stale (already merged) branches and worktrees (USR-69)."""

    removed_branches: list[str] = Field(default_factory=list)
    removed_worktrees: list[str] = Field(default_factory=list)
    trashed_worktrees: list[str] = Field(
        default_factory=list, description="merged worktrees that could not be deleted and were moved to the trash"
    )
    orphans_trashed: list[str] = Field(
        default_factory=list, description="unregistered directories without .git moved to the recoverable trash"
    )
    trash_purged: list[str] = Field(
        default_factory=list, description="trash entries older than the retention that were deleted"
    )
    skipped: list[str] = Field(default_factory=list)


# Paths per `git add` call when staging a ticket's changed files (command-line length on Windows).
_ADD_CHUNK = 100
# USR-264: ``git show :path`` on the index can fail transiently under git contention right after
# the gate. Retry a few times with a short pause before giving up on the delivery.
_STAGED_SHOW_ATTEMPTS = 3
_STAGED_SHOW_PAUSE_S = 1.0
STAGED_INSPECT_FAILED = "staged_inspect_failed"

GhRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


# Subcommands that publish work: refused once the run is cancelled (USR-166). Read-only calls stay allowed.
_PUBLISHING_GIT = frozenset({"commit", "push"})
_PUBLISHING_GH = frozenset({"create", "merge"})


def _refuse_if_cancelled(args: Sequence[str], publishing: frozenset[str]) -> None:
    """Raise ``RunCancelledError`` when ``args`` would publish and the active run was cancelled.

    No-op outside a cancellation scope (cloud worker run scope, or the launcher's ``local_cancel.watch``).
    The check re-verifies the scope's probe, so a request written a moment ago is honoured at once.
    """
    words = [a for a in args if not str(a).startswith("-")]
    if publishing.intersection(words[:2]):
        cancellation.ensure_not_cancelled()


def _run_gh(args: Sequence[str], cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess[str]:
    """Execute the GitHub CLI; never raises (missing gh yields returncode 127)."""
    _refuse_if_cancelled(args, _PUBLISHING_GH)
    cmd = ["gh", *args]
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120.0,
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(cmd, 124, "", f"gh timed out: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", f"OS error invoking gh: {exc}")


class TicketCompletionReport(BaseModel):
    """Normalized outcome of completing a ticket through autonomous Git lifecycle."""

    ok: bool
    ticket_id: str
    commit_sha: Optional[str] = None
    sync_result: Optional[GitSyncResult] = None
    delivery: Optional[DeliveryReport] = None
    message: str = ""


def _win_kwargs() -> dict[str, Any]:
    """Provide CREATE_NO_WINDOW flag on Windows to prevent console flashing."""
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def _run_git(
    args: Sequence[str],
    cwd: Path = PROJECT_ROOT,
    timeout_s: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a git command with UTF-8 encoding and sanitized error handling."""
    _refuse_if_cancelled(args, _PUBLISHING_GIT)
    cmd = ["git", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=check,
            **_win_kwargs(),
        )
        return proc
    except subprocess.TimeoutExpired as exc:
        logger.error("Git command timed out after %ss: %s", timeout_s, " ".join(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"Git command timed out after {timeout_s}s: {exc}",
        )
    except OSError as exc:
        logger.error("Failed to invoke git: %s", exc)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=127,
            stdout="",
            stderr=f"OS error invoking git: {exc}",
        )


class StagedInspectError(RuntimeError):
    """The index blob of a staged path could not be read even after retries (USR-264)."""

    def __init__(self, path: str, returncode: int, stderr: str, attempts: int) -> None:
        self.path = path
        self.returncode = returncode
        self.stderr = stderr
        self.attempts = attempts
        super().__init__(
            f"{STAGED_INSPECT_FAILED}: Cannot inspect staged content: {path} "
            f"(git show :{path} exit {returncode} after {attempts} attempt(s)): {stderr or '<no stderr>'}"
        )


_REGISTRY_LEDGER_FILES = frozenset({".factory/config/model_registry.json", ".factory/models/lifecycle_ledger.jsonl"})


def registry_allowlist() -> frozenset[str]:
    """Paths a ``registry`` delivery may change: the registry, the model ledger and the docgen targets."""
    from core.models.docgen import TARGETS

    return _REGISTRY_LEDGER_FILES | frozenset(TARGETS)


def registry_disallowed(changed: "Sequence[str] | set[str]") -> list[str]:
    allowed = registry_allowlist()
    return sorted(p for p in (c.replace("\\", "/").strip() for c in changed) if p and p not in allowed)


def _registry_title(title: str) -> str:
    text = title.strip().replace(chr(10), " ")
    return text if text.startswith("chore(models):") else f"chore(models): {text}"


def _delivery_pr_title(kind: str, ticket_id: str, title: str) -> str:
    if kind == "queue":
        return f"chore(backlog): registrar {ticket_id} - {title.strip()}"
    if kind == "registry":
        return _registry_title(title)
    return f"feat(core): {title.strip()} [{ticket_id}]"


class GitAutonomyManager:
    """Manages autonomous Git operations for the Dark Factory."""

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        *,
        ci_timeout_s: Optional[float] = None,
        ci_poll_s: Optional[float] = None,
        ci_sleep_fn: Optional[Callable[[float], None]] = None,
        ci_clock_fn: Optional[Callable[[], float]] = None,
        cleanup_sleep_fn: Optional[Callable[[float], None]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        sweep_grace_s: float = ticket_workspace.ORPHAN_MIN_AGE_S,
        trash_retention_days: float = ticket_workspace.TRASH_RETENTION_DAYS,
        protected_state_paths: Sequence[str] = DEFAULT_PROTECTED_STATE_PATHS,
        on_phase: Optional[PhaseCallback] = None,
        staged_show_sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.root = root
        # Pause between retries of a failed index read (USR-264); injectable so tests never sleep.
        self.staged_show_sleep_fn = staged_show_sleep_fn or time.sleep
        # Optional best-effort progress hook (USR-140): called as (phase, status, message, cause) on
        # commit/pr/ci/merge transitions. It can never affect delivery: errors are swallowed.
        self.on_phase = on_phase
        self._open_phase: Optional[str] = None
        # CI gate tuning (USR-85); None keeps the ci_checks defaults
        # (DARKFAC_CI_WAIT_SECONDS, 20 s poll, real clock). Injectable for tests.
        self.ci_timeout_s = ci_timeout_s
        self.ci_poll_s = ci_poll_s
        self.ci_sleep_fn = ci_sleep_fn
        self.ci_clock_fn = ci_clock_fn
        # Worktree cleanup / trash tuning (USR-69); injectable so tests never really sleep
        # or wait for a directory to age.
        self.cleanup_sleep_fn = cleanup_sleep_fn
        self.now_fn = now_fn
        # A directory or commit-less worktree touched within this window is never swept: it
        # may be a worktree that is being created right now.
        self.sweep_grace_s = sweep_grace_s
        self.trash_retention_days = trash_retention_days
        self.protected_state_paths = tuple(protected_state_paths)

    def _phase(self, phase: str, status: str, message: str = "", cause: Optional[str] = None) -> None:
        """Report a delivery phase transition to ``on_phase``; never raises."""
        if status == "running":
            self._open_phase = phase
        elif self._open_phase == phase:
            self._open_phase = None
        if self.on_phase is None:
            return
        try:
            self.on_phase(phase, status, message, cause)
        except Exception as exc:  # noqa: BLE001 - progress reporting must not alter delivery
            logger.warning("on_phase hook failed for %s/%s: %s", phase, status, type(exc).__name__)

    def _child_manager(self, root: Path) -> "GitAutonomyManager":
        """A manager for a disposable worktree (e.g. the queue-registration alert's own commit).

        Shares this manager's injectable clock/sleep/timeout settings (USR-246): without this, a
        manager built with ``GitAutonomyManager(root)`` falls back to real ``time.sleep`` for its
        CI gate even when the delivery that triggered it is running under a test's fake clock,
        turning a deterministic test into one that really waits out the CI poll/backoff loop.
        """
        return GitAutonomyManager(
            root,
            ci_timeout_s=self.ci_timeout_s,
            ci_poll_s=self.ci_poll_s,
            ci_sleep_fn=self.ci_sleep_fn,
            ci_clock_fn=self.ci_clock_fn,
            cleanup_sleep_fn=self.cleanup_sleep_fn,
            now_fn=self.now_fn,
            staged_show_sleep_fn=self.staged_show_sleep_fn,
        )

    def _close_open_phase(self, report: DeliveryReport) -> None:
        """A delivery that returned early leaves its phase open: close it with the outcome."""
        phase = self._open_phase
        if phase is None:
            return
        if report.ok:
            self._phase(phase, "skipped", report.message)
        else:
            self._phase(phase, "failed", report.message, report.action)

    def _check_staged_state_files(self, cwd: Path) -> None:
        check_staged_state_files(
            cwd,
            lambda args, directory: _run_git(args, cwd=directory),
            self.protected_state_paths,
        )

    def _check_staged_secrets(self, cwd: Path) -> None:
        """Inspect index blobs, including content staged before this commit call."""
        names = _run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=cwd)
        if names.returncode != 0:
            raise RuntimeError("Cannot inspect staged paths for secrets")
        paths = [path for path in names.stdout.split("\0") if path]
        findings: list[secret_scan.Finding] = []
        for path in paths:
            blob = self._show_staged_blob(path, cwd)
            if Path(path).suffix.lower() in secret_scan.BINARY_EXTENSIONS:
                continue
            if len(blob.stdout.encode("utf-8")) > secret_scan.MAX_FILE_BYTES or "\0" in blob.stdout[:8192]:
                continue
            findings.extend(secret_scan.scan_text(path, blob.stdout))
        # Temporary exceptions are for the repository gate only. A new commit
        # must never re-introduce the already exposed credential.
        permitted = tuple(entry for entry in secret_scan.ALLOWLIST if not entry.ticket)
        violations = secret_scan.apply_allowlist(findings, permitted)
        if violations:
            details = "\n".join(f"  - {finding.render()}" for finding in violations)
            raise RuntimeError(f"Staged content contains potential secrets:\n{details}")

    def _show_staged_blob(self, path: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        """Read ``:path`` from the index, retrying transient failures (USR-264)."""
        blob: Optional[subprocess.CompletedProcess[str]] = None
        for attempt in range(1, _STAGED_SHOW_ATTEMPTS + 1):
            blob = safe_show("", path, cwd=cwd, runner=lambda args, directory: _run_git(args, cwd=directory))
            if blob.returncode == 0:
                if attempt > 1:
                    logger.info("git show :%s succeeded on attempt %d", path, attempt)
                return blob
            logger.warning(
                "git show :%s failed (attempt %d/%d, exit %s): %s",
                path, attempt, _STAGED_SHOW_ATTEMPTS, blob.returncode, (blob.stderr or "").strip(),
            )
            if attempt < _STAGED_SHOW_ATTEMPTS:
                self.staged_show_sleep_fn(_STAGED_SHOW_PAUSE_S)
        assert blob is not None
        raise StagedInspectError(path, blob.returncode, (blob.stderr or "").strip(), _STAGED_SHOW_ATTEMPTS)

    def _check_ignored_staged_additions(self, cwd: Path) -> None:
        added = _run_git(["diff", "--cached", "--name-only", "--diff-filter=A", "-z"], cwd=cwd)
        if added.returncode != 0:
            raise RuntimeError("Cannot inspect staged additions")
        for path in filter(None, added.stdout.split("\0")):
            ignored = _run_git(["check-ignore", "--no-index", "--quiet", "--", path], cwd=cwd)
            if ignored.returncode == 0:
                raise RuntimeError(f"Refusing staged ignored path: {path}")
            if ignored.returncode != 1:
                raise RuntimeError(f"Cannot check ignore rules for staged path: {path}")

    def get_current_sha(self, cwd: Optional[Path] = None) -> str:
        """Return the current HEAD commit SHA."""
        target_dir = cwd or self.root
        res = _run_git(["rev-parse", "HEAD"], cwd=target_dir)
        return res.stdout.strip()

    def is_dirty(self, cwd: Optional[Path] = None) -> bool:
        """Check if working tree has any unstaged, staged, or untracked changes."""
        target_dir = cwd or self.root
        res = _run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=target_dir)
        return bool(res.stdout.strip())

    def changed_paths(self, cwd: Optional[Path] = None) -> list[str]:
        """Repository-relative paths changed in ``cwd`` (staged, unstaged, untracked, deleted).

        Read from ``git status --porcelain=v1 -z`` run INSIDE ``cwd``: in a ticket
        worktree that is exactly the ticket's own change set (USR-69).
        """
        target_dir = cwd or self.root
        res = _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=target_dir)
        if res.returncode != 0:
            logger.warning("git status failed in %s: %s", target_dir, res.stderr.strip())
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
            paths.append(path)
            if "R" in status or "C" in status:  # `XY <to>\0<from>\0`: stage both sides of a rename
                if index < len(tokens) and tokens[index]:
                    paths.append(tokens[index])
                index += 1
        return list(dict.fromkeys(paths))

    def commit_ticket(
        self,
        ticket_id: str,
        title: str,
        scope: str = "core",
        paths: Optional[Sequence[str | Path]] = None,
        cwd: Optional[Path] = None,
        custom_message: Optional[str] = None,
        kind: Literal["queue", "implementation", "registry"] = "implementation",
    ) -> Optional[str]:
        """Stage changes and produce a deterministic atomic commit bound to the ticket.

        Without ``paths`` only the paths changed IN ``cwd`` are staged (see
        :meth:`changed_paths`); a bare ``git add -A`` is never issued (USR-69).

        Returns the new commit SHA, or the current HEAD if working tree is clean.
        """
        target_dir = cwd or self.root

        # 1. Stage changes
        self._check_ignored_staged_additions(target_dir)
        if paths:
            stage_args = ["add", "--"] + [str(p) for p in paths]
            add = _run_git(stage_args, cwd=target_dir)
            if add.returncode != 0:
                raise RuntimeError(f"Git add failed: {add.stderr.strip()}")
        else:
            changed = self.changed_paths(target_dir)
            for start in range(0, len(changed), _ADD_CHUNK):
                chunk = changed[start : start + _ADD_CHUNK]
                add = _run_git(["--literal-pathspecs", "add", "-A", "--", *chunk], cwd=target_dir)
                if add.returncode != 0:
                    raise RuntimeError(f"Git add failed for {chunk[:3]}: {add.stderr.strip()}")

        # 2. Check if anything is staged
        staged_check = _run_git(["diff", "--cached", "--quiet"], cwd=target_dir)
        if staged_check.returncode == 0:
            logger.info("No staged changes to commit for ticket %s.", ticket_id)
            return self.get_current_sha(cwd=target_dir)
        if staged_check.returncode != 1:
            raise RuntimeError(f"Cannot inspect staged changes: {staged_check.stderr.strip()}")

        self._check_staged_state_files(target_dir)
        self._check_ignored_staged_additions(target_dir)
        self._check_staged_secrets(target_dir)

        # 3. Format message
        clean_title = title.strip().replace("\n", " ")
        if custom_message:
            commit_msg = custom_message
        else:
            if kind == "registry":
                subject = _registry_title(clean_title)
            elif kind == "queue":
                subject = f"chore(backlog): registrar {ticket_id} - {clean_title}"
            else:
                subject = f"feat({scope}): {clean_title} [{ticket_id}]"
            commit_msg = (
                f"{subject}\n\n"
                f"DarkFac-Ticket: {ticket_id}\n"
                f"DarkFac-Autonomy: zero-human-touch\n"
            )

        # 4. Commit
        res = _run_git(["commit", "-m", commit_msg], cwd=target_dir)
        if res.returncode != 0:
            logger.error("Git commit failed: %s", res.stderr)
            raise RuntimeError(f"Git commit failed: {res.stderr.strip()}")

        new_sha = self.get_current_sha(cwd=target_dir)
        logger.info("Committed ticket %s at %s", ticket_id, new_sha[:8])
        return new_sha

    def sync_environment(
        self,
        remote: str = "origin",
        branch: str = "main",
        cwd: Optional[Path] = None,
        allow_push: bool = True,
    ) -> GitSyncResult:
        """Synchronize the local branch with remote repository.

        Handles fetch, fast-forward when remote is ahead, and push when local is ahead.
        """
        target_dir = cwd or self.root

        # 1. Fetch remote tracking
        fetch_res = _run_git(["fetch", remote, branch], cwd=target_dir)
        if fetch_res.returncode != 0:
            logger.warning("Git fetch %s %s returned code %s: %s", remote, branch, fetch_res.returncode, fetch_res.stderr)
            # Fetch may fail if remote is not configured or network down
            return GitSyncResult(
                ok=False,
                action="error",
                message=f"Git fetch failed: {fetch_res.stderr.strip()}",
            )

        local_sha = self.get_current_sha(cwd=target_dir)
        remote_ref = f"{remote}/{branch}"
        remote_sha_res = _run_git(["rev-parse", remote_ref], cwd=target_dir)
        if remote_sha_res.returncode != 0:
            return GitSyncResult(
                ok=False,
                action="error",
                local_sha=local_sha,
                message=f"Cannot resolve remote ref {remote_ref}: {remote_sha_res.stderr.strip()}",
            )
        remote_sha = remote_sha_res.stdout.strip()

        # 2. Check if already identical
        if local_sha == remote_sha:
            return GitSyncResult(
                ok=True,
                action="up_to_date",
                local_sha=local_sha,
                remote_sha=remote_sha,
                message="Local branch is perfectly aligned with remote.",
            )

        # 3. Check if remote is ahead of local (fast-forward candidate)
        ancestor_check = _run_git(["merge-base", "--is-ancestor", local_sha, remote_sha], cwd=target_dir)
        if ancestor_check.returncode == 0:
            merge_res = _run_git(["merge", "--ff-only", remote_ref], cwd=target_dir)
            if merge_res.returncode == 0:
                new_local = self.get_current_sha(cwd=target_dir)
                return GitSyncResult(
                    ok=True,
                    action="merged_fast_forward",
                    local_sha=new_local,
                    remote_sha=remote_sha,
                    message="Fast-forwarded local branch to remote tip.",
                )
            return GitSyncResult(
                ok=False,
                action="error",
                local_sha=local_sha,
                remote_sha=remote_sha,
                message=f"Fast-forward merge failed: {merge_res.stderr.strip()}",
            )

        # 4. Check if local is ahead of remote (push candidate)
        local_ahead_check = _run_git(["merge-base", "--is-ancestor", remote_sha, local_sha], cwd=target_dir)
        if local_ahead_check.returncode == 0:
            if not allow_push:
                return GitSyncResult(
                    ok=True,
                    action="local_ahead_push_skipped",
                    local_sha=local_sha,
                    remote_sha=remote_sha,
                    message="Local is ahead of remote; push skipped as requested.",
                )

            push_res = _run_git(["push", remote, branch], cwd=target_dir)
            if push_res.returncode == 0:
                return GitSyncResult(
                    ok=True,
                    action="pushed",
                    local_sha=local_sha,
                    remote_sha=local_sha,
                    message="Successfully pushed local commits to remote.",
                )
            return GitSyncResult(
                ok=False,
                action="error",
                local_sha=local_sha,
                remote_sha=remote_sha,
                message=f"Git push failed: {push_res.stderr.strip()}",
            )

        # 5. Diverged branches
        return GitSyncResult(
            ok=False,
            action="diverged",
            local_sha=local_sha,
            remote_sha=remote_sha,
            message="Local and remote branches have diverged; automatic non-linear reconciliation required.",
        )

    # ------------------------------------------------------------------
    # Branch delivery (commit -> push -> PR -> merge -> cleanup)
    # ------------------------------------------------------------------

    def _git_out(self, args: Sequence[str], cwd: Path) -> str:
        res = _run_git(args, cwd=cwd)
        return res.stdout.strip() if res.returncode == 0 else ""

    def _current_branch(self, cwd: Path) -> str:
        return self._git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd)

    def _conflict_files(self, cwd: Path) -> list[str]:
        out = self._git_out(["diff", "--name-only", "--diff-filter=U"], cwd)
        return [line for line in out.splitlines() if line.strip()]

    def _main_root(self, cwd: Path) -> tuple[Path, bool]:
        """Return (main checkout root, cwd_is_secondary_worktree)."""
        common = self._git_out(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd)
        gitdir = self._git_out(["rev-parse", "--path-format=absolute", "--git-dir"], cwd)
        if not common or not gitdir:
            return cwd, False
        common_p, gitdir_p = Path(common).resolve(), Path(gitdir).resolve()
        if common_p == gitdir_p:
            top = self._git_out(["rev-parse", "--show-toplevel"], cwd)
            return (Path(top) if top else cwd), False
        return common_p.parent, True

    def gh_ready(self, cwd: Optional[Path] = None, gh_runner: Optional[GhRunner] = None) -> bool:
        """True when origin is a GitHub remote and gh is installed and authenticated."""
        target = cwd or self.root
        runner = gh_runner or _run_gh
        url = self._git_out(["config", "--get", "remote.origin.url"], target)
        if "github.com" not in url:
            return False
        return runner(["auth", "status"], target).returncode == 0

    def _resolve_known_conflicts(self, cwd: Path, remote_ref: str, files: Sequence[str]) -> tuple[bool, str]:
        """Resolve conflicts limited to the demands ledger and/or owner_actions.json (USR-312)."""
        known = {_LEDGER_RELATIVE, OWNER_ACTIONS_RELATIVE}
        if not files or not set(files) <= known:
            return False, "conflict outside the auto-resolvable files"
        if _LEDGER_RELATIVE in files:
            resolved, msg = self._resolve_ledger_conflict(cwd, remote_ref, commit=OWNER_ACTIONS_RELATIVE not in files)
            if not resolved:
                return False, msg
        if OWNER_ACTIONS_RELATIVE in files:
            resolved, msg = self._resolve_owner_actions_conflict(cwd, remote_ref)
            if not resolved:
                return False, msg
        return True, "merged"

    def _resolve_owner_actions_conflict(self, cwd: Path, remote_ref: str) -> tuple[bool, str]:
        """3-way merge of owner_actions.json by id, then renumber id collisions and commit (USR-312)."""
        target = cwd / OWNER_ACTIONS_RELATIVE
        mb = self._git_out(["merge-base", "HEAD", remote_ref], cwd)
        base_raw = ""
        if mb:
            res_base = safe_show(mb, OWNER_ACTIONS_RELATIVE, cwd=cwd)
            if res_base.returncode == 0:
                base_raw = res_base.stdout
        res_ours = safe_show("HEAD", OWNER_ACTIONS_RELATIVE, cwd=cwd)
        res_theirs = safe_show(remote_ref, OWNER_ACTIONS_RELATIVE, cwd=cwd)
        if res_ours.returncode != 0 or res_theirs.returncode != 0:
            return False, "failed to read owner_actions.json revisions for 3-way merge"
        ok, merged_text, err = merge_owner_actions_3way(base_raw, res_ours.stdout, res_theirs.stdout)
        if not ok:
            return False, err or "owner_actions.json 3-way merge failed"
        target.write_text(merged_text, encoding="utf-8")
        changes = renumber_duplicate_owner_actions(cwd, remote_ref=remote_ref)
        if changes:
            logger.info("owner_actions: colisao de id resolvida no merge: %s", "; ".join(changes))
        add_res = _run_git(["add", OWNER_ACTIONS_RELATIVE], cwd=cwd)
        if add_res.returncode != 0:
            return False, f"git add {OWNER_ACTIONS_RELATIVE} failed: {add_res.stderr.strip()}"
        commit_res = _run_git(["commit", "--no-edit"], cwd=cwd)
        if commit_res.returncode != 0:
            return False, f"git commit --no-edit failed: {commit_res.stderr.strip()}"
        return True, "merged"

    def _resolve_ledger_conflict(self, cwd: Path, remote_ref: str, *, commit: bool = True) -> tuple[bool, str]:
        """Attempt a 3-way line/ticket merge of .factory/demands/demands.json (USR-112)."""
        ledger_path = cwd / _LEDGER_RELATIVE
        if not ledger_path.is_file():
            return False, "ledger file not found"
        mb = self._git_out(["merge-base", "HEAD", remote_ref], cwd)
        base_raw = ""
        if mb:
            res_base = safe_show(mb, _LEDGER_RELATIVE, cwd=cwd)
            if res_base.returncode == 0:
                base_raw = res_base.stdout
        res_ours = safe_show("HEAD", _LEDGER_RELATIVE, cwd=cwd)
        res_theirs = safe_show(remote_ref, _LEDGER_RELATIVE, cwd=cwd)
        if res_ours.returncode != 0 or res_theirs.returncode != 0:
            return False, "failed to read demands.json revisions for 3-way merge"

        ok, merged_text, err = merge_demands_3way(base_raw, res_ours.stdout, res_theirs.stdout)
        if not ok:
            return False, err or "demands.json 3-way merge failed"

        ledger_path.write_text(merged_text, encoding="utf-8")
        add_res = _run_git(["add", _LEDGER_RELATIVE], cwd=cwd)
        if add_res.returncode != 0:
            return False, f"git add {_LEDGER_RELATIVE} failed: {add_res.stderr.strip()}"
        if not commit:
            return True, "staged"
        commit_res = _run_git(["commit", "--no-edit"], cwd=cwd)
        if commit_res.returncode != 0:
            return False, f"git commit --no-edit failed: {commit_res.stderr.strip()}"
        return True, "merged"

    def _sync_with_base(
        self, base: str, cwd: Path, *, preserve_commits: bool = False
    ) -> tuple[bool, list[str], str]:
        """Bring HEAD up to date with origin/base. Returns (ok, conflict_files, message)."""
        remote_ref = f"origin/{base}"
        if _run_git(["rev-parse", "--verify", remote_ref], cwd=cwd).returncode != 0:
            if _run_git(["rev-parse", "--verify", base], cwd=cwd).returncode == 0:
                remote_ref = base
        if _run_git(["merge-base", "--is-ancestor", remote_ref, "HEAD"], cwd=cwd).returncode == 0:
            return True, [], "up to date"
        if preserve_commits:
            # A rebase would rewrite the implementation SHA already recorded in the ledger.
            merge = _run_git(["merge", "--no-edit", remote_ref], cwd=cwd)
            if merge.returncode == 0:
                return True, [], "merged"
            files = self._conflict_files(cwd)
            if files and set(files) <= {_LEDGER_RELATIVE, OWNER_ACTIONS_RELATIVE}:
                resolved, msg = self._resolve_known_conflicts(cwd, remote_ref, files)
                if resolved:
                    return True, [], "merged"
                _run_git(["merge", "--abort"], cwd=cwd)
                return False, files, msg
            _run_git(["merge", "--abort"], cwd=cwd)
            return False, files, (merge.stderr or merge.stdout).strip()
        rebase = _run_git(["rebase", remote_ref], cwd=cwd)
        if rebase.returncode == 0:
            return True, [], "rebased"
        files = self._conflict_files(cwd)
        _run_git(["rebase", "--abort"], cwd=cwd)
        merge = _run_git(["merge", "--no-edit", remote_ref], cwd=cwd)
        if merge.returncode == 0:
            return True, [], "merged"
        files = self._conflict_files(cwd) or files
        if files and set(files) <= {_LEDGER_RELATIVE, OWNER_ACTIONS_RELATIVE}:
            resolved, msg = self._resolve_known_conflicts(cwd, remote_ref, files)
            if resolved:
                return True, [], "merged"
            _run_git(["merge", "--abort"], cwd=cwd)
            return False, files, msg
        _run_git(["merge", "--abort"], cwd=cwd)
        return False, files, (merge.stderr or merge.stdout).strip()

    def _repair_owner_action_duplicates(self, ticket_id: str, cwd: Path, remote_ref: str) -> list[str]:
        """Renumber any OA-NNN this branch collided on with ``remote_ref`` (USR-255).

        Runs right after the sync, before the branch is pushed and gated: a rebase/merge can leave
        the same id twice in ``owner_actions.json`` without a textual conflict (two branches each
        picked the next free number before either saw the other's commit). Left alone, the official
        gate fails with "item OA-NNN duplicado ignorado" well after the sync already succeeded.
        """
        try:
            changes = renumber_duplicate_owner_actions(cwd, remote_ref=remote_ref)
        except Exception:  # pragma: no cover - a repair attempt must never block delivery
            logger.exception("owner_actions: dedupe apos sync falhou para %s", ticket_id)
            return []
        if not changes:
            return []
        logger.info("owner_actions: renumerado apos sync com %s para %s: %s", remote_ref, ticket_id, "; ".join(changes))
        message = (
            f"fix(owner-actions): renumera OA duplicado apos sync com {remote_ref} [{ticket_id}]\n\n"
            + "\n".join(changes)
            + f"\n\nDarkFac-Ticket: {ticket_id}\nDarkFac-Autonomy: zero-human-touch\n"
        )
        try:
            self.commit_ticket(
                ticket_id, "renumera OA duplicado apos sync", cwd=cwd,
                paths=[OWNER_ACTIONS_RELATIVE], custom_message=message,
            )
        except Exception:  # pragma: no cover - the file fix stands even if this commit fails
            logger.exception("owner_actions: commit da renumeracao falhou para %s", ticket_id)
        return changes

    def _ledger_collision(self, cwd: Path, remote_ref: str) -> str:
        """Reject conflicting ticket identities before Git can resolve ledger rows."""
        candidate = cwd / _LEDGER_RELATIVE
        if not candidate.is_file():
            return ""
        shown = safe_show(remote_ref, _LEDGER_RELATIVE, cwd=cwd)
        if shown.returncode != 0:
            return ""
        collisions = title_collisions(shown.stdout, candidate.read_text(encoding="utf-8"))
        if not collisions:
            return ""
        return "demands.json ticket ID collision; merge blocked: " + "; ".join(collisions)

    def _find_open_pr(self, branch: str, cwd: Path, runner: GhRunner) -> Optional[tuple[int, str]]:
        res = runner(["pr", "list", "--head", branch, "--state", "open", "--json", "number,url"], cwd)
        if res.returncode != 0:
            return None
        try:
            items = json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if items:
            return int(items[0]["number"]), str(items[0]["url"])
        return None

    def _find_open_queue_duplicate(
        self, pr_title: str, branch: str, cwd: Path, runner: GhRunner
    ) -> Optional[tuple[int, str, str]]:
        """Open ``queue`` PR with exactly ``pr_title`` on a branch other than ``branch`` (USR-307).

        Returns ``(number, url, head_branch)`` of the oldest match, or ``None`` (also when ``gh``
        fails: dedupe is best effort and must never block a registration).
        """
        try:
            res = runner(
                ["pr", "list", "--label", "queue", "--state", "open", "--limit", "100",
                 "--json", "number,url,title,headRefName"],
                cwd,
            )
            if res.returncode != 0:
                return None
            rows = json.loads(res.stdout or "[]")
            matches = [
                r for r in rows
                if isinstance(r, dict) and r.get("title") == pr_title and r.get("headRefName")
                and r.get("headRefName") != branch
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("queue duplicate lookup failed: %s", exc)
            return None
        if not matches:
            return None
        first = min(matches, key=lambda r: int(r["number"]))
        return int(first["number"]), str(first["url"]), str(first["headRefName"])

    def _pr_state(self, number: int, cwd: Path, runner: GhRunner) -> tuple[str, Optional[str]]:
        res = runner(["pr", "view", str(number), "--json", "state,mergeCommit"], cwd)
        if res.returncode != 0:
            return "", None
        try:
            data = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            return "", None
        merge_commit = data.get("mergeCommit") or {}
        oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        return str(data.get("state", "")), oid

    def _changed_files(self, base: str, cwd: Path) -> list[str]:
        """Files the branch changes relative to its merge base with origin/base.

        ``--no-renames`` lists both sides of a rename so a file moved into the
        ledger directories cannot hide the path it left.
        """
        res = _run_git(["diff", "--name-only", "--no-renames", f"origin/{base}...HEAD"], cwd=cwd)
        if res.returncode != 0:
            return []
        return [line for line in res.stdout.splitlines() if line.strip()]

    def _record_completion(
        self, ticket_id: str, title: str, cwd: Path, custom_message: str | None = None
    ) -> str:
        """Commit implementation, then atomically record its SHA and completed status."""
        ledger = cwd / _LEDGER_RELATIVE
        if not ledger.is_file():  # small standalone repositories used by callers/tests
            if self.is_dirty(cwd):
                self.commit_ticket(ticket_id, title, cwd=cwd, custom_message=custom_message)
            return self.get_current_sha(cwd)
        store = DemandsStore(ledger)
        ticket = store.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"Ticket {ticket_id} not found in demands ledger")
        if ticket.status == DeliveryStatus.COMPLETED and ticket.delivery_evidence:
            if self.is_dirty(cwd):
                self.commit_ticket(ticket_id, title, cwd=cwd, custom_message=custom_message)
            return self.get_current_sha(cwd)
        if self.is_dirty(cwd):
            self.commit_ticket(ticket_id, title, cwd=cwd, custom_message=custom_message)
        implementation_sha = self.get_current_sha(cwd)
        ticket.status = DeliveryStatus.COMPLETED
        if ticket.is_live_deploy:
            ticket.delivery_evidence = f"{implementation_sha}:live_provisional"
        else:
            ticket.delivery_evidence = implementation_sha
        ticket.updated_at = datetime.now(timezone.utc)
        store.save_ticket(ticket)
        return self.commit_ticket(ticket_id, title, cwd=cwd, custom_message=custom_message) or implementation_sha

    def _ci_gate(
        self, number: int, pr_url: str, branch: str, base: str, cwd: Path, runner: GhRunner,
        kind: Literal["queue", "implementation", "registry"] = "implementation",
    ) -> tuple[Optional[DeliveryReport], str]:
        """Decide whether the PR may be merged (USR-85).

        Returns ``(blocking_report, note)``. ``blocking_report`` is ``None`` when
        the merge may proceed; ``note`` is the ``ci_gate=...`` marker for the
        final message. Ledger-only implementation PRs are rejected. Ledger-only
        queue PRs are gated like any other PR: the repository ruleset on the base
        branch requires status checks, so merging before they settle is refused.
        """
        changed = self._changed_files(base, cwd)
        if kind == "implementation" and is_ledger_only(changed):
            return DeliveryReport(
                ok=False, action="no_changes", pr_url=pr_url, branch=branch,
                message="Implementation PR changes only the ledger; merge withheld.",
            ), "ci_gate=rejected_ledger_only"
        if kind == "queue" and is_ledger_only(changed):
            logger.info("PR #%s touches only ledger/roadmap files; waiting for required checks", number)
        self._phase("ci", "running", f"aguardando checks obrigatorios do PR #{number}")
        verdict = ensure_green(
            number,
            cwd,
            runner,
            timeout_s=self.ci_timeout_s,
            poll_s=self.ci_poll_s,
            sleep_fn=self.ci_sleep_fn,
            clock_fn=self.ci_clock_fn,
            base_branch=base,
        )
        note = f"ci_gate={verdict.status}"
        if verdict.passed:
            self._phase("ci", "succeeded", note)
            return None, note
        blocked = self._blocked_report(verdict, pr_url, branch, base)
        self._phase("ci", "failed", blocked.message, verdict.status)
        return blocked, note

    @staticmethod
    def _blocked_report(verdict: CiVerdict, pr_url: str, branch: str, base: str) -> DeliveryReport:
        """Build the no-merge report for a pending_timeout/failed verdict."""
        if verdict.status == "pending_timeout":
            waiting = ", ".join(verdict.pending_checks) or "no result"
            return DeliveryReport(
                ok=False,
                action="ci_pending",
                pr_url=pr_url,
                branch=branch,
                message=(
                    f"CI still pending after {verdict.waited_s:.0f}s ({waiting}; {verdict.detail}); "
                    "merge withheld, PR and branch left open. ci_gate=pending_timeout"
                ),
            )
        checks = ", ".join(verdict.failed_checks)
        if verdict.base_red:
            headline = f"CI failed on {checks}; the same checks are already red on {base} (pre-existing failure)"
        else:
            headline = f"CI failed on {checks}"
        message = f"{headline}; merge withheld, PR and branch left open. ci_gate=failed"
        if verdict.log_tail:
            message += f"\n--- failed job log tail ---\n{verdict.log_tail}"
        return DeliveryReport(
            ok=False,
            action="base_red" if verdict.base_red else "ci_failed",
            pr_url=pr_url,
            branch=branch,
            message=message,
        )

    def deliver_branch(
        self,
        ticket_id: str,
        title: str,
        cwd: Optional[Path] = None,
        base: str = "main",
        gh_runner: Optional[GhRunner] = None,
        merge: bool = True,
        kind: Literal["queue", "implementation", "registry"] = "implementation",
        *,
        supervise_queue: bool = True,
        project_id: str = "darkfac",
    ) -> DeliveryReport:
        """Commit, push, open PR, merge (squash) and clean up a ticket branch.

        With merge=False (commercial acceptance required) the PR is opened but
        neither merged nor cleaned up. ``supervise_queue=False`` skips the stuck-PR watchdog and
        alert below: :mod:`core.git.queue_registration` sets it when this delivery IS the stuck-PR
        alert's own commit, so landing it never recurses into supervising itself (USR-246).
        """
        target = cwd or self.root
        runner = gh_runner or _run_gh
        try:  # USR-174: per-project data policy gates every delivery (fail-closed, structured)
            from core.enterprise.enforcement import DELIVERY_PROVIDER, DataPolicyError, enforce_data_policy

            enforce_data_policy(project_id, "delivery", provider=DELIVERY_PROVIDER)
        except DataPolicyError as exc:
            logger.error("deliver_branch refused by data policy: %s", exc)
            return DeliveryReport(ok=False, action="error", message=f"Delivery refused by data policy: {exc}")
        if kind == "queue" and merge and supervise_queue:
            # USR-238: every registration also reports queue PRs that stayed open too long.
            queue_registration.check_stalled_queue_prs(target, runner, manager_factory=self._child_manager)
        try:
            if kind not in ("queue", "implementation", "registry"):
                raise ValueError(f"Unknown delivery kind: {kind}")
            report = self._deliver(ticket_id, title, target, base, runner, merge, kind)
        except Exception as exc:  # fail-closed report instead of crashing the pipeline
            logger.exception("deliver_branch failed for %s", ticket_id)
            report = DeliveryReport(ok=False, action="error", message=str(exc))
        if kind == "queue" and not report.ok and report.pr_url and supervise_queue:
            queue_registration.alert_queue_pr_stuck(
                ticket_id, report.pr_url, report.message, cwd=target, gh_runner=runner,
                manager_factory=self._child_manager,
            )
        self._close_open_phase(report)
        return report

    def _deliver(
        self, ticket_id: str, title: str, cwd: Path, base: str, runner: GhRunner, merge: bool,
        kind: Literal["queue", "implementation", "registry"],
    ) -> DeliveryReport:
        # (a) commit if dirty
        if kind == "registry":
            changed = set(self._changed_files(base, cwd)) | set(self.changed_paths(cwd))
            outside = registry_disallowed(changed)
            if outside:
                return DeliveryReport(
                    ok=False, action="error",
                    message=f"Registry delivery refused: files outside the model allowlist: {', '.join(outside[:5])}",
                )
            if not changed:
                return DeliveryReport(ok=False, action="no_changes", message="Registry delivery has no changes.")
            if self.is_dirty(cwd=cwd):  # never touches the demands ledger: no ticket is completed
                self.commit_ticket(ticket_id, title, cwd=cwd, kind="registry")
        elif kind == "implementation":
            changed = set(self._changed_files(base, cwd)) | set(self.changed_paths(cwd))
            if is_ledger_only(list(changed)):
                return DeliveryReport(ok=False, action="no_changes", message="Implementation changes only the ledger.")
            self._record_completion(ticket_id, title, cwd)
        else:
            changed = set(self._changed_files(base, cwd)) | set(self.changed_paths(cwd))
            if not is_ledger_only(list(changed)):
                return DeliveryReport(ok=False, action="error", message="Queue registration must change only ledger files.")
            if self.is_dirty(cwd=cwd):
                self.commit_ticket(ticket_id, title, cwd=cwd, kind="queue")

        # (b) never work on base directly
        branch = self._current_branch(cwd)
        if branch in ("", "HEAD", base):
            branch = f"ticket/{ticket_id.lower()}"
            co = _run_git(["checkout", "-B", branch], cwd=cwd)
            if co.returncode != 0:
                return DeliveryReport(ok=False, action="error", message=f"checkout failed: {co.stderr.strip()}")

        # (c) fetch + sync with base
        fetch = _run_git(["fetch", "origin"], cwd=cwd)
        if fetch.returncode != 0:
            return DeliveryReport(
                ok=False, action="error", branch=branch, message=f"fetch failed: {fetch.stderr.strip()}"
            )
        remote_ref = f"origin/{base}"
        collision = self._ledger_collision(cwd, remote_ref)
        if collision:
            return DeliveryReport(ok=False, action="conflict", branch=branch, message=collision)
        # Local base is only realigned when fully contained in the branch (nothing is lost).
        if _run_git(["merge-base", "--is-ancestor", base, "HEAD"], cwd=cwd).returncode == 0:
            _run_git(["branch", "-f", base, remote_ref], cwd=cwd)

        ok, files, msg = self._sync_with_base(base, cwd, preserve_commits=kind != "queue")
        if not ok:
            return DeliveryReport(
                ok=False,
                action="conflict",
                branch=branch,
                conflict_files=files,
                message=f"Conflict with {remote_ref}: {', '.join(files) or msg}",
            )
        if kind == "implementation":
            # Queue PRs only ever touch the ledger (enforced below); the OA backlog collision this
            # guards against is only introduced by implementation branches (USR-255).
            self._repair_owner_action_duplicates(ticket_id, cwd, remote_ref)

        ahead = self._git_out(["rev-list", "--count", f"{remote_ref}..HEAD"], cwd)
        if ahead == "0":
            cleaned = self._cleanup(branch, base, cwd) if merge else False
            return DeliveryReport(
                ok=True, action="no_changes", branch=branch, cleaned=cleaned,
                message="Branch has no commits beyond base.",
            )
        if kind == "implementation" and is_ledger_only(self._changed_files(base, cwd)):
            return DeliveryReport(ok=False, action="no_changes", branch=branch, message="Implementation changes only the ledger.")
        if kind == "queue" and not is_ledger_only(self._changed_files(base, cwd)):
            return DeliveryReport(ok=False, action="error", branch=branch, message="Queue PR changes non-ledger files.")
        if kind == "registry":
            outside = registry_disallowed(self._changed_files(base, cwd))
            if outside:
                return DeliveryReport(
                    ok=False, action="error", branch=branch,
                    message=f"Registry PR changes files outside the model allowlist: {', '.join(outside[:5])}",
                )

        # (d) push. USR-307: a queue registration is identified by its PR title (ticket + title). When
        # an open queue PR with the same identity already exists on ANOTHER branch (replay of the same
        # operation, e.g. a second oa-queue-watchdog sweep), its head is updated in place instead of
        # opening a parallel chain of PRs.
        remote_branch = branch
        duplicate: Optional[tuple[int, str, str]] = None
        if kind == "queue":
            duplicate = self._find_open_queue_duplicate(
                f"chore(backlog): registrar {ticket_id} - {title.strip()}", branch, cwd, runner
            )
        self._phase("pr", "running", "push da branch e abertura do PR")
        if duplicate:
            remote_branch = duplicate[2]
            push = _run_git(["push", "--force", "origin", f"HEAD:refs/heads/{remote_branch}"], cwd=cwd, timeout_s=180.0)
        else:
            push = _run_git(["push", "--force-with-lease", "-u", "origin", branch], cwd=cwd, timeout_s=180.0)
        if push.returncode != 0:
            return DeliveryReport(
                ok=False, action="error", branch=branch, message=f"push failed: {push.stderr.strip()}"
            )

        # (e) PR (reuse existing)
        existing = (duplicate[:2] if duplicate else None) or self._find_open_pr(branch, cwd, runner)
        if existing:
            number, pr_url = existing
        else:
            body = f"{'Registro de fila' if kind == 'queue' else 'Entrega autonoma'} do ticket {ticket_id}.\n\nDarkFac-Ticket: {ticket_id}"
            if kind == "registry":
                body = "Atualizacao autonoma do registro de modelos (sem ticket a concluir).\n\nDarkFac-Autonomy: zero-human-touch"
            pr_title = _delivery_pr_title(kind, ticket_id, title)
            pr = runner(
                [
                    "pr", "create", "--base", base, "--head", branch,
                    "--title", pr_title, "--body", body,
                ],
                cwd,
            )
            if pr.returncode != 0:
                return DeliveryReport(
                    ok=False, action="error", branch=branch, message=f"gh pr create failed: {pr.stderr.strip()}"
                )
            pr_url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
            m = re.search(r"/pull/(\d+)", pr_url)
            if not m:
                return DeliveryReport(
                    ok=False, action="error", branch=branch, pr_url=pr_url, message="cannot parse PR number"
                )
            number = int(m.group(1))

        pr_title = _delivery_pr_title(kind, ticket_id, title)
        title_update = runner(["pr", "edit", str(number), "--title", pr_title], cwd)
        if title_update.returncode != 0:
            return DeliveryReport(ok=False, action="error", pr_url=pr_url, branch=branch,
                                  message=f"Could not set {kind} PR title: {title_update.stderr.strip()}")
        label = runner(["pr", "edit", str(number), "--add-label", kind], cwd)
        if label.returncode != 0:
            color = {"queue": "D4C5F9", "registry": "FBCA04"}.get(kind, "0E8A16")
            description = {"queue": "Ticket registered in backlog", "registry": "Model registry update"}.get(
                kind, "Ticket implementation")
            created = runner(["label", "create", kind, "--color", color,
                              "--description", description, "--force"], cwd)
            if created.returncode != 0:
                return DeliveryReport(ok=False, action="error", pr_url=pr_url, branch=branch,
                                      message=f"Could not create {kind} label: {created.stderr.strip()}")
            label = runner(["pr", "edit", str(number), "--add-label", kind], cwd)
            if label.returncode != 0:
                return DeliveryReport(ok=False, action="error", pr_url=pr_url, branch=branch,
                                      message=f"Could not label {kind} PR: {label.stderr.strip()}")

        self._phase("pr", "succeeded", pr_url)
        if kind == "implementation":
            self._register_catalog(ticket_id, title, cwd, base, pr_url)
        if not merge:
            return DeliveryReport(
                ok=True,
                action="awaiting_commercial_acceptance",
                pr_url=pr_url,
                branch=branch,
                message="PR opened; merge and cleanup withheld pending commercial acceptance.",
            )

        # (f) CI gate, then merge (retrying once after re-sync when not mergeable).
        # `gh pr merge` must never be reached without the gate approving the exact
        # head being merged (USR-85); the retry below re-runs the gate because the
        # re-sync pushes a new head.
        blocked, gate_note = self._ci_gate(number, pr_url, branch, base, cwd, runner, kind)
        if blocked is not None:
            return blocked
        merge_args = ["pr", "merge", str(number), "--squash", "--delete-branch"]
        self._phase("merge", "running", f"squash merge do PR {pr_url}")
        merge_res = runner(merge_args, cwd)
        state, merge_sha = self._pr_state(number, cwd, runner)
        if state != "MERGED" and merge_res.returncode != 0:
            err = f"{merge_res.stderr}{merge_res.stdout}".lower()
            if _POLICY_BLOCK_MARKER in err:
                # The ruleset on the base branch still lacks its required checks:
                # re-run the gate once (it waits for them) and try the merge again.
                blocked, gate_note = self._ci_gate(number, pr_url, branch, base, cwd, runner, kind)
                if blocked is not None:
                    return blocked
                merge_res = runner(merge_args, cwd)
                state, merge_sha = self._pr_state(number, cwd, runner)
                if state != "MERGED" and merge_res.returncode != 0:
                    pending = pending_check_labels(number, cwd, runner)
                    return DeliveryReport(
                        ok=False, action="error", pr_url=pr_url, branch=branch,
                        message=(
                            "gh pr merge failed: the base branch policy still prohibits the merge after "
                            f"re-running the CI gate ({gate_note}); pending required checks: "
                            f"{', '.join(pending) or 'none reported'}; PR and branch left open. "
                            f"gh said: {(merge_res.stderr or merge_res.stdout).strip()}"
                        ),
                    )
            elif _is_unmergeable(err):
                # USR-238: every re-sync pushes a new head that needs a fresh CI run, and main keeps
                # moving (most merges touch demands.json) while that run is in flight, so a single
                # retry loses the race. Re-apply onto the newest origin/main up to
                # _MERGE_RESYNC_ATTEMPTS times; the 3-way ledger merge keeps every ticket on both sides.
                for attempt in range(1, _MERGE_RESYNC_ATTEMPTS + 1):
                    _run_git(["fetch", "origin"], cwd=cwd)
                    collision = self._ledger_collision(cwd, remote_ref)
                    if collision:
                        return DeliveryReport(ok=False, action="conflict", pr_url=pr_url, branch=branch, message=collision)
                    ok, files, msg = self._sync_with_base(base, cwd, preserve_commits=kind != "queue")
                    if not ok:
                        return DeliveryReport(
                            ok=False, action="conflict", pr_url=pr_url, branch=branch, conflict_files=files,
                            message=f"Conflict on retry {attempt}: {', '.join(files) or msg}",
                        )
                    if remote_branch != branch:
                        _run_git(["push", "--force", "origin", f"HEAD:refs/heads/{remote_branch}"], cwd=cwd, timeout_s=180.0)
                    else:
                        _run_git(["push", "--force-with-lease", "origin", branch], cwd=cwd, timeout_s=180.0)
                    blocked, gate_note = self._ci_gate(number, pr_url, branch, base, cwd, runner, kind)
                    if blocked is not None:
                        return blocked
                    merge_res = runner(merge_args, cwd)
                    state, merge_sha = self._pr_state(number, cwd, runner)
                    if state == "MERGED" or merge_res.returncode == 0:
                        break
                    if not _is_unmergeable(f"{merge_res.stderr}{merge_res.stdout}".lower()):
                        break
                    logger.warning(
                        "PR #%s still unmergeable after re-sync %s/%s; main moved during CI",
                        number, attempt, _MERGE_RESYNC_ATTEMPTS,
                    )
        if state != "MERGED":
            return DeliveryReport(
                ok=False, action="error", pr_url=pr_url, branch=branch,
                message=f"gh pr merge failed: {(merge_res.stderr or merge_res.stdout).strip()}",
            )

        # (g) confirm merge sha reached origin/base
        # GitHub already reports MERGED: a failed or not-yet-propagated fetch must not turn into a false error.
        reached = False
        for attempt in range(_MERGE_CONFIRM_ATTEMPTS):
            _run_git(["fetch", "origin"], cwd=cwd)
            if merge_sha and _run_git(["merge-base", "--is-ancestor", merge_sha, remote_ref], cwd=cwd).returncode == 0:
                reached = True
                break
            if attempt + 1 < _MERGE_CONFIRM_ATTEMPTS:
                (self.ci_sleep_fn or time.sleep)(_MERGE_CONFIRM_DELAY_S)
        if not reached:
            return DeliveryReport(
                ok=False, action="error", pr_url=pr_url, branch=branch, merge_sha=merge_sha,
                message=f"merge commit {merge_sha} not found in {remote_ref}",
            )

        self._phase("merge", "succeeded", f"merge {merge_sha}")

        # (h) cleanup
        cleaned = self._cleanup(branch, base, cwd)
        return DeliveryReport(
            ok=True, action="merged", pr_url=pr_url, merge_sha=merge_sha, cleaned=cleaned, branch=branch,
            message=f"PR {pr_url} merged into {base}. {gate_note}",
        )

    def _register_catalog(self, ticket_id: str, title: str, cwd: Path, base: str, pr_url: str) -> None:
        """Best-effort cross-project catalog/evolution registration (USR-220).

        Writes only to git-ignored runtime state, so it never adds files to the PR. Any failure is
        logged and swallowed: it must not change the delivery outcome.
        """
        try:
            from core.catalog.delivery import read_from_worktree, register_delivery

            res = _run_git(["diff", "--name-status", "--no-renames", f"origin/{base}...HEAD"], cwd=cwd)
            if res.returncode != 0:
                return
            changes = [(parts[0], parts[-1]) for parts in (ln.split(chr(9)) for ln in res.stdout.splitlines()) if len(parts) >= 2]
            tags: list[str] = []
            ledger = cwd / _LEDGER_RELATIVE
            if ledger.is_file():
                ticket = DemandsStore(ledger).get_ticket(ticket_id)
                tags = list(ticket.tags) if ticket else []
            register_delivery(ticket_id, title, changes, read_from_worktree(cwd), tags, pr_url, push=True)
        except Exception as exc:  # noqa: BLE001 - advisory only
            logger.warning("catalog registration failed for %s: %s", ticket_id, exc)

    def _cleanup(self, branch: str, base: str, cwd: Path) -> bool:
        """Prune, fast-forward the main checkout, delete local branch, remove secondary worktree."""
        main_root, is_secondary = self._main_root(cwd)
        _run_git(["fetch", "--prune", "origin"], cwd=main_root)
        ok = True
        if is_secondary:
            # Retry/backoff, robust rmtree and the recoverable-trash fallback (USR-69):
            # a plain `git worktree remove` leaves the directory behind on Windows.
            removal = ticket_workspace.cleanup(
                cwd, main_root, sleep_fn=self.cleanup_sleep_fn, now_fn=self.now_fn
            )
            if not removal.removed:
                logger.warning("worktree cleanup failed for %s: %s", cwd, removal.detail or removal.errors)
                ok = False
            elif removal.method == "trash":
                logger.warning("worktree %s could not be deleted; moved to %s", cwd, removal.trash_path)
        elif self._current_branch(main_root) == branch:
            co = _run_git(["checkout", base], cwd=main_root)
            if co.returncode != 0:
                logger.warning("checkout %s failed: %s", base, co.stderr.strip())
                return False
        if self._current_branch(main_root) == base and not self.is_dirty(cwd=main_root):
            ff = _run_git(["merge", "--ff-only", f"origin/{base}"], cwd=main_root)
            if ff.returncode != 0:
                logger.warning("ff-only failed: %s", ff.stderr.strip())
                ok = False
        if branch != base:
            db = _run_git(["branch", "-D", branch], cwd=main_root)
            if db.returncode != 0:
                logger.warning("branch -D failed: %s", db.stderr.strip())
                ok = False
        return ok

    def sweep_stale(
        self, base: str = "main", gh_runner: Optional[GhRunner] = None, cwd: Optional[Path] = None
    ) -> SweepReport:
        """Remove ticket/*, df/* branches and .worktrees/.claude/worktrees already merged in origin/base.

        A branch counts as merged when it is an ancestor of origin/base, or when GitHub
        reports a merged PR whose head is exactly the branch tip (squash merges).
        Never removes branches with unmerged commits or dirty worktrees.
        """
        main_root, _ = self._main_root(cwd or self.root)
        report = SweepReport()
        _run_git(["fetch", "--prune", "origin"], cwd=main_root)
        remote_ref = f"origin/{base}"
        use_gh = gh_runner is not None or self.gh_ready(main_root)
        runner = gh_runner or _run_gh

        def merged(branch: str) -> bool:
            if _run_git(["merge-base", "--is-ancestor", branch, remote_ref], cwd=main_root).returncode == 0:
                return True
            if not use_gh:
                return False
            tip = self._git_out(["rev-parse", branch], main_root)
            res = runner(
                ["pr", "list", "--head", branch, "--state", "merged", "--json", "number,headRefOid"], main_root
            )
            if res.returncode != 0:
                return False
            try:
                return any(i.get("headRefOid") == tip for i in json.loads(res.stdout or "[]"))
            except (json.JSONDecodeError, AttributeError):
                return False

        wt_roots = [(main_root / rel).resolve() for rel in ticket_workspace.WORKTREE_ROOTS]
        entries = ticket_workspace.parse_worktree_list(self._git_out(["worktree", "list", "--porcelain"], main_root))
        remote_sha = self._git_out(["rev-parse", remote_ref], main_root)
        running_in = (cwd or self.root).resolve()
        now = (self.now_fn or ticket_workspace.utc_now)().timestamp()
        occupied: set[str] = set()
        for entry in entries:
            path = Path(entry["worktree"]).resolve()
            br = entry.get("branch", "").removeprefix("refs/heads/")
            if path == main_root.resolve() or not any(root in path.parents for root in wt_roots):
                if br:
                    occupied.add(br)
                continue
            head = entry.get("HEAD", "")
            if br:
                is_merged = merged(br)
            else:
                is_merged = (
                    bool(head)
                    and _run_git(["merge-base", "--is-ancestor", head, remote_ref], cwd=main_root).returncode == 0
                )
            if br:
                occupied.add(br)
            if not is_merged:
                report.skipped.append(f"worktree:{path} (not merged)")
                continue
            if "locked" in entry:
                report.skipped.append(f"worktree:{path} (locked: in use)")
                continue
            if path == running_in or path in running_in.parents:
                report.skipped.append(f"worktree:{path} (current working directory)")
                continue
            if self.is_dirty(cwd=path):
                report.skipped.append(f"worktree:{path} (dirty)")
                continue
            if head and head == remote_sha and now - ticket_workspace.latest_mtime(path) < self.sweep_grace_s:
                # No commit of its own yet and just touched: most likely a worktree being set up.
                report.skipped.append(f"worktree:{path} (fresh, no commits yet)")
                continue
            removal = ticket_workspace.cleanup(path, main_root, sleep_fn=self.cleanup_sleep_fn, now_fn=self.now_fn)
            if removal.removed:
                report.removed_worktrees.append(str(path))
                if removal.method == "trash":
                    report.trashed_worktrees.append(str(path))
                occupied.discard(br)
            else:
                report.skipped.append(f"worktree:{path} ({removal.detail or '; '.join(removal.errors)})")

        current = self._current_branch(main_root)
        out = self._git_out(["branch", "--format=%(refname:short)", "--list", "ticket/*", "df/*"], main_root)
        for br in (b.strip() for b in out.splitlines() if b.strip()):
            if br == current or br in occupied:
                report.skipped.append(f"branch:{br} (in use)")
                continue
            if not merged(br):
                report.skipped.append(f"branch:{br} (not merged)")
                continue
            if _run_git(["branch", "-D", br], cwd=main_root).returncode == 0:
                report.removed_branches.append(br)
        _run_git(["worktree", "prune"], cwd=main_root)

        # Directories no worktree owns (dead leftovers of failed removals): recoverable trash,
        # never a direct delete; then drop trash entries past the retention.
        orphans, notes = ticket_workspace.find_orphan_dirs(
            main_root, min_age_s=self.sweep_grace_s, now_fn=self.now_fn
        )
        report.skipped.extend(f"orphan:{note}" for note in notes)
        for orphan in orphans:
            try:
                target = ticket_workspace.trash_directory(orphan, main_root, now_fn=self.now_fn)
            except OSError as exc:
                report.skipped.append(f"orphan:{orphan} (could not move to trash: {exc})")
                continue
            report.orphans_trashed.append(f"{orphan} -> {target}")
        purged = ticket_workspace.purge_trash(
            main_root, retention_days=self.trash_retention_days, now_fn=self.now_fn
        )
        report.trash_purged.extend(purged.purged)
        report.skipped.extend(f"trash:{failure}" for failure in purged.failed)
        return report

    @staticmethod
    def _requires_commercial_acceptance(project_id: str) -> bool:
        """Look up the project descriptor; unknown projects are treated as internal."""
        try:
            from core.projects.registry import get_project_registry

            project = get_project_registry().get_project(project_id)
        except Exception as exc:
            logger.warning("Project registry lookup failed for %s: %s", project_id, exc)
            return False
        return bool(project and project.requires_commercial_acceptance)

    def complete_ticket(
        self,
        ticket_id: str,
        cwd: Optional[Path] = None,
        auto_push: bool = True,
        auto_commit: bool = True,
        custom_message: Optional[str] = None,
        gh_runner: Optional[GhRunner] = None,
        live_verifier: Optional[Callable[[str], Any]] = None,
    ) -> TicketCompletionReport:
        """Finalize ticket development: update DemandsStore to completed, commit, and push."""
        target_dir = cwd or self.root
        store_path = target_dir / ".factory" / "demands" / "demands.json"
        store = DemandsStore(store_path)

        ticket = store.get_ticket(ticket_id)
        if not ticket:
            return TicketCompletionReport(
                ok=False,
                ticket_id=ticket_id,
                message=f"Ticket '{ticket_id}' not found in demands store.",
            )

        # A previous implementation commit is visible in the branch diff; an as-yet
        # uncommitted implementation is visible in status. Check both before touching
        # the ledger so a ledger-only branch cannot be delivered as a completed ticket.
        base_ref = "origin/main"
        branch_diff = _run_git(["diff", "--name-only", "-z", f"{base_ref}...HEAD"], cwd=target_dir)
        if branch_diff.returncode != 0:
            return TicketCompletionReport(
                ok=False,
                ticket_id=ticket_id,
                message=f"Could not inspect ticket changes against {base_ref}: {branch_diff.stderr.strip()}",
            )
        changed = set(branch_diff.stdout.split("\0")) | set(self.changed_paths(target_dir))
        if not any(path and path != ".factory/demands/demands.json" for path in changed):
            message = "Ticket branch has no implementation changes outside the demands ledger."
            return TicketCompletionReport(
                ok=False,
                ticket_id=ticket_id,
                delivery=DeliveryReport(
                    ok=False, action="no_changes", branch=self._current_branch(target_dir), message=message
                ),
                message=message,
            )

        # Record a verifiable implementation SHA alongside completed in one ledger commit.
        commit_sha: Optional[str] = None
        if auto_commit:
            self._phase("commit", "running", "commit da implementacao e do registro de conclusao")
            try:
                commit_sha = self._record_completion(ticket.id, ticket.title, target_dir, custom_message)
            except StagedInspectError as exc:
                # Explicit, resumable cause instead of a traceback after an approved gate (USR-264).
                self._phase("commit", "failed", str(exc), STAGED_INSPECT_FAILED)
                return TicketCompletionReport(
                    ok=False,
                    ticket_id=ticket_id,
                    delivery=DeliveryReport(
                        ok=False, action=STAGED_INSPECT_FAILED,
                        branch=self._current_branch(target_dir), message=str(exc),
                    ),
                    message=str(exc),
                )
            except Exception as exc:
                self._phase("commit", "failed", str(exc), type(exc).__name__)
                raise
            self._phase("commit", "succeeded", commit_sha or "")
        else:
            return TicketCompletionReport(
                ok=False, ticket_id=ticket_id,
                message="Cannot complete a ticket without committing delivery evidence.",
            )

        # 3. Remote sync if requested
        sync_result: Optional[GitSyncResult] = None
        delivery: Optional[DeliveryReport] = None
        if auto_push:
            if self.gh_ready(cwd=target_dir, gh_runner=gh_runner):
                needs_acceptance = self._requires_commercial_acceptance(ticket.project_id)
                delivery = self.deliver_branch(
                    ticket.id, ticket.title, cwd=target_dir, gh_runner=gh_runner,
                    merge=not needs_acceptance, kind="implementation"
                )
                sync_result = GitSyncResult(
                    ok=delivery.ok,
                    action=delivery.action,
                    remote_sha=delivery.merge_sha,
                    message=delivery.message,
                )
                if not delivery.ok:
                    return TicketCompletionReport(
                        ok=False,
                        ticket_id=ticket.id,
                        commit_sha=commit_sha,
                        sync_result=sync_result,
                        delivery=delivery,
                        message=f"Ticket {ticket.id} recorded but delivery failed: {delivery.message}",
                    )
            else:
                logger.warning("gh unavailable or unauthenticated; falling back to direct push to main.")
                sync_result = self.sync_environment(cwd=target_dir, allow_push=True)

        # 4. Live proof verification for live-deploy tickets (USR-73)
        if ticket.is_live_deploy:
            target_sha = (
                (delivery.merge_sha if delivery and delivery.merge_sha else None)
                or commit_sha
                or self.get_current_sha(target_dir)
            )
            verify_res = None
            verify_ok = False
            try:
                verify_res = (
                    live_verifier(target_sha)
                    if live_verifier is not None
                    else self._default_live_verify(target_sha, target_dir)
                )
                verify_ok = bool(
                    getattr(verify_res, "ok", False)
                    or (isinstance(verify_res, dict) and verify_res.get("ok"))
                )
            except Exception as exc:
                verify_res = f"live verification exception: {type(exc).__name__}: {exc}"
                verify_ok = False

            if verify_ok:
                t = store.get_ticket(ticket.id)
                if t:
                    t.delivery_evidence = f"{target_sha}:live_converged"
                    t.updated_at = datetime.now(timezone.utc)
                    store.save_ticket(t)
            else:
                failure_evidence = (
                    f"[LIVE_CONVERGENCE_FAILURE] Failed at {datetime.now(timezone.utc).isoformat()} "
                    f"on SHA {target_sha}: {verify_res}"
                )
                if store.get_ticket(ticket.id):
                    store.requeue_ticket(ticket.id, reason=failure_evidence, reopen=True)

                defect_id = store.next_ticket_id(ticket.project_id)
                defect_ticket = UserTicket(
                    id=defect_id,
                    project_id=ticket.project_id,
                    title=f"Defeito: Falha de convergência ao vivo pós-deploy de {ticket.id}",
                    origin=DemandOrigin.AGENT,
                    status=DeliveryStatus.PLANNED,
                    item_type=RoadmapItemType.INFRASTRUCTURE,
                    lifecycle_stage=LifecycleStage.EXECUTION,
                    horizon=PlanningHorizon.NOW,
                    tags=[TAG_USER_DEMAND, "defect", "deploy-live-failure", "auto-generated"],
                    problem_statement=(
                        f"O ticket {ticket.id} ({ticket.title}) falhou na prova de convergência ao vivo "
                        f"pós-deploy no SHA {target_sha}.\nEvidência:\n{failure_evidence}"
                    ),
                    core_journey=[f"Investigar e restaurar convergência dos 3 nós após deploy do ticket {ticket.id}"],
                    acceptance_criteria=["Todos os 3 nós (Notebook, Desktop, VPS) convergem para o SHA em produção"],
                    dependencies=[ticket.id],
                )
                store.save_ticket(defect_ticket)
                return TicketCompletionReport(
                    ok=False,
                    ticket_id=ticket.id,
                    commit_sha=commit_sha,
                    sync_result=sync_result,
                    delivery=delivery,
                    message=f"Live convergence proof failed for {ticket.id}. Reverted to planned; defect ticket {defect_id} opened. {verify_res}",
                )

        return TicketCompletionReport(
            ok=True,
            ticket_id=ticket.id,
            commit_sha=commit_sha,
            sync_result=sync_result,
            delivery=delivery,
            message=f"Ticket {ticket.id} successfully completed and recorded.",
        )

    @staticmethod
    def _default_live_verify(target_sha: str, target_dir: Path) -> Any:
        try:
            from core.infra import node_sync
            return node_sync.verify(root=target_dir)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# ----------------------------------------------------------------------
# check-main: detect a red base branch (USR-85)
# ----------------------------------------------------------------------

TicketFinder = Callable[[str], Optional[str]]
TicketCreator = Callable[[str, str, Sequence[str]], Optional[str]]
Notifier = Callable[[str, str], bool]

_LEDGER_RELATIVE = ".factory/demands/demands.json"
_TICKET_ID_PATTERN = re.compile(r"\bUSR-\d+\b")


def _ledger_entries(raw: str) -> list[dict[str, Any]]:
    """Decode a ``demands.json`` payload (list, or ``{"demands": [...]}``)."""
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = payload.get("demands", [])
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def find_ticket_for_sha(short_sha: str, root: Path = PROJECT_ROOT) -> Optional[str]:
    """Return the id of an existing ticket whose title or tags mention ``short_sha``.

    Looks at the local ledger and at ``origin/main`` (after a best-effort fetch):
    a ticket queued from another machine only exists locally after a pull.
    """
    needle = short_sha.strip().lower()
    if not needle:
        return None
    _run_git(["fetch", "origin", "main"], cwd=root)
    sources: list[str] = []
    shown = safe_show("origin/main", _LEDGER_RELATIVE, cwd=root, runner=lambda args, directory: _run_git(args, cwd=directory))
    if shown.returncode == 0:
        sources.append(shown.stdout)
    local = root / ".factory" / "demands" / "demands.json"
    try:
        sources.append(local.read_text(encoding="utf-8"))
    except OSError:
        pass
    for raw in sources:
        for item in _ledger_entries(raw):
            tags = item.get("tags")
            parts = [str(item.get("title") or "")]
            if isinstance(tags, list):
                parts.extend(t for t in tags if isinstance(t, str))
            if needle in " ".join(parts).lower():
                return str(item.get("id") or "") or None
    return None


def create_defect_ticket(
    title: str, problem: str, criteria: Sequence[str], root: Path = PROJECT_ROOT
) -> Optional[str]:
    """Register a planned defect ticket through ``run_ticket.py --create --queue-only``."""
    cmd = [
        sys.executable,
        str(root / "run_ticket.py"),
        "--create",
        "--queue-only",
        "--title",
        title,
        "--problem",
        problem,
        "--criteria",
        *criteria,
    ]
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900.0,
            env=env,
            **_win_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not queue the defect ticket: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning("run_ticket --create --queue-only failed: %s", sanitize(proc.stderr.strip()))
        return None
    match = _TICKET_ID_PATTERN.search(proc.stdout)
    return match.group(0) if match else None


def notify_main_red(title: str, message: str) -> bool:
    """Send the red-main alert through the notification service; never raises."""
    try:
        from core.notifications import AlertCategory, AlertSeverity, NotificationService

        event = NotificationService().notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.CRITICAL,
            title=title,
            message=message,
            force=True,
        )
    except Exception as exc:  # alerting is best-effort; Telegram may be unconfigured
        logger.warning("Red-main alert not delivered: %s", sanitize(str(exc)))
        return False
    return event is not None


def build_main_red_ticket(status: MainStatus) -> tuple[str, str, list[str]]:
    """Title, problem statement and acceptance criteria of the defect ticket."""
    jobs = ", ".join(name for name, _ in status.red_jobs) or status.workflow
    title = f"CI vermelho no {status.branch} @{status.short_sha}: {jobs}"
    if len(title) > 120:
        title = f"{title[:117].rstrip()}..."
    problem = (
        f"O ultimo run concluido do workflow '{status.workflow}' em {status.branch} "
        f"(SHA {status.sha}) terminou em '{status.conclusion}'. "
        f"Jobs vermelhos: {jobs}. Run: {status.run_url}. "
        "Detectado por 'python -m core.git.autonomy check-main' (USR-85); PRs com CI vermelho "
        "nao sao mais mesclados automaticamente, entao ate corrigir este defeito a entrega "
        "de codigo fica bloqueada."
    )
    criteria = [
        f"O workflow '{status.workflow}' volta a ficar verde em {status.branch} "
        f"(novo run concluido com sucesso depois de {status.short_sha}).",
        "A causa raiz esta corrigida ou coberta por teste de regressao.",
    ]
    return title, problem, criteria


def run_check_main(
    *,
    runner: Optional[GhRunner] = None,
    root: Path = PROJECT_ROOT,
    branch: str = "main",
    workflow: str = DEFAULT_MAIN_WORKFLOW,
    open_ticket: bool = False,
    notify: bool = False,
    alert_owner: bool = False,
    ticket_finder: Optional[TicketFinder] = None,
    ticket_creator: Optional[TicketCreator] = None,
    notifier: Optional[Notifier] = None,
    owner_action_store: Optional[OwnerActionStore] = None,
    owner_action_notifier: Optional[main_ci_alert.Notifier] = None,
) -> tuple[int, dict[str, Any]]:
    """Probe the base branch CI. Exit code: 0 green, 1 red, 2 unknown (gh failure).

    ``open_ticket`` files ONE defect ticket per SHA (idempotent: an existing
    ticket mentioning the short SHA is reused). ``notify`` sends an alert on every
    invocation where the branch is red, so schedule it with a sensible cadence.
    ``alert_owner`` keeps the single ``OA-MAIN-CI`` owner-action alert (DarkHub +
    Telegram ops/owner) in sync (USR-247): raised, never duplicated, on red; closed
    on green. With no ``owner_action_store`` the write lands on ``main`` through its
    own autonomous ``queue`` PR (``core.git.main_ci_alert``), using ``root`` as the
    checkout to branch the throwaway worktree from.
    """
    status = check_main(runner or _run_gh, root, branch=branch, workflow=workflow)
    payload: dict[str, Any] = status.to_dict()
    if status.state == "green":
        if alert_owner:
            resolved = main_ci_alert.sync_main_ci_status(
                status, store=owner_action_store, cwd=root, gh_runner=runner or _run_gh,
                notifier=owner_action_notifier,
            )
            payload["owner_action"] = {"id": resolved.id, "closed": True} if resolved else None
        return 0, payload
    if status.state == "unknown":
        return 2, payload

    if open_ticket:
        finder = ticket_finder or (lambda sha: find_ticket_for_sha(sha, root))
        creator = ticket_creator or (lambda t, p, c: create_defect_ticket(t, p, c, root))
        existing = finder(status.short_sha)
        if existing:
            payload["ticket"] = {"id": existing, "created": False}
        else:
            title, problem, criteria = build_main_red_ticket(status)
            created = creator(title, problem, criteria)
            payload["ticket"] = (
                {"id": created, "created": True}
                if created
                else {"id": None, "created": False, "error": "could not queue the defect ticket"}
            )
    if notify:
        alert = notifier or notify_main_red
        jobs = ", ".join(f"{name} ({url})" if url else name for name, url in status.red_jobs)
        text = (
            f"O CI '{status.workflow}' esta vermelho em {status.branch} (SHA {status.short_sha}, "
            f"conclusao {status.conclusion}). Jobs: {jobs or 'desconhecidos'}. Run: {status.run_url}"
        )
        try:
            payload["notified"] = bool(alert(f"CI vermelho em {status.branch} ({status.short_sha})", text))
        except Exception as exc:  # never fatal
            logger.warning("Red-main alert failed: %s", sanitize(str(exc)))
            payload["notified"] = False
    if alert_owner:
        raised = main_ci_alert.sync_main_ci_status(
            status, store=owner_action_store, cwd=root, gh_runner=runner or _run_gh,
            notifier=owner_action_notifier,
        )
        payload["owner_action"] = {"id": raised.id, "closed": False} if raised else None
    return 1, payload


def format_check_main(payload: dict[str, Any]) -> str:
    """Plain-text (ASCII) rendering of a check-main payload."""
    head = f"{payload['workflow']} on {payload['branch']}"
    if payload["state"] == "green":
        line = f"[MAIN_GREEN] {head} is green (sha {payload['short_sha']})."
        owner_action = payload.get("owner_action")
        if owner_action:
            line += f"\n  owner action: {owner_action['id']} closed"
        return line
    if payload["state"] == "unknown":
        return f"[MAIN_UNKNOWN] {head}: {payload.get('detail') or 'state could not be determined'}"
    lines = [
        f"[MAIN_RED] {head}: conclusion={payload['conclusion']} sha={payload['sha']}",
        f"  run: {payload['run_url']}",
    ]
    for job in payload["red_jobs"]:
        lines.append(f"  red job: {job['name']} -> {job['url']}")
    ticket = payload.get("ticket")
    if ticket:
        if ticket.get("id"):
            lines.append(f"  ticket: {ticket['id']} ({'created' if ticket['created'] else 'already exists'})")
        else:
            lines.append(f"  ticket: not created ({ticket.get('error')})")
    if "notified" in payload:
        lines.append(f"  alert: {'sent' if payload['notified'] else 'not delivered'}")
    owner_action = payload.get("owner_action")
    if owner_action:
        lines.append(f"  owner action: {owner_action['id']} open")
    return "\n".join(lines)


def _configure_stdio() -> None:
    """Force UTF-8 on the standard streams (Windows consoles default to cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="DarkFac Git Autonomy CLI (USR-57)")
    subparsers = parser.add_subparsers(dest="command")

    # Command: sync
    sync_parser = subparsers.add_parser("sync", help="Synchronize local branch with remote repository")
    sync_parser.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    sync_parser.add_argument("--branch", default="main", help="Git branch name (default: main)")
    sync_parser.add_argument("--no-push", action="store_true", help="Do not push local commits to remote")

    # Command: commit
    commit_parser = subparsers.add_parser("commit", help="Commit current changes bound to a ticket")
    commit_parser.add_argument("ticket_id", help="Ticket ID (e.g. USR-57)")
    commit_parser.add_argument("--title", required=True, help="Title/summary of the change")
    commit_parser.add_argument("--scope", default="core", help="Conventional commit scope")

    # Command: complete
    comp_parser = subparsers.add_parser("complete", help="Complete ticket lifecycle: mark completed, commit, and push")
    comp_parser.add_argument("ticket_id", help="Ticket ID (e.g. USR-57)")
    comp_parser.add_argument("--no-push", action="store_true", help="Do not push after completing")
    comp_parser.add_argument("--no-commit", action="store_true", help="Do not commit working tree")

    deliver_parser = subparsers.add_parser("deliver", help="Push branch, open PR, merge and clean up")
    deliver_parser.add_argument("ticket_id", help="Ticket ID (e.g. USR-57)")
    deliver_parser.add_argument("--title", required=True, help="Title/summary of the change")
    deliver_parser.add_argument("--base", default="main", help="Base branch (default: main)")
    deliver_parser.add_argument("--kind", choices=("queue", "implementation", "registry"), default="implementation",
                                help="Delivery type (default: implementation)")
    deliver_parser.add_argument("--no-merge", action="store_true", help="Open PR only (commercial acceptance)")

    subparsers.add_parser("sweep", help="Remove merged ticket/df branches and stale worktrees")

    cm_parser = subparsers.add_parser(
        "check-main",
        help="Report whether CI is green on the base branch (exit 0 green, 1 red, 2 unknown)",
    )
    cm_parser.add_argument("--branch", default="main", help="Base branch to probe (default: main)")
    cm_parser.add_argument(
        "--workflow", default=DEFAULT_MAIN_WORKFLOW, help=f"Workflow name (default: {DEFAULT_MAIN_WORKFLOW})"
    )
    cm_parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    cm_parser.add_argument(
        "--open-ticket", action="store_true", help="File one defect ticket per red SHA (idempotent)"
    )
    cm_parser.add_argument("--notify", action="store_true", help="Send an alert when the branch is red")
    cm_parser.add_argument(
        "--alert-owner", action="store_true",
        help="Raise/close the single OA-MAIN-CI owner-action alert (DarkHub + Telegram) to match the branch state",
    )

    args = parser.parse_args(argv)

    if args.command == "check-main":
        code, payload = run_check_main(
            branch=args.branch, workflow=args.workflow, open_ticket=args.open_ticket, notify=args.notify,
            alert_owner=args.alert_owner,
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else format_check_main(payload))
        return code

    manager = GitAutonomyManager()

    if args.command == "sync":
        result = manager.sync_environment(
            remote=args.remote,
            branch=args.branch,
            allow_push=not args.no_push,
        )
        print(f"[{result.action.upper()}] {result.message}")
        return 0 if result.ok else 1

    if args.command == "commit":
        sha = manager.commit_ticket(
            ticket_id=args.ticket_id,
            title=args.title,
            scope=args.scope,
        )
        print(f"[COMMITTED] Ticket {args.ticket_id} at {sha}")
        return 0

    if args.command == "deliver":
        rep = manager.deliver_branch(args.ticket_id, args.title, base=args.base,
                                     merge=not args.no_merge, kind=args.kind)
        print(f"[{rep.action.upper()}] {rep.message} {rep.pr_url or ''}".rstrip())
        return 0 if rep.ok else 1

    if args.command == "sweep":
        sw = manager.sweep_stale()
        print(
            f"[SWEEP] branches={sw.removed_branches} worktrees={sw.removed_worktrees} "
            f"trashed={len(sw.trashed_worktrees)} orphans_trashed={len(sw.orphans_trashed)} "
            f"trash_purged={len(sw.trash_purged)} skipped={len(sw.skipped)}"
        )
        return 0

    if args.command == "complete":
        rep = manager.complete_ticket(
            ticket_id=args.ticket_id,
            auto_push=not args.no_push,
            auto_commit=not args.no_commit,
        )
        if rep.ok:
            print(f"[COMPLETED] Ticket {rep.ticket_id} (Commit: {rep.commit_sha})")
            if rep.sync_result:
                print(f"  -> Sync: [{rep.sync_result.action.upper()}] {rep.sync_result.message}")
            return 0
        else:
            print(f"[ERROR] Failed to complete ticket {args.ticket_id}: {rep.message}", file=sys.stderr)
            return 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
