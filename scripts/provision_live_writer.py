#!/usr/bin/env python3
"""Thin launcher for ``core.line.live_writer`` (USR-164): provisions the restricted writer role.

Run it from any directory::

    python C:\\dev\\DarkFac\\scripts\\provision_live_writer.py --print-sql
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.line.live_writer import main  # noqa: E402

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
