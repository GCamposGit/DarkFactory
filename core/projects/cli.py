"""Headless CLI interface for Dark Factory Project Registry (HF-20/HF-25)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.projects.models import ProjectDescriptor, ProjectKind
from core.projects.registry import get_project_registry


def _merge_descriptor(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge explicitly supplied fields; lists and null replace their prior values."""
    merged = dict(base)
    for key, value in update.items():
        previous = merged.get(key)
        merged[key] = (
            _merge_descriptor(previous, value)
            if isinstance(previous, dict) and isinstance(value, dict)
            else value
        )
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dark Factory Project Registry CLI",
        prog="python core/projects/cli.py",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    list_p = subparsers.add_parser("list", help="List all registered projects")
    list_p.add_argument("--json", action="store_true", help="Output raw JSON format")

    # get
    get_p = subparsers.add_parser("get", help="Get details for a specific project")
    get_p.add_argument("--id", required=True, help="Project unique identifier")
    get_p.add_argument("--json", action="store_true", help="Output raw JSON format")

    # register
    reg_p = subparsers.add_parser("register", help="Register or update a managed project")
    reg_p.add_argument("--descriptor-file", type=Path, help="Complete or partial project descriptor JSON")
    reg_p.add_argument("--id", default=argparse.SUPPRESS, help="Project unique slug ID")
    reg_p.add_argument("--name", default=argparse.SUPPRESS, help="Human-readable project title")
    reg_p.add_argument("--path", default=argparse.SUPPRESS, help="Absolute path to project directory")
    reg_p.add_argument("--kind", default=argparse.SUPPRESS, choices=[k.value for k in ProjectKind], help="Project archetype/kind")
    reg_p.add_argument("--prefix", default=argparse.SUPPRESS, help="Ticket prefix (e.g. SIT, JRV, DF)")
    reg_p.add_argument("--domain", default=argparse.SUPPRESS, help="Production URL or local domain")
    reg_p.add_argument("--deploy-target", default=argparse.SUPPRESS, help="Deployment method (e.g. dokploy_docker, hostinger_ftp)")
    reg_p.add_argument("--description", default=argparse.SUPPRESS, help="Brief functional summary")

    args = parser.parse_args(argv)
    registry = get_project_registry()

    if args.command == "list":
        projects = registry.list_projects()
        if getattr(args, "json", False):
            print(json.dumps([p.model_dump(mode="json", by_alias=True) for p in projects], indent=2))
        else:
            print(f"=== Registered Projects ({len(projects)}) ===")
            for p in projects:
                kind_val = p.kind.value if hasattr(p.kind, "value") else str(p.kind)
                print(f"[{p.prefix or 'USR'}] {p.id}: {p.name} ({kind_val})")
                print(f"  Path  : {p.path}")
                print(f"  Domain: {p.domain or 'none'} | Deploy: {p.deploy_target or 'none'}")
        return 0

    if args.command == "get":
        proj = registry.get_project(args.id)
        if not proj:
            print(f"Error: Project '{args.id}' not found.", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(proj.model_dump_json(indent=2, by_alias=True))
        else:
            kind_val = proj.kind.value if hasattr(proj.kind, "value") else str(proj.kind)
            print(f"=== Project: {proj.name} ({proj.id}) ===")
            print(f"Kind         : {kind_val}")
            print(f"Prefix       : {proj.prefix}")
            print(f"Path         : {proj.path}")
            print(f"Domain       : {proj.domain or 'none'}")
            print(f"Deploy Target: {proj.deploy_target or 'none'}")
            print(f"Description  : {proj.description}")
        return 0

    if args.command == "register":
        try:
            imported: dict[str, Any] = {}
            if args.descriptor_file is not None:
                imported = json.loads(args.descriptor_file.read_text(encoding="utf-8"))
                if not isinstance(imported, dict):
                    raise ValueError("descriptor must be an object")
            explicit = {
                key: getattr(args, key)
                for key in ("id", "name", "path", "kind", "prefix", "domain", "deploy_target", "description")
                if hasattr(args, key)
            }
            if "id" in explicit and "id" in imported and explicit["id"] != imported["id"]:
                raise ValueError("conflicting project IDs")
            project_id = explicit.get("id", imported.get("id"))
            if not isinstance(project_id, str):
                raise ValueError("project ID is required")
            # Older CLI exports used the Python field name; normalize before merging
            # so a partial import replaces the existing public validate key.
            commands = imported.get("commands")
            if isinstance(commands, dict) and "validate_cmds" in commands:
                if "validate" in commands:
                    raise ValueError("conflicting validation aliases")
                commands["validate"] = commands.pop("validate_cmds")
            existing = registry.get_project(project_id)
            base = existing.model_dump(mode="json", by_alias=True) if existing else {"prefix": "USR"}
            if "path" in explicit:
                explicit["path"] = str(Path(explicit["path"]).resolve())
            if "prefix" in explicit:
                explicit["prefix"] = explicit["prefix"].upper()
            desc = ProjectDescriptor.model_validate(_merge_descriptor(_merge_descriptor(base, imported), explicit))
        except (OSError, ValueError, ValidationError):
            # Validation errors include user-supplied values; never echo descriptors
            # which may contain a mistakenly pasted credential.
            print("Error: invalid project descriptor or conflicting IDs; registry unchanged.", file=sys.stderr)
            return 2
        registry.register_project(desc)
        print(f"Successfully registered project '{desc.id}' ({desc.name}) at '{desc.path}'.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
