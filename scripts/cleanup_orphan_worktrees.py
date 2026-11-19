"""CLI launcher for ``core.git.orphan_worktree_cleanup`` (USR-500 T3)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.git.orphan_worktree_cleanup import (  # noqa: E402
    CleanupSummary,
    SCHEDULE_STATE,
    SWEEP_INTERVAL,
    main,
    maybe_run_scheduled,
    sweep_orphan_worktrees,
)

__all__ = [
    "CleanupSummary",
    "REPO_ROOT",
    "SCHEDULE_STATE",
    "SWEEP_INTERVAL",
    "main",
    "maybe_run_scheduled",
    "sweep_orphan_worktrees",
]

if __name__ == "__main__":
    raise SystemExit(main())
