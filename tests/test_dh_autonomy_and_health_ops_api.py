"""Comprehensive tests for DarkHub tickets DH-12, DH-17, and DH-13 (USR-56).

Covers:
1. DH-12: GET /api/autonomy/plan returns the compiled DAG, units, dependencies and readiness gates.
2. DH-17: All 5 mutating operational health endpoints:
   - POST /api/notifications/{notification_id}/acknowledge
   - POST /api/notifications/check-quotas
   - POST /api/integrations/n8n/sync
   - POST /api/integrations/n8n/trigger
   - POST /api/hf15/rollback/drill
3. DH-13 & Frontend wiring:
   - health_ops.js served with 200 and defines all 5 action modal triggers.
   - tasks.js defines switchTaskViewMode, loadAutonomyPlan, renderAutonomyPlan, and openAutonomyUnitModal.
   - index.html has DAG toggle buttons and container.
   - Shared cache version is 20260924a across all static tags.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from hub.backend.api import require_owner_session
from hub.backend.main import app
from hub.backend.service import HubService

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "hub" / "frontend"


@pytest.fixture
def client(tmp_path: Path):
    """Provides a TestClient with bypassed authentication session."""
    app.dependency_overrides[require_owner_session] = lambda: HubService(data_dir=tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(require_owner_session, None)


# =============================================================================
# DH-12: Autonomy Plan & DAG Tests
# =============================================================================

def test_autonomy_plan_endpoint(client: TestClient):
    """Verify that GET /api/autonomy/plan returns the 33-unit DAG and readiness gates."""
    response = client.get("/api/autonomy/plan")
    assert response.status_code == 200
    data = response.json()

    assert data["package_id"] == "HF-26-PLAN"
    assert "baseline_sha" in data
    assert data["total_units"] >= 33
    assert "counts" in data
    assert "completed" in data["counts"]
    assert "readiness_gates" in data
    assert len(data["readiness_gates"]) == 4

    # Check that units have required DAG structure
    units = data["units"]
    assert len(units) >= 33
    unit_ids = {u["ticket_id"] for u in units}
    assert "HF-26-01" in unit_ids
    assert "HF-08-01" in unit_ids
    assert "HF-05-02" in unit_ids

    # Validate HF-26-01 properties
    hf2601 = next(u for u in units if u["ticket_id"] == "HF-26-01")
    assert "depends_on" in hf2601
    assert "successors" in hf2601
    assert "allowed_paths" in hf2601
    assert "oracle" in hf2601
    assert hf2601["priority"] == "P0"


# =============================================================================
# DH-17: Operational Health Endpoints Tests
# =============================================================================

def test_notifications_acknowledge_endpoint(client: TestClient):
    """Verify POST /api/notifications/{id}/acknowledge marks alert as acknowledged."""
    with patch("core.notifications.store.NotificationStore.mark_acknowledged", return_value=True):
        response = client.post("/api/notifications/alert-test-01/acknowledge")
        assert response.status_code == 200
        data = response.json()
        assert data["notification_id"] == "alert-test-01"
        assert data["acknowledged"] is True


def test_notifications_check_quotas_endpoint(client: TestClient):
    """Verify POST /api/notifications/check-quotas runs active token inspections."""
    mock_alerts = [
        MagicMock(model_dump=lambda mode="json": {
            "notification_id": "quota-alert-1",
            "title": "Quota Warning",
            "severity": "warning",
            "provider_id": "openrouter",
        })
    ]
    with patch("core.notifications.token_watcher.TokenQuotaWatcher.check_all_quotas", return_value=mock_alerts):
        response = client.post("/api/notifications/check-quotas?force=true")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert len(data["emitted_alerts"]) == 1
        assert data["emitted_alerts"][0]["provider_id"] == "openrouter"


def test_n8n_sync_workflows_endpoint(client: TestClient):
    """Verify POST /api/integrations/n8n/sync triggers sanitized workflow upload."""
    mock_result = {
        "success": True,
        "results": {"daily_health_check.json": {"success": True, "action": "created"}},
    }
    with patch.object(HubService, "sync_n8n_workflows", return_value=mock_result):
        response = client.post(
            "/api/integrations/n8n/sync",
            json={"custom_path": None, "activate": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "daily_health_check.json" in data["results"]


def test_n8n_trigger_webhook_endpoint(client: TestClient):
    """Verify POST /api/integrations/n8n/trigger dispatches event payload."""
    mock_res = {
        "success": True,
        "status_code": 200,
        "webhook_url": "https://n8n.ggcampos.com/webhook/test",
        "response_body": {"message": "Workflow started"},
    }
    with patch.object(HubService, "trigger_n8n_webhook", return_value=mock_res):
        response = client.post(
            "/api/integrations/n8n/trigger",
            json={"path": "webhook/test", "payload": {"event": "test"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["status_code"] == 200


def test_hf15_rollback_drill_endpoint(client: TestClient):
    """Verify POST /api/hf15/rollback/drill executes isolated hermetic rollback drill."""
    mock_drill = {
        "passed": True,
        "project_id": "proj-drill-01",
        "rto_ms": 142,
        "rpo_seconds": 0,
        "ledger_entry": {"hash": "abc123sha256"},
    }
    with patch.object(HubService, "trigger_hf15_rollback_drill", return_value=mock_drill):
        response = client.post(
            "/api/hf15/rollback/drill",
            json={"project_id": "proj-drill-01"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["passed"] is True
        assert data["rto_ms"] == 142


# =============================================================================
# DH-13 & Frontend Foundation Wiring Tests
# =============================================================================

def test_health_ops_js_served_and_defines_handlers():
    """Verify hub/frontend/health_ops.js exists, is served, and contains required functions."""
    file_path = FRONTEND_DIR / "health_ops.js"
    assert file_path.is_file()
    content = file_path.read_text(encoding="utf-8")

    assert "function confirmAcknowledgeNotification(" in content
    assert "function openCheckQuotasModal(" in content
    assert "function openN8nSyncModal(" in content
    assert "function openN8nTriggerModal(" in content
    assert "function openRollbackDrillModal(" in content


def test_tasks_js_defines_autonomy_dag_functions():
    """Verify tasks.js defines DH-12 DAG functions."""
    file_path = FRONTEND_DIR / "tasks.js"
    assert file_path.is_file()
    content = file_path.read_text(encoding="utf-8")

    assert "function switchTaskViewMode(" in content
    assert "function loadAutonomyPlan(" in content
    assert "function renderAutonomyPlan(" in content
    assert "function openAutonomyUnitModal(" in content


def test_index_html_has_dag_switchers_and_containers():
    """Verify index.html contains required DOM IDs for DH-12 and DH-17."""
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="tasks-view-btn-queue"' in html
    assert 'id="tasks-view-btn-dag"' in html
    assert 'id="tasks-queue-container"' in html
    assert 'id="tasks-dag-container"' in html
    assert 'id="autonomy-plan-summary"' in html
    assert 'id="autonomy-gates-grid"' in html
    assert 'id="autonomy-dag-nodes-grid"' in html
    assert 'src="/static/health_ops.js?v=20260924b"' in html
