"""USR-140: run_ticket publishes best-effort phase progress for the live line board.

Everything is hermetic: events go to an in-memory sink or a scratch SQLite file, never to a real
database, and ``run_ticket`` runs with a fake backlog, fake quotas and a fake agent.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.git.autonomy import DeliveryReport, GitAutonomyManager, SweepReport, TicketCompletionReport
from core.git.ticket_workspace import TicketWorkspace
from core.line import local_progress, routing
from core.line.agent_cli import AgentResult
from core.line.local_progress import (
    ENV_RUN_ID,
    ENV_SWITCH,
    LocalRunEvent,
    PostgresSink,
    ProgressPublisher,
    SqliteSink,
    open_progress,
    resolve_sink,
)
from core.line.routing import _HARNESS_TO_PROVIDER
from run_ticket import main

T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


def _postgres_url(host: str, *, sslmode: str | None = None) -> str:
    """Build harmless test connection values without tracking complete URLs or credentials."""
    query = f"?sslmode={sslmode}" if sslmode else ""
    return "postgresql" + "://" + host + ":5432/factory" + query


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[LocalRunEvent] = []

    def write(self, event: LocalRunEvent) -> None:
        self.events.append(event)

    @property
    def steps(self) -> list[tuple[str, str]]:
        return [(event.phase, event.status) for event in self.events]


class BrokenSink:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, event: LocalRunEvent) -> None:
        self.calls += 1
        raise RuntimeError("connection refused")


class SlowSink:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.release = threading.Event()

    def write(self, event: LocalRunEvent) -> None:
        self.release.wait(self.delay_s)


def _publisher(sink: Any, **kwargs: Any) -> ProgressPublisher:
    ticks = iter(range(10_000))
    return ProgressPublisher(
        ticket_id="USR-9",
        project_id="darkfac",
        title="Ticket de teste",
        sink=sink,
        worker="HOST-A",
        clock=lambda: T0 + timedelta(seconds=next(ticks)),
        **kwargs,
    )


# ------------------------------------------------------------------ publisher


def test_phases_are_published_in_order_with_run_identity() -> None:
    sink = RecordingSink()
    progress = _publisher(sink)

    progress.phase("preflight", "running", "roteamento")
    progress.phase("preflight", "succeeded", "rota claude", harness="claude")
    progress.phase("agent", "running", "dev")
    progress.phase("agent", "failed", "timeout", cause="agent_failed")
    progress.finish(False)

    assert sink.steps == [
        ("preflight", "running"),
        ("preflight", "succeeded"),
        ("agent", "running"),
        ("agent", "failed"),
        ("run", "failed"),
    ]
    assert len({event.run_id for event in sink.events}) == 1
    assert sink.events[0].run_id.startswith("local-USR-9-")
    assert [event.at for event in sink.events] == sorted(event.at for event in sink.events)
    # harness is sticky once known; worker and ticket identity are on every event
    assert [event.harness for event in sink.events] == [None, "claude", "claude", "claude", "claude"]
    assert {event.worker for event in sink.events} == {"HOST-A"}
    assert {(event.ticket_id, event.project_id, event.title) for event in sink.events} == {
        ("USR-9", "darkfac", "Ticket de teste")
    }
    assert sink.events[3].cause_code == "agent_failed"


def test_publish_failure_never_raises_and_mutes_after_repeated_failures(caplog: pytest.LogCaptureFixture) -> None:
    sink = BrokenSink()
    progress = _publisher(sink, max_consecutive_failures=2)

    with caplog.at_level(logging.WARNING, logger="darkfac.local_progress"):
        for index in range(5):
            progress.phase("agent", "running", f"tentativa {index}")
        progress.finish(True)

    assert sink.calls == 2  # muted after the second consecutive failure: no more connection attempts
    assert not progress.enabled
    assert len(progress.events) == 6  # still recorded locally for diagnostics
    assert progress.unpublished_phases == 6
    assert any("muted" in record.getMessage() for record in caplog.records)
    assert "connection refused" not in caplog.text  # driver text never reaches the logs


def test_a_success_between_failures_resets_the_failure_streak() -> None:
    state = {"fail": True}
    written: list[str] = []

    class Flaky:
        def write(self, event: LocalRunEvent) -> None:
            if state["fail"]:
                raise OSError("flap")
            written.append(event.phase)

    progress = _publisher(Flaky(), max_consecutive_failures=2)
    progress.phase("preflight", "running")  # fails (1)
    state["fail"] = False
    progress.phase("workspace", "running")  # ok, resets
    state["fail"] = True
    progress.phase("agent", "running")  # fails (1 again)
    state["fail"] = False
    progress.phase("gate", "running")  # still enabled

    assert written == ["workspace", "gate"]
    assert progress.enabled


def test_a_hung_sink_is_bounded_by_the_timeout() -> None:
    sink = SlowSink(delay_s=5.0)
    progress = _publisher(sink, timeout_s=0.05, max_consecutive_failures=1)
    started = time.monotonic()
    try:
        progress.phase("agent", "running")
        progress.phase("agent", "succeeded")  # muted: returns immediately
        elapsed = time.monotonic() - started
    finally:
        sink.release.set()
    assert elapsed < 2.0
    assert not progress.enabled


def test_hold_buffers_until_release_and_discard_publishes_nothing() -> None:
    sink = RecordingSink()
    held = _publisher(sink)
    held.hold()
    held.phase("preflight", "running")
    held.phase("preflight", "succeeded")
    assert sink.events == []
    held.release()
    assert sink.steps == [("preflight", "running"), ("preflight", "succeeded")]
    held.phase("workspace", "running")  # no longer held: goes straight out
    assert sink.steps[-1] == ("workspace", "running")

    dry = RecordingSink()
    dropped = _publisher(dry)
    dropped.hold()
    dropped.phase("preflight", "running")
    dropped.discard()
    dropped.release()
    dropped.phase("workspace", "running")
    dropped.finish(True)
    assert dry.events == []


def test_only_the_run_owner_closes_the_run_and_only_once() -> None:
    sink = RecordingSink()
    owner = _publisher(sink)
    owner.finish(True)
    owner.finish(False)
    assert sink.steps == [("run", "succeeded")]

    joined = RecordingSink()
    child = _publisher(joined, owns_run=False)
    child.phase("gate", "running")
    child.finish(True)
    assert joined.steps == [("gate", "running")]


def test_failed_finish_carries_the_recorded_outcome() -> None:
    sink = RecordingSink()
    progress = _publisher(sink)
    progress.set_outcome("entrega falhou: ci vermelho", "ci_failed")
    progress.set_outcome("ignorado", "outro", only_if_unset=True)
    progress.finish(False)
    last = sink.events[-1]
    assert (last.phase, last.status, last.cause_code, last.message) == (
        "run",
        "failed",
        "ci_failed",
        "entrega falhou: ci vermelho",
    )


def test_messages_are_single_line_and_bounded() -> None:
    sink = RecordingSink()
    progress = _publisher(sink)
    progress.phase("agent", "failed", "linha 1\nlinha 2 private-value\n" + "x" * 2000)
    message = sink.events[0].message
    assert "\n" not in message and "private-value" in message
    assert len(message) <= 400


def test_open_progress_joins_the_parent_run_from_the_environment() -> None:
    sink = RecordingSink()
    fresh = open_progress("USR-9", "darkfac", "t", sink=sink, environ={})
    assert fresh.owns_run and fresh.run_id.startswith("local-USR-9-")

    child = open_progress("USR-9", "darkfac", "t", sink=sink, environ={ENV_RUN_ID: "local-USR-9-parent"})
    assert child.run_id == "local-USR-9-parent" and not child.owns_run
    child.finish(True)
    assert sink.events == []


def test_open_progress_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(environ: object = None) -> None:
        raise RuntimeError("no sink for you")

    monkeypatch.setattr(local_progress, "resolve_sink", boom)
    progress = open_progress("USR-9", "darkfac", "t", environ={})
    assert not progress.enabled
    progress.phase("preflight", "running")  # does not raise


def test_resolve_sink_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    # pytest itself is running: the default environment never publishes
    assert resolve_sink() is None

    url = _postgres_url("control-db")
    assert isinstance(resolve_sink({"DARKFAC_HF02_DATABASE_URL": url}), PostgresSink)
    assert resolve_sink({"DARKFAC_HF02_DATABASE_URL": "mock://"}) is None  # mock URLs are not a database
    assert resolve_sink({"DARKFAC_HF02_DATABASE_URL": url, ENV_SWITCH: "off"}) is None

    # writer-capable URL wins over the Hub's read-only one
    sink = resolve_sink({"DARKHUB_CONTROL_DATABASE_URL": _postgres_url("readonly-db"), "DARKFAC_HF02_DATABASE_URL": url})
    assert isinstance(sink, PostgresSink) and sink.url == url

    # no URL: the local control.db is used only when it already exists
    assert resolve_sink({}) is None
    (tmp_path / "control.db").write_bytes(b"")
    local = resolve_sink({})
    assert isinstance(local, SqliteSink) and local.path == tmp_path / "control.db"


def test_sqlite_sink_appends_ordered_rows_beside_the_existing_tables(tmp_path: Path) -> None:
    import sqlite3
    from contextlib import closing

    db = tmp_path / "control.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE marker (x INTEGER)")
        conn.commit()
    progress = _publisher(SqliteSink(db), run_id="local-USR-9-x")
    progress.phase("preflight", "running", "a")
    progress.phase("preflight", "succeeded", "b", harness="codex")
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT run_id, phase, status, harness, worker FROM local_run_events ORDER BY event_id").fetchall()
    assert rows == [
        ("local-USR-9-x", "preflight", "running", None, "HOST-A"),
        ("local-USR-9-x", "preflight", "succeeded", "codex", "HOST-A"),
    ]


# ------------------------------------------------------------------ USR-455 T2: PostgresSink budgets


class _FakeUndefinedTable(Exception):
    pass


class _FakeErrorsNamespace:
    UndefinedTable = _FakeUndefinedTable


class _FakeConn:
    """One fake connection; ``outcomes`` is consumed one entry per ``INSERT`` (``None`` succeeds)."""

    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self._outcomes = list(outcomes)
        self.closed = False
        self.insert_count = 0
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        if not normalized.startswith(f"INSERT INTO {local_progress.TABLE}"):
            return  # DDL always succeeds
        self.insert_count += 1
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome

    def close(self) -> None:
        self.closed = True


class _FakePsycopg:
    """Records every ``connect`` call; ``connections`` gives each new connection's scripted outcomes."""

    def __init__(self, connections: list[list[BaseException | None]]) -> None:
        self.errors = _FakeErrorsNamespace()
        self._scripts = list(connections)
        self.made: list[_FakeConn] = []
        self.connect_calls = 0
        self.urls: list[str] = []
        self.last_kwargs: dict[str, Any] = {}

    def connect(self, url: str, **kwargs: Any) -> _FakeConn:
        self.connect_calls += 1
        self.urls.append(url)
        self.last_kwargs = kwargs
        outcomes = self._scripts.pop(0) if self._scripts else []
        conn = _FakeConn(outcomes)
        self.made.append(conn)
        return conn


