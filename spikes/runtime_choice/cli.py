"""Command-line interface for the HF-02 runtime choice laboratory.

Subcommands:
- ``preflight``: Check local dependencies, versions, and database availability.
- ``run``: Execute scenario suites sequentially, outputting results and manifest.
- ``verify``: Verify manifest integrity, scenario coverage, and assertion gates.
- ``summarize``: Produce markdown comparison tables and operational metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError

from spikes.runtime_choice.contracts import (
    AdapterCapabilities,
    CliExitCode,
    DecisionStatus,
    LabConfig,
    ResultStatus,
    RuntimeComparison,
    RuntimeKind,
    ScenarioCapability,
    ScenarioResult,
    ScenarioSpec,
    ValidationMode,
    WorkflowVersion,
)
from spikes.runtime_choice.controller import ScenarioController
from spikes.runtime_choice.effect_server import EffectServer
from spikes.runtime_choice.effect_store import (
    EXPECTED_SCENARIO_CATALOG_SHA256,
    NativeEffectStore,
    load_scenario_catalog,
    scenario_catalog_hash,
)
from spikes.runtime_choice.oracle import ScenarioOracle


def _current_code_sha() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        sha = res.stdout.strip().lower()
        if res.returncode == 0 and len(sha) == 40:
            return sha
    except Exception:
        pass
    return "0000000000000000000000000000000000000000"


def cmd_preflight(args: argparse.Namespace) -> int:
    """Validate environment readiness without printing sensitive credentials."""
    versions_path = Path(__file__).with_name("versions.json")
    versions_data: dict[str, Any] = {}
    if versions_path.exists():
        try:
            versions_data = json.loads(versions_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    catalog_digest = scenario_catalog_hash()
    catalog_valid = catalog_digest == EXPECTED_SCENARIO_CATALOG_SHA256

    db_env = os.environ.get("DARKFAC_HF02_DATABASE_URL")
    db_status = "ready" if db_env else "waiting_access"

    dbos_available = False
    try:
        import dbos  # type: ignore[import-not-found]
        dbos_available = True
    except ImportError:
        pass

    preflight_info = {
        "schema_version": "1",
        "collected_at": datetime.now(UTC).isoformat(),
        "python": {
            "version": sys.version.split()[0],
            "platform": sys.platform,
        },
        "scenario_catalog": {
            "path": "scenarios.json",
            "sha256": catalog_digest,
            "matches_frozen_digest": catalog_valid,
        },
        "runtimes": {
            "native_sqlite": {"status": "ready"},
            "dbos_postgres": {
                "dbos_module": "present" if dbos_available else "absent",
                "database_connection": db_status,
                "status": "ready" if (dbos_available and db_env) else "waiting_access",
            },
        },
    }

    print(json.dumps(preflight_info, indent=2))
    return CliExitCode.SUCCESS if catalog_valid else CliExitCode.CONTRACT_ERROR


def cmd_run(args: argparse.Namespace) -> int:
    """Execute scenario suites sequentially across selected runtimes."""
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    catalog = load_scenario_catalog()
    if args.suite == "core":
        selected_scenarios = [s for s in catalog if s.scenario_id in {"R01", "R02", "R03", "R09", "R11", "R12"}]
    else:
        selected_scenarios = catalog

    runtimes_to_run: list[RuntimeKind] = []
    if args.runtime in {"native_sqlite", "both"}:
        runtimes_to_run.append(RuntimeKind.NATIVE_SQLITE)
    if args.runtime in {"dbos_postgres", "both"}:
        runtimes_to_run.append(RuntimeKind.DBOS_POSTGRES)

    lab_id = args.lab_id or f"lab-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    env_ref = args.environment_ref or "docs/handoffs/HF-02-ENVIRONMENT.md"
    validation_mode = ValidationMode(args.validation_mode)

    all_results: list[ScenarioResult] = []
    capability_matrix: dict[str, AdapterCapabilities] = {}

    for runtime in runtimes_to_run:
        runtime_dir = out_dir / runtime.value
        runtime_dir.mkdir(parents=True, exist_ok=True)

        if runtime is RuntimeKind.NATIVE_SQLITE:
            adapter_caps = AdapterCapabilities(
                durable_steps=True,
                resume_after_crash=True,
                durable_wait=False,
                deduplicated_intake=False,
                cancel_before_next_step=False,
                version_isolation=False,
                bounded_concurrency=False,
            )
        else:
            adapter_caps = AdapterCapabilities(
                durable_steps=True,
                resume_after_crash=True,
                durable_wait=True,
                deduplicated_intake=True,
                cancel_before_next_step=True,
                version_isolation=True,
                bounded_concurrency=True,
            )
        capability_matrix[runtime.value] = adapter_caps

        # Check if DBOS is blocked by waiting_access
        if runtime is RuntimeKind.DBOS_POSTGRES and not os.environ.get("DARKFAC_HF02_DATABASE_URL"):
            for spec in selected_scenarios:
                all_results.append(
                    ScenarioResult(
                        lab_id=lab_id,
                        scenario_id=spec.scenario_id,
                        runtime=runtime,
                        repeat_index=1,
                        status=ResultStatus.BLOCKED,
                        environment_ref=env_ref,
                        validation_mode=validation_mode,
                        target_differences=[],
                        assertions={"database_available": False},
                        duration_ms=0.0,
                        effect_count=0,
                        actual_step_invocations=0,
                        error_code="STORE_UNAVAILABLE",
                    )
                )
            continue

        with EffectServer(runtime_dir) as server:
            oracle = ScenarioOracle(server.store)
            config = LabConfig(
                lab_id=lab_id,
                root_dir=runtime_dir,
                runtime=runtime,
                runtime_version="core-v1" if runtime is RuntimeKind.NATIVE_SQLITE else "dbos-2.31.1",
                workflow_version=WorkflowVersion.V1,
                database_alias=f"darkfac_hf02_{runtime.value[:10]}",
                database_url_env="DARKFAC_HF02_DATABASE_URL" if runtime is RuntimeKind.DBOS_POSTGRES else None,
                effect_base_url=server.base_url,
                lease_seconds=args.lease_seconds,
            )
            controller = ScenarioController(config, effect_server=server)

            repeats = args.repeats if adapter_caps.durable_steps else 1
            for spec in selected_scenarios:
                for rep in range(1, repeats + 1):
                    trace = controller.run_scenario(spec, repeat_index=rep)
                    result = oracle.evaluate(
                        trace,
                        spec,
                        environment_ref=env_ref,
                        validation_mode=validation_mode,
                    )
                    all_results.append(result)
                    time.sleep(0.15)

    # Serialize results.json
    results_path = out_dir / "results.json"
    results_path.write_text(
        json.dumps([r.model_dump(mode="json") for r in all_results], indent=2),
        encoding="utf-8",
    )

    # Build and serialize manifest.json
    manifest = RuntimeComparison(
        schema_version="1",
        baseline_snapshot_hash="1f931fcccc2f4a54cde1f0ba09825094dbe429af",
        environment_ref=env_ref,
        code_sha=_current_code_sha(),
        results=all_results,
        capability_matrix=capability_matrix,
        eligibility={
            k: {
                "eligible": all(r.status is ResultStatus.PASS for r in all_results if r.runtime.value == k),
                "unsupported_count": sum(1 for r in all_results if r.runtime.value == k and r.status is ResultStatus.UNSUPPORTED),
                "blocked_count": sum(1 for r in all_results if r.runtime.value == k and r.status is ResultStatus.BLOCKED),
                "fail_count": sum(1 for r in all_results if r.runtime.value == k and r.status is ResultStatus.FAIL),
            }
            for k in capability_matrix
        },
        operational_metrics={
            "total_scenarios_run": len(all_results),
            "total_duration_ms": sum(r.duration_ms for r in all_results),
        },
        decision_status=DecisionStatus.PENDING_ARCHITECT_REVIEW,
    )

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    # Generate COMPARISON.md
    summary_path = out_dir / "COMPARISON.md"
    summary_md = _generate_summary_markdown(manifest)
    summary_path.write_text(summary_md, encoding="utf-8")

    # Determine exit code according to HF-02 handoff contract:
    # 0: all required gates passed; 1: assertion failed or required capability unsupported;
    # 2: environment blocked; 3: contract error
    has_blocked = any(r.status is ResultStatus.BLOCKED for r in all_results)
    has_fails = any(r.status is ResultStatus.FAIL for r in all_results)
    has_unsupported = any(r.status is ResultStatus.UNSUPPORTED for r in all_results)
    if has_blocked:
        print("[ERROR] Scenarios blocked by environment", file=sys.stderr)
        return CliExitCode.ENVIRONMENT_BLOCKED
    if has_fails or has_unsupported:
        failed_items = [r for r in all_results if r.status in {ResultStatus.FAIL, ResultStatus.UNSUPPORTED}]
        for fi in failed_items:
            print(
                f"[FAIL] Scenario {fi.scenario_id} on {fi.runtime.value}: status={fi.status.value}, "
                f"error={fi.error_code}, diffs={fi.target_differences}",
                file=sys.stderr,
            )
        return CliExitCode.ASSERTION_FAILED
    return CliExitCode.SUCCESS


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify manifest integrity, hashes, and assertion validity."""
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return CliExitCode.CONTRACT_ERROR

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        comparison = RuntimeComparison.model_validate(data)
    except (ValidationError, ValueError, OSError) as exc:
        print(f"[ERROR] Invalid comparison manifest: {exc}", file=sys.stderr)
        return CliExitCode.CONTRACT_ERROR

    # Verify scenario catalog digest
    catalog_digest = scenario_catalog_hash()
    if catalog_digest != EXPECTED_SCENARIO_CATALOG_SHA256:
        print(f"[ERROR] Scenario catalogue hash mismatch: {catalog_digest}", file=sys.stderr)
        return CliExitCode.CONTRACT_ERROR

    # Check for empty results
    if not comparison.results:
        print("[ERROR] Comparison contains zero scenario results", file=sys.stderr)
        return CliExitCode.CONTRACT_ERROR

    # Check for mock_only contamination
    for result in comparison.results:
        if result.validation_mode is ValidationMode.MOCK_ONLY and result.status is ResultStatus.PASS:
            print(f"[ERROR] Result {result.scenario_id} marked pass with mock_only mode", file=sys.stderr)
            return CliExitCode.ASSERTION_FAILED

    # Check for any failed assertions
    failed_results = [r for r in comparison.results if r.status is ResultStatus.FAIL]
    if failed_results:
        print(f"[FAIL] {len(failed_results)} scenarios failed assertions", file=sys.stderr)
        for fr in failed_results:
            print(f"  - {fr.runtime.value} {fr.scenario_id}: {fr.assertions}", file=sys.stderr)
        return CliExitCode.ASSERTION_FAILED

    blocked_results = [r for r in comparison.results if r.status is ResultStatus.BLOCKED]
    if blocked_results:
        print(f"[BLOCKED] {len(blocked_results)} scenarios blocked on environment/access", file=sys.stderr)
        return CliExitCode.ENVIRONMENT_BLOCKED

    print("[PASS] Manifest verified successfully.")
    return CliExitCode.SUCCESS


