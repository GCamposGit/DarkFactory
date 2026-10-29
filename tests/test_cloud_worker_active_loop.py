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


# --------------------------------------------------------------------------
# USR-105: HumanRequest periodic probe sweeper and auto-resume
# --------------------------------------------------------------------------

def _setup_waiting_human_job(store, stage: str = "integration") -> tuple[str, Any]:
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test_channel",
        external_id=f"test-wait-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "Waiting Human Test",
            "problem": "Validate sweeper",
            "journey": "J",
            "non_goals": ["None"],
            "criteria": ["Done"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    run_id = receipt.run_id
    claim = store.claim("test-worker-1", ["grill_engine", "integrator"], datetime.now(UTC))
    store.finish(
        claim,
        StageResult(outcome="waiting_human", cause_code="infra_wait"),
        datetime.now(UTC),
    )
    return run_id, claim.job_key


def test_human_probe_sweeper_closes_waiting_job_on_a_finished_run(temp_stores, monkeypatch):
    """A completed run must not keep a waiting_human job in the owner queue."""
    store, artifact_store = temp_stores
    closed_mirrors = []
    from core.owner_actions import mirror

    monkeypatch.setattr(mirror, "close_mirrored_request", lambda run_id, stage: closed_mirrors.append((run_id, stage)))
    ghost_id, ghost_key = _setup_waiting_human_job(store)
    live_id, _live_key = _setup_waiting_human_job(store)
    conn = store._backend._connect()
    try:
        conn.execute("UPDATE runs SET status = 'completed' WHERE run_id = ?", (ghost_id,))
        conn.commit()
    finally:
        conn.close()

    worker = CloudWorker(worker_id="test-sweeper", store=store, artifact_store=artifact_store)
    assert worker.sweep_waiting_human_requests() == []

    ghost = store.get_run_status(ghost_id)
    assert ghost["status"] == "cancelled"
    assert {job["status"] for job in ghost["jobs"]} == {"cancelled"}
    assert all(job["cause_code"] == "ghost_open_job_on_terminal_run" for job in ghost["jobs"])
    assert (ghost_id, ghost_key.stage) in closed_mirrors

    live = store.get_run_status(live_id)
    assert live["status"] == "active"
    assert any(job["status"] == "waiting_human" for job in live["jobs"])


def test_human_probe_sweeper_resumes_green_infra_request(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "integration")

    from core.line.human import HumanRequest

    req = HumanRequest(
        kind="infra",
        run_id=run_id,
        blocking_stage="grill",
        guide_md="Infra guide",
        probe_cmd="python -c \"import sys; sys.exit(0)\"",
    )

    worker = CloudWorker(
        worker_id="test-sweeper",
        store=store,
        artifact_store=artifact_store,
        request_loader=lambda proj, r_id: req if r_id == run_id else None,
        human_probe_runner=lambda cmd, **kw: True,
    )

    # Before sweep: job is waiting_human
    waiting = store.list_waiting_jobs(statuses=("waiting_human",))
    assert len(waiting) == 1
    assert waiting[0].run_id == run_id

    # Run sweep directly
    resumed = worker.sweep_waiting_human_requests()
    assert resumed == [run_id]

    # After sweep: job was resumed into pending
    waiting_after = store.list_waiting_jobs(statuses=("waiting_human",))
    assert len(waiting_after) == 0

    status = store.get_run_status(run_id)
    grill_jobs = [j for j in status["jobs"] if j["stage"] == "grill"]
    assert any(j["status"] == "pending" for j in grill_jobs)


def test_human_probe_sweeper_recovers_legacy_quota_wait_without_saved_probe(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "development")
    conn = store._backend._connect()
    try:
        conn.execute(
            "UPDATE jobs SET cause_code = 'no_route_available' WHERE run_id = ? AND stage = ?",
            (run_id, job_key.stage),
        )
        conn.commit()
    finally:
        conn.close()

    from core.line.human import HumanRequest

    request = HumanRequest(kind="infra", run_id=run_id, blocking_stage=job_key.stage, guide_md="Old quota guide")
    probed = []
    worker = CloudWorker(
        worker_id="test-legacy-quota-sweeper",
        store=store,
        artifact_store=artifact_store,
        request_loader=lambda _project, _run_id: request,
        human_probe_runner=lambda cmd, **_kwargs: probed.append(cmd) or True,
    )

    assert worker.sweep_waiting_human_requests() == [run_id]
    assert probed == [f"python -m core.line.route_probe {job_key.stage}"]
    assert store.list_waiting_jobs(statuses=("waiting_human",)) == []


def test_human_probe_sweeper_closes_mirror_for_cancelled_run_with_no_open_job(temp_stores, monkeypatch):
    from types import SimpleNamespace

    from core.owner_actions import mirror, store as owner_action_store

    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store)
    store.cancel_run(run_id, reason="owner_cancelled", actor="owner")
    action = SimpleNamespace(
        id=f"OA-HR-{run_id}-{job_key.stage}",
        created_by="production-line",
        title=f"Linha parada (infra): run {run_id} aguarda voce no estagio '{job_key.stage}'",
    )

    class _ActionStore:
        def list_actions(self, *, include_done: bool = True):
            assert include_done is False
            return [action]

    closed = []
    monkeypatch.setattr(owner_action_store, "OwnerActionStore", _ActionStore)
    monkeypatch.setattr(mirror, "close_mirrored_request", lambda run_id, stage: closed.append((run_id, stage)))

    worker = CloudWorker(worker_id="test-cancelled-mirror-sweeper", store=store, artifact_store=artifact_store)
    assert worker.sweep_waiting_human_requests() == []
    assert closed == [(run_id, job_key.stage)]


