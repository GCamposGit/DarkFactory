"""DarkFac Git Autonomy and Environment Synchronization package.

The public names are resolved lazily (PEP 562) so that importing a light helper
such as ``core.git.ci_checks`` does not drag in the whole autonomy engine, and so
that ``python -m core.git.autonomy`` does not hit the runpy double-import warning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.git.autonomy import GitAutonomyManager, GitSyncResult, TicketCompletionReport

__all__ = ["GitAutonomyManager", "GitSyncResult", "TicketCompletionReport"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from core.git import autonomy

        return getattr(autonomy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
