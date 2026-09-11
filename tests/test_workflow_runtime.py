"""Deterministic HF-05-01 tests for durable runtime dispatch semantics."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.workflow.contracts import WorkflowState
from core.workflow.runtime import (
    EventStatus,
    JobOutcome,
    JobSpec,
    JobStatus,
    LeaseExpiredError,
    LeaseOwnershipError,
    RuntimeConflictError,
    RuntimeNotFoundError,
    WorkflowRuntime,
)
from core.workflow.readiness import ReadinessError


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def make_runtime(tmp_path: Path, *, stage_limits: dict[str, int] | None = None) -> tuple[WorkflowRuntime, Clock]:
    clock = Clock()
    return (
        WorkflowRuntime(
            tmp_path / "workflow.sqlite3",
            clock=clock,
            stage_limits=stage_limits,
            retry_delay_seconds=5,
        ),
        clock,
    )


def register_active(runtime: WorkflowRuntime, run_id: str, project_id: str, *, budget: float = 10.0) -> None:
    runtime.register_run(run_id, project_id, budget_ceiling=budget)
    runtime.transition_run(run_id, WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key=f"activate:{run_id}")


def job(
    job_id: str,
    run_id: str,
    project_id: str,
    *,
    priority: int = 0,
    depends_on: list[str] | None = None,
    conflict_keys: list[str] | None = None,
    estimated_cost: float = 1.0,
    max_attempts: int = 3,
    successor_event: str | None = None,
) -> JobSpec:
    return JobSpec(
        job_id=job_id,
        run_id=run_id,
        project_id=project_id,
        stage="implementation",
        priority=priority,
        depends_on=depends_on or [],
        conflict_keys=conflict_keys or [],
        estimated_cost=estimated_cost,
        max_attempts=max_attempts,
        successor_event=successor_event,
    )


def test_run_reload_and_job_enqueue_are_json_safe_and_idempotent(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    register_active(runtime, "run-1", "project-1")
    spec = job("job-1", "run-1", "project-1")

    first = runtime.enqueue_job(spec)
    replay = runtime.enqueue_job(spec)
    assert replay == first
    assert runtime.get_job("job-1").model_validate_json(first.model_dump_json()) == first

    runtime.close()
    reloaded = WorkflowRuntime(tmp_path / "workflow.sqlite3", clock=Clock())
    assert reloaded.get_run("run-1").project_id == "project-1"
    assert reloaded.get_job("job-1").status is JobStatus.READY
    reloaded.close()


def test_transition_outbox_is_atomic_and_conflicting_replay_is_rejected(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    assert runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate").state is WorkflowState.IMPLEMENTING_ECONOMY
    assert [event.event_type for event in runtime.pending_events()] == ["run.transitioned"]

    with pytest.raises(RuntimeConflictError):
        runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="activate")


def test_dependencies_and_stage_capacity_block_incorrect_dispatch(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path, stage_limits={"implementation": 1})
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("base", "run-1", "project-1", conflict_keys=["shared"]))
    runtime.enqueue_job(job("dependent", "run-1", "project-1", depends_on=["base"]))
    first = runtime.claim_next("worker-1")
    assert first is not None and first.job_id == "base"
    assert runtime.claim_next("worker-2") is None
    runtime.complete_job(first.lease_id, "worker-1", JobOutcome.SUCCEEDED)
    second = runtime.claim_next("worker-2")
    assert second is not None and second.job_id == "dependent"


def test_conflict_keys_block_only_the_conflicting_job(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)[0]
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("locked", "run-1", "project-1", priority=1, conflict_keys=["db:migrate"]))
    runtime.enqueue_job(job("independent", "run-1", "project-1", conflict_keys=["docs"]))

    first = runtime.claim_next("worker-1")
    assert first is not None and first.job_id == "locked"
    second = runtime.claim_next("worker-2")
    assert second is not None and second.job_id == "independent"


def test_project_fairness_prevents_one_project_from_starving_another(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)[0]
    register_active(runtime, "run-a", "project-a")
    register_active(runtime, "run-b", "project-b")
    runtime.enqueue_job(job("a-1", "run-a", "project-a", priority=100))
    runtime.enqueue_job(job("a-2", "run-a", "project-a", priority=100))
    runtime.enqueue_job(job("b-1", "run-b", "project-b", priority=1))

    first = runtime.claim_next("worker-1")
    second = runtime.claim_next("worker-2")
    assert first is not None and first.job_id == "a-1"
    assert second is not None and second.job_id == "b-1"


def test_only_lease_owner_can_complete_and_replay_has_no_second_effect(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)[0]
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("job-1", "run-1", "project-1", successor_event="stage.next"))
    lease = runtime.claim_next("worker-1")
    assert lease is not None
    with pytest.raises(LeaseOwnershipError):
        runtime.complete_job(lease.lease_id, "worker-2", JobOutcome.SUCCEEDED)

    completed = runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.25)
    replay = runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.25)
    assert completed == replay
    assert runtime.get_run("run-1").budget_spent == 0.25
    event_types = [event.event_type for event in runtime.pending_events()]
    assert event_types.count("job.succeeded") == 1
    assert event_types.count("stage.next") == 1


def test_expired_lease_reconciles_and_can_be_redispatched(tmp_path: Path) -> None:
    runtime, clock = make_runtime(tmp_path)
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("job-1", "run-1", "project-1", max_attempts=2))
    lease = runtime.claim_next("worker-1", lease_seconds=2)
    assert lease is not None
    clock.advance(3)
    report = runtime.reconcile()
    assert report.expired_leases == 1
    assert report.retry_scheduled == 1
    assert runtime.get_job("job-1").status is JobStatus.RETRY_SCHEDULED
    clock.advance(5)
    retried = runtime.claim_next("worker-2", lease_seconds=10)
    assert retried is not None and retried.attempt == 2
    with pytest.raises(LeaseExpiredError):
        runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED)


def test_budget_wait_and_cancellation_release_resources(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)[0]
    register_active(runtime, "run-1", "project-1", budget=1.0)
    runtime.enqueue_job(job("expensive", "run-1", "project-1", estimated_cost=2.0))
    assert runtime.claim_next("worker-1") is None
    assert runtime.get_job("expensive").status is JobStatus.WAITING_BUDGET

    runtime.enqueue_job(job("cheap", "run-1", "project-1", estimated_cost=1.0))
    lease = runtime.claim_next("worker-1")
    assert lease is not None and lease.job_id == "cheap"
    cancelled = runtime.cancel_run("run-1", reason="test cancellation")
    assert cancelled.state is WorkflowState.CANCELLED
    assert cancelled.budget_reserved == 0
    assert runtime.get_job("cheap").status is JobStatus.CANCELLED
    assert runtime.get_job("expensive").status is JobStatus.CANCELLED


def test_cancellation_also_stops_retry_scheduled_jobs(tmp_path: Path) -> None:
    runtime, clock = make_runtime(tmp_path)
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("job-1", "run-1", "project-1", max_attempts=2))
    lease = runtime.claim_next("worker-1", lease_seconds=1)
    assert lease is not None
    clock.advance(2)
    runtime.reconcile()
    assert runtime.get_job("job-1").status is JobStatus.RETRY_SCHEDULED
    assert runtime.cancel_run("run-1").state is WorkflowState.CANCELLED
    assert runtime.get_job("job-1").status is JobStatus.CANCELLED


def test_outbox_survives_restart_and_ack_is_idempotent(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    register_active(runtime, "run-1", "project-1")
    runtime.enqueue_job(job("job-1", "run-1", "project-1", estimated_cost=0.0))
    event = runtime.pending_events()[0]
    runtime.close()

    reloaded = WorkflowRuntime(tmp_path / "workflow.sqlite3", clock=Clock())
    restored = reloaded.pending_events()
    assert restored[0].event_id == event.event_id
    acked = reloaded.ack_event(event.event_id)
    assert acked.status is EventStatus.ACKED
    assert reloaded.ack_event(event.event_id).status is EventStatus.ACKED
    for pending in restored[1:]:
        reloaded.ack_event(pending.event_id)
    assert reloaded.pending_events() == []
    reloaded.close()


def test_cancel_transition_is_explicit_and_illegal_advance_stays_blocked(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    with pytest.raises(ValueError):
        runtime.transition_run("run-1", WorkflowState.DELIVERED)
    cancelled = runtime.cancel_run("run-1")
    assert cancelled.state is WorkflowState.CANCELLED


def test_local_runtime_rejects_delivery_registration_without_persisting_run(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)

    with pytest.raises(ReadinessError, match="LOCAL_RUNTIME_DELIVERY_FORBIDDEN"):
        runtime.register_run(
            "run-delivered",
            "project-1",
            initial_state=WorkflowState.DELIVERED,
        )

    with pytest.raises(RuntimeNotFoundError):
        runtime.get_run("run-delivered")
    assert runtime.pending_events() == []

    with sqlite3.connect(tmp_path / "workflow.sqlite3") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id = ?", ("run-delivered",)
        ).fetchone()[0] == 0


def test_local_runtime_rejects_delivery_transition_and_loaded_legacy_delivery(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="to-implement")
    runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="to-validate")
    runtime.transition_run("run-1", WorkflowState.INDEPENDENT_REVIEW, idempotency_key="to-review")
    events_before = runtime.pending_events()

    with pytest.raises(ReadinessError, match="LOCAL_RUNTIME_DELIVERY_FORBIDDEN"):
        runtime.transition_run("run-1", WorkflowState.DELIVERED, idempotency_key="to-delivered")

    assert runtime.get_run("run-1").state is WorkflowState.INDEPENDENT_REVIEW
    assert runtime.pending_events() == events_before
    runtime.close()

    with sqlite3.connect(tmp_path / "workflow.sqlite3") as connection:
        connection.execute(
            "UPDATE runs SET state = ? WHERE run_id = ?",
            (WorkflowState.DELIVERED.value, "run-1"),
        )
        connection.commit()

    reloaded = WorkflowRuntime(tmp_path / "workflow.sqlite3", clock=Clock())
    with pytest.raises(ReadinessError, match="LOCAL_RUNTIME_DELIVERY_FORBIDDEN"):
        reloaded.get_run("run-1")
    reloaded.close()


def test_old_transition_replay_does_not_reapply_state_mutation_or_outbox(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="validate-1")
    runtime.transition_run("run-1", WorkflowState.FAILED_VALIDATION, idempotency_key="fail-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="rework-1")
    events_before = runtime.pending_events()

    # Replaying validate-1 returns original RunRecord but does NOT mutate run or insert outbox event
    original_result = runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="validate-1")
    assert original_result.state is WorkflowState.VALIDATING
    assert runtime.get_run("run-1").state is WorkflowState.IMPLEMENTING_ECONOMY
    assert runtime.pending_events() == events_before


def test_transition_conflicting_key_rejected_even_if_current_state_matches(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="validate-1")
    runtime.transition_run("run-1", WorkflowState.FAILED_VALIDATION, idempotency_key="fail-1")
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="rework-1")

    # validate-1 was originally targeted to VALIDATING.
    # Calling it with target IMPLEMENTING_ECONOMY (which happens to be the current state) must fail with conflict.
    with pytest.raises(RuntimeConflictError):
        runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="validate-1")


def test_completion_replay_rejects_changed_cost_or_outcome_or_error(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1", budget_ceiling=10.0)
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    runtime.enqueue_job(job("j-1", "run-1", "project-1", estimated_cost=1.0))
    lease = runtime.claim_next("worker-1")
    assert lease is not None

    first = runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.25)
    assert runtime.get_run("run-1").budget_spent == 0.25

    # Replay with conflicting cost fails without changing budget
    with pytest.raises(RuntimeConflictError):
        runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=9.0)
    assert runtime.get_run("run-1").budget_spent == 0.25

    # Replay with conflicting outcome fails
    with pytest.raises(RuntimeConflictError):
        runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.FAILED, actual_cost=0.25)

    # Replay with conflicting error fails
    with pytest.raises(RuntimeConflictError):
        runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.25, error="unexpected")

    # Identical replay returns identical record
    replay = runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.25)
    assert replay == first
    assert runtime.get_run("run-1").budget_spent == 0.25


def test_completion_replay_preserves_historical_result_across_retries(tmp_path: Path) -> None:
    runtime, clock = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1", budget_ceiling=10.0)
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    runtime.enqueue_job(job("j-retry", "run-1", "project-1", estimated_cost=1.0, max_attempts=2))

    # Attempt 1 fails
    lease_1 = runtime.claim_next("worker-1")
    assert lease_1 is not None
    res_1 = runtime.complete_job(lease_1.lease_id, "worker-1", JobOutcome.FAILED, actual_cost=0.50, error="transient")
    assert res_1.attempt == 1
    assert res_1.status is JobStatus.RETRY_SCHEDULED
    assert res_1.last_error == "transient"

    # Attempt 2 succeeds
    clock.advance(10)
    runtime.reconcile()
    lease_2 = runtime.claim_next("worker-2")
    assert lease_2 is not None and lease_2.attempt == 2
    res_2 = runtime.complete_job(lease_2.lease_id, "worker-2", JobOutcome.SUCCEEDED, actual_cost=0.50)
    assert res_2.attempt == 2
    assert res_2.status is JobStatus.SUCCEEDED

    # Current job in DB is attempt 2, SUCCEEDED
    assert runtime.get_job("j-retry").status is JobStatus.SUCCEEDED
    assert runtime.get_job("j-retry").attempt == 2
    assert runtime.get_run("run-1").budget_spent == 1.0

    # Replay of attempt 1 returns the original attempt 1 record, without regressing current job in DB
    replay_1 = runtime.complete_job(lease_1.lease_id, "worker-1", JobOutcome.FAILED, actual_cost=0.50, error="transient")
    assert replay_1.attempt == 1
    assert replay_1.status is JobStatus.RETRY_SCHEDULED
    assert replay_1.last_error == "transient"
    assert runtime.get_job("j-retry").status is JobStatus.SUCCEEDED
    assert runtime.get_run("run-1").budget_spent == 1.0


def test_runtime_operations_survive_database_reload(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1", budget_ceiling=10.0)
    runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="k-act")
    runtime.enqueue_job(job("j-1", "run-1", "project-1", estimated_cost=1.0))
    lease = runtime.claim_next("worker-1")
    assert lease is not None
    runtime.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.40)
    events_before = runtime.pending_events()
    runtime.close()

    # Reload from disk
    reloaded = WorkflowRuntime(tmp_path / "workflow.sqlite3", clock=Clock())
    # Replay transition across reload
    reloaded.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="k-act")
    assert reloaded.pending_events() == events_before

    # Conflicting replay across reload
    with pytest.raises(RuntimeConflictError):
        reloaded.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="k-act")

    # Replay completion across reload
    reloaded.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=0.40)
    assert reloaded.get_run("run-1").budget_spent == 0.40

    with pytest.raises(RuntimeConflictError):
        reloaded.complete_job(lease.lease_id, "worker-1", JobOutcome.SUCCEEDED, actual_cost=5.0)
    reloaded.close()


def test_transactional_rollback_on_injected_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")

    def failing_record(*args, **kwargs):
        raise sqlite3.OperationalError("simulated crash during operation record")

    monkeypatch.setattr(runtime, "_record_operation", failing_record)
    with pytest.raises(sqlite3.OperationalError, match="simulated crash"):
        runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="fail-key")

    # Verify atomic rollback: neither operation record nor outbox event nor state was committed
    assert runtime.get_run("run-1").state is WorkflowState.READY_FOR_HANDOFF
    assert runtime.pending_events() == []
    with sqlite3.connect(tmp_path / "workflow.sqlite3") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM runtime_operations WHERE operation_key = ?",
            ("fail-key",),
        ).fetchone()[0] == 0


def test_two_distinct_operations_generate_two_valid_transitions_and_events(tmp_path: Path) -> None:
    runtime, _ = make_runtime(tmp_path)
    runtime.register_run("run-1", "project-1")
    t1 = runtime.transition_run("run-1", WorkflowState.IMPLEMENTING_ECONOMY, idempotency_key="step-1")
    assert t1.state is WorkflowState.IMPLEMENTING_ECONOMY

    t2 = runtime.transition_run("run-1", WorkflowState.VALIDATING, idempotency_key="step-2")
    assert t2.state is WorkflowState.VALIDATING

    events = runtime.pending_events()
    assert len(events) == 2
    assert {e.dedupe_key for e in events} == {"step-1", "step-2"}

