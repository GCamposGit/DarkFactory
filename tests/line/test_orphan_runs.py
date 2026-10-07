"""USR-142: sweeper for `active` runs with no open job and no recent activity."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line.orphan_runs import (
    ORPHAN_IDLE_LIMIT,
    ORPHAN_NO_SUCCESSOR_CAUSE,
    find_orphan_run,
    sweep_orphan_runs,
)
from core.workflow.control_contracts import JobKey, StageResult
from core.workflow.control_store import RUN_OPEN_JOB_STATUSES, SQLiteControlStore
from core.workflow.line_live import read_line_live
from core.workflow.successors import PRODUCTIVE_DAG
from tests.fixtures.line_live_seed import seed_line_live_demo

NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


class FakeStore:
    """In-memory stand-in exposing only what the sweeper reads; `fake_materialize` mutates it."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.list_error: Exception | None = None

    def add_run(self, run_id: str, jobs: list[tuple[str, int, str, float]], *, status: str = "active") -> None:
        """jobs: (stage, iteration, status, minutes since its last update)."""
        self.runs[run_id] = {
            "run_id": run_id,
            "status": status,
            "jobs": [
                {
                    "ticket_id": "darkfac",
                    "plan_version": "1.0",
                    "stage": stage,
                    "iteration": iteration,
                    "status": job_status,
                    "created_at": _ago(minutes + 1),
                    "updated_at": _ago(minutes),
                    "finished_at": None if job_status in RUN_OPEN_JOB_STATUSES else _ago(minutes),
                    "cause_code": None,
                    "output_refs": ["art://out"] if job_status == "succeeded" else [],
                }
                for stage, iteration, job_status, minutes in jobs
            ],
        }

    # -- the store surface used by the sweeper ----------------------------------------------------
    def list_active_run_ids(self) -> list[str]:
        if self.list_error is not None:
            raise self.list_error
        return [rid for rid, run in self.runs.items() if run["status"] == "active"]

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    def max_iteration(self, run_id: str, ticket_id: str, plan_version: str, stage: str) -> int:
        iterations = [j["iteration"] for j in self.runs[run_id]["jobs"] if j["stage"] == stage]
        return max(iterations, default=-1)


class RecordingMaterializer:
    """Mimics `materialize_result` for the outcomes the sweeper replays, on a `FakeStore`."""

    def __init__(self) -> None:
        self.calls: list[tuple[JobKey, StageResult]] = []

    def __call__(self, job_key: JobKey, result: StageResult, store: FakeStore, *, now: datetime, **_: Any) -> list[JobKey]:
        self.calls.append((job_key, result))
        run = store.runs[job_key.run_id]
        stage = PRODUCTIVE_DAG.get(job_key.stage)
        if result.outcome == "success" and stage is not None:
            run["jobs"].append(self._job(job_key, stage, job_key.iteration, "pending", now))
        elif result.outcome == "waiting_human":
            run["jobs"].append(self._job(job_key, job_key.stage, job_key.iteration, "waiting_human", now, result.cause_code))
        elif result.outcome == "success":  # terminal stage: nothing left, the run closes
            run["status"] = "completed"
        return []

    @staticmethod
    def _job(key: JobKey, stage: str, iteration: int, status: str, now: datetime, cause: str | None = None) -> dict[str, Any]:
        return {
            "ticket_id": key.ticket_id,
            "plan_version": key.plan_version,
            "stage": stage,
            "iteration": iteration,
            "status": status,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "finished_at": None,
            "cause_code": cause,
            "output_refs": [],
        }


def _statuses(store: FakeStore, run_id: str) -> list[tuple[str, str]]:
    return [(j["stage"], j["status"]) for j in store.runs[run_id]["jobs"]]


# ----------------------------------------------------------------------------- detection


def test_orphan_run_after_grill_is_detected_and_planning_is_scheduled() -> None:
    store = FakeStore()
    store.add_run("run-orphan", [("grill", 0, "succeeded", 15 * 24 * 60)])
    materialize = RecordingMaterializer()

    actions = sweep_orphan_runs(store, now=NOW, materialize=materialize)

    assert [(a.run_id, a.action, a.stage) for a in actions] == [("run-orphan", "scheduled", "grill")]
    (key, result), = materialize.calls
    assert (key.stage, key.iteration, result.outcome) == ("grill", 0, "success")
    assert result.output_refs == ["art://out"]
    assert _statuses(store, "run-orphan") == [("grill", "succeeded"), ("planning", "pending")]


