#!/usr/bin/env python3
"""USR-300 / USR-244: classify a change set as ``light`` (data/docs only) or ``heavy``.

Only an explicit allowlist is light; everything else (code, tests, workflows, ``.factory/config/**``,
``.factory/holdout/**``, governance markdown) is heavy and runs the full CI. An empty change set is
heavy (fail safe).

Usage:
  python scripts/ci_paths.py --base <sha> --head <sha>   # reads ``git diff --name-only --no-renames``
  python scripts/ci_paths.py --stdin                     # one path per line
Prints ``heavy=true|false`` (also appended to ``$GITHUB_OUTPUT`` when set).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import PurePosixPath

LIGHT_PREFIXES = (".factory/demands/", ".factory/owner_actions/", "docs/")
GOVERNANCE_MD = frozenset({"agents.md", "claude.md", "factory_rules.md", "mission.md"})


def is_light(path: str) -> bool:
    norm = path.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    if not norm or ".." in PurePosixPath(norm).parts:
        return False
    if PurePosixPath(norm).name.casefold() in GOVERNANCE_MD:
        return False
    if norm.startswith(LIGHT_PREFIXES):
        return True
    # root-level markdown only (nested *.md may be consumed by skills/tests)
    return "/" not in norm and norm.casefold().endswith(".md")


def is_heavy(paths: list[str]) -> bool:
    paths = [p for p in paths if p.strip()]
    return not paths or any(not is_light(p) for p in paths)


def _git_paths(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.stdin:
            paths = sys.stdin.read().splitlines()
        elif args.base and args.head:
            paths = _git_paths(args.base, args.head)
        else:
            parser.error("use --stdin or --base and --head")
            return 2
    except subprocess.CalledProcessError:
        paths = []  # cannot diff -> heavy
    line = f"heavy={'true' if is_heavy(paths) else 'false'}"
    print(line)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
