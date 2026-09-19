"""Deterministic unit tests for HF-05-05: Continuous Portfolio Supervisor.

Governed by:
- docs/handoffs/continuous-autonomy/HF-05-05.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/supervisor.py
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from core.workflow.control_contracts import (
    IntakeCommand,
    JobKey,
    StageResult,
)
from core.workflow.control_store import SQLiteControlStore
from core.workflow.supervisor import (
    PortfolioSupervisor,
    Supervisor,
    SupervisorConfig,
    SupervisorTickReport,
)

BASE_TIME = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)


def _make_intake_command(
    project_id: str,
    external_id: str = "ext-1",
    title: str = "Feature demand",
) -> IntakeCommand:
    return IntakeCommand(
        project_id=project_id,
        channel="web_portal",
        external_id=external_id,
        payload={
            "title": title,
            "problem": "Operational automation required",
            "journey": "User submits command and monitors execution",
            "non_goals": ["manual intervention"],
            "criteria": ["deterministic recovery"],
        },
        mode="autonomous",
        policy_ref="policy-v1",
    )


def test_supervisor_stable_scan_100_plus_projects_and_dynamic_insertion() -> None:
    """Validate stable paginated scan over 100+ projects without dropping newly inserted jobs."""
    store = SQLiteControlStore(db_path=":memory:")
    supervisor = PortfolioSupervisor(store=store, config=SupervisorConfig(page_size=25))

    # Pre-populate 100 projects
    for i in range(100):
        project_id = f"project_{i:03d}"
        cmd = _make_intake_command(project_id, external_id=f"cmd-{i}", title=f"Task {i}")
        store.accept(cmd, BASE_TIME)

    initial_projects = supervisor.scan_projects()
    assert len(initial_projects) == 100

    # Insert 10 more projects dynamically
    for i in range(100, 110):
        project_id = f"project_{i:03d}"
        cmd = _make_intake_command(project_id, external_id=f"cmd-{i}", title=f"Task {i}")
        store.accept(cmd, BASE_TIME)

    updated_projects = supervisor.scan_projects()
    assert len(updated_projects) == 110
    # Every project from 000 to 109 is present
    for i in range(110):
        assert f"project_{i:03d}" in updated_projects


def test_supervisor_lease_recovery_under_60s_with_simulated_clock() -> None:
    """Validate expired leases are swept and re-enqueued within <= 60s of simulated time."""
    store = SQLiteControlStore(db_path=":memory:", lease_duration_sec=45)
    supervisor = PortfolioSupervisor(store=store)

    cmd = _make_intake_command("proj-rec", external_id="ext-rec-1")
    receipt = store.accept(cmd, BASE_TIME)

    # Claim the job
    claim = store.claim(worker="worker-1", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim is not None
    assert claim.fencing_token == 1

    # Advance clock by 50 seconds (> 45s lease duration)
    advanced_time = BASE_TIME + timedelta(seconds=50)

    # Supervisor tick executes lease sweep
    report: SupervisorTickReport = supervisor.tick(now=advanced_time)
    assert report.repaired_leases_count == 1
    assert report.sla_met is True

    # Claim should now be re-claimable with incremented monotonic fencing token
    reclaimed = store.claim(worker="worker-2", capabilities=["grill_engine"], now=advanced_time)
    assert reclaimed is not None
    assert reclaimed.fencing_token == 3
    assert reclaimed.owner == "worker-2"


def test_supervisor_detects_consumer_absence_and_starved_projects() -> None:
    """Validate that unserved pending jobs exceeding ready-age trigger consumer_absent."""
    store = SQLiteControlStore(db_path=":memory:")
    supervisor = PortfolioSupervisor(
        store=store,
        config=SupervisorConfig(ready_age_threshold_sec=30.0),
    )

    # Initially empty
    empty_report = supervisor.tick(now=BASE_TIME)
    assert empty_report.consumer_absent is False
    assert empty_report.pending_jobs_count == 0

    # Submit demand but do not claim it
    cmd = _make_intake_command("proj-starved", external_id="starve-1")
    store.accept(cmd, BASE_TIME)

    # Immediately after submission (age 0s) - no active workers, but pending jobs exist
    immediate_report = supervisor.tick(now=BASE_TIME)
    assert immediate_report.pending_jobs_count == 1
    assert immediate_report.consumer_absent is True  # 1 pending, 0 running

    # Advance time past ready_age_threshold_sec
    stale_time = BASE_TIME + timedelta(seconds=35)
    stale_report = supervisor.tick(now=stale_time)
    assert stale_report.consumer_absent is True
    assert "proj-starved" in stale_report.starved_projects
    assert stale_report.max_ready_age_sec >= 35.0


def test_supervisor_cancellation_prevents_side_effects() -> None:
    """Validate that stop_event cleanly cancels supervisor loop without hanging or orphaned state."""
    store = SQLiteControlStore(db_path=":memory:")
    supervisor = Supervisor(
        store=store,
        config=SupervisorConfig(tick_interval_sec=1.0, max_sleep_sec=0.1),
    )

    stop_event = threading.Event()
    worker_thread = threading.Thread(target=supervisor.run, args=(stop_event,), daemon=True)
    worker_thread.start()

    time.sleep(0.05)
    assert worker_thread.is_alive()

    # Cancel
    stop_event.set()
    worker_thread.join(timeout=2.0)
    assert not worker_thread.is_alive()


def test_supervisor_outbox_reconciliation_and_dispatch() -> None:
    """Validate that outbox events are dispatched and materialized during supervisor tick."""
    store = SQLiteControlStore(db_path=":memory:")
    dispatched_events: list[dict] = []

    def fake_dispatcher(evt: dict) -> bool:
        dispatched_events.append(evt)
        return True

    supervisor = PortfolioSupervisor(
        store=store,
        outbox_dispatcher=fake_dispatcher,
    )

    cmd = _make_intake_command("proj-outbox", external_id="out-1")
    store.accept(cmd, BASE_TIME)

    # Initial tick should process pending outbox event created by accept
    report = supervisor.tick(now=BASE_TIME)
    assert len(dispatched_events) == 1
    assert report.outbox_repaired_count == 1

    # Subsequent tick should find zero pending outbox
    second_report = supervisor.tick(now=BASE_TIME + timedelta(seconds=1))
    assert second_report.outbox_repaired_count == 0


def test_supervisor_next_wakeup_bounded_by_max_sleep() -> None:
    """Validate next_wakeup adheres to tick_interval and max_sleep ceilings."""
    store = SQLiteControlStore(db_path=":memory:")
    supervisor = PortfolioSupervisor(
        store=store,
        config=SupervisorConfig(tick_interval_sec=30.0, max_sleep_sec=15.0),
    )

    wakeup = supervisor.next_wakeup(BASE_TIME)
    assert wakeup == BASE_TIME + timedelta(seconds=15.0)