def test_recent_run_below_the_idle_limit_is_ignored() -> None:
    store = FakeStore()
    store.add_run("run-recent", [("grill", 0, "succeeded", 29)])
    materialize = RecordingMaterializer()

    assert sweep_orphan_runs(store, now=NOW, materialize=materialize) == []
    assert materialize.calls == []
    assert _statuses(store, "run-recent") == [("grill", "succeeded")]


def test_the_idle_limit_is_the_panel_stall_threshold_and_boundary_is_exclusive() -> None:
    assert ORPHAN_IDLE_LIMIT == timedelta(minutes=30)
    store = FakeStore()
    store.add_run("run-edge", [("grill", 0, "succeeded", 30)])
    assert sweep_orphan_runs(store, now=NOW, materialize=RecordingMaterializer()) == []
    store.add_run("run-over", [("grill", 0, "succeeded", 31)])
    assert [a.run_id for a in sweep_orphan_runs(store, now=NOW, materialize=RecordingMaterializer())] == ["run-over"]


@pytest.mark.parametrize("open_status", ["pending", "running", "waiting_human", "waiting_dependency"])
def test_run_with_an_open_job_is_ignored_even_when_old(open_status: str) -> None:
    store = FakeStore()
    store.add_run("run-open", [("grill", 0, "succeeded", 600), ("planning", 0, open_status, 600)])
    materialize = RecordingMaterializer()

    assert sweep_orphan_runs(store, now=NOW, materialize=materialize) == []
    assert materialize.calls == []


def test_runs_that_are_not_active_or_have_no_jobs_are_ignored() -> None:
    store = FakeStore()
    store.add_run("run-done", [("retrospective", 0, "succeeded", 600)], status="completed")
    store.add_run("run-empty", [])
    materialize = RecordingMaterializer()

    assert sweep_orphan_runs(store, now=NOW, materialize=materialize) == []
    assert materialize.calls == []


def test_newest_job_decides_and_bounced_stages_use_their_own_iteration() -> None:
    store = FakeStore()
    store.add_run(
        "run-bounce",
        [("development", 0, "succeeded", 300), ("validation", 0, "retry", 200), ("development", 1, "succeeded", 100)],
    )
    materialize = RecordingMaterializer()

    sweep_orphan_runs(store, now=NOW, materialize=materialize)

    (key, _), = materialize.calls
    assert (key.stage, key.iteration) == ("development", 1)
    assert _statuses(store, "run-bounce")[-1] == ("validation", "pending")


def test_find_orphan_run_is_pure_and_reports_idle_time() -> None:
    store = FakeStore()
    store.add_run("run-x", [("grill", 0, "succeeded", 90)])
    orphan = find_orphan_run(store.runs["run-x"], now=NOW)
    assert orphan is not None
    assert orphan.last_job.stage == "grill"
    assert orphan.idle == timedelta(minutes=90)
    assert find_orphan_run(store.runs["run-x"], now=NOW - timedelta(minutes=70)) is None


# ----------------------------------------------------------------------- parking / closing


def test_run_without_a_schedulable_successor_is_parked_with_an_explicit_cause() -> None:
    store = FakeStore()
    store.add_run("run-cancelled", [("development", 0, "cancelled", 120)])
    materialize = RecordingMaterializer()

    actions = sweep_orphan_runs(store, now=NOW, materialize=materialize)

    assert [(a.action, a.detail) for a in actions] == [("parked", ORPHAN_NO_SUCCESSOR_CAUSE)]
    (key, result), = materialize.calls
    assert result.outcome == "waiting_human" and result.cause_code == ORPHAN_NO_SUCCESSOR_CAUSE
    assert (key.stage, key.iteration) == ("validation", 0)
    assert store.runs["run-cancelled"]["jobs"][-1]["status"] == "waiting_human"


