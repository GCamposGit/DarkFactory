"""Comprehensive test suite for HF-15: Acceptance Environment, Test Data, Rollback, and Observability.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 267 & lines 316-333)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 4, line 60 & Section 7, Scenarios G1-G8)
- Invariants:
  1. Fail-closed preflight and environment isolation
  2. Deterministic data coverage for all eight scenarios (G1-G8) and 10 portfolio projects
  3. Proven rollback with measurable RPO and RTO
  4. Structured observability and SLA tracking (dispatch <= 30s, reconciliation <= 60s)
  5. Secret redaction in telemetry and logs
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core.acceptance.cli import main as cli_main
from core.acceptance.environment import HF15EnvironmentManager
from core.acceptance.models import (
    HF15EnvironmentConfig,
    HF15Scenario,
    ScenarioDataFixture,
    ScenarioStatus,
)
from core.acceptance.observability import HF15ObservabilityTracker, sanitize_payload
from core.acceptance.rollback import HF15RollbackCoordinator
from core.acceptance.test_data import get_all_hf15_fixtures, seed_test_data
from hub.backend.main import app


@pytest.fixture
def sandbox_env(tmp_path: Path) -> HF15EnvironmentConfig:
    """Fixture providing isolated sandbox configuration."""
    return HF15EnvironmentConfig(
        sandbox_root=tmp_path / "hf15_sandbox",
        db_type="sqlite_sandbox",
        worker_slots=9,
        n8n_url="https://n8n.ggcampos.com",
        telegram_authorized_users=[8939220558],
        live_mode=False,
    )


# ---------------------------------------------------------------------------
# 1. Provisionamento e Preflight do Ambiente Sandbox
# ---------------------------------------------------------------------------


def test_environment_provisioning_and_metadata(sandbox_env: HF15EnvironmentConfig) -> None:
    """EnvironmentManager provisions directories, metadata, and SQLite sandbox."""
    mgr = HF15EnvironmentManager(config=sandbox_env)
    root = mgr.provision_environment()

    assert root.exists()
    assert (root / "db").is_dir()
    assert (root / "backups").is_dir()
    assert (root / "telemetry").is_dir()
    assert (root / "fixtures").is_dir()
    assert (root / "reports").is_dir()
    assert (root / "env_metadata.json").is_file()

    metadata = json.loads((root / "env_metadata.json").read_text(encoding="utf-8"))
    assert metadata["worker_slots"] == 9
    assert metadata["db_type"] == "sqlite_sandbox"
    assert metadata["live_mode"] is False


def test_preflight_verifications_fail_closed(sandbox_env: HF15EnvironmentConfig) -> None:
    """Preflight passes when slots >= 9 and fails closed when slots are insufficient."""
    mgr = HF15EnvironmentManager(config=sandbox_env)
    mgr.provision_environment()
    report = mgr.run_preflights()

    assert report.all_passed is True
    assert len(report.checks) >= 5
    check_names = {c.name for c in report.checks}
    assert "python_runtime" in check_names
    assert "worker_concurrency_slots" in check_names
    assert "database_connectivity" in check_names
    assert "sandbox_filesystem" in check_names

    # Test fail-closed on insufficient slots
    constrained_config = sandbox_env.model_copy(update={"worker_slots": 4})
    bad_mgr = HF15EnvironmentManager(config=constrained_config)
    bad_report = bad_mgr.run_preflights()

    assert bad_report.all_passed is False
    slot_check = next(c for c in bad_report.checks if c.name == "worker_concurrency_slots")
    assert slot_check.passed is False
    assert "Insufficient slots" in (slot_check.error or "")

    # A synthetic sandbox user is only test input; missing authorization still blocks readiness.
    no_users_config = sandbox_env.model_copy(update={"telegram_authorized_users": []})
    no_users_report = HF15EnvironmentManager(config=no_users_config).run_preflights()
    assert no_users_report.all_passed is False
    auth_check = next(c for c in no_users_report.checks if c.name == "telegram_gateway_auth")
    assert auth_check.passed is False


# ---------------------------------------------------------------------------
# 2. Dados de Teste Determinísticos (Cenários G1 a G8)
# ---------------------------------------------------------------------------


def test_deterministic_test_data_generation_g1_to_g8(tmp_path: Path) -> None:
    """Seed data generates all G1-G8 scenario fixtures and 10 portfolio projects."""
    fixtures_dir = tmp_path / "fixtures"
    seeded = seed_test_data(fixtures_dir)

    assert len(seeded) == 9  # 8 scenarios + portfolio_projects
    for scenario_name in ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]:
        assert scenario_name in seeded
        fixture_path = seeded[scenario_name]
        assert fixture_path.exists()
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture = ScenarioDataFixture.model_validate(raw)
        assert fixture.scenario_id.value == scenario_name
        assert fixture.payload

    # Specific Scenario Validations
    all_fixtures = {f.scenario_id.value: f for f in get_all_hf15_fixtures()}

    # G1: Ambiguous vs Clear demand
    g1 = all_fixtures["G1"]
    assert "ambiguous_demand" in g1.payload
    assert "clear_demand" in g1.payload
    assert g1.payload["clear_demand"]["expected_skip_grill"] is True

    # G2: Replaceable vs Irreplaceable keys
    g2 = all_fixtures["G2"]
    assert g2.payload["replaceable_key"]["name"] == "DEEPSEEK_API_KEY"
    assert g2.payload["irreplaceable_key"]["name"] == "TELEGRAM_BOT_TOKEN"

    # G3: Invalid worker environment blocks readiness
    g3 = all_fixtures["G3"]
    assert g3.payload["worker_preflight"]["network_probe"]["port_5678_open"] is False

    # G4: 9 slots, 4 dev + 5 test jobs
    g4 = all_fixtures["G4"]
    assert g4.payload["available_slots"] == 9
    assert len(g4.payload["dev_jobs"]) == 4
    assert len(g4.payload["test_jobs"]) == 5
    assert len(g4.payload["portfolio_projects"]) == 10

    # G5: Deduplication update_id
    g5 = all_fixtures["G5"]
    assert g5.payload["duplicate_events"][0]["update_id"] == 9001
    assert g5.payload["duplicate_events"][1]["update_id"] == 9001

    # G8: Commercial paid client acceptance
    g8 = all_fixtures["G8"]
    assert g8.payload["paid_project"]["tier"] == "commercial_paid"
    assert g8.payload["independent_project"]["tier"] == "internal_free"


# ---------------------------------------------------------------------------
# 3. Rollback Atômico e Medição de RPO/RTO
# ---------------------------------------------------------------------------


def test_rollback_execution_measures_rpo_rto_and_restores(tmp_path: Path) -> None:
    """Rollback restores corrupted state, verifies checksums, and measures RTO/RPO."""
    backup_root = tmp_path / "backups"
    coordinator = HF15RollbackCoordinator(backup_root=backup_root)

    record = coordinator.run_rollback_drill(
        project_id="proj-drill-01",
        sandbox_dir=tmp_path / "drill_workspace",
    )

    assert record.success is True
    assert record.project_id == "proj-drill-01"
    assert record.rto_seconds >= 0.0
    assert record.rpo_seconds >= 0.0
    assert len(record.evidence_hash) == 64
    assert record.trigger_reason.startswith("production_smoke_failure")

    # Verify history recorded
    history = coordinator.get_history("proj-drill-01")
    assert len(history) == 1
    assert history[0].rollback_id == record.rollback_id


def test_rollback_isolation_scenario_g8(tmp_path: Path) -> None:
    """Scenario G8 invariant: rollback of failing project does not affect independent projects."""
    coordinator = HF15RollbackCoordinator(backup_root=tmp_path / "backups")

    # Project A state (failing project)
    proj_a_dir = tmp_path / "proj_a_state"
    proj_a_dir.mkdir()
    (proj_a_dir / "service.txt").write_text("service_a_stable", encoding="utf-8")
    snap_a = coordinator.create_pre_release_checkpoint("proj_a", proj_a_dir)

    # Project B state (independent healthy project)
    proj_b_dir = tmp_path / "proj_b_state"
    proj_b_dir.mkdir()
    (proj_b_dir / "service.txt").write_text("service_b_production_active", encoding="utf-8")

    # Rollback Project A
    restore_target_a = tmp_path / "proj_a_restored"
    record = coordinator.execute_rollback(
        project_id="proj_a",
        snapshot_id=snap_a.snapshot_id,
        failed_artifact_digest="a" * 64,
        trigger_reason="smoke_failure",
        isolated_restore_target=restore_target_a,
    )

    assert record.success is True
    # Verify Project A restored
    assert (restore_target_a / "service.txt").read_text(encoding="utf-8") == "service_a_stable"
    # Verify Project B untouched
    assert (proj_b_dir / "service.txt").read_text(encoding="utf-8") == "service_b_production_active"


# ---------------------------------------------------------------------------
# 4. Observabilidade, Telemetria e SLAs
# ---------------------------------------------------------------------------


def test_observability_ledger_and_sla_tracking(tmp_path: Path) -> None:
    """Audit ledger logs events, sanitizes secrets, and computes SLA metrics."""
    ledger_path = tmp_path / "telemetry" / "observability_ledger.jsonl"
    tracker = HF15ObservabilityTracker(ledger_path=ledger_path)

    # Test secret sanitization
    raw_payload = {
        "bot_token": "8366386707:AAFTGyHUi38E6kgVKaJbIThww-G_F9OgCJg",
        "api_key": "sk-1234567890abcdef1234567890",
        "endpoint": "https://api.openai.com/v1?token=sk-99999999999999999999",
        "nested": {"secret": "secret_val"},
    }
    sanitized = sanitize_payload(raw_payload)
    assert sanitized["bot_token"] == "[REDACTED_SECRET]"
    assert sanitized["api_key"] == "[REDACTED_SECRET]"
    assert sanitized["nested"]["secret"] == "[REDACTED_SECRET]"

    # Record test events
    tracker.record_event(
        scenario_id="G1",
        event_type="dispatch_completed",
        duration_ms=450.0,
        details={"ticket_id": "TKT-01"},
    )
    tracker.record_event(
        scenario_id="G1",
        event_type="reconciliation_completed",
        duration_ms=1200.0,
    )
    tracker.record_event(
        scenario_id="G1",
        event_type="scenario_passed",
    )
    tracker.record_event(
        scenario_id="G4",
        event_type="slots_allocated",
        details={"active_slots": 9},
    )

    metrics = tracker.get_metrics_summary()
    assert metrics.passed_scenarios == 1
    assert metrics.failed_scenarios == 0
    assert metrics.avg_dispatch_latency_ms == 450.0
    assert metrics.avg_reconciliation_latency_ms == 1200.0
    assert metrics.max_active_slots_used == 9
    assert metrics.all_slas_met is True  # <= 30s and <= 60s

    # Verify ledger file content
    assert ledger_path.is_file()
    lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4

    recent = tracker.get_recent_events(limit=2)
    assert len(recent) == 2


# ---------------------------------------------------------------------------
# 5. CLI Headless (JSON Output)
# ---------------------------------------------------------------------------


def test_cli_headless_commands(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI commands operate headlessly and return valid JSON."""
    monkeypatch.setenv("DARKFAC_HF15_SANDBOX_ROOT", str(tmp_path / "cli_sandbox"))
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "12345678")

    # 1. Preflight
    code = cli_main(["--json", "preflight"])
    assert code == 0
    captured = capsys.readouterr()
    preflight_json = json.loads(captured.out)
    assert preflight_json["all_passed"] is True

    # 2. Seed Data
    code = cli_main(["seed-data", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    seed_json = json.loads(captured.out)
    assert seed_json["status"] == "seeded"
    assert seed_json["fixtures_count"] == 9

    # 3. Rollback Drill
    code = cli_main(["rollback-drill", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    drill_json = json.loads(captured.out)
    assert drill_json["success"] is True
    assert "evidence_hash" in drill_json

    # 4. Status
    code = cli_main(["status", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    status_json = json.loads(captured.out)
    assert status_json["environment"]["all_preflights_passed"] is True

    # 5. Metrics
    code = cli_main(["metrics", "--json"])
    assert code == 0
    captured = capsys.readouterr()
    metrics_json = json.loads(captured.out)
    assert metrics_json["total_scenarios"] == 8


# ---------------------------------------------------------------------------
# 6. Endpoints DarkHub (FastAPI HTTP Reachability)
# ---------------------------------------------------------------------------


def test_hub_endpoints_hf15(monkeypatch: pytest.MonkeyPatch) -> None:
    """DarkHub REST API exposes status, metrics, and rollback drill endpoints."""
    from hub.backend.api import get_hub_service

    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "12345678")
    client = TestClient(app)
    service = get_hub_service()

    # 1. GET /api/hf15/status
    res_status = client.get("/api/hf15/status")
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert "environment" in data_status
    assert "metrics" in data_status
    assert data_status["environment"]["all_preflights_passed"] is True

    # 2. GET /api/hf15/metrics
    res_metrics = client.get("/api/hf15/metrics")
    assert res_metrics.status_code == 200
    data_metrics = res_metrics.json()
    assert data_metrics["total_scenarios"] == 8
    assert "all_slas_met" in data_metrics

    # 3. POST /api/hf15/rollback/drill (requires owner session)
    unauth = client.post("/api/hf15/rollback/drill", json={"project_id": "proj-api-drill"})
    assert unauth.status_code == 401

    res_drill = client.post(
        "/api/hf15/rollback/drill",
        json={"project_id": "proj-api-drill"},
        headers={"X-Hub-Session": service.session_token},
    )
    assert res_drill.status_code == 200
    data_drill = res_drill.json()
    assert data_drill["success"] is True
    assert data_drill["project_id"] == "proj-api-drill"
    assert "rto_seconds" in data_drill
    assert "evidence_hash" in data_drill
