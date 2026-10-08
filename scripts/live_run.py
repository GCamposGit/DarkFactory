#!/usr/bin/env python3
"""Thin launcher for ``core.line.live_run`` (USR-164): live-board progress for interactive sessions.

Run it from any directory (the session never fails because of it)::

    python C:\\dev\\DarkFac\\scripts\\live_run.py open --ticket USR-XX --title "Titulo"
    python C:\\dev\\DarkFac\\scripts\\live_run.py phase agent --ticket USR-XX --message "subagente implementando"
    python C:\\dev\\DarkFac\\scripts\\live_run.py finish --ticket USR-XX
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.line.live_run import main  # noqa: E402

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
