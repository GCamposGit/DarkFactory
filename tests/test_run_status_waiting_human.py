"""A run parked on `waiting_human` must stay open (never `completed`) and legacy rows must heal.

Regression for run-d4d10d4aee94 (canary) / run-4e8bfda4fdaa (USR-62): the grill
stage parked in `waiting_human` and the run was closed as `completed` with
`completed_at` set, although nothing had been delivered.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.workflow.control_contracts import Claim, IntakeCommand, JobKey, RuntimeOwner, StageResult
from core.workflow.control_store import RUN_OPEN_JOB_STATUSES, SQLiteControlStore
from core.workflow.successors import materialize_result

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(
        db_path=tmp_path / "control.db",
        runtime_owner=RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec=45,
    )


def _accept(store: SQLiteControlStore, external_id: str = "ext-wh-1") -> tuple[str, Claim]:
    cmd = IntakeCommand(
        channel="cli",
        external_id=external_id,
        project_id="darkfac",
        payload={
            "title": "T",
            "problem": "p",
            "journey": "j",
            "non_goals": ["n"],
            "criteria": ["c"],
        },
        mode="autonomous",
        policy_ref="policy-v1",
    )
    receipt = store.accept(cmd, NOW)
    assert receipt.run_id is not None
    claim = store.claim("worker-a", ["economy", "coding"], NOW)
    assert claim is not None
    return receipt.run_id, claim


def _run_row(store: SQLiteControlStore, run_id: str) -> tuple[str, Any]:
    conn = store._connect()
    try:
        row = conn.execute("SELECT status, completed_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return row[0], row[1]
    finally:
        conn.close()


def _set_run_status(store: SQLiteControlStore, run_id: str, status: str) -> None:
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
            (status, NOW.isoformat(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_open_statuses_cover_parked_jobs_but_not_superseded_markers() -> None:
    assert {"pending", "running", "waiting_human", "waiting_dependency"} == set(RUN_OPEN_JOB_STATUSES)
    assert not {"succeeded", "failed", "cancelled", "retry", "replan"} & set(RUN_OPEN_JOB_STATUSES)


def test_materialize_waiting_human_keeps_run_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)

    materialize_result(claim.job_key, StageResult(outcome="waiting_human", cause_code="grill_pending"), store, now=NOW)

    status, completed_at = _run_row(store, run_id)
    assert status == "active"
    assert completed_at is None


def test_materialize_waiting_dependency_keeps_run_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)

    materialize_result(claim.job_key, StageResult(outcome="waiting_dependency"), store, now=NOW)

    assert _run_row(store, run_id)[0] == "active"


def test_materialize_failed_job_still_fails_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)
    jk = claim.job_key

    materialize_result(jk, StageResult(outcome="failed", cause_code="boom"), store, now=NOW)
    # A failed stage emits a pending retrospective; the run closes once it finishes.
    assert _run_row(store, run_id)[0] == "active"
    retro = JobKey(run_id=run_id, ticket_id=jk.ticket_id, plan_version=jk.plan_version, stage="retrospective", iteration=0)
    materialize_result(retro, StageResult(outcome="success", output_refs=["ref:x"]), store, now=NOW + timedelta(seconds=5))

    status, completed_at = _run_row(store, run_id)
    assert status == "failed"
    assert completed_at is not None


def test_materialize_last_job_success_still_completes_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)
    jk = claim.job_key
    stages = [
        "grill",
        "planning",
        "development",
        "validation",
        "independent_review",
        "integration",
        "build_deploy",
        "retrospective",
    ]
    for i, stage in enumerate(stages):
        key = JobKey(run_id=run_id, ticket_id=jk.ticket_id, plan_version=jk.plan_version, stage=stage, iteration=0)
        materialize_result(key, StageResult(outcome="success", output_refs=["ref:x"]), store, now=NOW + timedelta(seconds=i))

    status, completed_at = _run_row(store, run_id)
    assert status == "completed"
    assert completed_at is not None


def test_resume_heals_legacy_completed_run_and_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)
    materialize_result(claim.job_key, StageResult(outcome="waiting_human", cause_code="grill_pending"), store, now=NOW)
    _set_run_status(store, run_id, "completed")  # legacy row written by the old code
    assert _run_row(store, run_id)[0] == "completed"

    assert store.resume_job(claim.job_key, NOW + timedelta(minutes=1)) is True
    assert _run_row(store, run_id) == ("active", None)

    # Second resume is a no-op (job already pending) and must not disturb the run.
    assert store.resume_job(claim.job_key, NOW + timedelta(minutes=2)) is False
    assert _run_row(store, run_id) == ("active", None)


def test_resume_does_not_touch_failed_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run_id, claim = _accept(store)
    materialize_result(claim.job_key, StageResult(outcome="waiting_human", cause_code="grill_pending"), store, now=NOW)
    _set_run_status(store, run_id, "failed")

    assert store.resume_job(claim.job_key, NOW + timedelta(minutes=1)) is True
    assert _run_row(store, run_id)[0] == "failed"


def test_finish_success_with_parked_sibling_keeps_run_active(tmp_path: Path) -> None:
    """control_store.finish path: a parked sibling job keeps the run open."""
    store = _store(tmp_path)
    run_id, claim = _accept(store)
    conn = store._connect()
    try:
        conn.row_factory = sqlite3.Row
        src = dict(conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone())
        src.update(stage="parked_sibling", status="waiting_human", current_lease_id=None)
        cols = ", ".join(src)
        marks = ", ".join("?" for _ in src)
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(src.values()))
        conn.commit()
    finally:
        conn.close()

    store.finish(claim, StageResult(outcome="success", output_refs=["ref:x"]), NOW + timedelta(seconds=2))

    assert _run_row(store, run_id) == ("active", None)


# ---------------------------------------------------------------------------
# Postgres branch (psycopg monkeypatched, no network)
# ---------------------------------------------------------------------------


class _FakePg:
    """Records SQL; `open_jobs` answers the step-D COUNT(*) query."""

    def __init__(self, open_jobs: int, rowcount: int = 1) -> None:
        self.open_jobs = open_jobs
        self.rowcount = rowcount
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0

    def connect(self, url: str) -> "_FakeConn":
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pg: _FakePg) -> None:
        self.pg = pg

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor(self.pg)

    def commit(self) -> None:
        self.pg.commits += 1


class _FakeCursor:
    def __init__(self, pg: _FakePg) -> None:
        self.pg = pg
        self._last = ""
        self.rowcount = pg.rowcount

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = sql
        self.pg.executed.append((sql, params))

    def fetchone(self) -> Any:
        if "COUNT(*) FROM jobs" in self._last and "status = ANY" in self._last:
            return (self.pg.open_jobs,)
        if "COUNT(*) FROM jobs" in self._last:
            return (0,)
        if "SELECT run_id FROM runs" in self._last:
            return ("run-pg",)
        if "FOR UPDATE" in self._last:
            return ("running", 1, None)
        return None


def _pg_store(pg: _FakePg) -> PostgresControlStore:
    store = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value)
    store.mock_mode = False
    store._psycopg = pg
    store.raw_url = "postgresql://fake.invalid/db"
    return store


_PG_KEY = JobKey(run_id="run-pg", ticket_id="T-PG", plan_version="1.0", stage="grill", iteration=0)


def test_postgres_materialize_waiting_human_does_not_close_run() -> None:
    pg = _FakePg(open_jobs=1)  # the parked grill job itself counts as open
    materialize_result(_PG_KEY, StageResult(outcome="waiting_human", cause_code="grill_pending"), _pg_store(pg), now=NOW)

    count_sql = [(s, p) for s, p in pg.executed if "COUNT(*) FROM jobs" in s and "status = ANY" in s]
    assert count_sql, "step D must count open jobs"
    assert "waiting_human" in count_sql[0][1][1]
    assert "waiting_dependency" in count_sql[0][1][1]
    assert not any(s.startswith("UPDATE runs SET status") for s, _ in pg.executed)
    assert pg.commits == 1


def test_postgres_materialize_no_open_jobs_completes_run() -> None:
    pg = _FakePg(open_jobs=0)
    materialize_result(_PG_KEY, StageResult(outcome="success", output_refs=["ref:x"]), _pg_store(pg), now=NOW)

    updates = [p for s, p in pg.executed if s.startswith("UPDATE runs SET status")]
    assert updates and updates[0][0] == "completed"


def test_postgres_resume_job_heals_completed_run() -> None:
    pg = _FakePg(open_jobs=1)
    assert _pg_store(pg).resume_job(_PG_KEY, NOW) is True

    heal = [(s, p) for s, p in pg.executed if s.startswith("UPDATE runs SET status = 'active'")]
    assert heal
    assert "status = 'completed'" in heal[0][0]
    assert "completed_at = NULL" in heal[0][0]
    assert heal[0][1][1] == "run-pg"
    assert pg.commits == 1


def test_postgres_resume_job_no_match_does_not_heal() -> None:
    pg = _FakePg(open_jobs=1, rowcount=0)
    assert _pg_store(pg).resume_job(_PG_KEY, NOW) is False
    assert not any(s.startswith("UPDATE runs") for s, _ in pg.executed)
