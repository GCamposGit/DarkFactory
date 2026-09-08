"""Diagnostic CLI backed by the same roadmap service used by the Hub."""

from __future__ import annotations

import argparse
import json
import sys

from core.paths import project_root
from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.store import RoadmapUnavailableError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect the DarkFac operational roadmap.")
    parser.add_argument(
        "command",
        choices=("snapshot", "health", "history", "compare"),
        nargs="?",
        default="snapshot",
    )
    parser.add_argument("--project", default="darkfac")
    parser.add_argument("--search", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--from-snapshot", dest="from_snapshot", default=None)
    parser.add_argument("--to-snapshot", dest="to_snapshot", default=None)
    parser.add_argument("--pretty", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    service = build_repository_roadmap_service(project_root())
    try:
        if args.command == "health":
            payload = service.get_health(args.project)
        elif args.command == "history":
            payload = service.get_history(args.project, limit=args.limit)
        elif args.command == "compare":
            if not args.from_snapshot or not args.to_snapshot:
                parser.error("compare requires --from-snapshot and --to-snapshot")
            payload = service.compare_snapshots(
                args.project,
                args.from_snapshot,
                args.to_snapshot,
            )
        else:
            payload = service.get_snapshot(args.project, search=args.search)
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
