"""Headless CLI for adopting or starting projects with Dark Factory."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from .service import (
        AdoptionBlockedError,
        AdoptionError,
        apply_adoption,
        initialize_project,
        inspect_project,
        plan_adoption,
        prepare_adoption_worktree,
        prepare_task,
        verify_adoption,
    )
except ImportError:
    from core.adoption.service import (
        AdoptionBlockedError,
        AdoptionError,
        apply_adoption,
        initialize_project,
        inspect_project,
        plan_adoption,
        prepare_adoption_worktree,
        prepare_task,
        verify_adoption,
    )

SOURCE_ROOT = _REPO_ROOT


def _emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _seed_files(args: argparse.Namespace) -> dict[str, bytes]:
    seeds: dict[str, bytes] = {}
    for argument, target_name in (
        ("mission_file", "MISSION.md"),
        ("rules_file", "FACTORY_RULES.md"),
        ("harness_file", "harness.config.json"),
    ):
        selected = getattr(args, argument, None)
        if selected:
            seeds[target_name] = Path(selected).expanduser().resolve(strict=True).read_bytes()
    return seeds


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--autonomy-level", type=int, choices=range(0, 6), default=2)
    parser.add_argument("--mission-file", type=Path)
    parser.add_argument("--rules-file", type=Path)
    parser.add_argument("--harness-file", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dark Factory project-adoption gateway")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect_parser = commands.add_parser("inspect", help="inspect a target without writes")
    inspect_parser.add_argument("project", type=Path)

    plan_parser = commands.add_parser("plan", help="render an idempotent adoption plan")
    plan_parser.add_argument("project", type=Path)
    _add_source_options(plan_parser)

    apply_parser = commands.add_parser("apply", help="apply to an already clean worktree")
    apply_parser.add_argument("project", type=Path)
    _add_source_options(apply_parser)

    adopt_parser = commands.add_parser("adopt", help="create a clean worktree and adopt a project")
    adopt_parser.add_argument("project", type=Path)
    adopt_parser.add_argument("--branch", default="codex/darkfac-adoption")
    adopt_parser.add_argument("--destination", type=Path)
    adopt_parser.add_argument("--base-ref", default="HEAD")
    _add_source_options(adopt_parser)

    init_parser = commands.add_parser("init", help="initialize and adopt an empty project directory")
    init_parser.add_argument("project", type=Path)
    init_parser.add_argument("--name")
    _add_source_options(init_parser)

    verify_parser = commands.add_parser("verify", aliases=["status"], help="verify an adopted project")
    verify_parser.add_argument("project", type=Path)

    task_parser = commands.add_parser("prepare-task", help="create a governed task worktree")
    task_parser.add_argument("project", type=Path)
    task_parser.add_argument("--ticket", required=True)
    task_parser.add_argument("--title", required=True)
    task_parser.add_argument("--owner", required=True)
    task_parser.add_argument("--path", action="append", required=True, dest="paths")
    task_parser.add_argument("--validate", action="append", required=True, dest="validations")
    task_parser.add_argument("--branch")
    task_parser.add_argument("--destination", type=Path)
    task_parser.add_argument("--base-ref", default="HEAD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            _emit(inspect_project(args.project))
        elif args.command == "plan":
            plan = plan_adoption(
                args.project,
                source_root=args.source_root,
                autonomy_level=args.autonomy_level,
                seed_files=_seed_files(args),
            )
            _emit(plan)
            return 0 if plan.ready else 2
        elif args.command == "apply":
            _emit(
                apply_adoption(
                    args.project,
                    source_root=args.source_root,
                    autonomy_level=args.autonomy_level,
                    seed_files=_seed_files(args),
                )
            )
        elif args.command == "adopt":
            worktree = prepare_adoption_worktree(
                args.project,
                branch=args.branch,
                destination=args.destination,
                base_ref=args.base_ref,
            )
            result = apply_adoption(
                worktree,
                source_root=args.source_root,
                autonomy_level=args.autonomy_level,
                seed_files=_seed_files(args),
            )
            _emit({"worktree": str(worktree), "result": result.model_dump(mode="json")})
        elif args.command == "init":
            root = initialize_project(args.project, project_name=args.name)
            _emit(
                apply_adoption(
                    root,
                    source_root=args.source_root,
                    autonomy_level=args.autonomy_level,
                    seed_files=_seed_files(args),
                )
            )
        elif args.command in {"verify", "status"}:
            report = verify_adoption(args.project)
            _emit(report)
            return 0 if report.ready else 2
        elif args.command == "prepare-task":
            _emit(
                prepare_task(
                    args.project,
                    ticket_id=args.ticket,
                    title=args.title,
                    owner=args.owner,
                    allowed_paths=args.paths,
                    validate_commands=args.validations,
                    branch=args.branch,
                    destination=args.destination,
                    base_ref=args.base_ref,
                )
            )
        return 0
    except (AdoptionError, OSError) as exc:
        details = {"error": str(exc)}
        if isinstance(exc, AdoptionBlockedError):
            details["blockers"] = exc.blockers
        _emit(details)
        return 2


if __name__ == "__main__":
    sys.exit(main())
