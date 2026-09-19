"""Headless CLI interface for Factory Self-Evolution (HF-25)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.paths import project_root
from core.evolution.engine import FactoryEvolutionEngine
from core.evolution.models import EvolutionStatus, EvolutionTarget, EvolutionTrigger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DarkFac Factory Self-Evolution CLI (HF-25)",
        prog="python core/evolution/cli.py",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    status_p = subparsers.add_parser("status", help="Inspect evolution proposals and reports")
    status_p.add_argument("--json", action="store_true", help="Output raw JSON format")

    # propose
    propose_p = subparsers.add_parser("propose", help="Create a new evolutionary proposal")
    propose_p.add_argument("--target-kind", required=True, choices=[k.value for k in EvolutionTarget])
    propose_p.add_argument("--target-path", required=True, help="Relative path to file being evolved")
    propose_p.add_argument("--trigger", required=True, choices=[t.value for t in EvolutionTrigger])
    propose_p.add_argument("--patch-file", required=True, help="Path to file containing new content/patch")
    propose_p.add_argument("--rationale", required=True, help="Justification and RCA evidence")
    propose_p.add_argument("--id", help="Optional specific proposal ID")

    # evaluate
    eval_p = subparsers.add_parser("evaluate", help="Evaluate a candidate in the holdout sandbox")
    eval_p.add_argument("--id", required=True, help="Proposal ID to evaluate")
    eval_p.add_argument("--holdout-cmd", help="Optional holdout command to execute in sandbox")

    # promote
    promote_p = subparsers.add_parser("promote", help="Promote an approved proposal with rollback snapshot")
    promote_p.add_argument("--id", required=True, help="Proposal ID to promote")

    # rollback
    rollback_p = subparsers.add_parser("rollback", help="Revert a promoted proposal to previous snapshot")
    rollback_p.add_argument("--id", required=True, help="Proposal ID to rollback")

    args = parser.parse_args(argv)
    engine = FactoryEvolutionEngine()

    if args.command == "status":
        report = engine.get_report()
        if getattr(args, "json", False):
            print(report.model_dump_json(indent=2))
        else:
            print(f"=== Factory Evolution Subsystem (HF-25) ===")
            print(f"Total Proposals   : {report.total_proposals}")
            print(f"Active Promotions : {report.active_promotions}")
            print(f"Rejected Count    : {report.rejected_count}")
            print(f"\n--- Proposals ---")
            for p in report.proposals:
                print(f"[{p.status.value.upper()}] {p.proposal_id} -> {p.target_path} ({p.trigger.value})")
                print(f"   Rationale: {p.rationale[:80]}")
        return 0

    if args.command == "propose":
        patch_path = Path(args.patch_file)
        if not patch_path.is_file():
            print(f"Error: patch file not found: {patch_path}", file=sys.stderr)
            return 1
        content = patch_path.read_text(encoding="utf-8")
        try:
            prop = engine.propose(
                target_kind=args.target_kind,
                target_path=args.target_path,
                trigger=args.trigger,
                patch_content=content,
                rationale=args.rationale,
                proposal_id=args.id,
            )
            print(f"Registered proposal: {prop.proposal_id} [status={prop.status.value}]")
            return 0
        except Exception as exc:
            print(f"Proposal rejected: {exc}", file=sys.stderr)
            return 2

    if args.command == "evaluate":
        try:
            result = engine.evaluate_candidate(args.id, holdout_cmd=args.holdout_cmd)
            outcome = "PASSED" if result.passed else "FAILED"
            print(f"Evaluation {outcome}: proposal={result.proposal_id} tampering={result.tampering_detected}")
            print(f"Log: {result.log_summary}")
            return 0 if result.passed else 1
        except Exception as exc:
            print(f"Error evaluating candidate: {exc}", file=sys.stderr)
            return 2

    if args.command == "promote":
        try:
            snap = engine.promote_candidate(args.id)
            print(f"Promoted {args.id}! Rollback snapshot captured: {snap.snapshot_id}")
            return 0
        except Exception as exc:
            print(f"Promotion failed: {exc}", file=sys.stderr)
            return 2

    if args.command == "rollback":
        try:
            engine.rollback_candidate(args.id)
            print(f"Rollback successful for proposal {args.id}.")
            return 0
        except Exception as exc:
            print(f"Rollback failed: {exc}", file=sys.stderr)
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
