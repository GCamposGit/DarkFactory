"""DarkFac Git Autonomy and Environment Synchronization package.

The public names are resolved lazily (PEP 562) so that importing a light helper
such as ``core.git.ci_checks`` does not drag in the whole autonomy engine, and so
that ``python -m core.git.autonomy`` does not hit the runpy double-import warning.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.git.autonomy import GitAutonomyManager, GitSyncResult, TicketCompletionReport
    from core.git.ticket_workspace import TicketWorkspace

# public name -> module that defines it
_LAZY_EXPORTS: dict[str, str] = {
    "GitAutonomyManager": "core.git.autonomy",
    "GitSyncResult": "core.git.autonomy",
    "TicketCompletionReport": "core.git.autonomy",
    "TicketWorkspace": "core.git.ticket_workspace",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is not None:
        return getattr(importlib.import_module(module_name), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
