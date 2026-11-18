"""Safe worktree cleanup after a ticket reaches a terminal status (USR-500 T2)."""

from __future__ import annotations

import logging
from pathlib import Path

from core.git import ticket_workspace
from core.git.ticket_workspace import CleanupResult, NowFn, SleepFn

logger = logging.getLogger("darkfac.git.ticket_lifecycle")


def cleanup_terminal_ticket_worktree(
    ticket_id: str,
    *,
    root: Path,
    sleep_fn: SleepFn | None = None,
    now_fn: NowFn | None = None,
) -> CleanupResult | None:
    """Clean the latest worktree for a terminal ticket, preserving and reporting any block.

    All removal decisions stay in :func:`ticket_workspace.cleanup`, which rechecks the Git
    registration, local changes, locks, and live process holders before each destructive step.
    This function never changes ticket status: a blocked cleanup leaves the terminal event intact.
    """
    try:
        main_root = ticket_workspace.main_checkout_root(Path(root))
        worktree = ticket_workspace.find_ticket_worktree(ticket_id, main_root)
    except Exception as exc:  # noqa: BLE001 - cleanup diagnostics must not undo the terminal event
        logger.warning(
            "ticket %s is terminal; worktree cleanup could not locate its checkout: %s: %s",
            ticket_id,
            type(exc).__name__,
            exc,
        )
        return None

    if worktree is None:
        return None

    try:
        result = ticket_workspace.cleanup(
            worktree,
            main_root,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
    except Exception as exc:  # noqa: BLE001 - cleanup failure must not undo the terminal event
        logger.warning(
            "ticket %s is terminal; worktree cleanup failed unexpectedly and was preserved at %s: %s: %s",
            ticket_id,
            worktree,
            type(exc).__name__,
            exc,
        )
        return None
    if not result.removed:
        logger.warning(
            "ticket %s remains terminal; worktree preserved at %s: outcome=%s reason=%s detail=%s",
            ticket_id,
            worktree,
            result.outcome,
            result.reason,
            result.detail or "; ".join(result.errors),
        )
    elif result.method == "trash":
        logger.warning(
            "ticket %s worktree could not be deleted; moved to recoverable trash %s",
            ticket_id,
            result.trash_path,
        )
    return result
