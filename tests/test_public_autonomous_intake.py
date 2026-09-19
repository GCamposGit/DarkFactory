"""Deterministic tests for HF-08-02: Public Autonomous Intake via Hub and CLI.

Governed by:
- docs/handoffs/continuous-autonomy/HF-08-02.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/demands/cli.py
- hub/backend/api.py
- hub/backend/service.py
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.store import DemandsStore
from core.workflow.control_contracts import StoreUnavailableError
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated_hub(tmp_path: Path):
    """Sets up an isolated HubService with an SQLiteControlStore."""
    db_path = tmp_path / "control.db"
    demands_json = tmp_path / "demands.json"
    control_store = SQLiteControlStore(db_path=db_path)
    demands_store = DemandsStore(path=demands_json)

    service = HubService(project_root=tmp_path)
    auto_intake = AutonomousIntakeService(store=control_store, demands_store=demands_store)
    service.set_autonomous_intake_service(auto_intake)

    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        yield {
            "client": client,
            "service": service,
            "control_store": control_store,
            "demands_store": demands_store,
            "db_path": db_path,
        }
    finally:
        app.dependency_overrides.pop(get_hub_service, None)


def _sample_demand_payload(title: str = "Test Feature Pipeline") -> dict:
    return {
        "project_id": "darkfac",
        "title": title,
        "problem_statement": "Automate background workflow intake without manual steps",
        "core_journey": "User submits demand via API; system records and starts autonomous run",
        "non_goals": ["Build legacy UI"],
        "acceptance_criteria": ["202 Accepted on submit", "Initial job claimable in ControlStore"],
    }


def test_hub_api_intake_success_202(isolated_hub) -> None:
    """POST /api/demands/intake returns 202 Accepted and commits run and initial job."""
    client: TestClient = isolated_hub["client"]
    control_store: SQLiteControlStore = isolated_hub["control_store"]

    headers = {"Idempotency-Key": "req-intake-001"}
    resp = client.post("/api/demands/intake", json=_sample_demand_payload(), headers=headers)

    assert resp.status_code == status.HTTP_202_ACCEPTED
    data = resp.json()
    assert data["mode"] == "autonomous"
    assert data["demand_id"].startswith("dem-")
    assert data["run_id"].startswith("run-")
    assert data["initial_job_id"].startswith("job-")
    assert data["committed_at"] is not None

    # Verify initial grill job was committed and is claimable
    claim = control_store.claim(worker="grill-worker-1", capabilities=["grill_engine"], now=datetime.now(UTC))
    assert claim is not None
    assert claim.job_key.stage == "grill"
    assert claim.job_key.run_id == data["run_id"]


def test_hub_api_intake_idempotent_replay_202(isolated_hub) -> None:
    """Resubmitting identical payload with same Idempotency-Key returns identical receipt."""
    client: TestClient = isolated_hub["client"]

    headers = {"Idempotency-Key": "req-intake-replay"}
    payload = _sample_demand_payload("Replay Feature")

    resp1 = client.post("/api/demands/intake", json=payload, headers=headers)
    assert resp1.status_code == status.HTTP_202_ACCEPTED
    data1 = resp1.json()

    resp2 = client.post("/api/demands/intake", json=payload, headers=headers)
    assert resp2.status_code == status.HTTP_202_ACCEPTED
    data2 = resp2.json()

    assert data1["demand_id"] == data2["demand_id"]
    assert data1["run_id"] == data2["run_id"]
    assert data1["initial_job_id"] == data2["initial_job_id"]
    assert data1["committed_at"] == data2["committed_at"]


def test_hub_api_intake_conflicting_payload_409(isolated_hub) -> None:
    """Resubmitting differing payload with same Idempotency-Key returns 409 Conflict."""
    client: TestClient = isolated_hub["client"]

    headers = {"Idempotency-Key": "req-intake-conflict"}
    payload1 = _sample_demand_payload("Initial Feature A")
    payload2 = _sample_demand_payload("Altered Feature B")

    resp1 = client.post("/api/demands/intake", json=payload1, headers=headers)
    assert resp1.status_code == status.HTTP_202_ACCEPTED

    resp2 = client.post("/api/demands/intake", json=payload2, headers=headers)
    assert resp2.status_code == status.HTTP_409_CONFLICT
    assert "Idempotency conflict" in resp2.json()["detail"]


def test_hub_api_intake_unauthorized_token_401(isolated_hub) -> None:
    """Invalid or revoked operator token returns 401 Unauthorized before persistence."""
    client: TestClient = isolated_hub["client"]
    control_store: SQLiteControlStore = isolated_hub["control_store"]

    headers = {
        "Idempotency-Key": "req-intake-unauth",
        "X-Operator-Token": "invalid",
    }
    resp = client.post("/api/demands/intake", json=_sample_demand_payload(), headers=headers)

    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Unauthorized operator token" in resp.json()["detail"]

    # Verify no job was persisted
    claim = control_store.claim(worker="worker-1", capabilities=["grill_engine"], now=datetime.now(UTC))
    assert claim is None


def test_hub_api_intake_store_unavailable_503(isolated_hub) -> None:
    """Store unavailability fails closed with 503 and never accepts demand."""
    service: HubService = isolated_hub["service"]
    client: TestClient = isolated_hub["client"]

    mock_auto = MagicMock(spec=AutonomousIntakeService)
    mock_auto.accept.side_effect = StoreUnavailableError("Control database connection lost")
    service.set_autonomous_intake_service(mock_auto)

    headers = {"Idempotency-Key": "req-intake-503"}
    resp = client.post("/api/demands/intake", json=_sample_demand_payload(), headers=headers)

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "Underlying control store unavailable" in resp.json()["detail"]


def test_hub_api_intake_documentary_mode_202(isolated_hub) -> None:
    """Documentary mode returns 202 with null execution run and job IDs."""
    client: TestClient = isolated_hub["client"]

    headers = {"Idempotency-Key": "req-intake-doc"}
    resp = client.post(
        "/api/demands/intake?mode=documentary",
        json=_sample_demand_payload("Doc Mode Demand"),
        headers=headers,
    )

    assert resp.status_code == status.HTTP_202_ACCEPTED
    data = resp.json()
    assert data["mode"] == "documentary"
    assert data["demand_id"].startswith("dem-")
    assert data["run_id"] is None
    assert data["initial_job_id"] is None


def test_cli_intake_subprocess_success(tmp_path: Path) -> None:
    """CLI subprocess creates an atomic run and initial job via canonical intake."""
    db_path = tmp_path / "cli_control.db"
    demands_json = tmp_path / "cli_demands.json"
    demands_json.write_text("[]", encoding="utf-8")

    env = {
        **os.environ,
        "DARKFAC_DEMANDS_PATH": str(demands_json),
    }

    cmd = [
        sys.executable,
        "-m",
        "core.demands.cli",
        "intake",
        "--title",
        "CLI Autonomous Intake Test",
        "--problem",
        "Validate headless intake subprocess",
        "--non-goals",
        "No UI",
        "--criteria",
        "Exit 0 and valid JSON",
        "--external-id",
        "ext-cli-001",
        "--store-path",
        str(db_path),
        "--json",
    ]

    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        env=env,
    )

    out = json.loads(proc.stdout)
    assert out["demand_id"].startswith("dem-")
    assert out["run_id"].startswith("run-")
    assert out["initial_job_id"].startswith("job-")
    assert out["mode"] == "autonomous"

    # Verify job exists in DB
    store = SQLiteControlStore(db_path=db_path)
    claim = store.claim(worker="cli-worker", capabilities=["grill_engine"], now=datetime.now(UTC))
    assert claim is not None
    assert claim.job_key.run_id == out["run_id"]


def test_cli_intake_conflict_fails_closed(tmp_path: Path) -> None:
    """CLI intake with same external ID and conflicting title exits with code 1."""
    db_path = tmp_path / "cli_conflict.db"
    demands_json = tmp_path / "cli_conflict_demands.json"
    demands_json.write_text("[]", encoding="utf-8")

    env = {**os.environ, "DARKFAC_DEMANDS_PATH": str(demands_json)}

    base_args = [
        sys.executable,
        "-m",
        "core.demands.cli",
        "intake",
        "--external-id",
        "ext-cli-conflict",
        "--store-path",
        str(db_path),
    ]

    # First call succeeds
    proc1 = subprocess.run(
        base_args + ["--title", "First Call Title", "--problem", "Problem 1"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc1.returncode == 0

    # Conflicting call with same external-id but different title fails closed
    proc2 = subprocess.run(
        base_args + ["--title", "Conflicting Title", "--problem", "Problem 2"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc2.returncode == 1
    assert "conflito" in proc2.stderr.lower() or "conflito" in proc2.stdout.lower()
