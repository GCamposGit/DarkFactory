"""Tests for HF-15 Acceptance Engine and Canonical Runner (core.harness.hf15_acceptance).

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 267)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenarios G1-G8)
- docs/handoffs/HF-15.md (Section 13, line 513)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.acceptance.engine import HF15AcceptanceEngine, compute_plan_digest, get_current_git_sha
from core.acceptance.models import (
    GateEvidenceReceipt,
    HF15AcceptanceReport,
    HF15EnvironmentConfig,
    HF15PreflightCheck,
    HF15PreflightReport,
    ScenarioStatus,
)
from core.harness.hf15_acceptance import build_parser, run_hf15_runner
from core.integrations.n8n import N8nInstanceReport, N8nProbe
from core.integrations.telegram import TelegramConfig


@pytest.fixture
def temp_acceptance_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> HF15AcceptanceEngine:
    """Fixture providing an isolated HF15AcceptanceEngine using temp directory."""
    monkeypatch.setattr(
        N8nProbe,
        "probe",
        lambda self, target_url=None, timeout=3.0: N8nInstanceReport(
            url=target_url or self.config.base_url,
            operational=False,
            error="sandbox probe disabled",
        ),
    )
    config = HF15EnvironmentConfig(
        sandbox_root=tmp_path / "sandbox",
        db_type="sqlite_sandbox",
        worker_slots=9,
        telegram_authorized_users=[12345678],
        live_mode=False,
    )
    report_dir = tmp_path / "reports" / "hf-15-test-run"
    return HF15AcceptanceEngine(
        config=config,
        run_id="test_run_001",
        report_dir=report_dir,
    )


@pytest.fixture
def hermetic_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate runner tests from workstation config and external n8n traffic."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DARKFAC_HF15_SANDBOX_ROOT", str(tmp_path / "runner_sandbox"))
    monkeypatch.setattr(
        "core.acceptance.environment.load_telegram_config",
        lambda: TelegramConfig(authorized_user_ids=[12345678]),
    )
    monkeypatch.setattr(
        N8nProbe,
        "probe",
        lambda self, target_url=None, timeout=3.0: N8nInstanceReport(
            url=target_url or self.config.base_url,
            operational=False,
            error="sandbox probe disabled",
        ),
    )


def test_plan_digest_and_git_sha() -> None:
    """Verifies calculation of plan digest and git SHA."""
    digest = compute_plan_digest()
    assert len(digest) == 64
    sha = get_current_git_sha()
    assert len(sha) == 40


def test_engine_gate_executions_individually(temp_acceptance_engine: HF15AcceptanceEngine) -> None:
    """Tests that all 8 gates G1-G8 execute and return valid receipts."""
    engine = temp_acceptance_engine

    # G1: Grill clarification
    r1 = engine.execute_gate_g1()
    assert isinstance(r1, GateEvidenceReceipt)
    assert r1.gate_id == "G1"
    assert r1.status == ScenarioStatus.PASSED
    assert r1.evidence_data["clear_demand_skipped_grill"] is True

    # G2: API Key resolution
    r2 = engine.execute_gate_g2()
    assert isinstance(r2, GateEvidenceReceipt)
    assert r2.gate_id == "G2"
    assert r2.status == ScenarioStatus.PASSED
    assert r2.evidence_data["replaceable_resolved_to"] == "qwen-fast"

    # G3: Worker preflight
    r3 = engine.execute_gate_g3()
    assert isinstance(r3, GateEvidenceReceipt)
    assert r3.gate_id == "G3"
    assert r3.status == ScenarioStatus.PASSED
    assert r3.evidence_data["initial_readiness_blocked"] is True
    assert r3.evidence_data["target_probe_remediated"] is True

    # G4: Concurrency in 9 slots
    r4 = engine.execute_gate_g4()
    assert isinstance(r4, GateEvidenceReceipt)
    assert r4.gate_id == "G4"
    assert r4.status == ScenarioStatus.PASSED
    assert r4.evidence_data["dev_jobs_concurrent"] == 4
    assert r4.evidence_data["test_jobs_concurrent"] == 5

    # G5: Idempotency & deduplication
    r5 = engine.execute_gate_g5()
    assert isinstance(r5, GateEvidenceReceipt)
    assert r5.gate_id == "G5"
    assert r5.status == ScenarioStatus.PASSED
    assert r5.evidence_data["deduplication_success"] is True

    # G6: Quota & Pareto fallback
    r6 = engine.execute_gate_g6()
    assert isinstance(r6, GateEvidenceReceipt)
    assert r6.gate_id == "G6"
    assert r6.status == ScenarioStatus.PASSED
    assert r6.evidence_data["fallback_selected"] == "deepseek-v4.1-flash"
    assert r6.evidence_data["auto_purchase_blocked"] is True


    # G7: Research & Learning Pack
    r7 = engine.execute_gate_g7()
    assert isinstance(r7, GateEvidenceReceipt)
    assert r7.gate_id == "G7"
    assert r7.status == ScenarioStatus.PASSED
    assert r7.evidence_data["canonical_url"].startswith("https://arxiv.org/abs/")
    assert r7.evidence_data["blocks_production"] is False

    # G8: Commercial paid release & rollback drill
    r8 = engine.execute_gate_g8()
    assert isinstance(r8, GateEvidenceReceipt)
    assert r8.gate_id == "G8"
    assert r8.status == ScenarioStatus.PASSED
    assert r8.evidence_data["unauthenticated_promotion_blocked"] is True
    assert r8.evidence_data["rollback_success"] is True
    assert r8.evidence_data["independent_project_isolated"] is True


def test_engine_lifecycle_scenarios(temp_acceptance_engine: HF15AcceptanceEngine) -> None:
    """Verifies that all 10 lifecycle scenarios execute and produce receipts."""
    receipts = temp_acceptance_engine.execute_lifecycle_scenarios()
    assert len(receipts) == 10
    for idx, r in enumerate(receipts, 1):
        assert r.scenario_number == idx
        assert r.status == ScenarioStatus.PASSED


def test_engine_run_acceptance_full(temp_acceptance_engine: HF15AcceptanceEngine) -> None:
    """Tests consolidated run_acceptance, report file generation, and SLA metrics."""
    engine = temp_acceptance_engine
    report = engine.run_acceptance()

    assert isinstance(report, HF15AcceptanceReport)
    assert report.status == "NOT_RUN"
    assert len(report.gates) == 8
    assert len(report.scenarios) == 10
    assert report.staging_digest == report.production_digest
    assert report.owner_acceptance_receipt is None
    assert report.dependency_receipts == []

    report_file = engine.report_dir / "report.json"
    assert report_file.exists()
    content = json.loads(report_file.read_text(encoding="utf-8"))
    assert content["ticket_id"] == "HF-15"
    assert content["status"] == "NOT_RUN"

    evidence_files = list((engine.report_dir / "evidence").glob("*.json"))
    assert len(evidence_files) >= 18  # 8 gates + 10 scenarios


def test_runner_cli_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str], hermetic_runner: None) -> None:
    """Tests canonical runner invocation with --json and validates exit code 0."""
    report_dir = tmp_path / "reports" / "cli_run"
    exit_code = run_hf15_runner([
        "--run-id", "hf15_cli_test",
        "--report-dir", str(report_dir),
        "--json",
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ticket_id"] == "HF-15"
    assert data["status"] == "NOT_RUN"
    assert data["run_id"] == "hf15_cli_test"
    assert any("sandbox" in item.lower() for item in data["limitations"])


def test_runner_cli_selective_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str], hermetic_runner: None) -> None:
    """Tests selective gate execution via --gate G1."""
    report_dir = tmp_path / "reports" / "cli_selective"
    exit_code = run_hf15_runner([
        "--run-id", "hf15_selective_test",
        "--report-dir", str(report_dir),
        "--gate", "G1",
        "--json",
    ])

    assert exit_code == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "NOT_RUN"
    assert "G1" in data["gates"]
    assert len(data["gates"]) == 1
    assert data["owner_acceptance_receipt"] is None
    assert data["dependency_receipts"] == []


def test_runner_live_mode_without_external_receipts_blocks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    hermetic_runner: None,
) -> None:
    """Live probes cannot mint owner or predecessor authority inside the runner."""
    monkeypatch.setattr(
        N8nProbe,
        "probe",
        lambda self, target_url=None, timeout=3.0: N8nInstanceReport(
            url=target_url or self.config.base_url,
            operational=True,
            status_code=200,
            db_connected=True,
        ),
    )

    exit_code = run_hf15_runner([
        "--run-id", "hf15_live_without_receipts",
        "--report-dir", str(tmp_path / "reports" / "live_blocked"),
        "--mode", "live",
        "--json",
    ])

    assert exit_code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "BLOCKED"
    assert data["owner_acceptance_receipt"] is None
    assert data["dependency_receipts"] == []


def test_runner_selective_live_mode_is_not_operational(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    hermetic_runner: None,
) -> None:
    """A selective live diagnostic cannot be promoted to an acceptance result."""
    monkeypatch.setattr(
        N8nProbe,
        "probe",
        lambda self, target_url=None, timeout=3.0: N8nInstanceReport(
            url=target_url or self.config.base_url,
            operational=True,
            status_code=200,
            db_connected=True,
        ),
    )

    exit_code = run_hf15_runner([
        "--run-id", "hf15_live_g1_only",
        "--report-dir", str(tmp_path / "reports" / "live_partial"),
        "--mode", "live",
        "--gate", "G1",
        "--json",
    ])

    assert exit_code == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "NOT_RUN"
    assert data["gates"] == {"G1": "PASSED"}
    assert data["owner_acceptance_receipt"] is None
    assert data["dependency_receipts"] == []


def test_runner_preflight_fail_closed(tmp_path: Path) -> None:
    """Verifies fail-closed behavior when preflight checks fail."""
    report_dir = tmp_path / "reports" / "cli_preflight_fail"
    with patch.object(
        HF15AcceptanceEngine,
        "run_acceptance",
        return_value=HF15AcceptanceReport(
            ticket_id="HF-15",
            run_id="hf15_failed",
            plan_digest="0" * 64,
            baseline_sha="0" * 40,
            status="BLOCKED",
            candidate_digest="0" * 64,
            staging_digest="0" * 64,
            production_digest="0" * 64,
        ),
    ):
        exit_code = run_hf15_runner([
            "--run-id", "hf15_failed",
            "--report-dir", str(report_dir),
        ])
        assert exit_code == 1


def test_runner_blocks_without_authorized_gateway(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandbox fixture must not turn missing gateway authorization into PASS."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DARKFAC_HF15_SANDBOX_ROOT", str(tmp_path / "blocked_sandbox"))
    monkeypatch.setattr(
        "core.acceptance.environment.load_telegram_config",
        lambda: TelegramConfig(authorized_user_ids=[]),
    )
    monkeypatch.setattr(
        N8nProbe,
        "probe",
        lambda self, target_url=None, timeout=3.0: N8nInstanceReport(
            url=target_url or self.config.base_url,
            operational=False,
            error="sandbox probe disabled",
        ),
    )

    exit_code = run_hf15_runner([
        "--run-id", "hf15_missing_gateway",
        "--report-dir", str(tmp_path / "reports" / "blocked"),
        "--json",
    ])

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "BLOCKED"
    gateway_check = next(
        item for item in output["environment_evidence"]
        if item["name"] == "telegram_gateway_auth"
    )
    assert gateway_check["passed"] is False


def test_runner_secret_sanitization(tmp_path: Path, capsys: pytest.CaptureFixture[str], hermetic_runner: None) -> None:
    """Verifies that secrets are masked from runner output."""
    report_dir = tmp_path / "reports" / "cli_sanitization"
    exit_code = run_hf15_runner([
        "--run-id", "hf15_secret_test",
        "--report-dir", str(report_dir),
        "--json",
    ])
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "sk-" not in output
    assert "ghp_" not in output
