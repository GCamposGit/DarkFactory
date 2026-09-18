"""Headless CLI interface for Dark Factory Project Registry (HF-20/HF-25)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    reg_p.add_argument("--id", required=True, help="Project unique slug ID")
    reg_p.add_argument("--name", required=True, help="Human-readable project title")
    reg_p.add_argument("--path", required=True, help="Absolute path to project directory")
    reg_p.add_argument("--kind", required=True, choices=[k.value for k in ProjectKind], help="Project archetype/kind")
    reg_p.add_argument("--prefix", default="USR", help="Ticket prefix (e.g. SIT, JRV, DF)")
    reg_p.add_argument("--domain", default=None, help="Production URL or local domain")
    reg_p.add_argument("--deploy-target", default=None, help="Deployment method (e.g. dokploy_docker, hostinger_ftp)")
    reg_p.add_argument("--description", default="", help="Brief functional summary")

    args = parser.parse_args(argv)
    registry = get_project_registry()

    if args.command == "list":
        projects = registry.list_projects()
        if getattr(args, "json", False):
            print(json.dumps([p.model_dump(mode="json") for p in projects], indent=2))
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
            print(proj.model_dump_json(indent=2))
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
        desc = ProjectDescriptor(
            id=args.id,
            name=args.name,
            path=str(Path(args.path).resolve()),
            kind=ProjectKind(args.kind),
            prefix=args.prefix.upper(),
            domain=args.domain,
            deploy_target=args.deploy_target,
            description=args.description,
        )
        registry.register_project(desc)
        print(f"Successfully registered project '{desc.id}' ({desc.name}) at '{desc.path}'.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