def test_replay_that_creates_no_open_job_falls_back_to_parking() -> None:
    """Successor row already exists in a terminal state (ON CONFLICT DO NOTHING): the run must not loop."""
    store = FakeStore()
    store.add_run("run-stuck", [("grill", 0, "succeeded", 120)])

    def noop(*_: Any, **__: Any) -> list[JobKey]:
        return []

    park_calls: list[StageResult] = []

    def materialize(key: JobKey, result: StageResult, st: FakeStore, *, now: datetime, **_: Any) -> list[JobKey]:
        if result.outcome == "waiting_human":
            park_calls.append(result)
            return RecordingMaterializer()(key, result, st, now=now)
        return noop()

    actions = sweep_orphan_runs(store, now=NOW, materialize=materialize)

    assert [a.action for a in actions] == ["parked"]
    assert [c.cause_code for c in park_calls] == [ORPHAN_NO_SUCCESSOR_CAUSE]


def test_terminal_stage_replay_closes_the_run() -> None:
    store = FakeStore()
    store.add_run("run-last", [("retrospective", 0, "succeeded", 120)])

    actions = sweep_orphan_runs(store, now=NOW, materialize=RecordingMaterializer())

    assert [(a.action, a.detail) for a in actions] == [("closed", "completed")]


# ------------------------------------------------------------------ idempotency / safety


def test_second_sweep_is_a_noop() -> None:
    store = FakeStore()
    store.add_run("run-a", [("grill", 0, "succeeded", 120)])
    store.add_run("run-b", [("development", 0, "cancelled", 120)])
    materialize = RecordingMaterializer()

    first = sweep_orphan_runs(store, now=NOW, materialize=materialize)
    calls_after_first = len(materialize.calls)
    second = sweep_orphan_runs(store, now=NOW + timedelta(hours=2), materialize=materialize)

    assert sorted(a.action for a in first) == ["parked", "scheduled"]
    assert second == []
    assert len(materialize.calls) == calls_after_first


def test_max_actions_bounds_one_sweep_and_only_run_ids_and_dry_run_are_honoured() -> None:
    store = FakeStore()
    for index in range(3):
        store.add_run(f"run-{index}", [("grill", 0, "succeeded", 120)])
    materialize = RecordingMaterializer()

    assert len(sweep_orphan_runs(store, now=NOW, materialize=materialize, max_actions=2)) == 2

    dry = sweep_orphan_runs(store, now=NOW, materialize=materialize, only_run_ids=["run-2"], dry_run=True)
    assert [(a.run_id, a.action) for a in dry] == [("run-2", "orphan")]
    assert _statuses(store, "run-2") == [("grill", "succeeded")]


def test_store_failures_never_raise() -> None:
    store = FakeStore()
    store.list_error = RuntimeError("db down")
    assert sweep_orphan_runs(store, now=NOW) == []
    assert sweep_orphan_runs(object(), now=NOW) == []  # store without list_active_run_ids

    store.list_error = None
    store.add_run("run-boom", [("grill", 0, "succeeded", 120)])
    store.add_run("run-ok", [("grill", 0, "succeeded", 120)])

    def flaky(key: JobKey, result: StageResult, st: FakeStore, *, now: datetime, **_: Any) -> list[JobKey]:
        if key.run_id == "run-boom":
            raise RuntimeError("boom")
        return RecordingMaterializer()(key, result, st, now=now)

    actions = sweep_orphan_runs(store, now=NOW, materialize=flaky)
    assert [(a.run_id, a.action) for a in actions] == [("run-boom", "error"), ("run-ok", "scheduled")]


# ------------------------------------------------- real SQLite store + live panel (USR-142)


