#!/usr/bin/env python3
"""Thin launcher for ``core.line.live_progress_doctor`` (USR-455): read-only sink diagnostic.

Run it from any directory::

    python C:\\dev\\DarkFac\\scripts\\live_progress_doctor.py
    python C:\\dev\\DarkFac\\scripts\\live_progress_doctor.py --json
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.line.live_progress_doctor import main  # noqa: E402

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
