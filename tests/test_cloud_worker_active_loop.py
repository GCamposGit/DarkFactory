"""Tests for CloudWorker active loop, queue polling, and successor materialization."""

from __future__ import annotations

import logging
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner, StageResult


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

    def execute_grill(claim):
        content = json.dumps({
            "run_id": claim.job_key.run_id, "stage": claim.job_key.stage,
            "lease_id": claim.lease_id, "fencing_token": claim.fencing_token,
        })
        artifact = artifact_store.store_artifact(
            claim.job_key.run_id, "grill-test.json", content, "application/json"
        )
        return StageResult(
            outcome="success", output_refs=[artifact.relative_path],
            evidence_refs=[f"sha256:{artifact.sha256}"],
        )

    def verify_grill(claim, result):
        path = artifact_store.root_dir / result.output_refs[0]
        raw = path.read_bytes()
        payload = json.loads(raw)
        return (
            payload["run_id"] == claim.job_key.run_id
            and payload["stage"] == claim.job_key.stage
            and payload["lease_id"] == claim.lease_id
            and payload["fencing_token"] == claim.fencing_token
            and f"sha256:{hashlib.sha256(raw).hexdigest()}" in result.evidence_refs
        )

    worker = CloudWorker(
        worker_id="test-worker-1",
        max_slots=2,
        store=store,
        artifact_store=artifact_store,
        stage_executors={"grill": execute_grill},
        evidence_verifier=verify_grill,
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

    # The worker persists executor evidence, not a fabricated deliverable.
    grill_job = next(j for j in status_after["jobs"] if j["stage"] == "grill")
    assert grill_job["output_refs"] == [f"{receipt.run_id}/grill-test.json"]
    assert grill_job["evidence_refs"][0].startswith("sha256:")
    assert not any(artifact_store.root_dir.rglob("*_deliverable.json"))


def test_worker_without_executors_does_not_fabricate_completion(temp_stores):
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

    assert worker.poll_and_execute_once() is False

    final_status = store.get_run_status(receipt.run_id)
    assert final_status is not None
    assert final_status["status"] == "active"
    assert [j["stage"] for j in final_status["jobs"]] == ["grill"]
    assert final_status["jobs"][0]["status"] == "pending"


def test_worker_rejects_synthetic_success_without_successor(temp_stores, caplog):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        store=store,
        artifact_store=artifact_store,
        stage_executors={"grill": lambda claim: StageResult(
            outcome="success", output_refs=["ref://synthetic/output"],
            evidence_refs=["provider:deterministic_mock"],
        )},
        evidence_verifier=lambda claim, result: True,
    )
    receipt = store.accept(IntakeCommand(
        project_id="darkfac", channel="test", external_id=uuid4().hex,
        mode="autonomous", policy_ref="policy-v1",
        payload={"title": "Synthetic", "problem": "Reject false success",
                 "journey": "None", "non_goals": [], "criteria": []},
    ), datetime.now(UTC))
    with caplog.at_level(logging.ERROR, logger="darkfac.cloud_worker"):
        assert worker.poll_and_execute_once() is True
    assert "Job execution failed" in caplog.text
    jobs = store.get_run_status(receipt.run_id)["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["stage"] == "grill"
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["cause_code"] == "untrusted_stage_evidence"


def test_worker_cannot_advertise_unbound_capabilities():
    with pytest.raises(ValueError, match="bound stage executor"):
        CloudWorker(capabilities=["integrator"])


def test_worker_refuses_unverified_receipt_without_successor(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        store=store, artifact_store=artifact_store,
        stage_executors={"grill": lambda claim: StageResult(
            outcome="success", output_refs=["artifact://inexistente"],
            evidence_refs=["receipt://inexistente"],
        )},
        evidence_verifier=lambda claim, result: False,
    )
    receipt = store.accept(IntakeCommand(
        project_id="darkfac", channel="test", external_id=uuid4().hex,
        mode="autonomous", policy_ref="policy-v1",
        payload={"title": "Missing receipt", "problem": "Reject nonexistent proof",
                 "journey": "None", "non_goals": [], "criteria": []},
    ), datetime.now(UTC))
    assert worker.poll_and_execute_once() is True
    jobs = store.get_run_status(receipt.run_id)["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["cause_code"] == "untrusted_stage_evidence"


def test_worker_does_not_finish_against_expired_lease(temp_stores):
    store, artifact_store = temp_stores
    receipt = store.accept(IntakeCommand(
        project_id="darkfac", channel="test", external_id=uuid4().hex,
        mode="autonomous", policy_ref="policy-v1",
        payload={"title": "Lease expiry", "problem": "Reject stale finish",
                 "journey": "None", "non_goals": [], "criteria": []},
    ), datetime.now(UTC))
    claim = store.claim(worker="lease-test", capabilities=["grill_engine"], now=datetime.now(UTC))
    assert claim is not None
    expiry = datetime.now(UTC) + timedelta(milliseconds=150)
    with store._backend._connect() as conn:
        conn.execute("UPDATE claims SET expires_at = ? WHERE lease_id = ?",
                     (expiry.isoformat(), claim.lease_id))
        conn.commit()
    claim = claim.model_copy(update={"expires_at": expiry.isoformat()})

    calls = []

    def delayed_executor(current_claim):
        calls.append(current_claim.lease_id)
        time.sleep(0.25)
        return StageResult(
            outcome="success", output_refs=["artifact://fake"],
            evidence_refs=["receipt://fake"],
        )

    worker = CloudWorker(
        store=store, artifact_store=artifact_store,
        stage_executors={"grill": delayed_executor},
        evidence_verifier=lambda current_claim, result: True,
    )
    result = worker.dispatch_claimed_job(claim)
    assert calls == [claim.lease_id]
    assert result.success is False
    assert store.get_run_status(receipt.run_id)["jobs"][0]["status"] != "succeeded"


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
