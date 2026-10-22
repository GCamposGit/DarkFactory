"""Tests for USR-282: Cycle affinity, continuous replacement, and fleet loop.

Validates:
- Coordinator admits up to 3 cycles across distinct hosts (grokbox, notebook, desktop).
- Cycle affinity: demand remains on reserved host across stages and CI.
- Real availability: claims reserved cycle without artificial priority delays.
- Simultaneous replenishment: completions free hosts and admit next eligible tickets.
- Empty queue handling and immediate pickup on new demand arrival.
- Human block (waiting_human): demand parks, frees machine slot without executing a 4th cycle,
  resuming readmits within capacity.
- Offline host / expiration: stale reservation swept, recovered with fencing on healthy host,
  stale worker rejected on late publish.
- Coordinator restart resilience: state preserved across restarts without duplicate admissions.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import (
    Claim,
    FleetReservation,
    IntakeCommand,
    JobKey,
    RuntimeOwner,
    StageContext,
    StageResult,
    StaleLeaseError,
)
from core.workflow.handlers import HandlerRegistry
from core.workflow.reconciliation import reconcile_portfolio_state


class _TestStageHandler:
    def __init__(self, outcome: str = "success", cause_code: str | None = None) -> None:
        self.outcome = outcome
        self.cause_code = cause_code
        self.call_count = 0

    def handle(self, context: StageContext) -> StageResult:
        self.call_count += 1
        output_refs = [f"ref://{context.claim.job_key.stage}/{context.claim.job_key.run_id}"] if self.outcome == "success" else []
        return StageResult(
            outcome=self.outcome,
            output_refs=output_refs,
            cause_code=self.cause_code,
        )


def _build_test_registry(handlers_map: dict[str, _TestStageHandler]) -> HandlerRegistry:
    from core.workflow.handlers import build_handlers
    return build_handlers(bindings=handlers_map)


def _accept_intake_ticket(
    store: PostgresControlStore,
    project_id: str,
    ticket_id: str,
    now: datetime,
) -> tuple[str, str]:
    """Helper to accept an intake ticket and return (demand_id, run_id)."""
    cmd = IntakeCommand(
        project_id=project_id,
        channel="test-channel",
        external_id=f"ext-{ticket_id}-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="test-policy",
        payload={
            "ticket_id": ticket_id,
            "title": f"Title for {ticket_id}",
            "problem": f"Problem statement for {ticket_id}",
            "raw_intent": f"Build feature {ticket_id}",
            "journey": ["step 1"],
            "non_goals": [],
            "criteria": ["criterion 1"],
        },
    )
    receipt = store.accept(cmd, now=now)
    return receipt.demand_id, receipt.run_id


@pytest.fixture(autouse=True)
def _fixed_local_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    # The coordinator measures the real host's free RAM; pin it so admission does not depend on the machine.
    import types

    import core.line.gate_ram as gate_ram

    monkeypatch.setattr(gate_ram, "measure_workers", lambda *a, **k: types.SimpleNamespace(workers=4))


@pytest.fixture
def fleet_store(tmp_path: Path) -> PostgresControlStore:
    db_file = tmp_path / "fleet_control.db"
    store = PostgresControlStore(
        mock_mode=True,
        runtime_owner=RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec=30,
    )
    store._backend.db_path = db_file
    return store


def test_coordinator_admits_up_to_three_cycles_distinct_hosts(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

    # 6 tickets across 2 projects (darkfac and proj-b)
    tickets = [
        ("darkfac", "USR-301"),
        ("darkfac", "USR-302"),
        ("darkfac", "USR-303"),
        ("proj-b", "PRJ-101"),
        ("proj-b", "PRJ-102"),
        ("proj-b", "PRJ-103"),
    ]
    for proj_id, tick_id in tickets:
        _accept_intake_ticket(fleet_store, proj_id, tick_id, now)

    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )

    admitted = coordinator.admit_eligible_cycles(now=now)

    assert len(admitted) == 3
    hosts = [r.host_id.lower() for r in admitted]
    assert hosts == ["grokbox", "notebook", "desktop"]

    active = fleet_store.list_active_reservations()
    assert len(active) == 3
    active_hosts = {r.host_id.lower() for r in active}
    assert active_hosts == {"grokbox", "notebook", "desktop"}

    # Subsequent admission returns empty as fleet capacity (3) is fully allocated
    second_pass = coordinator.admit_eligible_cycles(now=now)
    assert len(second_pass) == 0


def test_cycle_affinity_demand_remains_on_reserved_host_across_stages(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    _, run_id = _accept_intake_ticket(fleet_store, "darkfac", "USR-301", now)

    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )
    admitted = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted) == 1
    assert admitted[0].host_id == "grokbox"

    grill_handler = _TestStageHandler(outcome="success")
    planning_handler = _TestStageHandler(outcome="success")
    registry = _build_test_registry({
        "grill": grill_handler,
        "planning": planning_handler,
    })

    worker_grokbox = CloudWorker(worker_id="worker-grokbox", host_id="grokbox", store=fleet_store, registry=registry)
    worker_notebook = CloudWorker(worker_id="worker-notebook", host_id="notebook", store=fleet_store, registry=registry)
    worker_desktop = CloudWorker(worker_id="worker-desktop", host_id="desktop", store=fleet_store, registry=registry)

    # 1. Non-reserved workers attempt to claim stage grill -> rejected by host affinity
    assert worker_notebook.poll_and_execute_once(now=now) is False
    assert worker_desktop.poll_and_execute_once(now=now) is False

    # 2. Reserved worker on grokbox claims and executes grill
    assert worker_grokbox.poll_and_execute_once(now=now) is True
    assert grill_handler.call_count == 1

    # Stage successor (planning) is now pending
    # 3. Non-reserved workers attempt to claim stage planning -> still rejected by affinity!
    assert worker_notebook.poll_and_execute_once(now=now) is False
    assert worker_desktop.poll_and_execute_once(now=now) is False

    # 4. Reserved worker claims planning and executes
    assert worker_grokbox.poll_and_execute_once(now=now) is True
    assert planning_handler.call_count == 1

    # Reservation remains active throughout
    res = fleet_store.get_active_reservation_for_run(run_id)
    assert res is not None
    assert res.host_id == "grokbox"
    assert res.is_active is True


def test_real_availability_without_fixed_priority_delay(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    _, run_id = _accept_intake_ticket(fleet_store, "darkfac", "USR-302", now)

    # Pre-reserve on notebook
    res = fleet_store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-302",
        run_id=run_id,
        host_id="notebook",
        now=now,
    )
    assert res is not None

    handler = _TestStageHandler(outcome="success")
    registry = _build_test_registry({"grill": handler})

    # Worker configured with secondary priority delay (ready_age_sec = 30.0)
    worker_notebook = CloudWorker(
        worker_id="worker-notebook",
        host_id="notebook",
        store=fleet_store,
        registry=registry,
        ready_age_sec=30.0,
    )

    # Immediate claim at t = 0 (no delay!) because host affinity is pre-established
    executed = worker_notebook.poll_and_execute_once(now=now)
    assert executed is True
    assert handler.call_count == 1


def test_simultaneous_replenishment_after_completions(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

    # 5 tickets
    for i in range(1, 6):
        _accept_intake_ticket(fleet_store, "darkfac", f"USR-T{i}", now)

    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )

    # Initial admission fills 3 slots
    admitted = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted) == 3
    assert {r.host_id.lower() for r in admitted} == {"grokbox", "notebook", "desktop"}

    res_grokbox = admitted[0]
    res_notebook = admitted[1]

    # Two cycles finish simultaneously (grokbox and notebook)
    fleet_store.release_reservation(res_grokbox.reservation_id, res_grokbox.fencing_token, status="completed", now=now)
    fleet_store.release_reservation(res_notebook.reservation_id, res_notebook.fencing_token, status="completed", now=now)

    active_before = fleet_store.list_active_reservations()
    assert len(active_before) == 1
    assert active_before[0].host_id.lower() == "desktop"

    # Replenishment tick: coordinator immediately admits 2 new tickets onto the freed hosts
    newly_admitted = coordinator.admit_eligible_cycles(now=now)
    assert len(newly_admitted) == 2
    new_hosts = {r.host_id.lower() for r in newly_admitted}
    assert new_hosts == {"grokbox", "notebook"}

    # Total active reservations is exactly 3; never exceeds capacity
    active_after = fleet_store.list_active_reservations()
    assert len(active_after) == 3


def test_empty_queue_graceful_and_new_arrival_immediate(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )

    # Empty queue returns empty list cleanly
    admitted = coordinator.admit_eligible_cycles(now=now)
    assert admitted == []

    # New demand arrives
    _accept_intake_ticket(fleet_store, "darkfac", "USR-NEW", now)

    # Immediately admitted on next check
    admitted = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted) == 1
    assert admitted[0].ticket_id == "USR-NEW"
    assert admitted[0].host_id.lower() == "grokbox"


def test_waiting_human_parks_and_frees_slot_without_fourth_cycle(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

    # 4 tickets: T1, T2, T3, T4
    _, run_t1 = _accept_intake_ticket(fleet_store, "darkfac", "T1", now)
    _accept_intake_ticket(fleet_store, "darkfac", "T2", now)
    _accept_intake_ticket(fleet_store, "darkfac", "T3", now)
    _accept_intake_ticket(fleet_store, "darkfac", "T4", now)

    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )

    admitted = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted) == 3
    # T1 is on grokbox

    # T1 handler emits waiting_human
    human_handler = _TestStageHandler(outcome="waiting_human", cause_code="needs_clarification")
    registry = _build_test_registry({"grill": human_handler})
    worker_grokbox = CloudWorker(worker_id="worker-grokbox", host_id="grokbox", store=fleet_store, registry=registry)

    worker_grokbox.poll_and_execute_once(now=now)
    assert human_handler.call_count == 1

    # T1 reservation should now be released/parked so grokbox is free
    res_t1 = fleet_store.get_active_reservation_for_run(run_t1)
    assert res_t1 is None

    # T4 is admitted onto the freed grokbox slot!
    admitted_second = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted_second) == 1
    assert admitted_second[0].ticket_id == "T4"
    assert admitted_second[0].host_id.lower() == "grokbox"

    # Active reservations count is 3 (T2, T3, T4)
    active = fleet_store.list_active_reservations()
    assert len(active) == 3
    assert {r.ticket_id for r in active} == {"T2", "T3", "T4"}

    # Resuming T1 when all 3 slots are full:
    # resume_job transitions T1 back to pending
    key_t1 = JobKey(run_id=run_t1, ticket_id="T1", plan_version="1.0", stage="grill", iteration=0)
    resumed = fleet_store.resume_job(key_t1, now=now)
    assert resumed is True

    # Coordinator cannot admit T1 yet because all 3 slots are occupied (no 4th cycle!)
    admitted_blocked = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted_blocked) == 0

    # One active ticket completes (e.g. T2 on notebook)
    res_t2 = fleet_store.get_active_reservation_for_ticket("darkfac", "T2")
    assert res_t2 is not None
    fleet_store.release_reservation(res_t2.reservation_id, res_t2.fencing_token, status="completed", now=now)

    # Now T1 is readmitted onto notebook!
    admitted_resumed = coordinator.admit_eligible_cycles(now=now)
    assert len(admitted_resumed) == 1
    assert admitted_resumed[0].ticket_id == "T1"
    assert admitted_resumed[0].host_id.lower() == "notebook"


def test_host_offline_expiration_fencing_recovery(fleet_store: PostgresControlStore) -> None:
    t0 = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
    _, run_id = _accept_intake_ticket(fleet_store, "darkfac", "USR-FAILOVER", t0)

    coordinator = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )
    admitted = coordinator.admit_eligible_cycles(now=t0)
    assert len(admitted) == 1
    res_initial = admitted[0]
    assert res_initial.host_id == "grokbox"
    assert res_initial.fencing_token == 1

    # Worker on grokbox claims the job
    stale_claim = fleet_store.claim(worker="worker-grokbox", capabilities=["grill_engine"], now=t0)
    assert stale_claim is not None

    # Host grokbox goes offline for 60 seconds (lease_duration_sec is 30)
    t_stale = t0 + timedelta(seconds=65)
    coordinator.host_health_prober = lambda h: h.lower() != "grokbox"

    # Sweep expired claims to return abandoned job to pending
    reconcile_portfolio_state(fleet_store, now=t_stale)

    # Coordinator sweep expires the stale reservation
    expired = fleet_store.reconcile_fleet_reservations(now=t_stale)
    assert len(expired) == 1
    assert expired[0].reservation_id == res_initial.reservation_id

    # The pending job is readmitted onto a healthy host (notebook) with a new reservation
    re_admitted = coordinator.admit_eligible_cycles(now=t_stale)
    assert len(re_admitted) == 1
    res_new = re_admitted[0]
    assert res_new.host_id.lower() == "notebook"
    assert res_new.ticket_id == "USR-FAILOVER"

    # Stale worker on grokbox attempts late finish -> rejected with StaleLeaseError!
    with pytest.raises(StaleLeaseError):
        fleet_store.finish(
            stale_claim,
            StageResult(outcome="success", output_refs=["ref://stale/out"]),
            now=t_stale,
        )


def test_coordinator_restart_resilience(fleet_store: PostgresControlStore) -> None:
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)

    # 4 tickets
    for i in range(1, 5):
        _accept_intake_ticket(fleet_store, "darkfac", f"RESTART-{i}", now)

    coord_1 = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )
    admitted_1 = coord_1.admit_eligible_cycles(now=now)
    assert len(admitted_1) == 3

    # Coordinator 1 shuts down
    coord_1.shutdown()

    # Coordinator 2 boots up pointing to the exact same store
    coord_2 = CloudCoordinator(
        database_url=None,
        max_concurrent_slots=3,
        store=fleet_store,
        fleet_hosts=["grokbox", "notebook", "desktop"],
    )

    status = coord_2.inspect_status()
    assert status.active_fleet_reservations_count == 3

    # Admit on coord_2 does not duplicate or admit 4th cycle
    admitted_2 = coord_2.admit_eligible_cycles(now=now)
    assert len(admitted_2) == 0

    active = fleet_store.list_active_reservations()
    assert len(active) == 3
    assert {r.host_id.lower() for r in active} == {"grokbox", "notebook", "desktop"}
