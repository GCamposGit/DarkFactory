"""Integration tests for HF-02-06 controller, oracle, and lab CLI in core test suite."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from spikes.runtime_choice.contracts import (
    DriverEvent,
    DriverEventKind,
    LabConfig,
    ResultStatus,
    RuntimeKind,
    RuntimeStatus,
    ScenarioSpec,
    WorkflowVersion,
)
from spikes.runtime_choice.controller import ExecutionTrace, ScenarioController
from spikes.runtime_choice.effect_server import EffectServer
from spikes.runtime_choice.effect_store import (
    EXPECTED_SCENARIO_CATALOG_SHA256,
    load_scenario_catalog,
    scenario_catalog_hash,
)
from spikes.runtime_choice.oracle import ScenarioOracle

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_scenario_catalog_integrity() -> None:
    """Validate that the scenario catalog matches the frozen SHA-256 digest."""
    digest = scenario_catalog_hash()
    assert digest == EXPECTED_SCENARIO_CATALOG_SHA256
    catalog = load_scenario_catalog()
    assert len(catalog) == 12
    assert [s.scenario_id for s in catalog] == [f"R{i:02d}" for i in range(1, 13)]


def test_cli_preflight_output_and_exit_code() -> None:
    """Preflight must emit clean JSON with catalog confirmation and exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.cli", "preflight"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["schema_version"] == "1"
    assert data["scenario_catalog"]["matches_frozen_digest"] is True
    assert data["runtimes"]["native_sqlite"]["status"] == "ready"


def test_cli_run_verify_and_summarize_native_core(tmp_path: Path) -> None:
    """Run core suite on native_sqlite, verify manifest, and produce summary."""
    run_out = tmp_path / "exp_core"
    run_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.runtime_choice.cli",
            "run",
            "--runtime",
            "native_sqlite",
            "--suite",
            "core",
            "--out",
            str(run_out),
            "--lease-seconds",
            "1.0",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    details = ""
    if run_res.returncode != 0:
        results_file = run_out / "results.json"
        if results_file.exists():
            details = f"\nResults:\n{results_file.read_text(encoding='utf-8')}"
    assert run_res.returncode == 0, f"run failed: {run_res.stderr}\n{run_res.stdout}{details}"

    manifest_file = run_out / "manifest.json"
    results_file = run_out / "results.json"
    comparison_file = run_out / "COMPARISON.md"

    assert manifest_file.exists()
    assert results_file.exists()
    assert comparison_file.exists()

    # Verify manifest
    verify_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.runtime_choice.cli",
            "verify",
            "--manifest",
            str(manifest_file),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert verify_res.returncode == 0, f"verify failed: {verify_res.stderr}\n{verify_res.stdout}"
    assert "[PASS] Manifest verified successfully." in verify_res.stdout

    # Summarize manifest
    summarize_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.runtime_choice.cli",
            "summarize",
            "--manifest",
            str(manifest_file),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert summarize_res.returncode == 0
    assert "# HF-02 Runtime Spike Comparison Report" in summarize_res.stdout


def test_cli_verify_rejects_corrupted_manifest(tmp_path: Path) -> None:
    """Verifier must reject manifest when structure or assertions are invalid."""
    bad_manifest = tmp_path / "corrupt_manifest.json"
    bad_manifest.write_text(json.dumps({"invalid": "data"}), encoding="utf-8")

    verify_res = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.runtime_choice.cli",
            "verify",
            "--manifest",
            str(bad_manifest),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert verify_res.returncode == 3
    assert "[ERROR] Invalid comparison manifest" in verify_res.stderr
