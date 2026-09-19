"""Tests for CloudWorker active loop, queue polling, and successor materialization."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner


@pytest.fixture
def temp_stores(tmp_path: Path):
    db_file = tmp_path / "control_mock.db"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # PostgresControlStore in mock mode backed by SQLiteControlStore
    store = PostgresControlStore(
        mock_mode=True,
        runtime_owner=RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec=30,
    )
    # Point the mock backend to db_file
    store._backend.db_path = db_file

    artifact_store = CloudArtifactStore(root_dir=artifacts_dir)
    return store, artifact_store


def test_worker_poll_and_execute_single_stage(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        worker_id="test-worker-1",
        max_slots=2,
        store=store,
        artifact_store=artifact_store,
    )

    # Ingest task
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test_channel",
        external_id=f"test-ext-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "Autonomous Pipeline Test",
            "problem": "Validate zero-touch worker loop execution",
            "journey": "Intake -> Claim -> Execute -> Successors",
            "non_goals": ["No manual intervention"],
            "criteria": ["Status = succeeded"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    assert receipt.run_id is not None

    # Verify initial run state
    status_before = store.get_run_status(receipt.run_id)
    assert status_before is not None
    assert status_before["status"] == "active"
    assert len(status_before["jobs"]) == 1
    assert status_before["jobs"][0]["stage"] == "grill"
    assert status_before["jobs"][0]["status"] == "pending"

    # Poll and execute once
    executed = worker.poll_and_execute_once()
    assert executed is True

    # Check that grill completed and successors were materialized
    status_after = store.get_run_status(receipt.run_id)
    assert status_after is not None
    stages = {j["stage"]: j["status"] for j in status_after["jobs"]}
    assert stages.get("grill") == "succeeded"
    assert stages.get("planning") == "pending"
    assert stages.get("memory_observation") == "pending"

    # Check artifact was persisted
    grill_job = next(j for j in status_after["jobs"] if j["stage"] == "grill")
    assert len(grill_job["output_refs"]) > 0
    art_path = artifact_store.root_dir / grill_job["output_refs"][0]
    assert art_path.exists()
    payload = json.loads(art_path.read_text(encoding="utf-8"))
    assert payload["run_id"] == receipt.run_id
    assert payload["stage"] == "grill"


def test_worker_autonomous_chain_to_completion(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        worker_id="test-worker-full",
        max_slots=4,
        store=store,
        artifact_store=artifact_store,
    )

    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test_channel",
        external_id=f"test-chain-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "Full Chain Autonomy Test",
            "problem": "Run full DAG chain to completion",
            "journey": "Intake -> Target Journey",
            "non_goals": ["No mocks", "No bypass"],
            "criteria": ["Status = completed"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))

    # Run worker poll loop until no more jobs are pending
    max_steps = 25
    steps = 0
    while steps < max_steps:
        executed = worker.poll_and_execute_once()
        if not executed:
            break
        steps += 1

    assert steps > 5, "Worker should have autonomously progressed through multiple DAG stages"

    final_status = store.get_run_status(receipt.run_id)
    assert final_status is not None
    assert final_status["status"] == "completed"

    stages = [j["stage"] for j in final_status["jobs"]]
    assert "grill" in stages
    assert "planning" in stages
    assert "development" in stages
    assert "validation" in stages
    assert "independent_review" in stages
    assert "integration" in stages
    assert "build_deploy" in stages
    assert "target_journey" in stages

    for j in final_status["jobs"]:
        assert j["status"] == "succeeded", f"Stage {j['stage']} was not succeeded: {j['status']}"


def test_worker_backpressure_when_saturated(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        worker_id="test-worker-sat",
        max_slots=1,
        store=store,
        artifact_store=artifact_store,
    )

    # Saturate worker slots
    worker.try_acquire_slot("fake-job:stage")
    assert worker.slot_status().is_saturated is True

    # When saturated, poll_and_execute_once should not claim work
    executed = worker.poll_and_execute_once()
    assert executed is False

    worker.release_slot("fake-job:stage")
    assert worker.slot_status().is_saturated is False


def test_worker_draining_rejects_new_claims(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        worker_id="test-worker-drain",
        max_slots=2,
        store=store,
        artifact_store=artifact_store,
    )

    worker.drain(timeout_seconds=0.1)
    assert worker.slot_status().is_draining is True

    executed = worker.poll_and_execute_once()
    assert executed is False
