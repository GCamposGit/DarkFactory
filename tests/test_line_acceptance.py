"""Tests for HF-27 Acceptance Probe (USR-93)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.acceptance import (
    CriterionResult,
    reconcile_roadmap_manifest,
    run_acceptance_verification,
    save_acceptance_reports,
    verify_v1,
    verify_v2,
    verify_v3,
    verify_v4,
)
from core.line.canary import CanaryReport, SmokeResult
from core.line.owner_intake import submit_ticket_to_line
from core.roadmap.models import DeliveryStatus, PlanningHorizon
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import router
from hub.backend.main import app
from hub.backend.models import LineStatusResponse
from hub.backend.service import HubService


def _fake_ticket(ticket_id: str) -> UserTicket:
    return UserTicket(
        id=ticket_id,
        title=f"Test {ticket_id}",
        tags=["line-ok"],
        horizon=PlanningHorizon.NOW,
        acceptance_criteria=["Works"],
        created_at=datetime.now(UTC),
    )


def test_verify_v1_without_runs(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    result = verify_v1(store=store)
    assert isinstance(result, CriterionResult)
    assert result.id == "V1"
    assert result.verified is False


def test_verify_v1_with_succeeded_run(tmp_path: Path) -> None:
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(_fake_ticket("USR-99"))
    store = SQLiteControlStore(db_path=tmp_path / "control.db")

    sub = submit_ticket_to_line("USR-99", demands_store=demands, store=store)
    assert sub.ok and sub.run_id

    # Simulate completed run with integration delivery evidence
    pr_url = "https://github.com/GCamposGit/DarkFactory/pull/299"
    merge_sha = "abc9876543210fedcba9876543210fedcba98765"
    from core.line.bindings import LINE_STAGES

    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE run_id = ?", (sub.run_id,))
        for stage in LINE_STAGES:
            if stage in ("grill", "retrospective"):
                continue
            output_refs = json.dumps([pr_url, merge_sha]) if stage == "integration" else "[]"
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    run_id, ticket_id, plan_version, stage, iteration, status,
                    role, required_capabilities, fencing_token, timeout_seconds,
                    retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                    created_at, updated_at, ready_at
                ) VALUES (?, ?, '1.0', ?, 0, 'succeeded', 'worker', '[]', 0, 1800, 0, 3, 0.0, ?, '[]', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
                """,
                (sub.run_id, "USR-99", stage, output_refs),
            )
        conn.commit()

    result = verify_v1(store=store)
    assert result.verified is True
    assert pr_url in result.evidence
    assert merge_sha in result.evidence


def test_verify_v2_canary_streak(tmp_path: Path) -> None:
    reports_dir = tmp_path / "canary_reports"
    reports_dir.mkdir(parents=True)

    # Initially empty: streak is 0
    res_empty = verify_v2(reports_dir=reports_dir, min_streak=7)
    assert res_empty.verified is False

    # Create 7 consecutive green reports
    base_date = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(7):
        day_str = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        report = CanaryReport(
            date=day_str,
            scenario="normal",
            external_id=f"canary-{day_str}",
            passed=True,
            outcome="passed",
            smoke=SmokeResult(
                ok=True,
                url="http://test/smoke",
                expected_date=day_str,
                expected_sha="a" * 40,
                observed_sha="a" * 40,
            ),
        )
        (reports_dir / f"{day_str}.json").write_text(
            json.dumps(report.model_dump(mode="json")), encoding="utf-8"
        )

    res_green = verify_v2(reports_dir=reports_dir, min_streak=7)
    assert res_green.verified is True
    assert res_green.details["streak"] == 7


def test_verify_v3_failover_and_cooldown() -> None:
    result = verify_v3()
    assert result.id == "V3"
    assert result.verified is True


def test_verify_v4_adoption() -> None:
    result = verify_v4()
    assert result.id == "V4"
    assert result.verified is True


def test_run_acceptance_verification_and_save(tmp_path: Path) -> None:
    out_dir = tmp_path / "reports" / "line-acceptance"
    report = run_acceptance_verification(
        v1_override=True,
        v2_override=True,
    )
    assert report.all_verified is True
    assert report.verified_count == 4

    json_path, md_path = save_acceptance_reports(report, out_dir=out_dir)
    assert json_path.exists()
    assert md_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["all_verified"] is True
    assert len(data["criteria"]) == 4

    md_content = md_path.read_text(encoding="utf-8")
    assert "V1: Demanda autonoma ponta a ponta" in md_content
    assert "VERIFICADO (APROVADO)" in md_content


def test_reconcile_roadmap_manifest(tmp_path: Path) -> None:
    roadmap_file = tmp_path / "darkfac.json"
    roadmap_file.write_text(
        json.dumps({
            "items": [
                {
                    "id": "HF-27",
                    "delivery_status": "planned",
                    "evidence_refs": [],
                }
            ]
        }),
        encoding="utf-8",
    )

    report = run_acceptance_verification(v1_override=True, v2_override=True)
    updated = reconcile_roadmap_manifest(report, roadmap_path=roadmap_file)
    assert updated is True

    data = json.loads(roadmap_file.read_text(encoding="utf-8"))
    hf27 = data["items"][0]
    assert hf27["delivery_status"] == DeliveryStatus.COMPLETED.value
    assert any(ref["evidence_id"] == "report:HF-27:line-acceptance" for ref in hf27["evidence_refs"])


def test_hub_get_line_status(tmp_path: Path) -> None:
    service = HubService(project_root=tmp_path, control_db_path=tmp_path / "control.db")
    status = service.get_line_status()
    assert isinstance(status, LineStatusResponse)
    assert isinstance(status.canary_streak, int)
    assert isinstance(status.active_runs, list)
    assert isinstance(status.nodes, list)


def test_api_get_line_status() -> None:
    client = TestClient(app)
    response = client.get("/api/line/status")
    assert response.status_code == 200
    data = response.json()
    assert "canary_streak" in data
    assert "active_runs_count" in data
    assert "nodes" in data
    assert "generated_at" in data
