"""Headless CLI interface for HF-15 Acceptance Environment, Data, Rollback, and Observability.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 267)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenarios G1-G8)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.acceptance.engine import HF15AcceptanceEngine
from core.acceptance.environment import HF15EnvironmentManager, load_hf15_config
from core.acceptance.observability import HF15ObservabilityTracker
from core.acceptance.rollback import HF15RollbackCoordinator
from core.acceptance.test_data import seed_test_data



def cmd_preflight(args: argparse.Namespace) -> int:
    """Executes environment preflight checks and reports readiness."""
    env_mgr = HF15EnvironmentManager()
    env_mgr.provision_environment()
    report = env_mgr.run_preflights()

    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        print(f"=== HF-15 Preflight Report ({report.environment_mode.upper()}) ===")
        print(f"Timestamp: {report.timestamp.isoformat()}")
        for c in report.checks:
            mark = "[PASS]" if c.passed else "[FAIL]"
            print(f"  {mark} {c.name}: {c.details}")
            if c.error:
                print(f"         Error: {c.error}")
        status_msg = "ALL CHECKS PASSED" if report.all_passed else "PREFLIGHT FAILED"
        print(f"Result: {status_msg}")

    return 0 if report.all_passed else 1


def cmd_seed_data(args: argparse.Namespace) -> int:
    """Seeds deterministic fixtures for scenarios G1-G8 and 10 portfolio projects."""
    env_mgr = HF15EnvironmentManager()
    root = env_mgr.provision_environment()
    fixtures_dir = root / "fixtures"
    seeded = seed_test_data(fixtures_dir)

    result = {
        "status": "seeded",
        "fixtures_count": len(seeded),
        "target_directory": str(fixtures_dir),
        "seeded_scenarios": list(seeded.keys()),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"=== HF-15 Test Data Seeded ({len(seeded)} fixtures) ===")
        print(f"Destination: {fixtures_dir}")
        for s in seeded.keys():
            print(f"  - {s}")

    return 0


def cmd_rollback_drill(args: argparse.Namespace) -> int:
    """Executes synthetic rollback drill and measures RPO/RTO."""
    env_mgr = HF15EnvironmentManager()
    root = env_mgr.provision_environment()
    coordinator = HF15RollbackCoordinator(backup_root=root / "backups")
    tracker = HF15ObservabilityTracker(
        ledger_path=root / "telemetry" / "observability_ledger.jsonl"
    )

    with tracker.measure_latency("G8", "rollback_drill_invoked"):
        record = coordinator.run_rollback_drill(
            project_id=args.project_id or "proj-drill-01",
            sandbox_dir=root / "backups" / "drill_workspace",
        )

    tracker.record_event(
        scenario_id="G8",
        event_type="rollback_completed",
        duration_ms=record.rto_seconds * 1000.0,
        details={
            "rollback_id": record.rollback_id,
            "rto_seconds": record.rto_seconds,
            "rpo_seconds": record.rpo_seconds,
            "success": record.success,
        },
    )

    if args.json:
        print(json.dumps(record.model_dump(mode="json"), indent=2))
    else:
        print(f"=== HF-15 Rollback Drill Completed ===")
        print(f"Rollback ID: {record.rollback_id}")
        print(f"Success: {record.success}")
        print(f"RTO: {record.rto_seconds:.4f}s")
        print(f"RPO: {record.rpo_seconds:.4f}s")
        print(f"Evidence Hash: {record.evidence_hash}")

    return 0 if record.success else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Returns overall acceptance environment status and SLA compliance."""
    env_mgr = HF15EnvironmentManager()
    root = env_mgr.provision_environment()
    preflight = env_mgr.run_preflights()
    tracker = HF15ObservabilityTracker(
        ledger_path=root / "telemetry" / "observability_ledger.jsonl"
    )
    metrics = tracker.get_metrics_summary()

    status_data = {
        "environment": {
            "sandbox_root": str(root),
            "mode": preflight.environment_mode,
            "all_preflights_passed": preflight.all_passed,
        },
        "metrics": metrics.model_dump(mode="json"),
    }

    if args.json:
        print(json.dumps(status_data, indent=2))
    else:
        print("=== HF-15 Acceptance Status ===")
        print(f"Mode: {preflight.environment_mode}")
        print(f"Preflights: {'PASS' if preflight.all_passed else 'FAIL'}")
        print(f"Scenarios Passed: {metrics.passed_scenarios}/{metrics.total_scenarios}")
        print(f"SLAs Met: {metrics.all_slas_met}")
        print(f"Avg Dispatch Latency: {metrics.avg_dispatch_latency_ms}ms (Target <= 30000ms)")
        print(f"Avg Reconciliation Latency: {metrics.avg_reconciliation_latency_ms}ms (Target <= 60000ms)")

    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Outputs aggregated performance and SLA metrics."""
    env_mgr = HF15EnvironmentManager()
    root = env_mgr.provision_environment()
    tracker = HF15ObservabilityTracker(
        ledger_path=root / "telemetry" / "observability_ledger.jsonl"
    )
    metrics = tracker.get_metrics_summary()

    if args.json:
        print(json.dumps(metrics.model_dump(mode="json"), indent=2))
    else:
        print("=== HF-15 Metrics Summary ===")
        print(f"Total Scenarios: {metrics.total_scenarios}")
        print(f"Passed: {metrics.passed_scenarios}")
        print(f"Failed: {metrics.failed_scenarios}")
        print(f"Rolled Back: {metrics.rolled_back_scenarios}")
        print(f"Avg Dispatch: {metrics.avg_dispatch_latency_ms:.2f} ms")
        print(f"Avg Reconciliation: {metrics.avg_reconciliation_latency_ms:.2f} ms")
        print(f"Avg RTO: {metrics.avg_rto_seconds:.4f} s")
        print(f"All SLAs Met: {metrics.all_slas_met}")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Executes full acceptance testing suite and outputs final report."""
    engine = HF15AcceptanceEngine(
        run_id=args.run_id,
        report_dir=args.report_dir,
    )
    report = engine.run_acceptance()
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        print(f"=== HF-15 Acceptance Completed ({report.status}) ===")
        print(f"Run ID: {report.run_id}")
        print(f"Plan Digest: {report.plan_digest[:16]}...")
        print(f"Baseline SHA: {report.baseline_sha[:16]}...")
        for gid, res in report.gates.items():
            print(f"  Gate {gid}: {res}")
        for snum, sres in report.scenarios.items():
            print(f"  Scenario {snum}: {sres}")

    return 0 if report.status == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="HF-15 Acceptance Environment, Rollback, and Observability CLI"
    )
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Format output as JSON"
    )
    parser.add_argument("--json", action="store_true", default=False, help="Format output as JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: preflight
    subparsers.add_parser("preflight", parents=[json_parent], help="Run environment preflight checks")

    # Subcommand: seed-data
    subparsers.add_parser("seed-data", parents=[json_parent], help="Seed deterministic fixtures for G1-G8")

    # Subcommand: rollback-drill
    drill_parser = subparsers.add_parser(
        "rollback-drill", parents=[json_parent], help="Execute rollback verification drill"
    )
    drill_parser.add_argument("--project-id", default="proj-drill-01", help="Target project ID")

    # Subcommand: status
    subparsers.add_parser("status", parents=[json_parent], help="Get acceptance readiness status")

    # Subcommand: metrics
    subparsers.add_parser("metrics", parents=[json_parent], help="Get observability metrics and SLA report")

    # Subcommand: run
    run_parser = subparsers.add_parser("run", parents=[json_parent], help="Execute full acceptance test run")
    run_parser.add_argument("--run-id", default=None, help="Unique run identifier")
    run_parser.add_argument("--report-dir", type=Path, default=None, help="Report directory")

    args = parser.parse_args(argv)
    args.json = bool(getattr(args, "json", False))

    dispatch_map = {
        "preflight": cmd_preflight,
        "seed-data": cmd_seed_data,
        "rollback-drill": cmd_rollback_drill,
        "status": cmd_status,
        "metrics": cmd_metrics,
        "run": cmd_run,
    }

    return dispatch_map[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

