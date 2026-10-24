"""USR-355: Postgres budget reset on no-route resume + OA-HR mirror closing for cancelled runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from core.orchestrator.adapters.control_postgres import POSTGRES_DDL, PostgresControlStore
from core.orchestrator.cloud_worker import CloudWorker
from core.workflow.control_contracts import JobKey

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
KEY = JobKey(run_id="r1", ticket_id="T", plan_version="1.0", stage="development", iteration=0)


class _Cur:
    def __init__(self, log: list[tuple[str, Any]], cause: str | None, fail_budget: bool) -> None:
        self.log, self.cause, self.fail_budget, self.rowcount, self._row = log, cause, fail_budget, 1, None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((sql, params))
        if self.fail_budget and "budget_reset_at" in sql:
            raise RuntimeError("column missing")
        self._row = (self.cause,) if "SELECT cause_code" in sql else None

    def fetchone(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, cur: _Cur) -> None:
        self.cur = cur

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def cursor(self) -> _Cur:
        return self.cur

    def commit(self) -> None:
        return None


def _store(cause: str | None, fail_budget: bool = False) -> tuple[PostgresControlStore, list[tuple[str, Any]]]:
    log: list[tuple[str, Any]] = []
    store = PostgresControlStore(mock_mode=True)
    store.mock_mode = False
    store.raw_url = "postgres://x"
    store._psycopg = SimpleNamespace(connect=lambda url: _Conn(_Cur(log, cause, fail_budget)))
    return store, log


def test_ddl_adds_budget_column_idempotently() -> None:
    assert "ALTER TABLE runs ADD COLUMN IF NOT EXISTS budget_reset_at" in POSTGRES_DDL


def test_resume_of_no_route_park_resets_budget() -> None:
    store, log = _store("no_route_available")
    assert store.resume_job(KEY, now=NOW)
    assert any("UPDATE runs SET budget_reset_at" in sql for sql, _ in log)


def test_resume_of_other_park_keeps_budget() -> None:
    store, log = _store("ci_pending")
    assert store.resume_job(KEY, now=NOW)
    assert not any("UPDATE runs SET budget_reset_at" in sql for sql, _ in log)


def test_budget_column_failure_never_breaks_resume_or_lookup() -> None:
    store, _ = _store("no_route_available", fail_budget=True)
    assert store.resume_job(KEY, now=NOW) is True
    store.get_run_budget_started_at("r1")  # degrades to created_at, must not raise


def test_sweep_closes_oa_hr_mirror_of_cancelled_run(monkeypatch: Any) -> None:
    import core.owner_actions.mirror as mirror
    import core.owner_actions.store as oa_store

    title = "Linha parada (infra): run run-aebc0411b972 aguarda voce no estagio 'development'"
    action = SimpleNamespace(id="OA-HR-1", created_by="production-line", title=title)

    class FakeStore:
        def list_actions(self, include_done: bool = True) -> list[Any]:
            return [action]

    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(oa_store, "OwnerActionStore", FakeStore)
    monkeypatch.setattr(mirror, "close_mirrored_request", lambda r, s: closed.append((r, s)))
    worker = object.__new__(CloudWorker)
    worker._store = SimpleNamespace(get_run_status=lambda run_id: {"status": "cancelled", "jobs": []})
    worker._close_terminal_request_mirrors()
    assert closed == [("run-aebc0411b972", "development")]
