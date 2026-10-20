"""Regenerate registry-derived doc sections (USR-185, slice 4).

Usage: python scripts/generate_model_docs.py [--check]
Exit 1 with --check when any doc diverges from .factory/config/model_registry.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models.docgen import has_problems, process  # noqa: E402


def main(argv: list[str]) -> int:
    check = "--check" in argv
    results = process(write=not check)
    for r in results:
        status = "DIVERGE" if r.changed else "ok"
        extra = f" missing={list(r.missing_sections)}" if r.missing_sections else ""
        extra += f" unknown={list(r.unknown_sections)}" if r.unknown_sections else ""
        print(f"{status}: {r.path}{extra}")
    if check:
        return 1 if has_problems(results) else 0
    return 1 if any(r.missing_sections or r.unknown_sections for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
