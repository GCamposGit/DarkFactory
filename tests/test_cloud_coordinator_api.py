"""Tests for CloudCoordinator FastAPI HTTP ingress and health status endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner

TEST_TOKEN = "test-only-coordinator-token-32-characters-long"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

@pytest.fixture
def coordinator_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DARKFAC_COORDINATOR_API_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("DARKFAC_INTAKE_ENABLED", "true")
    db_file = tmp_path / "coordinator_api.db"
    store = PostgresControlStore(
        mock_mode=True,
        runtime_owner=RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec=30,
    )
    store._backend.db_path = db_file

    coordinator = CloudCoordinator()
    coordinator._store_instance = store
    app = coordinator.create_app()
    return TestClient(app), store


def test_healthz_and_status(coordinator_client):
    client, _ = coordinator_client
    resp = client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"status": "ok"}

    resp_status = client.get("/status")
    assert resp_status.status_code == 401
    resp_status = client.get("/status", headers=AUTH)
    assert resp_status.status_code == 200
    assert resp_status.json()["role"] == "coordinator"
    assert client.get("/readyz").status_code == 401


def test_submit_task_ingress(coordinator_client):
    client, store = coordinator_client
    payload = {
        "channel": "dokploy_ingress",
        "external_id": f"task-{uuid4().hex[:8]}",
        "project_id": "darkfac",
        "mode": "autonomous",
        "policy_ref": "policy-cloud-v1",
        "payload": {
            "title": "Cloud Ingress Task",
            "problem": "Validate remote HTTP submission",
            "journey": "Client -> HTTP Ingress -> Postgres",
            "non_goals": ["No manual web terminal"],
            "criteria": ["Response 202 with run_id"],
        },
    }

    resp = client.post("/api/v1/tasks", json=payload, headers=AUTH)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["run_id"] is not None
    assert data["demand_id"] is not None

    # Idempotent replay returns same receipt
    resp_replay = client.post("/api/v1/tasks", json=payload, headers=AUTH)
    assert resp_replay.status_code == 202
    assert resp_replay.json()["run_id"] == data["run_id"]


def test_get_task_status_endpoint(coordinator_client):
    client, store = coordinator_client
    payload = {
        "channel": "dokploy_ingress",
        "external_id": f"task-{uuid4().hex[:8]}",
        "project_id": "darkfac",
        "mode": "autonomous",
        "policy_ref": "policy-cloud-v1",
        "payload": {
            "title": "Status Query Task",
            "problem": "Verify run status polling",
            "journey": "Client -> Query",
            "non_goals": ["No direct DB access"],
            "criteria": ["JSON run details returned"],
        },
    }

    resp = client.post("/api/v1/tasks", json=payload, headers=AUTH)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    resp_status = client.get(f"/api/v1/tasks/{run_id}", headers=AUTH)
    assert resp_status.status_code == 200
    status_data = resp_status.json()
    assert status_data["run_id"] == run_id
    assert status_data["status"] == "active"
    assert len(status_data["jobs"]) >= 1
    assert status_data["jobs"][0]["stage"] == "grill"


def test_get_task_status_not_found(coordinator_client):
    client, _ = coordinator_client
    resp = client.get("/api/v1/tasks/run-non-existent-999", headers=AUTH)
    assert resp.status_code == 404


def test_ingress_and_run_status_reject_missing_or_wrong_token(coordinator_client):
    client, store = coordinator_client
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        assert client.post("/api/v1/tasks", json={}, headers=headers).status_code == 401
        assert client.get("/api/v1/tasks/run-any", headers=headers).status_code == 401
    assert client.get("/status").status_code == 401


def test_api_fails_closed_when_secret_unconfigured(coordinator_client, monkeypatch):
    client, _ = coordinator_client
    monkeypatch.delenv("DARKFAC_COORDINATOR_API_TOKEN")
    assert client.post("/api/v1/tasks", json={}, headers=AUTH).status_code == 503
    assert client.get("/api/v1/tasks/run-any", headers=AUTH).status_code == 503


def test_intake_disabled_by_default(coordinator_client, monkeypatch):
    client, _ = coordinator_client
    monkeypatch.delenv("DARKFAC_INTAKE_ENABLED")
    payload = {
        "project_id": "darkfac", "channel": "test", "external_id": "disabled",
        "mode": "autonomous", "policy_ref": "policy-v1",
        "payload": {"title": "Disabled", "problem": "No activation",
                    "journey": "None", "non_goals": [], "criteria": []},
    }
    assert client.post("/api/v1/tasks", json=payload, headers=AUTH).status_code == 503


def test_ingress_rejects_other_project(coordinator_client):
    client, _ = coordinator_client
    payload = {
        "project_id": "another-project", "channel": "test", "external_id": "cross-project",
        "mode": "autonomous", "policy_ref": "policy-v1",
        "payload": {"title": "Cross project", "problem": "Reject scope escape",
                    "journey": "None", "non_goals": [], "criteria": []},
    }
    assert client.post("/api/v1/tasks", json=payload, headers=AUTH).status_code == 403


def test_status_hides_run_from_other_project(coordinator_client):
    client, store = coordinator_client
    receipt = store.accept(
        IntakeCommand(
            project_id="other-project", channel="test", external_id="foreign-run",
            mode="autonomous", policy_ref="policy-v1",
            payload={"title": "Foreign", "problem": "Isolation", "journey": "None",
                     "non_goals": [], "criteria": []},
        ), datetime.now(UTC),
    )
    assert client.get(f"/api/v1/tasks/{receipt.run_id}", headers=AUTH).status_code == 404


def test_intake_error_does_not_expose_exception(coordinator_client, monkeypatch):
    client, store = coordinator_client
    monkeypatch.setattr(store, "accept", lambda *args: (_ for _ in ()).throw(RuntimeError("secret-value")))
    payload = {
        "project_id": "darkfac", "channel": "test", "external_id": "error-case",
        "mode": "autonomous", "policy_ref": "policy-v1",
        "payload": {"title": "Error", "problem": "Check HTTP error", "journey": "None",
                    "non_goals": [], "criteria": []},
    }
    response = client.post("/api/v1/tasks", json=payload, headers=AUTH)
    assert response.status_code == 500
    assert "secret-value" not in response.text


def test_readiness_is_not_liveness(coordinator_client, monkeypatch):
    client, _ = coordinator_client
    monkeypatch.delenv("DARKFAC_INTAKE_ENABLED")
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz", headers=AUTH).status_code == 503
