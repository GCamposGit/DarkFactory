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


# --- USR-215: V2 streak from the control store + guidance for pending criteria ---------------


class _FakeCanaryStore:
    """Read-only control store double: canary runs per day and their job outcomes."""

    def __init__(self, days: dict[str, bool], now: datetime) -> None:
        from core.line.bindings import LINE_STAGES

        self._stages = [s for s in LINE_STAGES if s != "retrospective"]
        self._green: dict[str, bool] = {}
        self._rows: list[tuple[str, str]] = []
        for key, green in days.items():
            run_id = f"run-{key}"
            self._green[run_id] = green
            self._rows.append((run_id, f"{key}T06:00:00+00:00"))

    def _connect(self):  # noqa: ANN202
        rows = sorted(self._rows, key=lambda r: r[1], reverse=True)

        class _Cur:
            def fetchall(self_inner):  # noqa: ANN001, ANN202
                return rows

        class _Conn:
            def __enter__(self_inner):  # noqa: ANN001, ANN202
                return self_inner

            def __exit__(self_inner, *a):  # noqa: ANN001, ANN002, ANN202
                return False

            def execute(self_inner, sql, params=()):  # noqa: ANN001, ANN202
                assert "darkfac-canary" in sql
                return _Cur()

        return _Conn()

    def get_run_status(self, run_id: str):  # noqa: ANN201
        stages = self._stages if self._green[run_id] else self._stages[:2]
        status = "succeeded"
        jobs = [{"stage": s, "status": status} for s in stages]
        if not self._green[run_id]:
            jobs.append({"stage": "validation", "status": "failed"})
        return {"status": "completed", "jobs": jobs}


def _utc(y: int, m: int, d: int, h: int = 12) -> datetime:
    return datetime(y, m, d, h, tzinfo=UTC)


def test_verify_v2_streak_from_control_store_history() -> None:
    now = _utc(2026, 10, 11)
    # 04 green; 04-05 failures (04 also has a failed retry, 05 only failed); 06-10 green
    store = _FakeCanaryStore(
        {
            "2026-10-04": True,
            "2026-10-05": False,
            "2026-10-06": True,
            "2026-10-07": True,
            "2026-10-08": True,
            "2026-10-09": True,
            "2026-10-10": True,
        },
        now,
    )
    result = verify_v2(store=store, now=now, min_streak=7)
    assert result.verified is False
    assert result.details["streak"] == 5
    assert result.details["source"] == "control_store"
    assert "Faltam 2 dias verdes" in result.details["missing"]
    assert "11/10" in result.details["next_step"]


def test_verify_v2_control_store_streak_resets_after_gap_and_reaches_target() -> None:
    stale = _FakeCanaryStore({"2026-10-01": True, "2026-10-02": True}, _utc(2026, 10, 9))
    assert verify_v2(store=stale, now=_utc(2026, 10, 9)).details["streak"] == 0

    days = {f"2026-10-{d:02d}": True for d in range(4, 11)}
    done = verify_v2(store=_FakeCanaryStore(days, _utc(2026, 10, 11)), now=_utc(2026, 10, 11))
    assert done.verified is True
    assert done.details["streak"] == 7
    assert done.details["missing"] == ""


def test_verify_v1_pending_explains_next_step() -> None:
    result = verify_v1(store=_EmptyStore())
    assert result.verified is False
    assert "intake" in result.details["next_step"] and "release" in result.details["next_step"]
    assert result.details["missing"]


class _EmptyStore:
    def find_intake_runs(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return []


def test_verify_v1_proof_links_to_live_board(tmp_path: Path) -> None:
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(_fake_ticket("USR-98"))
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    sub = submit_ticket_to_line("USR-98", demands_store=demands, store=store)
    from core.line.bindings import LINE_STAGES

    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE run_id = ?", (sub.run_id,))
        for stage in LINE_STAGES:
            if stage in ("grill", "retrospective"):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    run_id, ticket_id, plan_version, stage, iteration, status,
                    role, required_capabilities, fencing_token, timeout_seconds,
                    retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                    created_at, updated_at, ready_at
                ) VALUES (?, ?, '1.0', ?, 0, 'succeeded', 'worker', '[]', 0, 1800, 0, 3, 0.0, '[]', '[]',
                    '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
                """,
                (sub.run_id, "USR-98", stage),
            )
        conn.commit()
    result = verify_v1(store=store)
    assert result.verified is True
    assert result.details["proof_url"] == f"/live.html?run={sub.run_id}"


class _PostgresLikeStore:
    """Mimics PostgresControlStore: no _connect, runs listed via list_project_runs."""

    def __init__(self, days):
        self._runs = [(f'run-{d}', f'{d}T08:00:00+00:00') for d in sorted(days, reverse=True)]

    def list_project_runs(self, project_id, limit=120):
        assert project_id == 'darkfac-canary'
        return self._runs[:limit]

    def get_run_status(self, run_id):
        from core.line.bindings import LINE_STAGES

        return {'status': 'completed', 'jobs': [{'stage': s, 'status': 'succeeded'} for s in LINE_STAGES]}


def test_verify_v2_reads_postgres_like_store_without_connect() -> None:
    now = _utc(2026, 10, 24, 1)
    days = [f'2026-10-{d}' for d in range(17, 24)]
    result = verify_v2(store=_PostgresLikeStore(days), now=now, min_streak=7)
    assert result.verified is True
    assert result.details['streak'] == 7
    assert result.details['source'] == 'control_store'
