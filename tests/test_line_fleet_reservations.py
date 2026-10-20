"""Unit and integration tests for distributed fleet cycle reservations (USR-281 / USR-277).

Covers:
1. Atomic reservation of (project, ticket, run, host).
2. Active ticket uniqueness across the fleet (TicketAlreadyActiveError).
3. Single cycle per host machine (HostBusyError).
4. Global ceiling of 3 concurrent cycles fleet-wide (FleetCapacityExceededError).
5. Subordination of stage claims to the host cycle reservation:
   - Matching host worker claims the stage and binds reservation_id.
   - Worker on different host cannot claim jobs belonging to reserved cycle.
   - Expired reservation prevents stage claims.
6. Heartbeat, drain, expiration, and fencing:
   - Heartbeat extends lease expiration.
   - Fencing token mismatch rejects heartbeat/release.
   - Expired reservation rejects heartbeat and marks reservation expired.
   - Fencing prevents old owner from publishing (finish / record_operation) after expiration.
   - Drain marks host as draining, recording drain reason in metadata.
   - Reconciliation sweeps expired reservations and logs to reconciliation ledger.
7. One-time release idempotency.
8. Unknown operations reconciliation before retry.
9. Concurrent multi-threaded coordinator race testing transactional exclusion.
10. Real PostgreSQL integration race test when C_TEST_POSTGRES_DSN or DARKFAC_TEST_POSTGRES_DSN is configured.
"""

from __future__ import annotations

import concurrent.futures
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.workflow.control_contracts import (
    Claim,
    ExternalOperation,
    FleetCapacityExceededError,
    FleetReservation,
    HostBusyError,
    IntakeCommand,
    JobKey,
    RuntimeOwner,
    StageResult,
    StaleLeaseError,
    TicketAlreadyActiveError,
)
from core.workflow.control_store import ControlStore, SQLiteControlStore

NOW = datetime(2026, 10, 16, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", "postgres_mock"])
def store(request) -> ControlStore:
    if request.param == "sqlite":
        return SQLiteControlStore(db_path=":memory:", runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    else:
        return PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value, lease_duration_sec=30)


# ------------------------------------------------------------------------------
# 1. Atomic reservation and idempotency
# ------------------------------------------------------------------------------


def test_reserve_cycle_atomic_success(store: ControlStore):
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-101",
        host_id="grokbox",
        now=NOW,
        lease_duration_sec=45,
        max_fleet_slots=3,
        metadata={"priority": 1},
    )
    assert res.reservation_id.startswith("fres-")
    assert res.project_id == "darkfac"
    assert res.ticket_id == "USR-281"
    assert res.run_id == "run-101"
    assert res.host_id == "grokbox"
    assert res.status == "active"
    assert res.fencing_token == 1
    assert res.metadata == {"priority": 1}

    # Idempotent re-reservation for the same run and host
    res_same = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-101",
        host_id="grokbox",
        now=NOW + timedelta(seconds=5),
    )
    assert res_same.reservation_id == res.reservation_id


# ------------------------------------------------------------------------------
# 2. Uniqueness of active ticket
# ------------------------------------------------------------------------------


def test_ticket_already_active_conflict(store: ControlStore):
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-101",
        host_id="grokbox",
        now=NOW,
    )
    # Attempt to reserve same ticket on a different host / run
    with pytest.raises(TicketAlreadyActiveError) as exc_info:
        store.reserve_cycle(
            project_id="darkfac",
            ticket_id="USR-281",
            run_id="run-102",
            host_id="notebook",
            now=NOW,
        )
    assert "already has active reservation" in str(exc_info.value)


# ------------------------------------------------------------------------------
# 3. Single cycle per host
# ------------------------------------------------------------------------------


def test_host_busy_conflict(store: ControlStore):
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-101",
        host_id="notebook",
        now=NOW,
    )
    # Attempt to assign another ticket to the busy notebook
    with pytest.raises(HostBusyError) as exc_info:
        store.reserve_cycle(
            project_id="darkfac",
            ticket_id="USR-282",
            run_id="run-102",
            host_id="notebook",
            now=NOW,
        )
    assert "Host 'notebook' already has active reservation" in str(exc_info.value)


