"""Tests for CloudCoordinator FastAPI HTTP ingress and health status endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.workflow.control_contracts import RuntimeOwner


@pytest.fixture
def coordinator_client(tmp_path: Path):
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
    assert data["role"] == "coordinator"
    assert data["application_version"] == "v1"
    assert "database_status" in data
    assert "http_port" in data

    resp_status = client.get("/status")
    assert resp_status.status_code == 200
    assert resp_status.json()["role"] == "coordinator"


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

    resp = client.post("/api/v1/tasks", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["run_id"] is not None
    assert data["demand_id"] is not None

    # Idempotent replay returns same receipt
    resp_replay = client.post("/api/v1/tasks", json=payload)
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

    resp = client.post("/api/v1/tasks", json=payload)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    resp_status = client.get(f"/api/v1/tasks/{run_id}")
    assert resp_status.status_code == 200
    status_data = resp_status.json()
    assert status_data["run_id"] == run_id
    assert status_data["status"] == "active"
    assert len(status_data["jobs"]) >= 1
    assert status_data["jobs"][0]["stage"] == "grill"


def test_get_task_status_not_found(coordinator_client):
    client, _ = coordinator_client
    resp = client.get("/api/v1/tasks/run-non-existent-999")
    assert resp.status_code == 404
