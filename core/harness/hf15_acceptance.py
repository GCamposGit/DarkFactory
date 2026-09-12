"""Canonical Entrypoint Runner for HF-15 Acceptance and Factory Operation.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 267 & lines 316-333 / HF-15)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenarios G1-G8)
- docs/handoffs/HF-15.md (Section 13, line 513)

Command-line usage:
    python -m core.harness.hf15_acceptance --config <hf15-config.json> --run-id <run-id> --json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

# Ensure robust UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.acceptance.engine import HF15AcceptanceEngine
from core.acceptance.environment import load_hf15_config
from core.acceptance.models import HF15AcceptanceReport, HF15EnvironmentConfig
from core.acceptance.observability import sanitize_payload

logger = logging.getLogger("darkfac.harness.hf15_acceptance")


def build_parser() -> argparse.ArgumentParser:
    """Builds CLI argument parser for HF-15 canonical acceptance runner."""
    parser = argparse.ArgumentParser(
        prog="python -m core.harness.hf15_acceptance",
        description="HF-15 Canonical Acceptance and Factory Operation Runner",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to HF-15 environment configuration file (JSON)",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Unique identifier for the acceptance run",
    )
    parser.add_argument(
        "--mode",
        choices=["sandbox", "live"],
        default="sandbox",
        help="Execution mode: isolated sandbox or live cloud/target",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Custom destination directory for acceptance report and evidence",
    )
    parser.add_argument(
        "--gate",
        action="append",
        choices=["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "all"],
        help="Selective gate(s) to execute (repeatable, default: all)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        type=int,
        choices=range(1, 11),
        help="Selective lifecycle scenario(s) to execute (1-10, repeatable, default: all)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON report to stdout",
    )
    return parser


def run_hf15_runner(argv: Optional[Sequence[str]] = None) -> int:
    """Executes the HF-15 canonical acceptance process and returns exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve environment config
    base_config = load_hf15_config(args.config)
    if args.mode == "live":
        base_config = base_config.model_copy(update={"live_mode": True})

    # Resolve gate filters
    gate_filter: Optional[List[str]] = None
    if args.gate and "all" not in args.gate:
        gate_filter = args.gate

    # Initialize Engine
    engine = HF15AcceptanceEngine(
        config=base_config,
        run_id=args.run_id,
        report_dir=args.report_dir,
    )

    # Run Acceptance
    report: HF15AcceptanceReport = engine.run_acceptance(
        gate_filter=gate_filter,
        scenario_filter=args.scenario,
    )

    # Output formatting with secret sanitization
    report_dict = sanitize_payload(report.model_dump(mode="json"))

    if args.json:
        print(json.dumps(report_dict, indent=2))
    else:
        print(f"============================================================")
        print(f"  DARK FACTORY: HF-15 ACCEPTANCE RUNNER — ONDA 1")
        print(f"============================================================")
        print(f"Run ID:            {report.run_id}")
        print(f"Plan Digest:       {report.plan_digest[:16]}...")
        print(f"Baseline SHA:      {report.baseline_sha[:16]}...")
        print(f"Status:            {report.status}")
        print(f"Candidate Digest:  {report.candidate_digest[:16]}...")
        print(f"Report Location:   {engine.report_dir / 'report.json'}")
        print(f"------------------------------------------------------------")
        print(f"Gates (G1 - G8):")
        for gid, res in report.gates.items():
            mark = "[PASS]" if res == "PASSED" else "[FAIL]"
            print(f"  {mark} {gid}: {res}")
        print(f"------------------------------------------------------------")
        print(f"Scenarios (1 - 10):")
        for snum, sres in report.scenarios.items():
            mark = "[PASS]" if sres == "PASSED" else "[FAIL]"
            print(f"  {mark} Scenario {snum}: {sres}")
        print(f"------------------------------------------------------------")
        print(f"SLAs & Timings:")
        print(f"  Avg Dispatch Latency:       {report.timings.get('dispatch_latency_ms', 0.0):.2f} ms (SLA <= 30s)")
        print(f"  Avg Reconciliation Latency: {report.timings.get('reconciliation_latency_ms', 0.0):.2f} ms (SLA <= 60s)")
        print(f"  Avg Rollback RTO:           {report.recovery.get('avg_rto_seconds', 0.0):.4f} s")
        print(f"============================================================")

    # Return exit code: 0 for PASS, 1 for FAIL or BLOCKED
    return 0 if report.status == "PASS" else 1


def main() -> None:
    """CLI script entrypoint."""
    sys.exit(run_hf15_runner(sys.argv[1:]))


if __name__ == "__main__":
    main()