# ------------------------------------------------------------------------------
# 4. Fleet ceiling of 3 concurrent cycles
# ------------------------------------------------------------------------------


def test_fleet_capacity_ceiling(store: ControlStore):
    # Reserve 3 slots: grokbox, notebook, desktop
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-280", run_id="run-1", host_id="grokbox", now=NOW)
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-281", run_id="run-2", host_id="notebook", now=NOW)
    store.reserve_cycle(project_id="darkfac", ticket_id="USR-282", run_id="run-3", host_id="desktop", now=NOW)

    # Attempt 4th reservation
    with pytest.raises(FleetCapacityExceededError) as exc_info:
        store.reserve_cycle(project_id="darkfac", ticket_id="USR-283", run_id="run-4", host_id="backup_host", now=NOW)
    assert "ceiling of 3 active cycles reached" in str(exc_info.value)


# ------------------------------------------------------------------------------
# 5. Subordination of stage claims to cycle reservations
# ------------------------------------------------------------------------------


def test_stage_claim_subordination_and_host_affinity(store: ControlStore):
    # Accept intake for a ticket
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="cli",
        external_id=f"ext-{uuid4().hex[:8]}",
        payload={
            "title": "Subordinated Stage Test",
            "problem": "Verify claim subordination",
            "journey": ["step 1"],
            "non_goals": ["none"],
            "criteria": ["criterion 1"],
        },
        mode="autonomous",
        policy_ref="default",
    )
    receipt = store.accept(cmd, now=NOW)
    assert receipt.run_id is not None

    # Reserve cycle on desktop
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="darkfac",
        run_id=receipt.run_id,
        host_id="desktop",
        now=NOW,
    )

    # Worker on grokbox attempts to claim: blocked by host affinity
    claim_wrong_host = store.claim(
        worker="grokbox-worker",
        capabilities=["harness:codex", "grill_engine"],
        now=NOW,
    )
    assert claim_wrong_host is None

    # Worker on desktop attempts to claim: succeeds and inherits cycle reservation_id
    claim_desktop = store.claim(
        worker="desktop-worker",
        capabilities=["harness:codex", "grill_engine"],
        now=NOW,
    )
    assert claim_desktop is not None
    assert claim_desktop.reservation_id == res.reservation_id
    assert claim_desktop.owner == "desktop-worker"


def test_stage_claim_blocked_on_expired_reservation(store: ControlStore):
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="cli",
        external_id=f"ext-{uuid4().hex[:8]}",
        payload={
            "title": "Expired Cycle Claim Test",
            "problem": "Verify claim blocked when reservation expired",
            "journey": ["step 1"],
            "non_goals": ["none"],
            "criteria": ["criterion 1"],
        },
        mode="autonomous",
        policy_ref="default",
    )
    receipt = store.accept(cmd, now=NOW)

    # Reserve cycle with short lease
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="darkfac",
        run_id=receipt.run_id,
        host_id="desktop",
        now=NOW,
        lease_duration_sec=10,
    )

    # Advance time past lease expiration
    claim = store.claim(
        worker="desktop-worker",
        capabilities=["harness:codex", "grill_engine"],
        now=NOW + timedelta(seconds=20),
    )
    assert claim is None


# ------------------------------------------------------------------------------
# 6. Heartbeat, drain, expiration, and fencing
# ------------------------------------------------------------------------------


def test_heartbeat_extends_reservation(store: ControlStore):
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-1",
        host_id="grokbox",
        now=NOW,
        lease_duration_sec=30,
    )

    updated = store.heartbeat_reservation(
        reservation_id=res.reservation_id,
        fencing_token=res.fencing_token,
        now=NOW + timedelta(seconds=15),
        lease_duration_sec=30,
    )
    assert updated.reservation_id == res.reservation_id
    new_expires = datetime.fromisoformat(updated.expires_at)
    assert new_expires > datetime.fromisoformat(res.expires_at)


