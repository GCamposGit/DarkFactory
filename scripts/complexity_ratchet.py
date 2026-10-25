"""Complexity ratchet (USR-377): function size may not get worse.

Measures every function in ``core/`` and ``hub/backend/`` (lines of code) and
compares with the versioned baseline ``.factory/config/complexity_baseline.json``.

Fails when:
  * a new function is longer than ``LIMIT`` lines, or
  * a function already above ``LIMIT`` (or recorded in the baseline) grows.

``--update`` rewrites the baseline: only functions above ``LIMIT`` are recorded,
and recorded sizes can only shrink (growth still fails, even with --update).
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

LIMIT = 150
ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("core", "hub/backend")
BASELINE = ROOT / ".factory" / "config" / "complexity_baseline.json"


def measure_source(source: str) -> dict[str, int]:
    """Return {qualified_name: line_count} for each function in the source."""
    tree = ast.parse(source)
    result: dict[str, int] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                end = child.end_lineno or child.lineno
                result[name] = end - child.lineno + 1
                visit(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return result


def measure_tree(root: Path = ROOT) -> dict[str, int]:
    """Measure every .py under SCAN_DIRS; keys are 'relpath::qualname'."""
    sizes: dict[str, int] = {}
    for rel in SCAN_DIRS:
        for path in sorted((root / rel).rglob("*.py")):
            try:
                funcs = measure_source(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel_path = path.relative_to(root).as_posix()
            for name, size in funcs.items():
                sizes[f"{rel_path}::{name}"] = size
    return sizes


def compare(current: dict[str, int], baseline: dict[str, int], limit: int = LIMIT) -> list[str]:
    """Return violation messages (empty means ratchet holds)."""
    problems: list[str] = []
    for key, size in sorted(current.items()):
        allowed = baseline.get(key)
        if allowed is None:
            if size > limit:
                problems.append(f"NEW {key}: {size} lines > {limit}")
        elif size > allowed:
            problems.append(f"GREW {key}: {size} lines > baseline {allowed}")
    return problems


def build_baseline(current: dict[str, int], baseline: dict[str, int], limit: int = LIMIT) -> dict[str, int]:
    """Only functions above the limit; sizes never increase past the old baseline."""
    new: dict[str, int] = {}
    for key, size in current.items():
        if size > limit:
            new[key] = min(size, baseline.get(key, size))
    return dict(sorted(new.items()))


def load_baseline(path: Path = BASELINE) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.get("functions", {}).items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", help="rewrite the baseline (shrink only)")
    args = parser.parse_args(argv)

    current = measure_tree()
    baseline = load_baseline()
    bootstrap = not BASELINE.exists()
    problems = [] if bootstrap else compare(current, baseline)
    if problems:
        print("Complexity ratchet FAILED:")
        for line in problems:
            print(f"  {line}")
        return 1
    if args.update or bootstrap:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"limit": LIMIT, "functions": build_baseline(current, baseline)}
        BASELINE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Baseline written: {len(payload['functions'])} functions above {LIMIT} lines.")
        return 0
    print(f"Complexity ratchet OK ({len(current)} functions, {len(baseline)} in baseline).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
