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
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Optional, Sequence

from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus

logger = logging.getLogger("darkfac.git.autonomy")


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
        description="merged, conflict, awaiting_commercial_acceptance, no_changes or error"
    )
    pr_url: Optional[str] = None
    merge_sha: Optional[str] = None
    cleaned: bool = False
    branch: Optional[str] = None
    conflict_files: list[str] = Field(default_factory=list)
    message: str = ""


class SweepReport(BaseModel):
    """Outcome of sweeping stale (already merged) branches and worktrees."""

    removed_branches: list[str] = Field(default_factory=list)
    removed_worktrees: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


GhRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def _run_gh(args: Sequence[str], cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess[str]:
    """Execute the GitHub CLI; never raises (missing gh yields returncode 127)."""
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


class GitAutonomyManager:
    """Manages autonomous Git operations for the Dark Factory."""

    def __init__(self, root: Path = PROJECT_ROOT) -> None:
        self.root = root

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

    def commit_ticket(
        self,
        ticket_id: str,
        title: str,
        scope: str = "core",
        paths: Optional[Sequence[str | Path]] = None,
        cwd: Optional[Path] = None,
        custom_message: Optional[str] = None,
    ) -> Optional[str]:
        """Stage changes and produce a deterministic atomic commit bound to the ticket.

        Returns the new commit SHA, or the current HEAD if working tree is clean.
        """
        target_dir = cwd or self.root

        # 1. Stage changes
        if paths:
            stage_args = ["add", "--"] + [str(p) for p in paths]
            _run_git(stage_args, cwd=target_dir)
        else:
            _run_git(["add", "-A"], cwd=target_dir)

        # 2. Check if anything is staged
        staged_check = _run_git(["diff", "--cached", "--quiet"], cwd=target_dir)
        if staged_check.returncode == 0:
            logger.info("No staged changes to commit for ticket %s.", ticket_id)
            return self.get_current_sha(cwd=target_dir)

        # 3. Format message
        clean_title = title.strip().replace("\n", " ")
        if custom_message:
            commit_msg = custom_message
        else:
            commit_msg = (
                f"feat({scope}): {clean_title} [{ticket_id}]\n\n"
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

    def _sync_with_base(self, base: str, cwd: Path) -> tuple[bool, list[str], str]:
        """Bring HEAD up to date with origin/base. Returns (ok, conflict_files, message)."""
        remote_ref = f"origin/{base}"
        if _run_git(["merge-base", "--is-ancestor", remote_ref, "HEAD"], cwd=cwd).returncode == 0:
            return True, [], "up to date"
        rebase = _run_git(["rebase", remote_ref], cwd=cwd)
        if rebase.returncode == 0:
            return True, [], "rebased"
        files = self._conflict_files(cwd)
        _run_git(["rebase", "--abort"], cwd=cwd)
        merge = _run_git(["merge", "--no-edit", remote_ref], cwd=cwd)
        if merge.returncode == 0:
            return True, [], "merged"
        files = self._conflict_files(cwd) or files
        _run_git(["merge", "--abort"], cwd=cwd)
        return False, files, (merge.stderr or merge.stdout).strip()

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

    def deliver_branch(
        self,
        ticket_id: str,
        title: str,
        cwd: Optional[Path] = None,
        base: str = "main",
        gh_runner: Optional[GhRunner] = None,
        merge: bool = True,
    ) -> DeliveryReport:
        """Commit, push, open PR, merge (squash) and clean up a ticket branch.

        With merge=False (commercial acceptance required) the PR is opened but
        neither merged nor cleaned up.
        """
        target = cwd or self.root
        runner = gh_runner or _run_gh
        try:
            return self._deliver(ticket_id, title, target, base, runner, merge)
        except Exception as exc:  # fail-closed report instead of crashing the pipeline
            logger.exception("deliver_branch failed for %s", ticket_id)
            return DeliveryReport(ok=False, action="error", message=str(exc))

    def _deliver(
        self, ticket_id: str, title: str, cwd: Path, base: str, runner: GhRunner, merge: bool
    ) -> DeliveryReport:
        # (a) commit if dirty
        if self.is_dirty(cwd=cwd):
            self.commit_ticket(ticket_id, title, cwd=cwd)

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
        # Local base is only realigned when fully contained in the branch (nothing is lost).
        if _run_git(["merge-base", "--is-ancestor", base, "HEAD"], cwd=cwd).returncode == 0:
            _run_git(["branch", "-f", base, remote_ref], cwd=cwd)

        ok, files, msg = self._sync_with_base(base, cwd)
        if not ok:
            return DeliveryReport(
                ok=False,
                action="conflict",
                branch=branch,
                conflict_files=files,
                message=f"Conflict with {remote_ref}: {', '.join(files) or msg}",
            )

        ahead = self._git_out(["rev-list", "--count", f"{remote_ref}..HEAD"], cwd)
        if ahead == "0":
            cleaned = self._cleanup(branch, base, cwd) if merge else False
            return DeliveryReport(
                ok=True, action="no_changes", branch=branch, cleaned=cleaned,
                message="Branch has no commits beyond base.",
            )

        # (d) push
        push = _run_git(["push", "--force-with-lease", "-u", "origin", branch], cwd=cwd, timeout_s=180.0)
        if push.returncode != 0:
            return DeliveryReport(
                ok=False, action="error", branch=branch, message=f"push failed: {push.stderr.strip()}"
            )

        # (e) PR (reuse existing)
        existing = self._find_open_pr(branch, cwd, runner)
        if existing:
            number, pr_url = existing
        else:
            body = f"Entrega autonoma do ticket {ticket_id}.\n\nDarkFac-Ticket: {ticket_id}"
            pr = runner(
                [
                    "pr", "create", "--base", base, "--head", branch,
                    "--title", f"{title.strip()} [{ticket_id}]", "--body", body,
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

        if not merge:
            return DeliveryReport(
                ok=True,
                action="awaiting_commercial_acceptance",
                pr_url=pr_url,
                branch=branch,
                message="PR opened; merge and cleanup withheld pending commercial acceptance.",
            )

        # (f) merge, retrying once after re-sync when not mergeable
        merge_args = ["pr", "merge", str(number), "--squash", "--delete-branch"]
        merge_res = runner(merge_args, cwd)
        state, merge_sha = self._pr_state(number, cwd, runner)
        if state != "MERGED" and merge_res.returncode != 0:
            err = f"{merge_res.stderr}{merge_res.stdout}".lower()
            if "not mergeable" in err or "conflict" in err or "out of date" in err:
                _run_git(["fetch", "origin"], cwd=cwd)
                ok, files, msg = self._sync_with_base(base, cwd)
                if not ok:
                    return DeliveryReport(
                        ok=False, action="conflict", pr_url=pr_url, branch=branch, conflict_files=files,
                        message=f"Conflict on retry: {', '.join(files) or msg}",
                    )
                _run_git(["push", "--force-with-lease", "origin", branch], cwd=cwd, timeout_s=180.0)
                merge_res = runner(merge_args, cwd)
                state, merge_sha = self._pr_state(number, cwd, runner)
        if state != "MERGED":
            return DeliveryReport(
                ok=False, action="error", pr_url=pr_url, branch=branch,
                message=f"gh pr merge failed: {(merge_res.stderr or merge_res.stdout).strip()}",
            )

        # (g) confirm merge sha reached origin/base
        _run_git(["fetch", "origin"], cwd=cwd)
        if not merge_sha or _run_git(["merge-base", "--is-ancestor", merge_sha, remote_ref], cwd=cwd).returncode != 0:
            return DeliveryReport(
                ok=False, action="error", pr_url=pr_url, branch=branch, merge_sha=merge_sha,
                message=f"merge commit {merge_sha} not found in {remote_ref}",
            )

        # (h) cleanup
        cleaned = self._cleanup(branch, base, cwd)
        return DeliveryReport(
            ok=True, action="merged", pr_url=pr_url, merge_sha=merge_sha, cleaned=cleaned, branch=branch,
            message=f"PR {pr_url} merged into {base}.",
        )

    def _cleanup(self, branch: str, base: str, cwd: Path) -> bool:
        """Prune, fast-forward the main checkout, delete local branch, remove secondary worktree."""
        main_root, is_secondary = self._main_root(cwd)
        _run_git(["fetch", "--prune", "origin"], cwd=main_root)
        ok = True
        if is_secondary:
            rm = _run_git(["worktree", "remove", "--force", str(cwd)], cwd=main_root)
            if rm.returncode != 0:
                logger.warning("worktree remove failed: %s", rm.stderr.strip())
                ok = False
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

        wt_roots = [(main_root / ".worktrees").resolve(), (main_root / ".claude" / "worktrees").resolve()]
        porcelain = self._git_out(["worktree", "list", "--porcelain"], main_root)
        entries: list[dict[str, str]] = []
        for block in porcelain.replace("\r\n", "\n").split("\n\n"):
            entry: dict[str, str] = {}
            for line in block.splitlines():
                key, _, val = line.partition(" ")
                entry[key] = val
            if entry.get("worktree"):
                entries.append(entry)
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
            if self.is_dirty(cwd=path):
                report.skipped.append(f"worktree:{path} (dirty)")
                continue
            rm = _run_git(["worktree", "remove", "--force", str(path)], cwd=main_root)
            if rm.returncode == 0:
                report.removed_worktrees.append(str(path))
                occupied.discard(br)
            else:
                report.skipped.append(f"worktree:{path} ({rm.stderr.strip()})")

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

        # 1. Update ticket in DemandsStore
        ticket.status = DeliveryStatus.COMPLETED
        ticket.updated_at = datetime.now(timezone.utc)
        store.save_ticket(ticket)
        logger.info("Marked ticket %s as completed in demands store.", ticket_id)

        # 2. Atomic commit if requested
        commit_sha: Optional[str] = None
        if auto_commit:
            commit_sha = self.commit_ticket(
                ticket_id=ticket.id,
                title=ticket.title,
                scope="core" if not ticket.tags else ticket.tags[0].replace("user-", ""),
                cwd=target_dir,
                custom_message=custom_message,
            )
        else:
            commit_sha = self.get_current_sha(cwd=target_dir)

        # 3. Remote sync if requested
        sync_result: Optional[GitSyncResult] = None
        delivery: Optional[DeliveryReport] = None
        if auto_push:
            if self.gh_ready(cwd=target_dir, gh_runner=gh_runner):
                needs_acceptance = self._requires_commercial_acceptance(ticket.project_id)
                delivery = self.deliver_branch(
                    ticket.id, ticket.title, cwd=target_dir, gh_runner=gh_runner, merge=not needs_acceptance
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

        return TicketCompletionReport(
            ok=True,
            ticket_id=ticket.id,
            commit_sha=commit_sha,
            sync_result=sync_result,
            delivery=delivery,
            message=f"Ticket {ticket.id} successfully completed and recorded.",
        )


def main() -> int:
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
    deliver_parser.add_argument("--no-merge", action="store_true", help="Open PR only (commercial acceptance)")

    subparsers.add_parser("sweep", help="Remove merged ticket/df branches and stale worktrees")

    args = parser.parse_args()
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
        rep = manager.deliver_branch(args.ticket_id, args.title, base=args.base, merge=not args.no_merge)
        print(f"[{rep.action.upper()}] {rep.message} {rep.pr_url or ''}".rstrip())
        return 0 if rep.ok else 1

    if args.command == "sweep":
        sw = manager.sweep_stale()
        print(f"[SWEEP] branches={sw.removed_branches} worktrees={sw.removed_worktrees} skipped={len(sw.skipped)}")
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
