#!/usr/bin/env python3
"""DarkHub coverage diagnosis (USR-42).

Shows how much of the factory is reflected in the DarkHub and what is missing.
The same gate runs in CI through tests/test_hub_coverage.py.

    python scripts/hub_coverage.py            # summary + problems (exit 1 on problems)
    python scripts/hub_coverage.py --pending  # pending capabilities grouped by DH-xx
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from hub.backend.coverage import evaluate_repository, load_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pending", action="store_true", help="list pending capabilities per roadmap item")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    report = evaluate_repository()
    print(
        f"DarkHub coverage: {report.coverage_ratio:.0%} of owner-facing capabilities have a surface "
        f"(surfaced={report.surfaced}, pending={report.pending}, waived={report.waived})"
    )

    if args.pending:
        manifest = load_manifest()
        grouped: dict[str, list[str]] = defaultdict(list)
        for kind, entries in (("api", manifest.api_routes), ("core", manifest.core_modules)):
            for key, entry in entries.items():
                if entry.pending:
                    grouped[entry.pending].append(f"[{kind}] {key}")
        for roadmap_id in sorted(grouped):
            print(f"\n{roadmap_id} ({len(grouped[roadmap_id])})")
            for item in sorted(grouped[roadmap_id]):
                print(f"  - {item}")

    if report.problems:
        print("\n[HUB_COVERAGE_FAIL]")
        for problem in report.problems:
            print(f"  {problem}")
        return 1
    print("[HUB_COVERAGE_PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
