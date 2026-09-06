"""Diagnostic CLI backed by the same roadmap service used by the Hub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.store import RoadmapUnavailableError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect the DarkFac operational roadmap.")
    parser.add_argument("command", choices=("snapshot", "health"), nargs="?", default="snapshot")
    parser.add_argument("--project", default="darkfac")
    parser.add_argument("--search", default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = build_repository_roadmap_service(Path(__file__).resolve().parents[2])
    try:
        payload = (
            service.get_health(args.project)
            if args.command == "health"
            else service.get_snapshot(args.project, search=args.search)
        )
    except (KeyError, RoadmapUnavailableError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2 if args.pretty else None,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
