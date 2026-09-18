"""Headless CLI interface for Cross-Project Reusable Catalog (HF-25)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.catalog.manager import CrossProjectCatalogManager
from core.catalog.models import ComponentKind


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DarkFac Cross-Project Reusable Catalog CLI (HF-25)",
        prog="python core/catalog/cli.py",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = subparsers.add_parser("list", help="List all reusable components in catalog")
    list_p.add_argument("--kind", choices=[k.value for k in ComponentKind], help="Filter by component kind")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON format")

    # inspect
    inspect_p = subparsers.add_parser("inspect", help="Inspect a specific component manifest")
    inspect_p.add_argument("--id", required=True, help="Component ID to inspect")
    inspect_p.add_argument("--json", action="store_true", help="Output raw JSON format")

    # sync
    sync_p = subparsers.add_parser("sync", help="Synchronize a component into a target project")
    sync_p.add_argument("--id", required=True, help="Component ID to sync")
    sync_p.add_argument("--target-project", required=True, help="Target project ID from registry")
    sync_p.add_argument("--no-overwrite", action="store_true", help="Do not overwrite existing files")

    # export
    export_p = subparsers.add_parser("export", help="Package files from a project into catalog")
    export_p.add_argument("--source", required=True, help="Source project ID")
    export_p.add_argument("--id", required=True, help="New component unique slug ID")
    export_p.add_argument("--name", required=True, help="Component title")
    export_p.add_argument("--kind", required=True, choices=[k.value for k in ComponentKind])
    export_p.add_argument("--files", required=True, nargs="+", help="Relative file paths to include")
    export_p.add_argument("--description", required=True, help="Functional summary")

    args = parser.parse_args(argv)
    manager = CrossProjectCatalogManager()

    if args.command == "list":
        kind = ComponentKind(args.kind) if args.kind else None
        components = manager.list_components(kind=kind)
        if getattr(args, "json", False):
            print(json.dumps([c.model_dump(mode="json") for c in components], indent=2))
        else:
            print(f"=== Reusable Component Catalog ({len(components)} items) ===")
            for c in components:
                print(f"• [{c.kind.value}] {c.id} (v{c.version}) — Source: {c.source_project_id}")
                print(f"  Description: {c.description}")
                print(f"  Files: {', '.join(f.path for f in c.files)}")
        return 0

    if args.command == "inspect":
        comp = manager.get_component(args.id)
        if not comp:
            print(f"Error: Component '{args.id}' not found.", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(comp.model_dump_json(indent=2))
        else:
            print(f"=== Component: {comp.name} ({comp.id}) ===")
            print(f"Version     : {comp.version}")
            print(f"Kind        : {comp.kind.value}")
            print(f"Source      : {comp.source_project_id}")
            print(f"Archetypes  : {', '.join(comp.compatible_archetypes) or 'any'}")
            print(f"Description : {comp.description}")
            print(f"Files ({len(comp.files)}):")
            for f in comp.files:
                print(f"  - {f.path} (SHA: {f.sha256[:12]}...)")
        return 0

    if args.command == "sync":
        try:
            res = manager.sync_to_project(
                component_id=args.id,
                target_project_id=args.target_project,
                overwrite=not args.no_overwrite,
            )
            print(f"Sync complete: {res.message}")
            for f in res.files_synced:
                print(f"  [+] {f}")
            for s in res.files_skipped:
                print(f"  [skip] {s}")
            return 0
        except Exception as exc:
            print(f"Sync error: {exc}", file=sys.stderr)
            return 2

    if args.command == "export":
        try:
            desc = manager.export_from_project(
                source_project_id=args.source,
                component_id=args.id,
                name=args.name,
                kind=args.kind,
                file_paths=args.files,
                description=args.description,
            )
            print(f"Exported component '{desc.id}' ({len(desc.files)} files) to catalog.")
            return 0
        except Exception as exc:
            print(f"Export error: {exc}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
