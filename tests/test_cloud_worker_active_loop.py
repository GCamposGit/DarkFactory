"""Tests for CloudWorker active loop, queue polling, and successor materialization.

HF-27-08 rewired `CloudWorker.dispatch_claimed_job` to dispatch through the
line's real `HandlerRegistry` (`core.line.bindings.build_line_registry`)
instead of the old generic-prompt/`deterministic_mock`/`<stage>_deliverable.json`
artifact pipeline (removed by this ticket). The two DAG-progression tests
below inject a fake `HandlerRegistry` (one fake `StageHandler` per line
stage, each returning a real `success` StageResult with distinct
`output_refs`) so they exercise CloudWorker's own claim/dispatch/finish/
materialize wiring without needing a real git remote, `gh`, or agent CLI --
that per-stage behaviour is covered by `tests/line/test_stage_*.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from core.line.bindings import LINE_STAGES
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner, StageContext, StageResult
from core.workflow.handlers import HandlerRegistry


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


class _FakeStageHandler:
    """Always-succeeds StageHandler stub, one per line stage."""

    def __init__(self, stage: str) -> None:
        self.stage = stage

    def handle(self, context: StageContext) -> StageResult:
        return StageResult(outcome="success", output_refs=[f"ref://{self.stage}/{context.claim.job_key.run_id}"])


def _fake_line_registry() -> HandlerRegistry:
    from core.workflow.handlers import build_handlers

    return build_handlers(bindings={stage: _FakeStageHandler(stage) for stage in LINE_STAGES})


def test_worker_poll_and_execute_single_stage(temp_stores):
    store, artifact_store = temp_stores
    worker = CloudWorker(
        worker_id="test-worker-1",
        max_slots=2,
        store=store,
        artifact_store=artifact_store,
        registry=_fake_line_registry(),
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

    # Check that grill completed and successors were materialized.
    # HF-27-08 D-e: no more per-stage memory_observation fan-out.
    status_after = store.get_run_status(receipt.run_id)
    assert status_after is not None
    stages = {j["stage"]: j["status"] for j in status_after["jobs"]}
    assert stages.get("grill") == "succeeded"
    assert stages.get("planning") == "pending"
    assert "memory_observation" not in stages

    grill_job = next(j for j in status_after["jobs"] if j["stage"] == "grill")
    assert grill_job["output_refs"] == [f"ref://grill/{receipt.run_id}"]


def test_worker_autonomous_chain_to_completion(temp_stores):
    store, artifact_store = temp_stores
    from core.orchestrator.cloud_worker import DEFAULT_CAPABILITIES

    worker = CloudWorker(
        worker_id="test-worker-full",
        max_slots=4,
        store=store,
        artifact_store=artifact_store,
        registry=_fake_line_registry(),
        # HF-27-08: every line stage but validation/retrospective requires
        # git + harness:any (core.line.bindings.required_caps); this worker
        # must carry them to progress the whole chain in one test.
        capabilities=list(DEFAULT_CAPABILITIES) + ["git", "harness:any"],
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
            "journey": "Intake -> retrospective",
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
    # HF-27-08 D-a: line DAG stages, plus the single D-e retrospective job
    # (target_journey merged into build_deploy; no memory_observation).
    for stage in LINE_STAGES:
        assert stage in stages, f"stage {stage} missing from run's jobs"
    assert set(stages) == set(LINE_STAGES)

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
