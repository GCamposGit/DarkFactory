"""Automated test suite for HF-05-03: Canonical Persistence and Control Stores.

Governed by ADR-HF-001, CONTRACTS.md, and control.json.
Validates:
1. Full job lifecycle: accept -> claim -> heartbeat -> finish -> outbox materialize.
2. Concurrent claim contention (only one worker acquires the job).
3. Idempotent intake submission and detection of payload conflicts.
4. Stale lease detection, fencing token monotonically increasing, and lease steal on expiry.
5. Idempotent external operations ledger (deduplication by operation_key).
6. Reconciliation sweep of expired leases.
7. Verification on both SQLiteControlStore and PostgresControlStore.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from core.workflow.control_contracts import (
    Claim,
    ExternalOperation,
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
    InvalidResultError,
    JobKey,
    OutboxEvent,
    OutboxNotFoundError,
    ReconcilePage,
    RuntimeOwner,
    StageResult,
    StaleLeaseError,
)
from core.workflow.control_store import ControlStore, SQLiteControlStore
from core.orchestrator.adapters.control_postgres import PostgresControlStore


@pytest.fixture(params=["sqlite", "postgres_mock"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ControlStore:
    """Provides a fresh ControlStore instance for each backend."""
    if request.param == "sqlite":
        db_file = tmp_path / f"control_{request.node.name}.db"
        return SQLiteControlStore(
            db_path=db_file,
            runtime_owner=RuntimeOwner.HF05_SQLITE.value,
            lease_duration_sec=45,
        )
    elif request.param == "postgres_mock":
        return PostgresControlStore(
            mock_mode=True,
            runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
            lease_duration_sec=45,
        )
    raise ValueError(f"Unknown store backend: {request.param}")


def _sample_intake_command(
    channel: str = "cli",
    external_id: str = "ext-001",
    payload_text: str = "Run continuous autonomy unit test",
    project_id: str = "darkfac",
    mode: str = "autonomous",
) -> IntakeCommand:
    return IntakeCommand(
        channel=channel,
        external_id=external_id,
        project_id=project_id,
        payload={
            "title": "Continuous Autonomy Task",
            "problem": payload_text,
            "journey": "Unit testing the control store persistence",
            "non_goals": ["No manual deployment"],
            "criteria": ["Test must pass deterministically"],
        },
        mode=mode,
        policy_ref="policy-v1",
    )


def test_full_lifecycle_accept_claim_heartbeat_finish(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command()

    receipt = store.accept(cmd, now)
    assert receipt.run_id is not None
    assert receipt.initial_job_id is not None
    assert receipt.mode == "autonomous"

    claim = store.claim(worker="worker-alpha", capabilities=["economy", "coding"], now=now)
    assert claim is not None
    assert claim.owner_worker == "worker-alpha"
    assert claim.fencing_token == 1
    assert claim.lease_id is not None
    assert claim.job_key.run_id == receipt.run_id

    hb_time = now + timedelta(seconds=10)
    refreshed_claim = store.heartbeat(claim, hb_time)
    assert refreshed_claim.fencing_token == claim.fencing_token
    assert refreshed_claim.lease_id == claim.lease_id

    finish_time = now + timedelta(seconds=20)
    result = StageResult(
        outcome="success",
        output_refs=["ref:artifact-1"],
        evidence_refs=["ref:evidence-1"],
    )
    store.finish(refreshed_claim, result, finish_time)

    next_claim = store.claim(worker="worker-beta", capabilities=["economy", "coding"], now=finish_time)
    assert next_claim is None


def test_concurrent_claim_contention_single_winner(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent_control.db"
    store1 = SQLiteControlStore(db_path=db_file)
    store2 = SQLiteControlStore(db_path=db_file)

    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="ext-race-1")
    store1.accept(cmd, now)

    claims = []
    errors = []

    def claim_worker(s: SQLiteControlStore, worker_id: str) -> None:
        try:
            c = s.claim(worker=worker_id, capabilities=["economy", "coding"], now=now)
            claims.append(c)
        except Exception as ex:
            errors.append(ex)

    t1 = threading.Thread(target=claim_worker, args=(store1, "worker-1"))
    t2 = threading.Thread(target=claim_worker, args=(store2, "worker-2"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    successful_claims = [c for c in claims if c is not None]
    assert len(successful_claims) == 1
    assert successful_claims[0].fencing_token == 1


def test_idempotent_intake_and_conflict_detection(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(channel="webhook", external_id="idemp-1")

    r1 = store.accept(cmd, now)
    assert r1.run_id is not None

    r2 = store.accept(cmd, now + timedelta(seconds=1))
    assert r2.run_id == r1.run_id
    assert r2.initial_job_id == r1.initial_job_id

    conflicting_cmd = _sample_intake_command(
        channel="webhook",
        external_id="idemp-1",
        payload_text="Completely different divergent payload text",
    )
    with pytest.raises(IdempotencyConflict):
        store.accept(conflicting_cmd, now + timedelta(seconds=2))


def test_lease_expiry_allows_steal_with_higher_fencing_and_rejects_stale_worker(store: ControlStore) -> None:
    t0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="steal-test-1")
    store.accept(cmd, t0)

    claim1 = store.claim("worker-1", ["economy", "coding"], t0)
    assert claim1 is not None
    assert claim1.fencing_token == 1

    t_expired = t0 + timedelta(seconds=50)

    # Reconcile sweeps expired lease and returns job to pending
    page = store.reconcile(t_expired)
    assert len(page.repaired_keys) >= 1

    claim2 = store.claim("worker-2", ["economy", "coding"], t_expired)
    assert claim2 is not None
    assert claim2.owner_worker == "worker-2"
    assert claim2.fencing_token > claim1.fencing_token
    assert claim2.lease_id != claim1.lease_id

    with pytest.raises(StaleLeaseError):
        store.heartbeat(claim1, t_expired + timedelta(seconds=5))

    stale_result = StageResult(
        outcome="success",
        output_refs=["ref:zombie-artifact"],
    )
    with pytest.raises(StaleLeaseError):
        store.finish(claim1, stale_result, t_expired + timedelta(seconds=6))

    valid_result = StageResult(
        outcome="success",
        output_refs=["ref:valid-worker2-artifact"],
    )
    store.finish(claim2, valid_result, t_expired + timedelta(seconds=10))


def test_external_operations_deduplication(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="ext-ops-1")
    store.accept(cmd, now)
    claim = store.claim("worker-ops", ["economy", "coding"], now)
    assert claim is not None

    op = ExternalOperation(
        operation_key="dokploy_deploy:service_01:rev_abc123",
        request_digest="sha256_req_digest_999",
        provider="dokploy",
        external_id="dep_job_555",
        status="succeeded",
        claim_lease_id=claim.lease_id,
        fencing_token=claim.fencing_token,
        observed_at=now.isoformat(),
        response_digest="sha256_resp_digest_888",
    )

    store.record_operation(op, claim)

    fetched = store.get_operation("dokploy_deploy:service_01:rev_abc123")
    assert fetched is not None
    assert fetched.external_id == "dep_job_555"
    assert fetched.status == "succeeded"
    assert fetched.fencing_token == claim.fencing_token


def test_reconciliation_sweep_recovers_orphaned_jobs(store: ControlStore) -> None:
    t0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="recon-1")
    store.accept(cmd, t0)

    claim = store.claim("crashed-worker", ["economy", "coding"], t0)
    assert claim is not None

    page1 = store.reconcile(t0 + timedelta(seconds=20))
    assert len(page1.repaired_keys) == 0

    page2 = store.reconcile(t0 + timedelta(seconds=60))
    assert len(page2.repaired_keys) >= 1

    reclaimed = store.claim("recovery-worker", ["economy", "coding"], t0 + timedelta(seconds=65))
    assert reclaimed is not None
    assert reclaimed.owner_worker == "recovery-worker"
    assert reclaimed.fencing_token >= 2


def test_outbox_materialize_lifecycle(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="outbox-lifecycle-1")
    receipt = store.accept(cmd, now)

    pending = store.get_pending_outbox()
    assert len(pending) >= 1
    intake_event = next(e for e in pending if e["aggregate_id"] == receipt.run_id)
    assert intake_event["status"] == "pending"

    # Materialize the pending outbox event
    store.materialize(intake_event, now)

    # Verify event is no longer pending
    remaining = store.get_pending_outbox()
    assert not any(e["outbox_id"] == intake_event["outbox_id"] for e in remaining)

    # Emit and materialize custom event
    custom_event = OutboxEvent(
        event_type="test_custom_dispatch",
        aggregate_type="run",
        aggregate_id=receipt.run_id or "run-1",
        payload={"step": "validation"},
    )
    emitted = store.emit_outbox(custom_event, now)
    assert emitted.outbox_id is not None
    store.materialize({"outbox_id": emitted.outbox_id}, now)

    # Materializing non-existent outbox event fails with OutboxNotFoundError
    with pytest.raises(OutboxNotFoundError):
        store.materialize({"outbox_id": 999999}, now)


def test_crash_before_commit_rollback_leaves_store_intact(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="crash-test-1")
    store.accept(cmd, now)
    claim = store.claim("worker-crash", ["economy", "coding"], now)
    assert claim is not None
    assert claim.fencing_token == 1

    # Attempt finish with stale fencing token (simulating concurrent mismatch/corruption)
    stale_claim = Claim(
        job_key=claim.job_key,
        lease_id=claim.lease_id,
        owner=claim.owner,
        fencing_token=999,
        expires_at=claim.expires_at,
    )
    result = StageResult(outcome="success", output_refs=["ref:out-1"])
    with pytest.raises(StaleLeaseError):
        store.finish(stale_claim, result, now + timedelta(seconds=5))

    # Verify atomic rollback: valid claim is intact and can still be heartbeat and finished
    refreshed = store.heartbeat(claim, now + timedelta(seconds=10))
    assert refreshed.fencing_token == 1

    store.finish(claim, result, now + timedelta(seconds=15))


def test_intake_mode_documentary(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="doc-mode-1", mode="documentary")
    receipt = store.accept(cmd, now)

    assert receipt.mode == "documentary"
    assert receipt.run_id is None
    assert receipt.initial_job_id is None

    # Documentary mode must not create claimable pending jobs
    claim = store.claim("worker-doc", ["economy", "coding"], now)
    assert claim is None


def test_stage_result_retry_and_terminal_outcomes(store: ControlStore) -> None:
    t0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    cmd = _sample_intake_command(external_id="retry-lifecycle-1")
    store.accept(cmd, t0)

    # Claim 1 -> retry
    claim1 = store.claim("w1", ["economy", "coding"], t0)
    assert claim1 is not None
    assert claim1.fencing_token == 1
    retry_res = StageResult(outcome="retry", cause_code="TRANSIENT_ERR")
    store.finish(claim1, retry_res, t0 + timedelta(seconds=5))

    # Job is re-enqueued as pending; Claim 2 gets incremented fencing token
    claim2 = store.claim("w2", ["economy", "coding"], t0 + timedelta(seconds=6))
    assert claim2 is not None
    assert claim2.fencing_token == 2

    # Retry 2
    store.finish(claim2, retry_res, t0 + timedelta(seconds=10))

    # Claim 3
    claim3 = store.claim("w3", ["economy", "coding"], t0 + timedelta(seconds=11))
    assert claim3 is not None
    assert claim3.fencing_token == 3

    # Retry 3 -> hits max_retries default (3)
    store.finish(claim3, retry_res, t0 + timedelta(seconds=15))

    # Exceeded max_retries -> status is failed, no further claim possible
    claim_after_fail = store.claim("w4", ["economy", "coding"], t0 + timedelta(seconds=20))
    assert claim_after_fail is None

    # Test waiting_human outcome on a separate command
    cmd_wh = _sample_intake_command(external_id="wh-lifecycle-1")
    store.accept(cmd_wh, t0)
    claim_wh = store.claim("w-wh", ["economy", "coding"], t0)
    assert claim_wh is not None
    wh_res = StageResult(outcome="waiting_human", cause_code="GATE_G1_DECISION_PENDING")
    store.finish(claim_wh, wh_res, t0 + timedelta(seconds=5))

    # Waiting human is paused; cannot be claimed until unblocked
    assert store.claim("w-wh2", ["economy", "coding"], t0 + timedelta(seconds=6)) is None


def test_list_active_projects_and_metrics(store: ControlStore) -> None:
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    for p in ["alpha", "beta", "gamma"]:
        cmd = _sample_intake_command(project_id=p, external_id=f"proj-{p}")
        store.accept(cmd, now)

    projects, cursor = store.list_active_projects(limit=2)
    assert len(projects) == 2
    assert cursor is not None

    next_projects, next_cursor = store.list_active_projects(cursor=cursor, limit=2)
    assert len(next_projects) >= 1
    all_projects = sorted(set(projects + next_projects))
    assert "alpha" in all_projects
    assert "beta" in all_projects
    assert "gamma" in all_projects

    # Check metrics after 45s (older than starved threshold 30s)
    metrics = store.get_ready_age_metrics(now + timedelta(seconds=45))
    assert metrics["active_runs"] >= 3
    assert metrics["pending_count"] >= 3
    assert metrics["max_ready_age_sec"] >= 45.0
    assert len(metrics["starved_projects"]) >= 3

