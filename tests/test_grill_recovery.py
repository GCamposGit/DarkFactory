from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import JobKey


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    run_id = "run-grill-sweep"
    job = JobKey(run_id=run_id, ticket_id="USR-550", plan_version="p1", stage="grill", iteration=0)
    store = MagicMock()
    store.list_waiting_jobs.return_value = [job]
    worker = CloudWorker(store=store)
    worker._close_terminal_request_mirrors = lambda: None
    worker._close_ghost_open_job = lambda *_args: False
    project = object()
    monkeypatch.setattr("core.line.bindings.project_for_run", lambda *_args: project)
    resumed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.line.human.resume_blocked_job",
        lambda _store, rid, stage, **_kwargs: resumed.append((rid, stage)) or True,
    )
    return worker, store, project, run_id, job, now, resumed


def test_valid_unexpired_grill_stays_waiting_and_is_not_renotified(sweep, monkeypatch):
    worker, _store, project, run_id, _job, now, resumed = sweep
    deadline = now + timedelta(hours=1)
    monkeypatch.setattr("core.line.stage_grill.inspect_pending_grill", lambda *_: (2, deadline))
    notified: list[str] = []
    worker._grill_resender = lambda _project, rid: notified.append(rid) or True

    assert worker.sweep_waiting_human_requests(now) == []
    assert resumed == []
    assert notified == [run_id]


@pytest.mark.parametrize("session", [None, "expired"])
def test_orphan_or_expired_grill_is_resumed_by_sweep(sweep, monkeypatch, session):
    worker, _store, project, run_id, _job, now, resumed = sweep
    if session is None:
        proof = None
    else:
        proof = (1, now - timedelta(seconds=1))
    monkeypatch.setattr("core.line.stage_grill.inspect_pending_grill", lambda *_: proof)

    assert worker.sweep_waiting_human_requests(now) == [run_id]
    assert resumed == [(run_id, "grill")]