def test_human_probe_sweeper_does_not_resume_red_infra_request(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "integration")

    from core.line.human import HumanRequest

    req = HumanRequest(
        kind="infra",
        run_id=run_id,
        blocking_stage="grill",
        guide_md="Infra guide",
        probe_cmd="python -c \"import sys; sys.exit(1)\"",
    )

    worker = CloudWorker(
        worker_id="test-sweeper",
        store=store,
        artifact_store=artifact_store,
        request_loader=lambda proj, r_id: req if r_id == run_id else None,
        human_probe_runner=lambda cmd, **kw: False,
    )

    resumed = worker.sweep_waiting_human_requests()
    assert resumed == []

    # Still waiting_human
    waiting = store.list_waiting_jobs(statuses=("waiting_human",))
    assert len(waiting) == 1


def test_human_probe_sweeper_ignores_grill_and_commercial_acceptance(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "integration")

    from core.line.human import HumanRequest

    for kind in ("grill", "commercial_acceptance"):
        req = HumanRequest(
            kind=kind,
            run_id=run_id,
            blocking_stage="grill",
            guide_md="Guide",
            probe_cmd="python -c \"import sys; sys.exit(0)\"",
        )
        called = {"probed": False}

        def _probe(cmd, **kw):
            called["probed"] = True
            return True

        worker = CloudWorker(
            worker_id="test-sweeper",
            store=store,
            artifact_store=artifact_store,
            request_loader=lambda proj, r_id: req,
            human_probe_runner=_probe,
        )

        resumed = worker.sweep_waiting_human_requests()
        assert resumed == []
        assert not called["probed"]


def test_human_probe_sweeper_handles_exception_without_crashing_worker(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "integration")

    from core.line.human import HumanRequest

    req = HumanRequest(
        kind="infra",
        run_id=run_id,
        blocking_stage="grill",
        guide_md="Guide",
        probe_cmd="timeout",
    )

    def _exploding_probe(cmd, **kw):
        raise RuntimeError("Network timeout or crash")

    worker = CloudWorker(
        worker_id="test-sweeper",
        store=store,
        artifact_store=artifact_store,
        request_loader=lambda proj, r_id: req,
        human_probe_runner=_exploding_probe,
    )

    # Must never raise or crash
    resumed = worker.sweep_waiting_human_requests()
    assert resumed == []


def test_human_probe_sweeper_respects_interval(temp_stores):
    store, artifact_store = temp_stores
    sweep_calls = {"n": 0}

    worker = CloudWorker(
        worker_id="test-sweeper",
        store=store,
        artifact_store=artifact_store,
        human_probe_interval_s=600.0,
    )

    orig_sweep = worker.sweep_waiting_human_requests
    def _tracked_sweep(now=None):
        sweep_calls["n"] += 1
        return orig_sweep(now=now)

    worker.sweep_waiting_human_requests = _tracked_sweep

    t0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    worker.poll_and_execute_once(now=t0)
    assert sweep_calls["n"] == 1

    # 300s later: interval (600s) has not passed, no new sweep
    t1 = datetime(2026, 10, 1, 12, 5, 0, tzinfo=UTC)
    worker.poll_and_execute_once(now=t1)
    assert sweep_calls["n"] == 1

    # 601s later: interval passed, sweep triggers
    t2 = datetime(2026, 10, 1, 12, 10, 1, tzinfo=UTC)
    worker.poll_and_execute_once(now=t2)
    assert sweep_calls["n"] == 2


def test_human_probe_sweeper_resends_unnotified_grill(temp_stores):
    store, artifact_store = temp_stores
    run_id, job_key = _setup_waiting_human_job(store, "grill")

    resender_calls = []

    def _fake_resender(project, r_id):
        resender_calls.append((project, r_id))
        return True

    worker = CloudWorker(
        worker_id="test-sweeper",
        store=store,
        artifact_store=artifact_store,
        grill_resender=_fake_resender,
    )

    worker.sweep_waiting_human_requests()
    assert len(resender_calls) == 1
    assert resender_calls[0][1] == run_id


