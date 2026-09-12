"""Headless CLI interface for the HF-09 implementation, quality, and independent review cycle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.workflow.contracts import (
    EnvironmentKind,
    EnvironmentManifest,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.cycle import (
    ExternalTargetProbe,
    ImplementationCandidate,
    ImplementationCycleService,
)
from core.workflow.readiness import ReadinessGate
from core.workflow.reconciliation import reconcile_environment_manifest
from core.workflow.runtime import WorkflowRuntime


def _build_service(db_path: Path | str | None = None) -> ImplementationCycleService:
    database_path = Path(db_path) if db_path else PROJECT_ROOT / ".factory" / "workflow" / "runtime.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    runtime = WorkflowRuntime(database_path)
    gate = ReadinessGate()
    return ImplementationCycleService(runtime, gate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory Implementation & Review Cycle CLI (HF-09)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: reconcile
    rec_p = subparsers.add_parser("reconcile", help="Reconcile an EnvironmentManifest against code/diff")
    rec_p.add_argument("--manifest-json", required=True, help="Path to JSON file containing EnvironmentManifest")
    rec_p.add_argument("--code-file", default=None, help="Path to code file or diff to inspect")
    rec_p.add_argument("--json", action="store_true", help="Output raw JSON")

    # Subcommand: validate-candidate
    val_p = subparsers.add_parser("validate-candidate", help="Run deterministic validation and target probes")
    val_p.add_argument("--ticket-id", required=True, help="Ticket ID")
    val_p.add_argument("--unit-pass", action="store_true", help="Mark unit tests as passing")
    val_p.add_argument("--unit-fail", action="store_true", help="Mark unit tests as failing")
    val_p.add_argument("--probe-status", choices=["passed", "firewall_blocked", "scope_denied"], default="passed")
    val_p.add_argument("--firewall-blocked", action="store_true", help="Simulate firewall block on target")
    val_p.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args(argv)

    if args.command == "reconcile":
        manifest_path = Path(args.manifest_json)
        if not manifest_path.exists():
            print(f"Error: Manifest file not found: {args.manifest_json}", file=sys.stderr)
            return 1
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = EnvironmentManifest.model_validate(manifest_data)

        code_text = ""
        if args.code_file:
            code_p = Path(args.code_file)
            if code_p.exists():
                code_text = code_p.read_text(encoding="utf-8")

        reconciled, diff = reconcile_environment_manifest(manifest, code_or_diff=code_text)

        if args.json:
            out = {
                "has_changes": diff.has_changes,
                "summary": diff.summary,
                "original_ref": manifest.environment_ref,
                "reconciled_ref": reconciled.environment_ref,
                "reconciled_manifest": reconciled.model_dump(mode="json"),
            }
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(f"Manifest Reconciliation: {diff.summary}")
            print(f"Reconciled Environment Ref: {reconciled.environment_ref}")
        return 0

    if args.command == "validate-candidate":
        unit_pass = True if args.unit_pass else (False if args.unit_fail else True)
        firewall_allowed = not args.firewall_blocked and args.probe_status != "firewall_blocked"
        probe = ExternalTargetProbe(
            probe_id=f"probe_{args.ticket_id}",
            endpoint_url="https://api.internal.local:8443",
            firewall_allowed=firewall_allowed,
            status=args.probe_status if not args.firewall_blocked else "firewall_blocked",
            message="Probe simulation",
        )
        is_pass = unit_pass and probe.firewall_allowed and probe.status == "passed"
        if args.json:
            out = {
                "ticket_id": args.ticket_id,
                "unit_tests_pass": unit_pass,
                "target_probe_pass": is_pass,
                "status": "passed" if is_pass else "failed",
                "probe_status": probe.status,
                "firewall_allowed": probe.firewall_allowed,
            }
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(f"Validation for {args.ticket_id}: {'PASSED' if is_pass else 'FAILED'}")
            print(f"Unit Tests: {'PASS' if unit_pass else 'FAIL'}, Target Probe: {probe.status}")
        return 0 if is_pass else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
