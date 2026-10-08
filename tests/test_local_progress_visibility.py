"""USR-164: a local-only sink is never silent, and the restricted writer role can publish.

Hermetic: scratch SQLite files and a fake ``psycopg.connect``; no real database.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line import local_progress
from core.line.local_progress import (
    ENV_SWITCH,
    LocalRunEvent,
    PostgresSink,
    SqliteSink,
    open_progress,
    resolve_sink,
)

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_a_silent_local_sqlite_sink_warns_that_the_hub_will_not_see_the_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    (tmp_path / "control.db").write_bytes(b"")
    warnings: list[str] = []
    progress = open_progress("USR-9", "darkfac", "t", environ={}, warn=warnings.append)
    assert isinstance(progress.sink, SqliteSink)

    for index in range(3):
        progress.phase("agent", "running", f"fase {index}")

    assert len(warnings) == 1
    assert "NAO aparecera" in warnings[0] and "DARKFAC_HF02_DATABASE_URL" in warnings[0]
    assert "DARKFAC_LOCAL_PROGRESS=local" in warnings[0] and "live_progress.md" in warnings[0]
    # it still writes to the local control.db: the warning is about visibility, not about publishing
    with closing(sqlite3.connect(tmp_path / "control.db")) as conn:
        assert conn.execute("SELECT count(*) FROM local_run_events").fetchone() == (3,)


def test_the_local_switch_keeps_the_sqlite_sink_on_purpose_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    (tmp_path / "control.db").write_bytes(b"")
    url = "postgresql://writer:pw@127.0.0.1:5432/control"
    environ = {ENV_SWITCH: "local", "DARKFAC_HF02_DATABASE_URL": url}
    assert isinstance(resolve_sink(environ), SqliteSink)  # "local" ignores the database URLs

    warnings: list[str] = []
    progress = open_progress("USR-9", "darkfac", "t", environ=environ, warn=warnings.append)
    progress.phase("agent", "running")
    assert warnings == [] and progress.warning is None

    # without a local control.db there is nothing to write to: that is a configuration gap, not a choice
    (tmp_path / "control.db").unlink()
    assert resolve_sink(environ) is None
    assert local_progress.missing_sink_reason(environ) is not None


def test_an_injected_sqlite_sink_does_not_warn_about_visibility(tmp_path: Path) -> None:
    warnings: list[str] = []
    progress = open_progress(
        "USR-9", "darkfac", "t", sink=SqliteSink(tmp_path / "x.db"), environ={}, warn=warnings.append
    )
    progress.phase("agent", "running")
    assert warnings == []


class _FakeDb:
    """State shared by the fake psycopg connections: does the table exist, may the role create it?"""

    def __init__(self, *, table_exists: bool, can_create: bool) -> None:
        self.table_exists = table_exists
        self.can_create = can_create
        self.statements: list[str] = []


class _FakeConn:
    def __init__(self, db: _FakeDb) -> None:
        self.db = db

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        import psycopg

        verb = " ".join(str(sql).split()).split(" ", 1)[0].upper()
        self.db.statements.append(verb)
        if verb == "INSERT" and not self.db.table_exists:
            raise psycopg.errors.UndefinedTable('relation "local_run_events" does not exist')
        if verb == "CREATE":
            if not self.db.can_create:
                raise psycopg.errors.InsufficientPrivilege("permission denied for schema public")
            self.db.table_exists = True


def _fake_connect(monkeypatch: pytest.MonkeyPatch, db: _FakeDb) -> None:
    psycopg = pytest.importorskip("psycopg")
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: _FakeConn(db))


def _event(phase: str = "agent") -> LocalRunEvent:
    return LocalRunEvent(
        run_id="local-USR-9-x", ticket_id="USR-9", project_id="darkfac", phase=phase, status="running", at=T0
    )


def test_postgres_sink_inserts_first_so_a_restricted_role_never_needs_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(table_exists=True, can_create=False)  # cannot CREATE: ordinary INSERTs must still work
    _fake_connect(monkeypatch, db)
    sink = PostgresSink("postgresql://restricted@h/db")
    sink.write(_event())
    sink.write(_event("gate"))
    assert db.statements == ["INSERT", "INSERT"]


def test_postgres_sink_creates_the_table_only_when_it_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(table_exists=False, can_create=True)
    _fake_connect(monkeypatch, db)
    sink = PostgresSink("postgresql://owner@h/db")
    sink.write(_event())
    assert db.statements == ["INSERT", "CREATE", "CREATE", "INSERT"]  # table + index, then the retry
    sink.write(_event("gate"))
    assert db.statements[-1] == "INSERT" and db.statements.count("CREATE") == 2


def test_a_restricted_role_with_a_missing_table_points_to_the_provisioning_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg = pytest.importorskip("psycopg")
    db = _FakeDb(table_exists=False, can_create=False)
    _fake_connect(monkeypatch, db)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
        PostgresSink("postgresql://restricted@h/db").write(_event())
    reason = local_progress.describe_publish_failure(caught.value)
    assert reason.startswith("sem permissao de escrita")
    assert "provision_live_writer.py" in reason