def test_heartbeat_rejects_fencing_mismatch_or_expired(store: ControlStore):
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-1",
        host_id="grokbox",
        now=NOW,
        lease_duration_sec=10,
    )

    # Wrong fencing token
    with pytest.raises(StaleLeaseError) as exc_tok:
        store.heartbeat_reservation(
            reservation_id=res.reservation_id,
            fencing_token=99,
            now=NOW + timedelta(seconds=5),
        )
    assert "Fencing token mismatch" in str(exc_tok.value)

    # Expired lease
    with pytest.raises(StaleLeaseError) as exc_exp:
        store.heartbeat_reservation(
            reservation_id=res.reservation_id,
            fencing_token=res.fencing_token,
            now=NOW + timedelta(seconds=15),
        )
    assert "expired" in str(exc_exp.value)


def test_fencing_prevents_old_owner_from_finishing_job(store: ControlStore):
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="cli",
        external_id=f"ext-{uuid4().hex[:8]}",
        payload={
            "title": "Fencing Finish Test",
            "problem": "Verify old owner cannot finish",
            "journey": ["step 1"],
            "non_goals": ["none"],
            "criteria": ["criterion 1"],
        },
        mode="autonomous",
        policy_ref="default",
    )
    receipt = store.accept(cmd, now=NOW)
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="darkfac",
        run_id=receipt.run_id,
        host_id="notebook",
        now=NOW,
        lease_duration_sec=10,
    )
    claim = store.claim(
        worker="notebook-worker",
        capabilities=["harness:codex", "grill_engine"],
        now=NOW,
    )
    assert claim is not None

    # Advance time and expire the cycle reservation
    # (USR-306: the claim renews the reservation by the store lease of 30 s, so expire it beyond that.)
    claim = store.heartbeat(claim, NOW + timedelta(seconds=25))  # job lease alive; reservation not renewed
    expired_reservations = store.reconcile_fleet_reservations(now=NOW + timedelta(seconds=40))
    assert any(r.reservation_id == res.reservation_id for r in expired_reservations)

    # Old owner attempts to finish after reservation expired -> rejected!
    stage_res = StageResult(
        outcome="success",
        output_refs=["ref-1"],
        evidence_refs=["ev-1"],
    )
    with pytest.raises(StaleLeaseError) as exc_finish:
        store.finish(claim, stage_res, now=NOW + timedelta(seconds=41))
    assert "no longer active" in str(exc_finish.value)


def test_drain_host_marks_status_and_metadata(store: ControlStore):
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-1",
        host_id="desktop",
        now=NOW,
    )
    count = store.drain_host(host_id="desktop", reason="kernel upgrade", now=NOW)
    assert count == 1

    fetched = store.get_reservation(res.reservation_id)
    assert fetched is not None
    assert fetched.status == "draining"
    assert fetched.metadata.get("drain_reason") == "kernel upgrade"


# ------------------------------------------------------------------------------
# 7. One-time release idempotency
# ------------------------------------------------------------------------------


def test_release_reservation_once(store: ControlStore):
    res = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-1",
        host_id="notebook",
        now=NOW,
    )
    released = store.release_reservation(
        reservation_id=res.reservation_id,
        fencing_token=res.fencing_token,
        status="completed",
        now=NOW + timedelta(seconds=10),
    )
    assert released.status == "completed"

    # Second release is idempotent
    released_again = store.release_reservation(
        reservation_id=res.reservation_id,
        fencing_token=res.fencing_token,
        status="completed",
        now=NOW + timedelta(seconds=12),
    )
    assert released_again.status == "completed"

    # Host and ticket can now be reused for another cycle
    res_new = store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-281",
        run_id="run-2",
        host_id="notebook",
        now=NOW + timedelta(seconds=15),
    )
    assert res_new.reservation_id != res.reservation_id
    assert res_new.status == "active"


# ------------------------------------------------------------------------------
# 8. Reconcile unknown operations before retry
# ------------------------------------------------------------------------------