# --------------------------------------------------------------------------
# USR-106: RouteWaiter on cloud_worker dispatch when route is unavailable
# --------------------------------------------------------------------------

def test_worker_resumes_sweeping_and_claiming_once_the_line_lock_is_released(temp_stores, tmp_path, monkeypatch):
    """USR-427: ``poll_and_execute_once`` (core/orchestrator/cloud_worker.py:1359) bails out at
    ``line_lock.is_locked()`` -- before any sweep or ``store.claim`` -- whenever another LIVE
    process holds the local line lock (the gate's own ``run_ticket`` run holds exactly this kind
    of lock while it runs this very suite, which is what made USR-423's context-retention
    sweeper assertions flaky: see ``docs/manual/testes.md``/``tests/line/test_context_retention.py``
    for the companion fix). This pins both halves of that contract: the early-return while held,
    and the resumed sweeping/claiming once released, on the SAME worker instance.
    """
    import json
    import subprocess
    import sys
    import time

    from core.line import line_lock

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))

    store, artifact_store = temp_stores
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test_channel",
        external_id=f"test-linelock-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "Line Lock Early Return Test",
            "problem": "Validate poll_and_execute_once bails out under a foreign line lock",
            "journey": "Intake -> Claim",
            "non_goals": ["No manual intervention"],
            "criteria": ["Status = succeeded"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))

    worker = CloudWorker(
        worker_id="test-linelock-worker",
        store=store,
        artifact_store=artifact_store,
        registry=_fake_line_registry(),
    )

    sweep_calls: list[str] = []
    for name in (
        "sweep_waiting_human_requests",
        "sweep_stale_workspaces",
        "sweep_stale_artifacts",
        "sweep_recovery_contexts",
        "sweep_orphan_runs",
    ):
        monkeypatch.setattr(worker, name, lambda *a, _n=name, **kw: sweep_calls.append(_n))

    claim_calls = []
    orig_claim = store.claim

    def _tracked_claim(*a, **kw):
        claim_calls.append(True)
        return orig_claim(*a, **kw)

    monkeypatch.setattr(store, "claim", _tracked_claim)

    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_dir = line_lock.lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        line_lock.lock_path().write_text(
            json.dumps(
                {
                    "pid": holder.pid,
                    "ticket_id": "foreign",
                    "hostname": "test",
                    "started_at": now,
                    "heartbeat_at": now,
                }
            ),
            encoding="utf-8",
        )
        assert line_lock.is_locked() is True

        # Locked: poll bails out before touching any sweeper or claiming work.
        executed_while_locked = worker.poll_and_execute_once()
        assert executed_while_locked is False
        assert sweep_calls == []
        assert claim_calls == []

        status_while_locked = store.get_run_status(receipt.run_id)
        assert status_while_locked["jobs"][0]["status"] == "pending"
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    line_lock.lock_path().unlink()
    assert line_lock.is_locked() is False

    # Unlocked: the SAME worker instance resumes sweeping and claims the pending job.
    executed_after_release = worker.poll_and_execute_once()
    assert executed_after_release is True
    assert sweep_calls, "sweeps must run once the foreign lock is gone"
    assert claim_calls == [True]

    status_after_release = store.get_run_status(receipt.run_id)
    assert status_after_release["jobs"][0]["status"] == "succeeded"


def test_cloud_worker_dispatch_no_route_uses_route_waiter(temp_stores):
    store, artifact_store = temp_stores
    cmd = IntakeCommand(
        project_id="darkfac",
        channel="test_channel",
        external_id=f"test-noroute-{uuid4().hex[:6]}",
        mode="autonomous",
        policy_ref="policy-v1",
        payload={
            "title": "No Route Test",
            "problem": "P",
            "journey": "J",
            "non_goals": ["N"],
            "criteria": ["C"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    run_id = receipt.run_id

    from core.line.route_wait import RouteWaiter

    waiter = RouteWaiter()
    worker = CloudWorker(
        worker_id="test-no-route-worker",
        store=store,
        artifact_store=artifact_store,
        capabilities=["git"],  # no harness:* capability
        autodetect_tooling=True,
        route_waiter=waiter,
    )

    # Worker claims and dispatches grill job
    executed = worker.poll_and_execute_once()
    assert executed is True

    status = store.get_run_status(run_id)
    grill_jobs = [j for j in status["jobs"] if j["stage"] == "grill"]
    assert len(grill_jobs) >= 1
    # Must use RouteWaiter (starts with no_route_available not_before=...)
    assert any((j.get("cause_code") or "").startswith("no_route_available not_before=") for j in grill_jobs)
