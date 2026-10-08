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
from spikes.runtime_choice.native_adapter import PARK_AFTER_OBSERVED_ENV
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
            "10.0",
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


def _r02_config(tmp_path: Path, effect_base_url: str) -> LabConfig:
    return LabConfig(
        lab_id="usr157",
        root_dir=tmp_path,
        runtime=RuntimeKind.NATIVE_SQLITE,
        runtime_version="native-core",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_usr157",
        effect_base_url=effect_base_url,
        lease_seconds=3.0,
        scenario_timeout_seconds=30.0,
    )


def _index_of(diagnostic: str, fragment: str) -> int:
    position = diagnostic.find(fragment)
    assert position >= 0, f"{fragment!r} missing from diagnostics: {diagnostic}"
    return position


def test_r02_crash_lands_at_the_parked_checkpoint_and_orders_diagnostics(tmp_path: Path) -> None:
    """USR-157: R02 kills a parked driver, so the outcome no longer races the workflow.

    The step count is exactly the frozen spec (S0, S1, S1 again after the crash, S3, S4),
    the effect service never loses a client mid-request, and the failure diagnostics carry
    the ordered event timeline that CI run 37596976676 lacked.
    """

    r02_spec = next(s for s in load_scenario_catalog() if s.scenario_id == "R02")

    with EffectServer(tmp_path) as server:
        controller = ScenarioController(_r02_config(tmp_path, server.base_url), effect_server=server)
        try:
            trace = controller.run_scenario(r02_spec)
        finally:
            controller.shutdown()
        result = ScenarioOracle(server.store).evaluate(trace, r02_spec)

    assert result.status is ResultStatus.PASS, trace.diagnostics
    assert result.actual_step_invocations == r02_spec.expected_step_invocations == 5
    assert result.effect_count == 1
    assert trace.barrier_reached == {"s1_checkpoint_reached": True}

    timeline = next(line for line in trace.diagnostics if line.startswith("controller_timeline:"))
    ordered = [
        "p1 spawned",
        "p1 event step_observed step=S1",
        "p1 killed",
        "lease wait finished",
        "p2 spawned",
        "p2 event step_started step=S1",
        "p2 event completed",
        "p2 stopped",
    ]
    positions = [_index_of(timeline, fragment) for fragment in ordered]
    assert positions == sorted(positions)
    # The parked driver is gone before it can start S3: p1 never reports a later stage.
    assert "p1 event step_started step=S3" not in timeline

    requests = next(line for line in trace.diagnostics if line.startswith("effect_service_requests:"))
    assert "POST /effects status=201" in requests
    # Nobody hung up mid-request: the kill happened while no request was in flight.
    assert "peer_gone" not in requests


def test_spawn_driver_never_inherits_the_park_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The park hook is armed explicitly for the first R02 driver only."""

    captured: list[dict[str, str]] = []

    class FakeProcess:
        pid = 4242
        stderr = None

    def fake_popen(*_args: object, **kwargs: dict[str, str]) -> FakeProcess:
        captured.append(dict(kwargs["env"]))  # type: ignore[arg-type]
        return FakeProcess()

    monkeypatch.setattr("spikes.runtime_choice.controller.subprocess.Popen", fake_popen)
    monkeypatch.setenv(PARK_AFTER_OBSERVED_ENV, "S1")
    controller = ScenarioController(_r02_config(tmp_path, "http://127.0.0.1:1"))

    controller._spawn_driver(tmp_path / "config.json")
    controller._spawn_driver(tmp_path / "config.json", {PARK_AFTER_OBSERVED_ENV: "S1"})

    assert PARK_AFTER_OBSERVED_ENV not in captured[0]
    assert captured[1][PARK_AFTER_OBSERVED_ENV] == "S1"