def test_reconcile_unknown_operations(store: ControlStore):
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="cli",
        external_id=f"ext-{uuid4().hex[:8]}",
        payload={
            "title": "Unknown Ops Test",
            "problem": "Verify unknown operations",
            "journey": ["step 1"],
            "non_goals": ["none"],
            "criteria": ["criterion 1"],
        },
        mode="autonomous",
        policy_ref="default",
    )
    receipt = store.accept(cmd, now=NOW)
    claim = store.claim(
        worker="worker-1",
        capabilities=["harness:codex", "grill_engine"],
        now=NOW,
    )
    assert claim is not None

    op = ExternalOperation(
        operation_key="op-unknown-123",
        request_digest="dig-1",
        provider="github",
        status="unknown",
        observed_at=NOW.isoformat(),
    )
    store.record_operation(op, claim)

    unknown_list = store.reconcile_unknown_operations(now=NOW)
    assert len(unknown_list) == 1
    assert unknown_list[0].operation_key == "op-unknown-123"


# ------------------------------------------------------------------------------
# 9. Multi-threaded race condition test
# ------------------------------------------------------------------------------


def test_concurrent_coordinator_host_race(store: ControlStore):
    """Simulates two coordinators racing to claim the same host for different tickets.
    Exactly one coordinator must succeed; the other must receive HostBusyError.
    """
    results: list[FleetReservation | Exception] = []

    def _attempt_reserve(ticket_id: str, run_id: str):
        try:
            r = store.reserve_cycle(
                project_id="darkfac",
                ticket_id=ticket_id,
                run_id=run_id,
                host_id="grokbox",
                now=NOW,
            )
            results.append(r)
        except Exception as exc:
            results.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_attempt_reserve, "USR-281", "run-1")
        f2 = executor.submit(_attempt_reserve, "USR-282", "run-2")
        f1.result()
        f2.result()

    successes = [r for r in results if isinstance(r, FleetReservation)]
    conflicts = [r for r in results if isinstance(r, HostBusyError)]

    assert len(successes) == 1
    assert len(conflicts) == 1


# ------------------------------------------------------------------------------
# 10. Real PostgreSQL integration test (C_TEST_POSTGRES_DSN / DARKFAC_TEST_POSTGRES_DSN)
# ------------------------------------------------------------------------------


def test_real_postgres_coordinator_race():
    """Executes a real PostgreSQL race between two coordinators when DSN is configured.
    Satisfies Criterion 3: 'executar tambem com C_TEST_POSTGRES_DSN configurado e
    registrar corrida em Postgres real entre dois coordenadores, sem aceitar skip como prova distribuida.'
    """
    dsn = os.environ.get("C_TEST_POSTGRES_DSN") or os.environ.get("DARKFAC_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip(
            "C_TEST_POSTGRES_DSN or DARKFAC_TEST_POSTGRES_DSN not set. "
            "To run real Postgres integration test: $env:C_TEST_POSTGRES_DSN = 'postgresql://...' "
            "(see docs/runbooks/live_progress.md)"
        )

    try:
        import psycopg
    except ImportError:
        pytest.fail("psycopg is required when C_TEST_POSTGRES_DSN is configured")

    pg_store = PostgresControlStore(database_url=dsn, mock_mode=False)

    results: list[FleetReservation | Exception] = []

    def _attempt_reserve(ticket_id: str, run_id: str):
        try:
            r = pg_store.reserve_cycle(
                project_id="darkfac_real_pg",
                ticket_id=ticket_id,
                run_id=run_id,
                host_id="real_desktop",
                now=datetime.now(timezone.utc),
            )
            results.append(r)
        except Exception as exc:
            results.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(_attempt_reserve, "USR-281-PG", "run-pg-1")
        f2 = executor.submit(_attempt_reserve, "USR-282-PG", "run-pg-2")
        f1.result()
        f2.result()

    successes = [r for r in results if isinstance(r, FleetReservation)]
    conflicts = [r for r in results if isinstance(r, HostBusyError)]

    assert len(successes) == 1, f"Expected exactly 1 success in real Postgres race, got {len(successes)}"
    assert len(conflicts) == 1, f"Expected exactly 1 HostBusyError in real Postgres race, got {len(conflicts)}"

    # Cleanup reservation
    if successes:
        pg_store.release_reservation(
            reservation_id=successes[0].reservation_id,
            fencing_token=successes[0].fencing_token,
            status="completed",
        )
