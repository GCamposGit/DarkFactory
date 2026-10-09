#!/usr/bin/env python3
"""Official gate on an immutable candidate in an isolated worktree (USR-175).

Use this instead of running ``core/harness/runner.py --quick`` by hand in the shared checkout::

    python C:\\dev\\DarkFac\\scripts\\gate_isolated.py                  # candidate = current HEAD
    python C:\\dev\\DarkFac\\scripts\\gate_isolated.py --ref <sha|branch>

It pins a detached worktree to the candidate SHA, runs the gate there and removes the worktree.
Concurrent updates to ``main`` (or any other checkout) cannot change the candidate. Exit codes:
0 pass; 2 setup error; 3 candidate moved or dirtied during the run; otherwise the gate's own code.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.git import gate_isolation  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the official gate on an immutable candidate (isolated worktree)")
    parser.add_argument("--ref", default="HEAD", help="Candidate commit-ish (sha or branch); default HEAD")
    parser.add_argument("--timeout", type=float, default=None, help="Gate timeout in seconds (default: none)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    result = gate_isolation.run_isolated_gate(cwd=Path.cwd(), ref=args.ref, timeout_s=args.timeout)
    print(gate_isolation.format_summary(result))
    return result.returncode


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
