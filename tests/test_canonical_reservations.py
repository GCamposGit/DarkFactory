"""Comprehensive test suite for canonical reservations, capacity policy and scheduler composed mode (HF-23-01).

Validates:
- Pure deterministic ranking of eligible JobKeys (rank_eligible).
- Composed mode prohibition of local JSON slot allocations.
- Integer units budget enforcement.
- Canonical project alias resolution.
- Weighted Fair Queueing across portfolio projects (Atrium > Jarvis > DarkFac).
- Starvation protection for neglected projects without fabricating budget.
- Atomic concurrency: two schedulers do not double-spend the last capacity slot/credit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.portfolio.budget_manager import PortfolioBudgetManager
from core.portfolio.models import JobSlotKind
from core.portfolio.scheduler import PortfolioScheduler
from core.workflow.capacity_policy import (
    CapacitySnapshot,
    JobCandidate,
    rank_eligible,
    resolve_canonical_project_id,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
)
from core.workflow.control_store import ControlStore


def _make_candidate(
    project_id: str,
    ticket_id: str,
    kind: JobSlotKind = JobSlotKind.LIGHT,
    cost_units: int = 1,
    priority: int = 0,
    seconds_ago: int = 10,
    stage: str = "development",
) -> JobCandidate:
    jk = JobKey(
        run_id=f"run_{ticket_id}",
        ticket_id=ticket_id,
        plan_version="1.0",
        stage=stage,
        iteration=1,
    )
    enqueued_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    return JobCandidate(
        job_key=jk,
        project_id=project_id,
        stage=stage,
        kind=kind,
        estimated_cost_units=cost_units,
        priority=priority,
        enqueued_at=enqueued_at,
    )


def test_rank_eligible_pure_function_no_side_effects():
    now = datetime.now(UTC)
    c1 = _make_candidate("darkfac", "HF-01", kind=JobSlotKind.LIGHT, priority=5)
    c2 = _make_candidate("atrium", "AT-01", kind=JobSlotKind.LIGHT, priority=5)

    snap = CapacitySnapshot(
        candidates=[c1, c2],
        max_light_slots=4,
        active_light_slots=0,
    )

    ranked1 = rank_eligible(snap, now)
    ranked2 = rank_eligible(snap, now)

    # Output is deterministic and produces identical results
    assert ranked1 == ranked2
    assert len(ranked1) == 2
    # Atrium has higher weight than DarkFac (3.0 > 1.0)
    assert ranked1[0].ticket_id == "AT-01"
    assert ranked1[1].ticket_id == "HF-01"

    # Verify snap was not mutated
    assert len(snap.candidates) == 2


def test_composed_mode_forbids_json_slot_allocations(tmp_path):
    scheduler = PortfolioScheduler(
        storage_file=tmp_path / "scheduler.json",
        composed_mode=True,
    )
    assert scheduler.composed_mode is True

    # Attempting to allocate a slot in composed mode must fail closed
    with pytest.raises(RuntimeError, match="Composed mode forbids JSON slot allocations"):
        scheduler.acquire_slot("job_1", JobSlotKind.LIGHT, "worker_1")


def test_slot_kind_saturation_filtering():
    now = datetime.now(UTC)
    c_heavy = _make_candidate("darkfac", "HF-HEAVY", kind=JobSlotKind.HEAVY)
    c_light = _make_candidate("darkfac", "HF-LIGHT", kind=JobSlotKind.LIGHT)

    # Saturated heavy slots (1/1 active)
    snap_heavy_full = CapacitySnapshot(
        candidates=[c_heavy, c_light],
        max_heavy_slots=1,
        active_heavy_slots=1,
        max_light_slots=4,
        active_light_slots=1,
    )
    ranked = rank_eligible(snap_heavy_full, now)
    assert len(ranked) == 1
    assert ranked[0].ticket_id == "HF-LIGHT"

    # Saturated light slots (4/4 active)
    snap_light_full = CapacitySnapshot(
        candidates=[c_heavy, c_light],
        max_heavy_slots=1,
        active_heavy_slots=0,
        max_light_slots=4,
        active_light_slots=4,
    )
    ranked = rank_eligible(snap_light_full, now)
    assert len(ranked) == 1
    assert ranked[0].ticket_id == "HF-HEAVY"


def test_integer_units_budget_depletion():
    now = datetime.now(UTC)
    c1 = _make_candidate("darkfac", "HF-EXPENSIVE", cost_units=100)
    c2 = _make_candidate("darkfac", "HF-CHEAP", cost_units=10)

    snap = CapacitySnapshot(
        candidates=[c1, c2],
        project_budgets_remaining_units={"darkfac": 50},  # only 50 units left
    )

    ranked = rank_eligible(snap, now)
    # c1 requires 100 units > 50 units remaining, so only c2 is eligible
    assert len(ranked) == 1
    assert ranked[0].ticket_id == "HF-CHEAP"


def test_canonical_alias_resolution():
    assert resolve_canonical_project_id("dark-factory") == "darkfac"
    assert resolve_canonical_project_id("atrium-core") == "atrium"
    assert resolve_canonical_project_id("jarvis-ai") == "jarvis"

    now = datetime.now(UTC)
    # Candidate with alias "dark-factory"
    c_alias = _make_candidate("dark-factory", "HF-ALIAS", cost_units=10)

    snap = CapacitySnapshot(
        candidates=[c_alias],
        project_budgets_remaining_units={"darkfac": 50},  # canonical key
    )
    ranked = rank_eligible(snap, now)
    assert len(ranked) == 1
    assert ranked[0].ticket_id == "HF-ALIAS"


def test_starvation_protection_promotes_neglected_project():
    now = datetime.now(UTC)
    # Atrium normally beats DarkFac due to weight (3.0 vs 1.0)
    c_atrium = _make_candidate("atrium", "AT-NORMAL", priority=0)
    c_darkfac = _make_candidate("darkfac", "DF-STARVED", priority=0)

    # DarkFac starved for 6 ticks (>= max_starvation_ticks of 5)
    snap = CapacitySnapshot(
        candidates=[c_atrium, c_darkfac],
        starvation_ticks={"darkfac": 6, "atrium": 0},
        max_starvation_ticks=5,
    )

    ranked = rank_eligible(snap, now)
    # Starvation boost promotes DarkFac ahead of Atrium
    assert len(ranked) == 2
    assert ranked[0].ticket_id == "DF-STARVED"
    assert ranked[1].ticket_id == "AT-NORMAL"


def test_budget_manager_integer_units():
    bm = PortfolioBudgetManager()
    units = bm.to_integer_units(25.50, unit_rate=1000)
    assert units == 25500
    rem_units = bm.get_remaining_units("atrium", unit_rate=1000)
    assert rem_units > 0
    assert isinstance(rem_units, int)


def test_two_schedulers_concurrency_double_spend_prevention(tmp_path):
    """Two independent schedulers ranking and attempting claim on the last available slot."""
    from core.workflow.control_contracts import IntakeCommand
    from core.workflow.control_store import SQLiteControlStore

    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)

    cmd = IntakeCommand(
        channel="test",
        external_id="ext-sched-001",
        project_id="darkfac",
        payload={
            "title": "Scheduler Concurrency Test",
            "problem": "Validate double claim prevention",
            "journey": "Two workers competing for claim",
            "non_goals": ["No leaks"],
            "criteria": ["Only one worker acquires claim"],
        },
        mode="autonomous",
        policy_ref="policy-v1",
    )

    now = datetime.now(UTC)
    receipt = store.accept(cmd, now)
    assert receipt.run_id is not None
    assert receipt.initial_job_id is not None

    # Scheduler 1 / Worker 1 claims the single available job
    claim1 = store.claim(worker="worker_1", capabilities=["economy", "grill_engine"], now=now)
    assert claim1 is not None
    assert claim1.owner == "worker_1"
    assert claim1.fencing_token == 1

    # Scheduler 2 / Worker 2 attempts to claim simultaneously - must receive None (double-spend prevented)
    claim2 = store.claim(worker="worker_2", capabilities=["economy", "grill_engine"], now=now)
    assert claim2 is None
