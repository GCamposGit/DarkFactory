#!/usr/bin/env python3
r"""Thin launcher for ``core.owner_actions.cli`` (USR-190): the owner action backlog shown in the DarkHub.

Run it from any directory::

    python C:\dev\DarkFac\scripts\owner_action.py list
    python C:\dev\DarkFac\scripts\owner_action.py add --title "Titulo" --priority high --why "Motivo" --step "Passo 1"
    python C:\dev\DarkFac\scripts\owner_action.py done OA-001
    python C:\dev\DarkFac\scripts\owner_action.py answer OA-005 --option A
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.owner_actions.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
