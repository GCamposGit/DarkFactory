"""DF-21 tests for the read-only DarkHub task dashboard projection (migrated in DH-16)."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from core.usage.models import ModelCallEvent
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


def _make_service(tmp_path: Path) -> HubService:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")

    control_db = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=control_db)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-001', 'darkfac', 'task-running', 'v1', 'hf05_sqlite', 'autonomous', 'active', 'd', 'c', '2026-09-08T12:00:00+00:00', '2026-09-08T12:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code, actual_cost, evidence_refs, created_at, updated_at)"
            " VALUES ('run-001', 'task-running', 'p1', 'render', 0, 'running', 'worker', NULL, 0.25, ?, '2026-09-08T12:00:00+00:00', '2026-09-08T12:00:00+00:00')",
            (json.dumps(["commit:abc123", "step:checkpoint-1"]),),
        )
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-002', 'darkfac', 'task-failed', 'v1', 'hf05_sqlite', 'autonomous', 'failed', 'd', 'c', '2026-09-08T12:00:00+00:00', '2026-09-08T12:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code, actual_cost, evidence_refs, created_at, updated_at)"
            " VALUES ('run-002', 'task-failed', 'p1', 'build', 0, 'failed', 'worker', 'timeout do worker', 0.0, '[]', '2026-09-08T12:00:00+00:00', '2026-09-08T12:00:00+00:00')",
        )
        conn.commit()

    service = HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        control_db_path=control_db,
        control_database_url="",
    )
    service.record_model_usage(
        ModelCallEvent(
            invocation_id="dashboard-event-001",
            provider="ollama",
            model="qwen",
            input_tokens=10,
            output_tokens=20,
            cost_usd=0.75,
        )
    )
    return service


def test_task_dashboard_combines_queue_runs_cost_evidence_and_exceptions(tmp_path: Path):
    service = _make_service(tmp_path)

    report = service.get_task_dashboard()

    assert report.queued_count == 2
    assert report.running_count == 1
    assert report.total_cost_usd == 0.25
    item_running = next(i for i in report.queue if i.task_id == "task-running")
    assert item_running.run_id == "run-001"
    assert item_running.stage == "render"
    assert item_running.cost_usd == 0.25
    item_failed = next(i for i in report.queue if i.task_id == "task-failed")
    assert any("timeout do worker" in exc for exc in item_failed.exceptions)
    assert report.sources == {"control": "sqlite:ok", "usage": "control"}


def test_task_dashboard_is_graceful_when_ledgers_are_missing(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    service = HubService(
        data_dir=data_dir,
        roadmap_root=Path.cwd(),
        usage_dir=tmp_path / "usage",
        control_db_path=tmp_path / "control.db",
        control_database_url="",
    )

    report = service.get_task_dashboard()

    assert report.queue == []
    assert report.sources["control"] == "sqlite:missing"
    assert not (tmp_path / "control.db").exists(), "the Hub must never create the control store"
    assert "state" not in report.sources, "legacy state source is retired in DH-16"
    assert "runs" not in report.sources, "legacy runs source is retired in DH-16"
    assert report.warnings == []


def test_task_dashboard_api_and_static_journey(tmp_path: Path):
    service = _make_service(tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.get("/api/tasks/dashboard")
        assert response.status_code == 200
        assert response.json()["queue"][0]["run_id"] == "run-001"

        alias_response = client.get("/api/tasks")
        assert alias_response.status_code == 200

        # HF-13: Verify root aliases and trailing-slash variants prevent 404
        root_response = client.get("/tasks/dashboard")
        assert root_response.status_code == 200
        assert root_response.json()["queue"][0]["run_id"] == "run-001"

        root_slash = client.get("/tasks/dashboard/")
        assert root_slash.status_code == 200

        api_slash = client.get("/api/tasks/dashboard/")
        assert api_slash.status_code == 200

        root_alias = client.get("/tasks")
        assert root_alias.status_code == 200

        root_alias_slash = client.get("/tasks/")
        assert root_alias_slash.status_code == 200

        api_alias_slash = client.get("/api/tasks/")
        assert api_alias_slash.status_code == 200

        page = client.get("/")
        assert page.status_code == 200
        assert 'id="tasks-dashboard-section"' in page.text
        assert "/static/tasks.js" in page.text
    finally:
        app.dependency_overrides.clear()