def cmd_summarize(args: argparse.Namespace) -> int:
    """Generate Markdown summary from manifest."""
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"[ERROR] Manifest not found: {manifest_path}", file=sys.stderr)
        return CliExitCode.CONTRACT_ERROR

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    comparison = RuntimeComparison.model_validate(data)
    summary_md = _generate_summary_markdown(comparison)

    if args.out:
        Path(args.out).write_text(summary_md, encoding="utf-8")
    else:
        print(summary_md)
    return CliExitCode.SUCCESS


def _generate_summary_markdown(manifest: RuntimeComparison) -> str:
    lines = [
        "# HF-02 Runtime Spike Comparison Report",
        "",
        f"- Code SHA: `{manifest.code_sha}`",
        f"- Baseline Snapshot: `{manifest.baseline_snapshot_hash}`",
        f"- Decision Status: `{manifest.decision_status.value}`",
        f"- Environment Ref: `{manifest.environment_ref}`",
        "",
        "## Capability Matrix",
        "",
        "| Capability | Native SQLite | DBOS PostgreSQL |",
        "| --- | --- | --- |",
    ]

    native_caps = manifest.capability_matrix.get(RuntimeKind.NATIVE_SQLITE.value)
    dbos_caps = manifest.capability_matrix.get(RuntimeKind.DBOS_POSTGRES.value)

    caps_list = [
        ("Durable Steps", "durable_steps"),
        ("Resume After Crash", "resume_after_crash"),
        ("Durable Wait", "durable_wait"),
        ("Deduplicated Intake", "deduplicated_intake"),
        ("Cancel Before Next Step", "cancel_before_next_step"),
        ("Version Isolation", "version_isolation"),
        ("Bounded Concurrency", "bounded_concurrency"),
    ]

    for label, attr in caps_list:
        nat_val = getattr(native_caps, attr, False) if native_caps else "N/A"
        dbos_val = getattr(dbos_caps, attr, False) if dbos_caps else "N/A"
        lines.append(f"| {label} | {nat_val} | {dbos_val} |")

    lines.extend([
        "",
        "## Scenario Results",
        "",
        "| Scenario | Runtime | Status | Duration (ms) | Recovery (ms) | Peak RSS (MiB) | Effects |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])

    for r in manifest.results:
        rec_ms = f"{r.recovery_ms:.1f}" if r.recovery_ms is not None else "-"
        rss = f"{r.rss_peak_mib:.1f}" if r.rss_peak_mib is not None else "-"
        lines.append(
            f"| {r.scenario_id} | {r.runtime.value} | {r.status.value} | {r.duration_ms:.1f} | {rec_ms} | {rss} | {r.effect_count} |"
        )

    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HF-02 Runtime Lab CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # preflight
    subparsers.add_parser("preflight", help="Check dependencies and catalog hash")

    # run
    run_parser = subparsers.add_parser("run", help="Run scenario suites")
    run_parser.add_argument("--runtime", choices=["native_sqlite", "dbos_postgres", "both"], default="both")
    run_parser.add_argument("--suite", choices=["acceptance", "core"], default="core")
    run_parser.add_argument("--out", required=True, help="Output directory")
    run_parser.add_argument("--lab-id", default=None, help="Lab ID")
    run_parser.add_argument("--repeats", type=int, default=1, help="Repeats per scenario")
    run_parser.add_argument("--lease-seconds", type=float, default=2.0, help="Lease seconds")
    run_parser.add_argument("--validation-mode", choices=["real_lab", "target_environment", "mock_only"], default="real_lab")
    run_parser.add_argument("--environment-ref", default="docs/handoffs/HF-02-ENVIRONMENT.md")

    # verify
    verify_parser = subparsers.add_parser("verify", help="Verify comparison manifest")
    verify_parser.add_argument("--manifest", required=True, help="Path to manifest.json")

    # summarize
    summarize_parser = subparsers.add_parser("summarize", help="Summarize results into Markdown")
    summarize_parser.add_argument("--manifest", required=True, help="Path to manifest.json")
    summarize_parser.add_argument("--out", default=None, help="Output Markdown path")

    args = parser.parse_args(argv)

    if args.command == "preflight":
        return cmd_preflight(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "summarize":
        return cmd_summarize(args)
    return CliExitCode.CONTRACT_ERROR


if __name__ == "__main__":
    sys.exit(main())
