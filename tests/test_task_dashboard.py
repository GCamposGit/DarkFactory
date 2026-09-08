"""DF-21 tests for the read-only DarkHub task dashboard projection."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from core.orchestrator.store import OrchestratorStore
from core.usage.models import ModelCallEvent
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


def _make_service(tmp_path: Path) -> HubService:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")

    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "tasks": {
                    "task-running": {
                        "id": "task-running",
                        "status": "IMPLEMENTING",
                        "updated_at": "2026-09-08T12:00:00+00:00",
                        "metadata": {
                            "title": "Painel operacional",
                            "stage": "render",
                            "priority": 10,
                            "evidence_refs": [{"label": "commit", "value": "abc123", "source": "git"}],
                        },
                    },
                    "task-failed": {
                        "id": "task-failed",
                        "status": "FAILED",
                        "metadata": {"title": "Tarefa com falha", "error": "timeout do worker"},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    orchestrator_path = tmp_path / "orchestrator.sqlite3"
    store = OrchestratorStore(orchestrator_path)
    store.create_run(
        "task-running",
        run_id="run-001",
        checkpoint={"stage": "render", "cost_usd": 0.25, "evidence": {"step": "checkpoint-1"}},
    )

    service = HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        state_path=state_path,
        orchestrator_path=orchestrator_path,
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
    assert report.total_cost_usd == 0.75
    assert report.queue[0].task_id == "task-running"
    assert report.queue[0].run_id == "run-001"
    assert report.queue[0].stage == "render"
    assert report.queue[0].cost_usd == 0.25
    assert {item.label for item in report.queue[0].evidence} == {"commit", "step"}
    assert report.queue[1].exceptions == ["timeout do worker"]
    assert report.sources == {"state": "ok", "runs": "ok", "usage": "ok"}


def test_task_dashboard_is_graceful_when_ledgers_are_missing(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    service = HubService(data_dir=data_dir, roadmap_root=Path.cwd(), usage_dir=tmp_path / "usage")

    report = service.get_task_dashboard()

    assert report.queue == []
    assert report.sources["state"] == "missing"
    assert report.sources["runs"] == "missing"
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

        page = client.get("/")
        assert page.status_code == 200
        assert 'id="tasks-dashboard-section"' in page.text
        assert "/static/tasks.js" in page.text
    finally:
        app.dependency_overrides.clear()
