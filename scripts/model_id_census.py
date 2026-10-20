"""Census of hand-written model ids outside an allowlist (USR-185, slice 1).

Counts occurrences of literal model ids (claude-*, gpt-*, grok-*, gemini-*, deepseek-*) in tracked files outside
the registry, fixtures, docs/proposals and tests. Informational only: enforcement is a later slice.

Usage: python scripts/model_id_census.py [--verbose]
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MODEL_ID_RE = re.compile(r"\b(?:claude|gpt|grok|gemini|deepseek)-[a-z0-9][a-z0-9.\-]*[a-z0-9]", re.IGNORECASE)
SCAN_SUFFIXES = {".py", ".json", ".md", ".yaml", ".yml", ".toml", ".txt", ".ps1", ".html", ".js", ".ts"}
ALLOWLIST_PREFIXES: tuple[str, ...] = (
    ".factory/config/model_registry.json",
    "core/models/registry.py",
    "scripts/model_id_census.py",
    "docs/proposals/",
    "docs/plans/",
    "tests/",
    "core/benchmarks/data/",
)


def is_allowlisted(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    return norm.startswith(ALLOWLIST_PREFIXES) or "/fixtures/" in norm


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files"], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def census(root: Path = REPO_ROOT) -> tuple[int, dict[str, int]]:
    """Return (total occurrences outside the allowlist, per-file counts)."""
    per_file: dict[str, int] = {}
    for rel in tracked_files(root):
        if is_allowlisted(rel) or Path(rel).suffix.lower() not in SCAN_SUFFIXES:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        count = len(MODEL_ID_RE.findall(text))
        if count:
            per_file[rel] = count
    return sum(per_file.values()), per_file


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    total, per_file = census()
    if "--verbose" in argv:
        for rel, count in sorted(per_file.items(), key=lambda kv: -kv[1]):
            print(f"{count:5d}  {rel}")
    print(f"literal_model_ids_outside_allowlist={total} files={len(per_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
