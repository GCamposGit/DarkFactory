"""HF-27-08 review items 4 and 6: owner notifications (idempotent per
job_key) and handler-exception containment in CloudWorker.dispatch_claimed_job.

No network, no real git remote, no real Telegram/agent CLI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner, StageResult
from core.workflow.handlers import HandlerRegistry


@pytest.fixture
def store(tmp_path):
    s = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=30)
    s._backend.db_path = tmp_path / "control.db"
    return s


class _AlwaysStageHandler:
    def __init__(self, result: StageResult) -> None:
        self.result = result
        self.calls = 0

    def handle(self, context):
        self.calls += 1
        return self.result


class _RaisingHandler:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, context):
        self.calls += 1
        raise RuntimeError("boom")


def _accept_grill(store, project_id="darkfac") -> str:
    cmd = IntakeCommand(
        project_id=project_id, channel="test", external_id=f"notif-{project_id}", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    assert receipt.run_id is not None
    return receipt.run_id


# --------------------------------------------------------------------------
# Item 6: handler exception -> retry(handler_error), lease freed immediately
# --------------------------------------------------------------------------


def test_handler_exception_becomes_retry_not_stuck_running(store) -> None:
    registry = HandlerRegistry()
    registry[("grill", "v1")] = _RaisingHandler()

    # HF-27-08 review item 9: the real "darkfac" project (default registry
    # entry) now stamps grill jobs with ["git", "harness:any"] too.
    worker = CloudWorker(
        worker_id="w1", max_slots=1, store=store, capabilities=["grill_engine", "git", "harness:any"], registry=registry
    )
    run_id = _accept_grill(store)

    executed = worker.poll_and_execute_once()
    assert executed is True

    status = store.get_run_status(run_id)
    job = next(j for j in status["jobs"] if j["stage"] == "grill")
    # Not stuck "running": either retried (pending) or, if it happened to
    # exceed retry_count in this store's generic finish() path, failed --
    # either way it is NOT left running until lease expiry.
    assert job["status"] != "running"
    assert (job.get("cause_code") or "").startswith("handler_error:RuntimeError")


# --------------------------------------------------------------------------
# Item 4: owner notifications are idempotent per (job_key, kind)
# --------------------------------------------------------------------------


def test_failure_notification_sent_once_per_job_key(store, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = HandlerRegistry()
    registry[("grill", "v1")] = _AlwaysStageHandler(StageResult(outcome="failed", cause_code="boom"))
    worker = CloudWorker(
        worker_id="w1", max_slots=1, store=store, capabilities=["grill_engine", "git", "harness:any"], registry=registry
    )

    sent: list[str] = []
    monkeypatch.setattr(worker, "_send_failure_summary", lambda claim, result: sent.append(claim.job_key.canonical_key()) or True)

    run_id = _accept_grill(store)
    executed = worker.poll_and_execute_once()
    assert executed is True
    assert len(sent) == 1

    # Simulate a crash-reclaim: re-run the SAME job_key/iteration with a
    # fresh claim (new lease/fencing token) -- e.g. via reconcile(). The
    # failure notification must not be sent a second time for the same job.
    status = store.get_run_status(run_id)
    grill_job = next(j for j in status["jobs"] if j["stage"] == "grill")
    # The job already finished (failed); nothing left to reclaim in this
    # store's normal flow, so directly exercise the idempotency guard:
    # calling _notify_once again for the same key must not resend.
    from core.workflow.control_contracts import Claim, JobKey

    jk = JobKey(run_id=run_id, ticket_id="darkfac", plan_version="1.0", stage="grill", iteration=0)
    fake_claim = Claim(
        job_key=jk, lease_id="lease-replay", owner="w1", fencing_token=99,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    worker._notify_once(fake_claim, "failed", lambda: sent.append("second-call") or True)
    assert sent == [jk.canonical_key()]  # no "second-call" appended