def _execute(db: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_sweeper_on_the_real_store_schedules_planning_and_the_panel_stops_flagging_the_run(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    ids = seed_line_live_demo(db, NOW)
    run_id = ids["queued"]
    finished = (NOW - timedelta(hours=2)).isoformat()
    _execute(
        db,
        "UPDATE jobs SET status = 'succeeded', started_at = ?, finished_at = ?, updated_at = ?, output_refs = ? WHERE run_id = ?",
        (finished, finished, finished, '["art://grill"]', run_id),
    )
    before = next(r for r in read_line_live(db, database_url="", now=NOW).runs if r.run_id == run_id)
    assert before.stalled_reason is not None and before.stalled_reason.startswith("nenhuma etapa agendada")

    store = SQLiteControlStore(db_path=db)
    actions = sweep_orphan_runs(store, now=NOW)

    assert [(a.run_id, a.action, a.stage) for a in actions] == [(run_id, "scheduled", "grill")]
    status = store.get_run_status(run_id)
    assert status is not None and status["status"] == "active"
    planning = [j for j in status["jobs"] if j["stage"] == "planning"]
    assert [(j["iteration"], j["status"]) for j in planning] == [(0, "pending")]

    after = next(r for r in read_line_live(db, database_url="", now=NOW).runs if r.run_id == run_id)
    assert not (after.stalled_reason or "").startswith("nenhuma etapa agendada")
    assert not after.stalled
    assert [(st.stage, st.status) for st in after.stages if st.stage == "planning"] == [("planning", "pending")]

    assert sweep_orphan_runs(store, now=NOW) == []  # idempotent on the real store too


def test_sweeper_on_the_real_store_parks_a_cancelled_orphan_and_the_panel_shows_attention(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    ids = seed_line_live_demo(db, NOW)
    run_id = ids["queued"]
    old = (NOW - timedelta(hours=3)).isoformat()
    _execute(
        db,
        "UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE run_id = ?",
        (old, old, run_id),
    )

    store = SQLiteControlStore(db_path=db)
    actions = sweep_orphan_runs(store, now=NOW)

    assert [(a.action, a.detail) for a in actions] == [("parked", ORPHAN_NO_SUCCESSOR_CAUSE)]
    status = store.get_run_status(run_id)
    assert status is not None
    parked = [j for j in status["jobs"] if j["status"] == "waiting_human"]
    assert [(j["stage"], j["cause_code"]) for j in parked] == [("planning", ORPHAN_NO_SUCCESSOR_CAUSE)]
    assert store.list_active_run_ids().count(run_id) == 1
    after = next(r for r in read_line_live(db, database_url="", now=NOW).runs if r.run_id == run_id)
    assert after.state == "attention"
    assert sweep_orphan_runs(store, now=NOW) == []


# ------------------------------------------------------------------------ worker scheduling


class _IdleStore:
    claimed = 0

    def claim(self, **_: Any) -> None:
        type(self).claimed += 1
        return None

    def list_waiting_jobs(self, **_: Any) -> list:
        return []

    def get_run_status(self, run_id: str) -> None:
        return None


def test_cloud_worker_runs_the_orphan_sweep_on_its_own_interval() -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    calls: list[datetime] = []

    def sweeper(store: Any, *, now: datetime) -> list[Any]:
        calls.append(now)
        return []

    worker = CloudWorker(
        worker_id="w",
        max_slots=1,
        store=_IdleStore(),  # type: ignore[arg-type]
        capabilities=[],
        orphan_run_sweep_interval_s=300.0,
        orphan_run_sweeper=sweeper,
        workspace_sweeper=lambda **_: [],
    )
    t0 = datetime(2026, 10, 7, tzinfo=timezone.utc)
    worker.poll_and_execute_once(now=t0)
    worker.poll_and_execute_once(now=t0 + timedelta(minutes=2))  # inside the interval
    worker.poll_and_execute_once(now=t0 + timedelta(minutes=6))
    assert calls == [t0, t0 + timedelta(minutes=6)]


def test_cloud_worker_sweep_failure_never_blocks_polling() -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    def sweeper(store: Any, *, now: datetime) -> list[Any]:
        raise RuntimeError("sweeper exploded")

    _IdleStore.claimed = 0
    worker = CloudWorker(
        worker_id="w",
        max_slots=1,
        store=_IdleStore(),  # type: ignore[arg-type]
        capabilities=[],
        orphan_run_sweeper=sweeper,
        workspace_sweeper=lambda **_: [],
    )
    assert worker.poll_and_execute_once(now=datetime(2026, 10, 7, tzinfo=timezone.utc)) is False
    assert _IdleStore.claimed == 1