def _pg_event(phase: str = "preflight", status: str = "running") -> LocalRunEvent:
    return LocalRunEvent(run_id="local-USR-9-x", ticket_id="USR-9", project_id="darkfac", phase=phase, status=status, at=T0)


def test_postgres_sink_reuses_one_connection_across_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[None, None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))

    sink.write(_pg_event("preflight", "running"))
    sink.write(_pg_event("preflight", "succeeded"))

    assert fake.connect_calls == 1
    assert fake.made[0].insert_count == 2


def test_postgres_sink_bootstraps_the_table_without_reconnecting(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[_FakeUndefinedTable("relation does not exist"), None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))

    sink.write(_pg_event())

    assert fake.connect_calls == 1  # UndefinedTable is handled by DDL, not a connection-level failure
    assert fake.made[0].closed is False
    assert any(s.startswith("CREATE TABLE") for s in fake.made[0].statements)


def test_postgres_sink_reconnects_once_and_retries_after_a_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakePsycopg(connections=[[OSError("server closed the connection unexpectedly")], [None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))

    sink.write(_pg_event())  # does not raise: the retry on the fresh connection succeeds

    assert fake.connect_calls == 2
    assert fake.made[0].closed is True
    assert fake.made[1].insert_count == 1


def test_postgres_sink_propagates_when_the_retry_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[OSError("down")], [OSError("still down")]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))

    with pytest.raises(OSError, match="still down"):
        sink.write(_pg_event())

    assert fake.connect_calls == 2
    assert fake.made[0].closed is True and fake.made[1].closed is True
    assert sink._conn is None  # a failed retry must not leave a dead connection for the next phase


def test_postgres_sink_retry_count_is_configurable_but_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[OSError("down")], [OSError("still down")], [OSError("still down again")]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"), retries=99)

    with pytest.raises(OSError, match="still down again"):
        sink.write(_pg_event())

    assert sink.retries == local_progress.MAX_SINK_RETRIES
    assert fake.connect_calls == local_progress.MAX_SINK_RETRIES + 1


def test_postgres_sink_uses_the_control_store_connection_options(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))

    sink.write(_pg_event())

    assert fake.last_kwargs == {
        "autocommit": True,
        "connect_timeout": 5,
        "options": "-c statement_timeout=5000",
    }


def test_control_store_and_progress_sink_use_the_same_resolved_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    url = _postgres_url("control-db", sslmode="require")
    resolved = local_progress.resolve_control_database({"DARKFAC_HF02_DATABASE_URL": url})
    assert resolved is not None

    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = object.__new__(PostgresControlStore)
    store._psycopg = fake
    store._resolved_database = resolved
    sink = PostgresSink(resolved)

    store._connect()
    control_kwargs = dict(fake.last_kwargs)
    sink._connect()

    assert fake.connect_calls == 2
    assert {key: value for key, value in fake.last_kwargs.items() if key != "autocommit"} == control_kwargs


def test_a_sink_retry_success_is_transparent_to_the_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[OSError("flap")], [None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))
    progress = _publisher(sink, max_consecutive_failures=1)

    progress.phase("preflight", "running")

    assert progress.failures == 0
    assert progress.enabled
    assert progress.publish_status()["published"] is True
    assert progress.publish_status()["warning"] is None
    assert "failure" not in progress.publish_status()


