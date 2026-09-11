"""HF-05 public-API counterexamples, separate from the normal suite.

Only disposable local SQLite databases; no workers, cloud or network.
"""
from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.workflow.contracts import WorkflowState as S
from core.workflow.runtime import JobOutcome, JobSpec, RuntimeConflictError, WorkflowRuntime


@pytest.fixture
def runtime(tmp_path):
    value = WorkflowRuntime(tmp_path / "review.sqlite3", clock=lambda: datetime(2026, 9, 9, tzinfo=UTC))
    yield value
    value.close()


@pytest.mark.parametrize("route", ["registration", "transitions"])
def test_local_runtime_cannot_declare_delivery_without_evidence(runtime, route):
    try:
        if route == "registration":
            runtime.register_run("r", "p", initial_state=S.DELIVERED)
        else:
            runtime.register_run("r", "p")
            for target in [S.IMPLEMENTING_ECONOMY, S.VALIDATING, S.INDEPENDENT_REVIEW, S.DELIVERED]:
                runtime.transition_run("r", target)
    except ValueError:
        return
    assert runtime.get_run("r").state is not S.DELIVERED


def test_completion_replay_rejects_changed_cost(runtime):
    runtime.register_run("r", "p", budget_ceiling=10.0)
    runtime.transition_run("r", S.IMPLEMENTING_ECONOMY)
    runtime.enqueue_job(JobSpec(job_id="j", run_id="r", project_id="p", stage="implementation", estimated_cost=1.0))
    lease = runtime.claim_next("w")
    assert lease is not None
    runtime.complete_job(lease.lease_id, "w", JobOutcome.SUCCEEDED, actual_cost=0.25)
    with pytest.raises(RuntimeConflictError):
        runtime.complete_job(lease.lease_id, "w", JobOutcome.SUCCEEDED, actual_cost=9.0)


def test_old_transition_replay_does_not_reapply_state_mutation(runtime):
    runtime.register_run("r", "p")
    runtime.transition_run("r", S.IMPLEMENTING_ECONOMY, idempotency_key="activate")
    runtime.transition_run("r", S.VALIDATING, idempotency_key="validate-first")
    runtime.transition_run("r", S.FAILED_VALIDATION, idempotency_key="validation-failed")
    runtime.transition_run("r", S.IMPLEMENTING_ECONOMY, idempotency_key="rework")
    events_before = runtime.pending_events()
    runtime.transition_run("r", S.VALIDATING, idempotency_key="validate-first")
    assert runtime.pending_events() == events_before
    assert runtime.get_run("r").state is S.IMPLEMENTING_ECONOMY
