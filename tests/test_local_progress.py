"""USR-140: run_ticket publishes best-effort phase progress for the live line board.

Everything is hermetic: events go to an in-memory sink or a scratch SQLite file, never to a real
database, and ``run_ticket`` runs with a fake backlog, fake quotas and a fake agent.
"""

from __future__ import annotations

import json
import logging
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
from core.git.autonomy import GitAutonomyManager, SweepReport, TicketCompletionReport
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
# Built at runtime so no tracked file contains a literal key-shaped string (test_no_tracked_secrets).
_FAKE_SECRET = "sk-" + "ant-api03-" + "SECRETSECRETSECRET"


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
        raise RuntimeError("connection refused " + "postgresql://" + "user:" + "hunter2" + "@10.0.0.9:5432/db")


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
    assert any("muted" in record.getMessage() for record in caplog.records)
    assert "hunter2" not in caplog.text  # credentials inside driver errors never reach the logs


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


def test_messages_are_single_line_redacted_and_bounded() -> None:
    sink = RecordingSink()
    progress = _publisher(sink)
    progress.phase("agent", "failed", f"linha 1\nlinha 2 {_FAKE_SECRET}\n" + "x" * 2000)
    message = sink.events[0].message
    assert "\n" not in message and "SECRETSECRETSECRET" not in message
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

    url = "postgresql://writer:pw@127.0.0.1:5432/control"
    assert isinstance(resolve_sink({"DARKFAC_HF02_DATABASE_URL": url}), PostgresSink)
    assert resolve_sink({"DARKFAC_HF02_DATABASE_URL": "mock://"}) is None  # mock URLs are not a database
    assert resolve_sink({"DARKFAC_HF02_DATABASE_URL": url, ENV_SWITCH: "off"}) is None

    # writer-capable URL wins over the Hub's read-only one
    sink = resolve_sink({"DARKHUB_CONTROL_DATABASE_URL": "postgresql://ro@h/db", "DARKFAC_HF02_DATABASE_URL": url})
    assert isinstance(sink, PostgresSink) and sink._url == url

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
    monkeypatch.setattr(run_ticket, "rebase_on_origin_main", lambda cwd: (True, [], "already up to date"))
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


def test_an_agent_failure_closes_the_run_as_failed_on_the_agent_phase(
    launcher: Callable[[Any], _Launcher], capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    env = launcher(sink)
    env.agent.side_effect = lambda req: AgentResult(
        ok=False, harness=req.harness, error_kind="timeout", exit_code=None, stderr_tail="", duration_s=9.0, text=""
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

    assert main(["USR-99", "--harness", "codex", "--json"]) == 2

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


# ------------------------------------------------------------------ USR-154: one warning when it cannot publish

# Built by concatenation so no tracked file carries a literal URL with a password (test_no_tracked_secrets).
_PW_URL = "postgresql://" + "writer:" + "hunter2" + "@10.0.0.9:5432/control"


class _RaisingSink:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def write(self, event: LocalRunEvent) -> None:
        self.calls += 1
        raise self.exc


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
    assert "DARKFAC_HF02_DATABASE_URL" in warnings[0] and "DARKHUB_LINE_DATABASE_URL" in warnings[0]
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
    ("exc", "expected"),
    [
        (PermissionError("permission denied for schema public"), "sem permissao de escrita"),
        (RuntimeError("attempt to write a readonly database"), "sem permissao de escrita"),
        (TimeoutError("progress write exceeded 5s"), "inalcancavel"),
        (ConnectionRefusedError("could not connect to server"), "inalcancavel"),
        (ModuleNotFoundError("No module named 'psycopg'"), "psycopg"),
        (ValueError("weird " + _PW_URL), "falha ao gravar (ValueError)"),
    ],
    ids=["permission", "readonly", "timeout", "refused", "driver", "other"],
)
def test_a_failing_sink_warns_once_at_the_first_failure_with_a_sanitized_reason(
    exc: BaseException, expected: str
) -> None:
    warnings: list[str] = []
    progress = _publisher(_RaisingSink(exc), warn=warnings.append)

    progress.phase("preflight", "running")
    progress.phase("agent", "running")
    progress.phase("gate", "running")
    progress.finish(True)

    assert len(warnings) == 1
    assert expected in warnings[0]
    assert "hunter2" not in warnings[0] and "postgresql://" not in warnings[0]
    assert not progress.enabled  # muted after repeated failures, run unaffected


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
    sink = _RaisingSink(PermissionError("permission denied for table local_run_events " + _PW_URL))
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