def test_available_progress_sink_prefers_its_explicit_target_and_publishes_without_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_url = _postgres_url("control-db")
    sink_url = _postgres_url("line-db")
    sink, source = local_progress.resolve_sink_source({
        "DARKFAC_HF02_DATABASE_URL": control_url,
        "DARKHUB_LINE_DATABASE_URL": sink_url,
    })
    assert isinstance(sink, PostgresSink)
    assert source == "DARKHUB_LINE_DATABASE_URL"
    assert sink.url == sink_url

    fake = _FakePsycopg(connections=[[None, None]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    warnings: list[str] = []
    progress = _publisher(sink, warn=warnings.append)
    progress.phase("preflight", "succeeded", "rota")
    progress.phase("workspace", "succeeded", "worktree")
    progress.finish(True)

    status = progress.publish_status()
    assert status["published"] is True and status["warning"] is None
    assert fake.urls == [sink_url]
    assert fake.last_kwargs == {
        "autocommit": True,
        "connect_timeout": 10,
        "options": "-c statement_timeout=10000",
    }
    assert warnings == []


def test_a_fully_failed_write_counts_as_exactly_one_publisher_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakePsycopg(connections=[[OSError("down")], [OSError("still down")]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))
    progress = _publisher(sink, max_consecutive_failures=2)

    progress.phase("preflight", "running")

    assert progress.failures == 1
    assert progress.enabled  # not muted yet: needs a second consecutive failure


def test_progress_sink_prefers_its_own_url_with_bounded_defaults() -> None:
    control_url = _postgres_url("control-db")
    sink_url = _postgres_url("line-db")
    sink = resolve_sink(
        {
            "DARKFAC_HF02_DATABASE_URL": control_url,
            "DARKHUB_LINE_DATABASE_URL": sink_url,
        }
    )
    assert isinstance(sink, PostgresSink)
    assert sink.url == sink_url
    assert sink.connection.psycopg_kwargs() == {
        "connect_timeout": 10,
        "options": "-c statement_timeout=10000",
    }
    assert sink.write_timeout_s == 30.0


def test_progress_sink_timeouts_are_overridable_and_fall_back_on_invalid_values() -> None:
    url = _postgres_url("line-db")
    sink = resolve_sink(
        {
            "DARKFAC_HF02_DATABASE_URL": url,
            "DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S": "15",
            "DARKFAC_LOCAL_PROGRESS_STATEMENT_TIMEOUT_S": "not-a-number",
            "DARKFAC_LOCAL_PROGRESS_WRITE_TIMEOUT_S": "42.5",
            "DARKFAC_LOCAL_PROGRESS_RETRIES": "2",
        }
    )
    assert isinstance(sink, PostgresSink)
    assert sink.connection.psycopg_kwargs() == {
        "connect_timeout": 15,
        "options": "-c statement_timeout=10000",  # invalid override: falls back to the default
    }
    assert sink.write_timeout_s == 42.5
    assert sink.retries == 2


def test_progress_sink_timeout_and_retry_overrides_have_hard_upper_bounds() -> None:
    sink = resolve_sink(
        {
            "DARKHUB_LINE_DATABASE_URL": _postgres_url("line-db"),
            "DARKFAC_LOCAL_PROGRESS_CONNECT_TIMEOUT_S": "9999",
            "DARKFAC_LOCAL_PROGRESS_STATEMENT_TIMEOUT_S": "inf",
            "DARKFAC_LOCAL_PROGRESS_WRITE_TIMEOUT_S": "9999",
            "DARKFAC_LOCAL_PROGRESS_RETRIES": "999",
        }
    )

    assert isinstance(sink, PostgresSink)
    assert sink.connection.psycopg_kwargs() == {
        "connect_timeout": int(local_progress.MAX_SINK_CONNECT_TIMEOUT_S),
        "options": f"-c statement_timeout={int(local_progress.DEFAULT_SINK_STATEMENT_TIMEOUT_S * 1000)}",
    }
    assert sink.write_timeout_s == local_progress.MAX_WRITE_TIMEOUT_S
    assert sink.retries == local_progress.MAX_SINK_RETRIES


def test_open_progress_uses_the_resolved_control_store_write_timeout() -> None:
    sink = RecordingSink()
    progress = open_progress(
        "USR-9", "darkfac", "t", sink=sink,
        environ={"DARKFAC_LOCAL_PROGRESS_WRITE_TIMEOUT_S": "42.5"},
    )
    assert progress.timeout_s == local_progress.DEFAULT_WRITE_TIMEOUT_S


def test_progress_publisher_default_timeout_matches_the_module_constant() -> None:
    publisher = ProgressPublisher(ticket_id="USR-9", sink=None)
    assert publisher.timeout_s == local_progress.DEFAULT_WRITE_TIMEOUT_S


# ------------------------------------------------------------------ run_ticket wiring

_TICKET = run_ticket.UserTicket(
    id="USR-99", project_id="darkfac", title="Ticket de teste", problem_statement="problema de teste"
)


def _quotas(**overrides: float) -> dict[str, dict[str, Any]]:
    quotas: dict[str, dict[str, Any]] = {}
    for harness, provider in _HARNESS_TO_PROVIDER.items():
        headroom = overrides.get(harness, 60.0)
        quotas[harness] = {
            "provider": provider, "headroom": headroom, "is_critical": headroom <= 15.0,
            "status": "SAUDAVEL" if headroom > 15.0 else "CRITICO",
        }
    return quotas


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str) -> Any:
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self) -> list[Any]:
        return [_TICKET]

    parked: list[tuple[Any, ...]] = []
    requeued: list[tuple[Any, ...]] = []
    requeued_status: Any = None

    def update_status(self, ticket_id: str, status: Any, notes: Any = None, delivery_evidence: Any = None) -> None:
        _Store.parked.append((ticket_id, status, delivery_evidence))

    def requeue_ticket(self, ticket_id: str, reason: str = "", *, reopen: bool = False) -> tuple[Any, bool]:
        _Store.requeued_status = run_ticket.DeliveryStatus.PLANNED
        _Store.requeued.append((ticket_id, reason, reopen))
        return _TICKET.model_copy(update={"status": _Store.requeued_status}), True


class _Launcher:
    def __init__(self, sink: Any, agent: MagicMock, workspace: TicketWorkspace) -> None:
        self.sink = sink
        self.agent = agent
        self.workspace = workspace


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[Any], _Launcher]:
    """Hermetic launcher; ``launcher(sink)`` wires every publisher of the run to ``sink``."""
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas())
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=lambda _s: None))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", lambda cwd, **_k: (True, [], "already up to date"))
    monkeypatch.setattr(GitAutonomyManager, "sweep_stale", lambda self, *a, **k: SweepReport())
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["feature.py"])
    workspace_dir = tmp_path / "ticket-workspace"
    workspace_dir.mkdir()
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id, path=workspace_dir, branch="ticket/usr-99", main_root=tmp_path,
        base_ref="origin/main", base_sha="0" * 40,
    )
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda args, ticket: workspace)
    agent = MagicMock(name="run_agent")
    agent.side_effect = lambda req: AgentResult(
        ok=True, text="done", harness=req.harness, model=req.model, duration_s=1.0, exit_code=0
    )
    monkeypatch.setattr(run_ticket, "run_agent", agent)

    def wire(sink: Any) -> _Launcher:
        def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
            return ProgressPublisher(
                ticket_id=ticket_id, project_id=project_id, title=title, sink=sink, worker="HOST-A"
            )

        monkeypatch.setattr(run_ticket, "open_progress", make)
        return _Launcher(sink, agent, workspace)

    return wire


