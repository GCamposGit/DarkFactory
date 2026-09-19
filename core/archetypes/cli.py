"""Command-line interface for Dark Factory Archetypes subsystem (HF-20)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from .models import ScaffoldRequest
    from .registry import get_registry
    from .scaffolder import ArchetypeScaffolder
except ImportError:
    from core.archetypes.models import ScaffoldRequest
    from core.archetypes.registry import get_registry
    from core.archetypes.scaffolder import ArchetypeScaffolder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.archetypes.cli",
        description="Dark Factory Project Archetypes & One-Shot Scaffolding (HF-20)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: list
    list_parser = subparsers.add_parser("list", help="List available project archetypes")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # Subcommand: inspect
    inspect_parser = subparsers.add_parser("inspect", help="Inspect an archetype specification")
    inspect_parser.add_argument("archetype_id", help="ID of the archetype to inspect")

    # Subcommand: init
    init_parser = subparsers.add_parser("init", help="Scaffold a new project from an archetype")
    init_parser.add_argument("archetype_id", help="ID of the archetype to instantiate")
    init_parser.add_argument("target_dir", help="Target directory for the new project")
    init_parser.add_argument("--name", required=True, help="Project name")
    init_parser.add_argument("--author", default="Executive Leader", help="Author name")
    init_parser.add_argument("--title", default="Executive Portfolio & Showcase", help="Author title")
    init_parser.add_argument("--bio", default="Strategy, Investments and AI Transformation", help="Author bio")
    init_parser.add_argument("--domain", default="example.com", help="Primary domain")
    init_parser.add_argument("--deploy", default="hostinger_ftp", help="Deployment target")

    args = parser.parse_args(argv)
    registry = get_registry()
    scaffolder = ArchetypeScaffolder(registry)

    if args.command == "list":
        archetypes = registry.list_archetypes()
        if getattr(args, "json", False):
            print(json.dumps([item.model_dump() for item in archetypes], indent=2))
        else:
            print("\n  DARK FACTORY: AVAILABLE ARCHETYPES (HF-20)\n" + "=" * 60)
            for item in archetypes:
                print(f"  * {item.id:<20} | {item.title} (v{item.version})")
                print(f"    Stack: {item.stack.framework} / {item.stack.styling} / {item.stack.runtime}")
                print(f"    Deploy: {', '.join(item.stack.deployment_targets)}")
                print(f"    Desc: {item.description}\n")
        return 0

    elif args.command == "inspect":
        item = registry.get_archetype(args.archetype_id)
        if not item:
            print(f"Error: Archetype '{args.archetype_id}' not found.", file=sys.stderr)
            return 1
        print(json.dumps(item.model_dump(), indent=2))
        return 0

    elif args.command == "init":
        req = ScaffoldRequest(
            archetype_id=args.archetype_id,
            project_name=args.name,
            target_dir=args.target_dir,
            author_name=args.author,
            author_title=args.title,
            author_bio=args.bio,
            domain=args.domain,
            deploy_target=args.deploy,
        )
        result = scaffolder.scaffold(req)
        if not result.success:
            print(f"Scaffolding failed: {result.error_message}", file=sys.stderr)
            return 1

        print(f"\n[OK] Successfully scaffolded '{result.project_name}' from archetype '{result.archetype_id}'!")
        print(f"  Target: {result.target_dir}")
        print(f"  Files created: {len(result.files_created)}")
        print("\nNext steps:")
        for step in result.next_steps:
            print(f"  -> {step}")
        print()
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
