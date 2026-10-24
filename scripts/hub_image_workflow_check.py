#!/usr/bin/env python3
"""Check that the GHCR image workflow paths exactly match Hub runtime COPY sources."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.infra.deploy_classifier import runtime_image_paths  # noqa: E402


def workflow_paths(path: Path) -> set[str]:
    """Read the push.paths list from a GitHub Actions workflow without PyYAML."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)paths:\s*(?:#.*)?$", line)
        if not match:
            continue
        indent = len(match.group(1))
        result: set[str] = set()
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                continue
            candidate_indent = len(candidate) - len(candidate.lstrip())
            if candidate_indent <= indent:
                break
            item = re.match(r"^\s*-\s*(['\"]?)(.*?)\1\s*(?:#.*)?$", candidate)
            if item:
                result.add(item.group(2))
        return result
    raise ValueError(f"workflow has no paths: trigger: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workflow",
        type=Path,
        default=ROOT / ".github" / "workflows" / "hub-image.yml",
        help="workflow YAML to compare (default: .github/workflows/hub-image.yml)",
    )
    args = parser.parse_args()
    try:
        configured = workflow_paths(args.workflow)
    except (OSError, ValueError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    runtime = set(runtime_image_paths())
    extra = sorted(configured - runtime)
    missing = sorted(runtime - configured)
    print("Sobram no workflow: " + (", ".join(extra) if extra else "nenhum"))
    print("Faltam no workflow: " + (", ".join(missing) if missing else "nenhum"))
    if extra or missing:
        return 1
    print("Trigger do workflow alinhado com os caminhos runtime do Dockerfile.hub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