def _delivery_ok(monkeypatch: pytest.MonkeyPatch, phases: list[tuple[str, str]] | None = None) -> None:
    def complete(self: GitAutonomyManager, **kwargs: Any) -> TicketCompletionReport:
        for phase, status in phases or []:
            self._phase(phase, status, f"{phase} {status}")
        return TicketCompletionReport(ok=True, ticket_id=kwargs["ticket_id"], commit_sha="c0ffee")

    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", complete)


def test_a_delivered_run_publishes_every_phase_in_order(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    launcher(sink)
    _delivery_ok(
        monkeypatch,
        [
            ("commit", "running"), ("commit", "succeeded"),
            ("pr", "running"), ("pr", "succeeded"),
            ("ci", "running"), ("ci", "succeeded"),
            ("merge", "running"), ("merge", "succeeded"),
        ],
    )

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    assert sink.steps == [
        ("preflight", "running"), ("preflight", "succeeded"),
        ("workspace", "running"), ("workspace", "succeeded"),
        ("agent", "running"), ("agent", "succeeded"),
        ("gate", "running"), ("gate", "skipped"),
        ("commit", "running"), ("commit", "succeeded"),
        ("pr", "running"), ("pr", "succeeded"),
        ("ci", "running"), ("ci", "succeeded"),
        ("merge", "running"), ("merge", "succeeded"),
        ("deploy", "skipped"),
        ("run", "succeeded"),
    ]
    assert len({event.run_id for event in sink.events}) == 1
    assert {event.ticket_id for event in sink.events} == {"USR-99"}
    by_phase = {event.phase: event for event in sink.events if event.status == "succeeded"}
    assert by_phase["agent"].harness == "codex"
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_delivered_run_reports_publication_as_a_one_line_esteira_status(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """USR-455 T3: the operator must see publicado/mudo in the launcher's own output, never only a log."""
    sink = RecordingSink()
    launcher(sink)
    _delivery_ok(monkeypatch)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    captured = capsys.readouterr()
    assert "[ESTEIRA] progresso publicado no Hub" in captured.err
    payload = json.loads(captured.out)
    assert payload["live_progress"] == {
        "published": True, "warning": None, "cause_code": None, "unpublished_phases": 0,
    }


def test_an_unconfigured_sink_reports_a_muted_esteira_status_not_just_a_log(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Even with zero failed writes (nothing was ever attempted), the operator sees 'mudo', not silence."""
    launcher(None)
    _delivery_ok(monkeypatch)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    captured = capsys.readouterr()
    assert "[ESTEIRA] progresso mudo" in captured.err
    payload = json.loads(captured.out)
    assert payload["live_progress"]["published"] is False


def test_an_insufficient_privilege_failure_reports_muted_with_the_short_reason_only_once(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one-line status must not repeat the already-printed [AVISO] Esteira ao vivo sentence."""
    sink = _RaisingSink(PermissionError("permission denied for table local_run_events"))

    def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
        return ProgressPublisher(
            ticket_id=ticket_id, project_id=project_id, title=title, sink=sink, worker="HOST-A",
            warn=local_progress.stderr_warning,
        )

    monkeypatch.setattr(run_ticket, "open_progress", make)
    _delivery_ok(monkeypatch)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    captured = capsys.readouterr()
    assert captured.err.count("[AVISO] Esteira ao vivo") == 1
    assert "[ESTEIRA] progresso mudo - sem permissao de escrita" in captured.err
    assert "hunter2" not in captured.err + captured.out


def test_an_agent_failure_closes_the_run_as_failed_on_the_agent_phase(
    launcher: Callable[[Any], _Launcher], capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    env = launcher(sink)
    # Not "timeout": USR-256 gave a timeout with real changes in the worktree its own path (advance
    # straight to the gate instead of failing, with its own cause code), covered on its own in
    # tests/test_run_ticket_routing.py. This test is about the plain agent-failure phase transitions.
    env.agent.side_effect = lambda req: AgentResult(
        ok=False, harness=req.harness, error_kind="crash", exit_code=None, stderr_tail="", duration_s=9.0, text=""
    )

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 1

    assert sink.steps[-2:] == [("agent", "failed"), ("run", "failed")]
    assert sink.events[-2].cause_code == "agent_failed"
    assert "gate" not in {event.phase for event in sink.events}


def test_a_dry_run_publishes_nothing(launcher: Callable[[Any], _Launcher]) -> None:
    sink = RecordingSink()
    launcher(sink)
    assert main(["USR-99", "--harness", "codex", "--dry-run", "--json"]) == 0
    assert sink.events == []


def test_a_refused_critical_quota_is_published_as_a_failed_preflight(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = RecordingSink()
    launcher(sink)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas(codex=5.0))

    assert main(["USR-99", "--harness", "codex", "--json"]) == 75

    assert sink.steps == [("preflight", "running"), ("preflight", "failed"), ("run", "failed")]
    assert sink.events[1].cause_code == "quota_critical"


@pytest.mark.parametrize("sink_factory", [BrokenSink, lambda: SlowSink(30.0)], ids=["raising", "hanging"])
def test_publication_failure_never_changes_the_run_result(
    launcher: Callable[[Any], _Launcher],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sink_factory: Callable[[], Any],
) -> None:
    sink = sink_factory()
    launcher(sink)
    _delivery_ok(monkeypatch)
    # keep the hanging case fast: every write is abandoned after a few milliseconds
    original = local_progress.ProgressPublisher.__init__

    def quick(self: ProgressPublisher, *args: Any, **kwargs: Any) -> None:
        kwargs["timeout_s"] = 0.05
        original(self, *args, **kwargs)

    monkeypatch.setattr(local_progress.ProgressPublisher, "__init__", quick)
    try:
        code = main(["USR-99", "--harness", "codex", "--skip-validation", "--json"])
    finally:
        if isinstance(sink, SlowSink):
            sink.release.set()

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_a_failed_delivery_still_fails_the_run_and_records_why(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    launcher(sink)

    def blocked(self: GitAutonomyManager, **kwargs: Any) -> TicketCompletionReport:
        self._phase("ci", "running", "aguardando")
        self._phase("ci", "failed", "ci vermelho", "failed")
        return TicketCompletionReport(ok=False, ticket_id=kwargs["ticket_id"], message="CI failed on build")

    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", blocked)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 1

    assert sink.steps[-3:] == [("ci", "running"), ("ci", "failed"), ("run", "failed")]
    assert sink.events[-1].cause_code == "delivery_failed"
    assert "CI failed on build" in sink.events[-1].message
    capsys.readouterr()


def _red(ticket_id: str, log: str) -> TicketCompletionReport:
    return TicketCompletionReport(
        ok=False, ticket_id=ticket_id, message="ci",
        delivery=DeliveryReport(ok=False, action="ci_failed", message=f"CI failed on build\n{log}"),
    )


def test_red_ci_goes_back_to_the_agent_once_then_revalidates(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = launcher(RecordingSink())
    fixes: list[str] = []
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda wt, t, stage, fb: fixes.append(f"{stage}|{fb}"))
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *a: (True, None))
    calls = iter([_red("USR-99", "FAILED tests/test_x.py::t\nnoise line"), TicketCompletionReport(ok=True, ticket_id="USR-99", commit_sha="c0ffee")])
    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", lambda self, **k: next(calls))

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 75
    assert len(fixes) == 1 and fixes[0].startswith("ci-vermelho|") and "FAILED tests/test_x.py" in fixes[0]
    # The second complete_ticket result remains unused: no push/merge of an unreviewed fix.
    assert "noise line" not in fixes[0]
    capsys.readouterr()
    phases: list[str] = []
    original_review = run_ticket._review_candidate
    monkeypatch.setattr(run_ticket, "_candidate_binding", lambda *a, **k: ("corrected-candidate", "policy"))
    monkeypatch.setattr(run_ticket, "_run_gate_with_self_correction", lambda *a, **k: phases.append("gate"))
    def reviewed(*args, **kwargs):
        phases.append("review")
        return original_review(*args, **kwargs)
    monkeypatch.setattr(run_ticket, "_review_candidate", reviewed)
    def green_delivery(self, **kwargs):
        phases.append("merge")
        return next(calls)
    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", green_delivery)
    before = env.agent.call_count
    assert main(["--resume-delivery", "USR-99", "--worktree", str(env.workspace.path), "--json"]) == 0
    assert phases == ["gate", "review", "merge"]
    assert env.agent.call_count == before  # no second BUILD or repair
    record = run_ticket.run_outcome.ci_recovery_path(_TICKET.id)
    assert not record.exists()  # merge clears the durable repair allowance
    capsys.readouterr()


def test_ci_failed_pr_births_recovery_wait_and_the_next_tick_resumes(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """A red PR opens a CI-recovery wait; the due tick resumes delivery on a completed ledger."""
    from types import SimpleNamespace

    from core.line import pending_delivery as pd
    from core.line.ticket_engine import artifacts_root
    from core.paths import state_root

    launcher(RecordingSink())
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda *_a, **_k: "fixed")
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(
        GitAutonomyManager, "complete_ticket",
        lambda _self, **kwargs: _red(kwargs["ticket_id"], "FAILED tests/test_x.py::t"),
    )

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 75
    capsys.readouterr()

    wait_path = state_root() / "pending_delivery" / "USR-99.json"
    row = pd.PendingDelivery.model_validate_json(wait_path.read_text(encoding="utf-8"))
    assert row.cause == "ci_fix_requires_revalidation"
    assert row.status == "waiting"
    assert row.resume_delivery is True
    context = artifacts_root() / "USR-99" / ".darkfac" / "runs" / "USR-99"
    assert not (context / "merge.json").exists()
    assert not (context / "deploy_report.json").exists()

    monkeypatch.setattr(pd, "_children", {})
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    monkeypatch.setattr(
        "core.demands.queue_store.control_store",
        lambda: SimpleNamespace(
            mock_mode=False, list_tickets=lambda: [{"id": "USR-99", "status": "completed"}],
        ),
    )
    monkeypatch.setattr(
        "core.git.ticket_workspace.list_worktrees",
        lambda _root: [{"worktree": row.worktree}],
    )
    monkeypatch.setattr(pd, "_route", lambda _row: ("grok", "grok"))
    monkeypatch.setattr(pd.run_outcome, "main_sha", lambda _repo: "")
    calls: list[list[str]] = []
    handle = SimpleNamespace(pid=424242, poll=lambda: None)
    monkeypatch.setattr(pd.subprocess, "Popen", lambda cmd, **_kw: calls.append(list(cmd)) or handle)

    early = pd.tick(tmp_path, directory=wait_path.parent, now=row.next_check - 1)
    assert early[0]["status"] == "waiting"
    assert calls == []
    held = pd.PendingDelivery.model_validate_json(wait_path.read_text(encoding="utf-8"))
    assert held.status == "waiting"
    assert held.cause == "ci_fix_requires_revalidation"

    result = pd.tick(tmp_path, directory=wait_path.parent, now=row.next_check + 1)
    assert result[0]["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", "USR-99", "--worktree", row.worktree]
    saved = pd.PendingDelivery.model_validate_json(wait_path.read_text(encoding="utf-8"))
    assert saved.status == "running"
    assert saved.cause == "ci_fix_requires_revalidation"


def test_red_ci_twice_requeues_with_cause_and_log(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    env = launcher(sink)
    _Store.requeued.clear()
    _Store.requeued_status = None
    fixes: list[str] = []
    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", lambda wt, t, stage, fb: fixes.append(fb))
    monkeypatch.setattr(run_ticket, "_commit_fix_agent_changes_safely", lambda *a: (True, None))
    monkeypatch.setattr(GitAutonomyManager, "complete_ticket", lambda self, **k: _red("USR-99", "FAILED again"))
    comments: list[list[str]] = []
    def gh_run(command, **kwargs):
        if command[:3] == ["git", "branch", "--show-current"]:
            return SimpleNamespace(returncode=0, stdout="ticket/usr-99\n", stderr="")
        comments.append(list(command))
        return SimpleNamespace(returncode=1, stdout="", stderr="gh unavailable")

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 75
    record = run_ticket.run_outcome.ci_recovery_path(_TICKET.id)
    assert json.loads(record.read_text(encoding="utf-8")) == {"attempts": 1}
    _, exhausted = run_ticket._retry_red_ci(
        GitAutonomyManager(env.workspace.main_root), _TICKET, env.workspace.path,
        _red("USR-99", "FAILED again"), False, _publisher(sink),
    )
    assert exhausted.startswith("ci_recovery_budget_exhausted")
    assert json.loads(record.read_text(encoding="utf-8")) == {"attempts": 1, "exhausted": True}
    monkeypatch.setattr(run_ticket.subprocess, "run", gh_run)
    run_ticket._ci_red_twice_result(_TICKET, exhausted, env.workspace.path, env.workspace.branch,
                                   "resume", _publisher(sink), True, True)
    assert _Store.requeued[-1] == ("USR-99", "ci_recovery_budget_exhausted", True)
    assert _Store.requeued_status == run_ticket.DeliveryStatus.PLANNED
    from core.line.admission_store import AdmissionStore
    from core.paths import state_root
    assert AdmissionStore(state_root() / "admission.sqlite3").budget("darkfac", "USR-99")["candidate_failures"] == 1
    assert len(fixes) == 1
    assert sink.events[-1].cause_code == "ci_recovery_budget_exhausted"
    assert "FAILED again" in sink.events[-1].message
    capsys.readouterr()


# ------------------------------------------------------------------ USR-154: one warning when it cannot publish

class _RaisingSink:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def write(self, event: LocalRunEvent) -> None:
        self.calls += 1
        raise self.exc


def test_driver_exception_url_and_sensitive_parameters_never_reach_warning_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_url = (
        "postgresql://writer:secret-pass@private-db:5432/factory"
        "?token=secret-token&sslpassword=secret-ssl&api_key=secret-key"
    )
    exc = _NamedConnectionTimeout(f"Connection timed out while opening {secret_url}")
    progress = _publisher(_RaisingSink(exc), warn=local_progress.stderr_warning)

    with caplog.at_level(logging.WARNING, logger="darkfac.local_progress"):
        progress.phase("preflight", "running")

    output = caplog.text + json.dumps(progress.publish_status()) + (progress.warning or "")
    for sensitive in (
        secret_url,
        "secret-pass",
        "secret-token",
        "secret-ssl",
        "secret-key",
        "private-db",
    ):
        assert sensitive not in output
    assert "ConnectionTimeout" in output
    assert "cause=tcp_timeout" in caplog.text


def test_no_database_url_warns_once_with_the_variable_names_and_never_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))  # no control.db here: nothing to fall back to
    warnings: list[str] = []
    progress = open_progress("USR-9", "darkfac", "t", environ={}, warn=warnings.append)
    assert progress.sink is None

    for index in range(4):
        progress.phase("agent", "running", f"fase {index}")
    progress.finish(True)

    assert len(warnings) == 1
    assert "DARKFAC_HF02_DATABASE_URL" in warnings[0]
    assert "live_progress.md" in warnings[0]
    assert progress.warning == warnings[0]


def test_unconfigured_warning_is_off_for_the_explicit_switch_pytest_and_a_parent_that_warned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    warnings: list[str] = []
    off = open_progress("USR-9", "darkfac", "t", environ={ENV_SWITCH: "off"}, warn=warnings.append)
    off.phase("agent", "running")
    assert warnings == [] and off.warning is None

    # default channel under pytest (default environment): silent, so existing suites never see it
    default = open_progress("USR-9", "darkfac", "t")
    default.phase("agent", "running")
    assert default.warning is None

    # a delivery subprocess whose parent already warned stays quiet
    child = open_progress("USR-9", "darkfac", "t", environ={local_progress.ENV_WARNED: "1"})
    child.phase("agent", "running")
    assert child.warning is None


def test_a_dry_run_never_warns_about_missing_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    warnings: list[str] = []
    progress = open_progress("USR-9", "darkfac", "t", environ={}, warn=warnings.append)
    progress.hold()
    progress.phase("preflight", "running")
    progress.discard()
    progress.release()
    assert warnings == []


@pytest.mark.parametrize(
    ("exc", "expected", "expected_cause"),
    [
        (PermissionError("permission denied for schema public"), "sem permissao de escrita", "read_only"),
        (RuntimeError("attempt to write a readonly database"), "sem permissao de escrita", "read_only"),
        (TimeoutError("progress write exceeded 5s"), "inalcancavel", "tcp_timeout"),
        (OSError("could not translate host name 'internal-db' to address"), "nao resolveu via DNS", "dns_error"),
        (ConnectionRefusedError("could not connect to server"), "inalcancavel", "connection_refused"),
        (ModuleNotFoundError("No module named 'psycopg'"), "psycopg", "missing_driver"),
        (ValueError("weird endpoint metadata"), "falha ao gravar (ValueError)", "unknown"),
    ],
    ids=["permission", "readonly", "timeout", "dns", "refused", "driver", "other"],
)
def test_a_failing_sink_warns_once_at_the_first_failure_with_a_sanitized_reason(
    exc: BaseException, expected: str, expected_cause: str
) -> None:
    """USR-455 T2: the single warning of a run carries a structured ``cause_code``, not just free text."""
    warnings: list[str] = []
    progress = _publisher(_RaisingSink(exc), warn=warnings.append)

    progress.phase("preflight", "running")
    progress.phase("agent", "running")
    progress.phase("gate", "running")
    progress.finish(True)

    assert len(warnings) == 1
    assert expected in warnings[0]
    assert str(exc) not in warnings[0]
    assert not progress.enabled  # muted after repeated failures, run unaffected
    assert progress.warning_cause_code == expected_cause
    assert '"event":"live_progress_degraded"' in warnings[0]
    assert '"unpublished_phases":4' in warnings[0]
    assert progress.publish_status()["unpublished_phases"] == 4
    structured_failure = progress.publish_status()["failure"]
    assert structured_failure == {
        "phase": "preflight",
        "category": expected_cause,
        "message": progress.warning_reason,
    }
    from core.line.ticket_stages import publish_status

    launcher_status = publish_status(progress)
    assert launcher_status["failure"] == structured_failure
    assert str(exc) not in json.dumps(launcher_status)
    assert local_progress.classify_connection_cause(exc) == expected_cause


_PSYCOPG_INSTALL_CMD = 'python -m pip install "psycopg[binary]>=3.2.0,<3.3.0"'


class _NamedConnectionTimeout(OSError):
    """Stands in for psycopg's ``ConnectionTimeout``: the classifier keys off the type name."""


_NamedConnectionTimeout.__name__ = "ConnectionTimeout"


def test_control_store_and_progress_sink_keep_separate_targets_and_timeouts_safely(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """USR-455 T1: the former 3s sink deadline fails where the 5s store succeeds; the configured sink now publishes."""
    from core.line import live_progress_doctor
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    control_url = _postgres_url("control-db")
    sink_url = _postgres_url("line-db")
    environ = {
        "DARKFAC_HF02_DATABASE_URL": control_url,
        "DARKHUB_LINE_DATABASE_URL": sink_url,
    }

    class SplitReachabilityPsycopg:
        errors = _FakeErrorsNamespace()

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def connect(self, url: str, **kwargs: Any) -> _FakeConn:
            self.calls.append((url, kwargs))
            # This network path needs 4 seconds to connect. Control store allows 5s; the old sink allowed 3s.
            if kwargs.get("connect_timeout", 0) < 4:
                raise _NamedConnectionTimeout("connect timed out")
            return _FakeConn([])

    fake = SplitReachabilityPsycopg()
    monkeypatch.setitem(sys.modules, "psycopg", fake)

    diagnosis = live_progress_doctor.diagnose(environ)
    assert diagnosis.control_store.source == "DARKFAC_HF02_DATABASE_URL"
    assert diagnosis.sink.source == "DARKHUB_LINE_DATABASE_URL"
    assert diagnosis.control_store.host == "control-db"
    assert diagnosis.sink.host == "line-db"
    assert diagnosis.control_store.credential_present is False
    assert diagnosis.sink.credential_present is False

    resolved_control = local_progress.resolve_control_database(environ)
    assert resolved_control is not None
    control_store = object.__new__(PostgresControlStore)
    control_store._psycopg = fake
    control_store._resolved_database = resolved_control
    # A representative reservation read succeeds at the control-store destination.
    control_connection = control_store._connect()
    control_connection.execute("SELECT reservation_id FROM fleet_reservations")
    control_connection.close()
    # Recreate the former sink connect budget against that same network path.
    with pytest.raises(_NamedConnectionTimeout):
        fake.connect(control_url, connect_timeout=3)
    main_result = {"ok": True, "reservation_acquired": True}

    sink, source = local_progress.resolve_sink_source(environ)
    assert isinstance(sink, PostgresSink)
    assert source == "DARKHUB_LINE_DATABASE_URL"
    warnings: list[str] = []
    progress = _publisher(sink, warn=warnings.append, max_consecutive_failures=1)
    with caplog.at_level(logging.WARNING, logger="darkfac.local_progress"):
        progress.phase("preflight", "succeeded")
        progress.phase("workspace", "succeeded")
        progress.finish(True)

    assert main_result == {"ok": True, "reservation_acquired": True}
    assert progress.publish_status()["published"] is True
    assert progress.publish_status()["warning"] is None
    assert fake.calls[0][0] == control_url
    assert fake.calls[0][1]["connect_timeout"] == 5
    assert fake.calls[1] == (control_url, {"connect_timeout": 3})
    assert fake.calls[2][0] == sink_url
    assert fake.calls[2][1]["connect_timeout"] == 10
    assert warnings == []

    structured = json.dumps(diagnosis.model_dump(mode="json"), ensure_ascii=False)
    public_output = structured + caplog.text + json.dumps(progress.publish_status())
    assert control_url not in public_output and sink_url not in public_output
    assert "control-db" in structured and "line-db" in structured
    assert "password" not in public_output.lower() and "token" not in public_output.lower()


def test_driver_exception_with_secret_dsn_is_absent_from_progress_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret_url = (
        "postgresql://writer:secret-pass@private-db:5432/factory"
        "?token=secret-token&sslpassword=secret-ssl&api_key=secret-key"
    )
    driver_error = _NamedConnectionTimeout(f"connection timed out for {secret_url}")
    fake = _FakePsycopg(connections=[[driver_error], [driver_error]])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    warnings: list[str] = []
    progress = _publisher(PostgresSink(secret_url), warn=warnings.append, max_consecutive_failures=1)

    with caplog.at_level(logging.WARNING, logger="darkfac.local_progress"):
        progress.phase("preflight", "running")
        progress.finish(False)

    public_output = "\n".join(warnings) + caplog.text + json.dumps(progress.publish_status())
    assert warnings and progress.publish_status()["cause_code"] == "tcp_timeout"
    for sensitive in (secret_url, "writer", "secret-pass", "secret-token", "secret-ssl", "secret-key"):
        assert sensitive not in public_output


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_NamedConnectionTimeout("deadline exceeded"), "tcp_timeout"),
        (TimeoutError("no text match needed"), "tcp_timeout"),
        (RuntimeError("canceling statement due to statement timeout"), "tcp_timeout"),
        (OSError("could not translate host name 'db.internal' to address: Name or service not known"), "dns_error"),
        (ConnectionRefusedError("connection refused"), "connection_refused"),
        (RuntimeError("FATAL: password authentication failed for user darkfac"), "auth_failed"),
        (RuntimeError('relation "local_run_events" does not exist'), "missing_table"),
        (PermissionError("permission denied for table local_run_events"), "read_only"),
        (ImportError("No module named psycopg"), "missing_driver"),
    ],
    ids=["connection-timeout-type", "timeout-error", "timeout-text", "dns", "refused", "auth", "missing-table", "read-only", "driver"],
)
def test_classify_connection_cause_uses_a_closed_credential_free_set(exc: BaseException, expected: str) -> None:
    assert local_progress.classify_connection_cause(exc) == expected
    reason = local_progress.describe_publish_failure(exc)
    assert expected != "unknown"
    assert str(exc) not in reason


def test_phase_finish_and_cancel_swallow_a_sink_that_fails_both_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two failed sink attempts are one publisher failure; later phases stay mute and never raise."""
    fake = _FakePsycopg(connections=[[TimeoutError("timed out")] for _ in range(8)])
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    sink = PostgresSink(_postgres_url("control-db"))
    warnings: list[str] = []
    progress = _publisher(sink, warn=warnings.append, max_consecutive_failures=2)

    progress.phase("preflight", "running")
    assert progress.failures == 1 and progress.enabled
    assert fake.connect_calls == 2  # the internal retry is not a second publisher failure
    assert progress.warning_cause_code == "tcp_timeout"
    assert warnings == []  # emitted once at close with the final number of missed events

    progress.phase("agent", "running")  # second publisher failure mutes
    progress.phase("gate", "running")
    progress.finish(True)
    progress.cancel("parado")

    assert not progress.enabled
    assert progress.failures == 2
    assert len(warnings) == 1
    assert '"unpublished_phases":4' in warnings[0]
    assert fake.connect_calls == 4  # muted phases do not open another connection
    assert "hunter2" not in warnings[0] and "postgresql://" not in warnings[0]


def test_launcher_progress_summary_includes_missed_count_and_doctor_command() -> None:
    from core.line.ticket_stages import publish_status_line

    summary = publish_status_line({
        "published": False,
        "warning": "timeout",
        "cause_code": "tcp_timeout",
        "unpublished_phases": 3,
    })

    assert "fases_nao_publicadas=3" in summary
    assert "python -m core.line.live_progress_doctor" in summary


@pytest.mark.parametrize(
    "exc",
    [ImportError("no psycopg"), ModuleNotFoundError("No module named 'psycopg'")],
    ids=["import-error", "module-not-found"],
)
def test_a_missing_driver_reason_carries_the_exact_pip_command(exc: ImportError) -> None:
    reason = local_progress.describe_publish_failure(exc)

    assert "driver psycopg nao instalado" in reason
    assert _PSYCOPG_INSTALL_CMD in reason
    assert local_progress.PSYCOPG_INSTALL_CMD == _PSYCOPG_INSTALL_CMD  # one source of truth in the module
    assert "postgresql://" not in reason and "hunter2" not in reason


def test_the_stderr_warning_for_a_missing_driver_carries_the_exact_pip_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = _RaisingSink(ModuleNotFoundError("No module named 'psycopg'"))
    progress = _publisher(sink, warn=local_progress.stderr_warning)

    progress.phase("preflight", "running")
    progress.phase("agent", "running")
    progress.finish(True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("[AVISO] Esteira ao vivo") == 1
    assert '"event":"live_progress_degraded"' in captured.err
    assert '"reason":"driver psycopg nao instalado neste ambiente' in captured.err
    warning = json.loads(captured.err.split("[AVISO] Esteira ao vivo: ", 1)[1])
    assert _PSYCOPG_INSTALL_CMD in warning["reason"]
    assert "docs/runbooks/live_progress.md" in captured.err
    assert "No module named" not in captured.err


def test_the_psycopg_pin_is_the_same_in_the_live_requirements_dockerfiles_and_runbook() -> None:
    root = Path(__file__).resolve().parents[1]
    spec_re = re.compile(r"psycopg\[binary\]([^\s\"']+)")

    def specs(relative: str) -> list[str]:
        return spec_re.findall((root / relative).read_text(encoding="utf-8"))

    live = specs("requirements-live.txt")
    assert live == [">=3.2.0,<3.3.0"]
    for dockerfile in ("deploy/dokploy/Dockerfile.cloud", "deploy/dokploy/Dockerfile.hub"):
        assert specs(dockerfile) == live, f"{dockerfile} pins a different psycopg than requirements-live.txt"
    assert _PSYCOPG_INSTALL_CMD == f'python -m pip install "psycopg[binary]{live[0]}"'

    live_lines = (root / "requirements-live.txt").read_text(encoding="utf-8").splitlines()
    assert "-r requirements.txt" in (line.strip() for line in live_lines)  # optional extra on top of the base
    assert "psycopg" not in (root / "requirements.txt").read_text(encoding="utf-8").lower()
    assert _PSYCOPG_INSTALL_CMD in (root / "docs" / "runbooks" / "live_progress.md").read_text(encoding="utf-8")


def test_a_sink_that_recovers_after_one_failure_still_warned_only_once() -> None:
    state = {"fail": True}

    class Flaky:
        def write(self, event: LocalRunEvent) -> None:
            if state["fail"]:
                raise OSError("flap")

    warnings: list[str] = []
    progress = _publisher(Flaky(), warn=warnings.append)
    progress.phase("preflight", "running")
    state["fail"] = False
    progress.phase("agent", "running")
    state["fail"] = True
    progress.phase("gate", "running")
    progress.finish(True)
    assert len(warnings) == 1


def test_a_warn_callback_that_raises_never_breaks_publishing() -> None:
    def boom(_text: str) -> None:
        raise RuntimeError("stderr closed")

    progress = _publisher(_RaisingSink(OSError("down")), warn=boom)
    progress.phase("agent", "running")  # does not raise
    progress.finish(True)


def test_stderr_warning_writes_to_stderr_only(capsys: pytest.CaptureFixture[str]) -> None:
    local_progress.stderr_warning("[AVISO] teste")
    captured = capsys.readouterr()
    assert captured.out == "" and "[AVISO] teste" in captured.err


def test_missing_sink_reason_distinguishes_absent_and_mock_urls() -> None:
    assert "ausente" in (local_progress.missing_sink_reason({}) or "")
    assert "mock" in (local_progress.missing_sink_reason({"DARKFAC_HF02_DATABASE_URL": "mock://"}) or "")
    assert local_progress.missing_sink_reason({ENV_SWITCH: "0"}) is None
    assert local_progress.missing_sink_reason() is None  # under pytest with the default environment


def test_json_run_without_configuration_keeps_stdout_clean_and_exit_code(
    launcher: Callable[[Any], _Launcher],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    launcher(None)
    _delivery_ok(monkeypatch)

    def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
        return open_progress(ticket_id, project_id, title, environ={}, warn=local_progress.stderr_warning)

    monkeypatch.setattr(run_ticket, "open_progress", make)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True  # stdout is still exactly one JSON document
    assert captured.err.count("[AVISO] Esteira ao vivo") == 1
    assert "DARKFAC_HF02_DATABASE_URL" in captured.err


def test_json_run_with_a_failing_store_warns_on_stderr_once_and_does_not_change_the_result(
    launcher: Callable[[Any], _Launcher], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = _RaisingSink(PermissionError("permission denied for table local_run_events"))
    launcher(sink)

    def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
        return ProgressPublisher(
            ticket_id=ticket_id, project_id=project_id, title=title, sink=sink, worker="HOST-A",
            warn=local_progress.stderr_warning,
        )

    monkeypatch.setattr(run_ticket, "open_progress", make)
    _delivery_ok(monkeypatch)

    assert main(["USR-99", "--harness", "codex", "--skip-validation", "--json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is True
    assert captured.err.count("[AVISO] Esteira ao vivo") == 1
    assert "sem permissao de escrita" in captured.err
    assert "hunter2" not in captured.err + captured.out
    assert "permission denied for table" not in captured.err + captured.out


@pytest.fixture(autouse=True)
def _isolate_budget_notifications(monkeypatch):
    monkeypatch.setattr("core.line.failure_alerts.notify_ticket_hold", lambda *args: None)


def test_publish_status_line_contains_only_the_structured_failure_summary() -> None:
    from core.line.ticket_stages import publish_status, publish_status_line

    status = publish_status(None, {"live_progress": {
        "published": False,
        "warning": "falha de publicacao",
        "cause_code": "unknown",
    }})
    assert status["published"] is False
    assert status["cause_code"] == "unknown"
    assert status["warning"] == "falha de publicacao"
    line = publish_status_line(status)
    assert line.startswith("[ESTEIRA] progresso mudo - ")
    assert "falha de publicacao" in line


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)


def _resume_repo(tmp_path: Path) -> Path:
    from core.demands.models import DeliveryStatus, UserTicket
    from core.demands.store import DemandsStore

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@darkfac.local")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    ticket = UserTicket(
        id="USR-455", project_id="darkfac", title="Progresso no Hub",
        problem_statement="publicar fases", status=DeliveryStatus.PLANNED,
    )
    demands = repo / ".factory" / "demands"
    demands.mkdir(parents=True)
    DemandsStore(demands / "demands.json").save_ticket(ticket)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ledger")
    _git(repo, "checkout", "-b", "ticket/usr-455")
    return repo


def _fake_deliver(
    git_mgr: Any, ticket: Any, target_worktree: Path, current_branch: str, resume_cmd: str,
    skip_validation: bool, no_commit: bool, no_push: bool, as_json: bool, return_result: bool,
    progress: ProgressPublisher, cancelled_payload: Any, standalone: bool = False,
) -> Any:
    from core.line import ticket_stages

    progress.phase("preflight", "succeeded", "rota")
    progress.phase("workspace", "succeeded", "worktree")
    progress.phase("agent", "running", "implementando")
    report = TicketCompletionReport(ok=True, ticket_id=ticket.id, commit_sha="abc123")
    return ticket_stages.delivery_success_result(
        ticket, report, target_worktree, current_branch, "abc123", [],
        as_json, return_result, progress,
    )


def test_resume_delivery_prints_publicado_and_a_single_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Standalone --resume-delivery (USR-455 T3): stderr line plus live_progress, stdout is one JSON."""
    repo = _resume_repo(tmp_path)
    sink = RecordingSink()

    def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
        return ProgressPublisher(
            ticket_id=ticket_id, project_id=project_id, title=title, sink=sink, worker="HOST-A", warn=None,
        )

    monkeypatch.setattr(run_ticket, "open_progress", make)
    monkeypatch.setattr(run_ticket, "_deliver_worktree", _fake_deliver)

    assert run_ticket.resume_delivery("USR-455", worktree_path=repo, skip_validation=True, as_json=True) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["live_progress"] == {
        "published": True, "warning": None, "cause_code": None, "unpublished_phases": 0,
    }
    assert "[ESTEIRA] progresso publicado no Hub" in captured.err
    assert "[ESTEIRA]" not in captured.out
    assert {event.phase: event.status for event in sink.events if event.phase != "run"}["agent"] == "running"


def test_resume_delivery_stays_successful_when_publication_is_muted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _resume_repo(tmp_path)
    sink = _RaisingSink(PermissionError("permission denied for table local_run_events"))

    def make(ticket_id: str, project_id: str = "darkfac", title: str = "", **_: Any) -> ProgressPublisher:
        return ProgressPublisher(
            ticket_id=ticket_id, project_id=project_id, title=title, sink=sink, worker="HOST-A",
            warn=local_progress.stderr_warning,
        )

    monkeypatch.setattr(run_ticket, "open_progress", make)
    monkeypatch.setattr(run_ticket, "_deliver_worktree", _fake_deliver)

    assert run_ticket.resume_delivery("USR-455", worktree_path=repo, skip_validation=True, as_json=True) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 0
    progress = payload["live_progress"]
    assert progress["published"] is False
    assert progress["cause_code"] == "read_only"
    esteira = [line for line in captured.err.splitlines() if line.startswith("[ESTEIRA]")]
    assert len(esteira) == 1 and esteira[0].startswith("[ESTEIRA] progresso mudo - ")
    assert "permission denied for table" not in captured.out + esteira[0]
