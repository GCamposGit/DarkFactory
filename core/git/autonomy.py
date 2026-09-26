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
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Optional, Sequence

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


class TicketCompletionReport(BaseModel):
    """Normalized outcome of completing a ticket through autonomous Git lifecycle."""

    ok: bool
    ticket_id: str
    commit_sha: Optional[str] = None
    sync_result: Optional[GitSyncResult] = None
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

    def complete_ticket(
        self,
        ticket_id: str,
        cwd: Optional[Path] = None,
        auto_push: bool = True,
        auto_commit: bool = True,
        custom_message: Optional[str] = None,
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
        if auto_push:
            sync_result = self.sync_environment(cwd=target_dir, allow_push=True)

        return TicketCompletionReport(
            ok=True,
            ticket_id=ticket.id,
            commit_sha=commit_sha,
            sync_result=sync_result,
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
