"""USR-164: interactive Claude Code sessions publish their ticket progress to the live line board.

``scripts/live_run.py`` (``core.line.live_run``) is the thin, stable CLI a chat session calls to open,
advance, finish or cancel one local run per ticket. Everything here is hermetic: a recording sink or a
scratch SQLite control store, a scratch state directory, no network and no real database.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line import live_run
from core.line.local_progress import (
    ENV_RUN_ID,
    ENV_SWITCH,
    ENV_WARNED,
    LocalRunEvent,
    ProgressSink,
    SqliteSink,
)
from core.workflow.line_live import read_line_live
from tests.fixtures.line_live_seed import seed_line_live_demo

REPO = Path(__file__).resolve().parents[1]


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
        raise ConnectionRefusedError("could not connect to server")


class FlakySink:
    """Fails the first ``fail_first`` writes (unreachable), then records normally."""

    def __init__(self, fail_first: int) -> None:
        self.remaining = fail_first
        self.events: list[LocalRunEvent] = []

    def write(self, event: LocalRunEvent) -> None:
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionRefusedError("could not connect to server")
        self.events.append(event)

    @property
    def steps(self) -> list[tuple[str, str]]:
        return [(event.phase, event.status) for event in self.events]


class LostAckSink:
    """Writes the event to ``inner`` and then reports a failure for the closing ``run`` event (lost ack)."""

    def __init__(self, inner: ProgressSink) -> None:
        self.inner = inner
        self.lose_ack = True

    def write(self, event: LocalRunEvent) -> None:
        self.inner.write(event)
        if self.lose_ack and event.phase == "run":
            raise TimeoutError("acknowledgement lost")


@pytest.fixture()
def cli(tmp_path: Path) -> Callable[..., int]:
    """``cli("open", "--ticket", "USR-1")`` runs the command against a scratch state directory."""
    state = tmp_path / "live_runs"
    sink_box: dict[str, Any] = {}

    def run(*argv: str, sink: Any = None, warnings: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
        sink_box.setdefault("warnings", warnings)
        return live_run.main(
            list(argv),
            environ={} if environ is None else environ,
            sink=sink,
            state_dir=state,
            hostname="NOTEBOOK-TEST",
            warn=(warnings.append if warnings is not None else None),
        )

    run.state_dir = state  # type: ignore[attr-defined]
    return run


def _state_file(cli: Any, ticket: str = "USR-164") -> Path:
    return cli.state_dir / f"{ticket}.json"


# ---------------------------------------------------------------- lifecycle


def test_open_publishes_a_run_with_harness_and_machine_and_is_idempotent(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    sink = RecordingSink()
    assert cli("open", "--ticket", "USR-164", "--title", "Esteira ao vivo", "--harness", "claude", sink=sink) == 0
    first = capsys.readouterr().out
    assert "USR-164" in first and sink.steps == [("preflight", "running")]
    event = sink.events[0]
    assert (event.ticket_id, event.project_id, event.title) == ("USR-164", "darkfac", "Esteira ao vivo")
    assert (event.harness, event.worker) == ("claude", "NOTEBOOK-TEST")
    assert event.run_id.startswith("local-USR-164-")
    assert _state_file(cli).is_file()

    # opening again reuses the run and publishes nothing new
    assert cli("open", "--ticket", "USR-164", sink=sink) == 0
    assert sink.steps == [("preflight", "running")]
    assert len({e.run_id for e in sink.events}) == 1


def test_phases_close_the_previous_one_and_repeats_are_idempotent(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    cli("phase", "workspace", "--ticket", "USR-164", "--message", "worktree propria", sink=sink)
    cli("phase", "workspace", "--ticket", "USR-164", "--message", "worktree propria", sink=sink)  # repeat
    cli("phase", "agent", "--ticket", "USR-164", "--message", "subagente sonnet", sink=sink)
    cli("phase", "agent", "--ticket", "USR-164", "--message", "metade do trabalho", sink=sink)  # progress update
    cli("phase", "gate", "--ticket", "USR-164", sink=sink)
    assert sink.steps == [
        ("preflight", "running"),
        ("preflight", "succeeded"),
        ("workspace", "running"),
        ("workspace", "succeeded"),
        ("agent", "running"),
        ("agent", "running"),
        ("agent", "succeeded"),
        ("gate", "running"),
    ]
    assert len({e.run_id for e in sink.events}) == 1


def test_an_explicit_terminal_status_is_published_as_given(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    cli("phase", "gate", "--ticket", "USR-164", "--status", "failed", "--cause", "gate_red", "--message", "3 testes", sink=sink)
    assert sink.steps[-1] == ("gate", "failed")
    assert sink.events[-1].cause_code == "gate_red"
    # the failed phase is no longer active: the next phase does not "close" it again
    cli("phase", "agent", "--ticket", "USR-164", sink=sink)
    assert sink.steps[-1] == ("agent", "running")
    assert ("gate", "succeeded") not in sink.steps


def test_phase_without_open_opens_the_run_implicitly(cli: Any) -> None:
    sink = RecordingSink()
    assert cli("phase", "agent", "--ticket", "USR-170", "--title", "Sem open", sink=sink) == 0
    assert sink.steps == [("agent", "running")]
    assert sink.events[0].run_id.startswith("local-USR-170-")
    assert sink.events[0].title == "Sem open"


def test_finish_closes_the_active_phase_and_the_run_once(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    cli("phase", "deploy", "--ticket", "USR-164", sink=sink)
    assert cli("finish", "--ticket", "USR-164", "--message", "tudo entregue", sink=sink) == 0
    assert sink.steps[-2:] == [("deploy", "succeeded"), ("run", "succeeded")]
    assert not _state_file(cli).exists()

    before = list(sink.steps)
    assert cli("finish", "--ticket", "USR-164", sink=sink) == 0  # nothing open: no-op
    assert sink.steps == before


def test_failed_finish_marks_the_active_phase_failed_with_the_cause(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    cli("phase", "ci", "--ticket", "USR-164", sink=sink)
    cli("finish", "--ticket", "USR-164", "--failed", "--cause", "ci_red", "--message", "CI vermelho", sink=sink)
    assert sink.steps[-2:] == [("ci", "failed"), ("run", "failed")]
    assert sink.events[-1].cause_code == "ci_red"
    assert sink.events[-1].message == "CI vermelho"


def test_cancel_records_the_active_phase_and_the_run_as_cancelled(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    cli("phase", "agent", "--ticket", "USR-164", sink=sink)
    assert cli("cancel", "--ticket", "USR-164", sink=sink) == 0
    assert sink.steps[-2:] == [("agent", "cancelled"), ("run", "cancelled")]
    assert sink.events[-1].cause_code == "owner_cancelled"
    assert not _state_file(cli).exists()
    before = list(sink.steps)
    cli("cancel", "--ticket", "USR-164", sink=sink)
    assert sink.steps == before


def test_a_stale_run_is_replaced_by_a_new_one_on_open(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink)
    path = _state_file(cli)
    data = json.loads(path.read_text(encoding="utf-8"))
    old = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    data.update(updated_at=old, run_id="local-USR-164-old")
    path.write_text(json.dumps(data), encoding="utf-8")
    cli("open", "--ticket", "USR-164", sink=sink)
    assert sink.events[-1].run_id != "local-USR-164-old"
    assert sink.steps == [("preflight", "running"), ("preflight", "running")]


def test_the_parent_run_environment_is_ignored(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink, environ={ENV_RUN_ID: "local-USR-1-other", ENV_WARNED: "1"})
    cli("finish", "--ticket", "USR-164", sink=sink, environ={ENV_RUN_ID: "local-USR-1-other"})
    assert sink.events[0].run_id != "local-USR-1-other"
    assert sink.steps[-1] == ("run", "succeeded")  # the CLI owns its run: it can close it


# ---------------------------------------------------------------- never fails the session


def test_an_unreachable_sink_warns_once_per_run_and_never_fails(cli: Any) -> None:
    sink = BrokenSink()
    warnings: list[str] = []
    codes = [
        cli("open", "--ticket", "USR-164", sink=sink, warnings=warnings),
        cli("phase", "agent", "--ticket", "USR-164", sink=sink, warnings=warnings),
        cli("phase", "gate", "--ticket", "USR-164", sink=sink, warnings=warnings),
        cli("finish", "--ticket", "USR-164", sink=sink, warnings=warnings),
    ]
    assert codes == [0, 0, 0, 0]
    assert len(warnings) == 1  # one warning per run, not per process
    assert "[AVISO]" in warnings[0] and "inalcancavel" in warnings[0] and "live_progress.md" in warnings[0]
    assert "could not connect" not in warnings[0]  # the driver message is never echoed


def test_no_configured_sink_warns_with_the_variable_names_and_exits_zero(
    cli: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "empty_state"))  # no control.db: no SQLite fallback
    warnings: list[str] = []
    assert cli("open", "--ticket", "USR-164", "--json", warnings=warnings) == 0
    assert len(warnings) == 1 and "DARKFAC_HF02_DATABASE_URL" in warnings[0]
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["published"] is False and payload["warning"] == warnings[0]
    assert payload["ticket_id"] == "USR-164" and payload["run_id"].startswith("local-USR-164-")


def test_the_explicit_off_switch_is_silent(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    warnings: list[str] = []
    assert cli("open", "--ticket", "USR-164", warnings=warnings, environ={ENV_SWITCH: "off"}) == 0
    assert warnings == []
    assert "USR-164" in capsys.readouterr().out


def test_an_unwritable_state_directory_never_fails_the_session(tmp_path: Path) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    sink = RecordingSink()
    warnings: list[str] = []
    code = live_run.main(
        ["open", "--ticket", "USR-164"], environ={}, sink=sink, state_dir=blocker / "live_runs",
        hostname="H", warn=warnings.append,
    )
    assert code == 0
    assert sink.steps == [("preflight", "running")]  # still published


def test_a_corrupt_state_file_is_replaced_not_fatal(cli: Any) -> None:
    sink = RecordingSink()
    cli.state_dir.mkdir(parents=True)
    _state_file(cli).write_text("{not json", encoding="utf-8")
    assert cli("phase", "agent", "--ticket", "USR-164", sink=sink) == 0
    assert sink.steps == [("agent", "running")]


def test_usage_errors_are_the_only_non_zero_exit(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    sink = RecordingSink()
    assert cli("phase", "nonsense", "--ticket", "USR-164", sink=sink) == 2
    assert "preflight" in capsys.readouterr().err and sink.events == []
    assert cli("open", sink=sink) == 2  # --ticket is required
    assert sink.events == []


def test_the_default_harness_comes_from_the_operating_harness_variable(cli: Any) -> None:
    sink = RecordingSink()
    cli("open", "--ticket", "USR-164", sink=sink, environ={"DARKFAC_OPERATING_HARNESS": "codex"})
    cli("open", "--ticket", "USR-165", sink=sink)
    assert [e.harness for e in sink.events] == ["codex", "claude"]


# ---------------------------------------------------------------- an unpublished closing keeps the state (USR-202)


def _saved(cli: Any, ticket: str = "USR-164") -> dict[str, Any]:
    return json.loads(_state_file(cli, ticket).read_text(encoding="utf-8"))


def _outcome(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out)


def _open_with_active_phase(cli: Any, sink: Any, phase: str = "agent", ticket: str = "USR-164") -> str:
    cli("open", "--ticket", ticket, sink=sink)
    cli("phase", phase, "--ticket", ticket, "--message", "em andamento", sink=sink)
    return str(_saved(cli, ticket)["run_id"])


def test_finish_without_a_reachable_sink_keeps_the_state_and_a_later_finish_republishes_the_same_run(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    good = RecordingSink()
    run_id = _open_with_active_phase(cli, good)
    capsys.readouterr()

    assert cli("finish", "--ticket", "USR-164", "--message", "entregue", "--json", sink=BrokenSink()) == 0
    first = _outcome(capsys)
    assert first["state_kept"] is True and first["published"] is False and first["run_id"] == run_id
    assert "mantido" in first["warning"] and "mesmo comando" in first["warning"]
    kept = _saved(cli)
    # the run identity and the phase still open survive: the replay must close the right phase of the same run
    assert (kept["run_id"], kept["active_phase"], kept["closing"]) == (run_id, "agent", "finish")

    revived = RecordingSink()
    assert cli("finish", "--ticket", "USR-164", "--json", sink=revived) == 0
    second = _outcome(capsys)
    assert second["state_kept"] is False and second["published"] is True and second["run_id"] == run_id
    assert revived.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert {event.run_id for event in revived.events} == {run_id}
    assert revived.events[-1].message == "entregue"  # the message of the pending finish is replayed
    assert not _state_file(cli).exists()  # cleared only after the closing was published

    after = RecordingSink()
    assert cli("finish", "--ticket", "USR-164", "--json", sink=after) == 0
    assert after.events == [] and _outcome(capsys)["state_kept"] is False  # nothing left to close


def test_a_failed_finish_is_replayed_with_its_cause_message_and_open_phase(cli: Any) -> None:
    run_id = _open_with_active_phase(cli, RecordingSink(), phase="ci")
    cli("finish", "--ticket", "USR-164", "--failed", "--cause", "ci_red", "--message", "CI vermelho", sink=BrokenSink())
    kept = _saved(cli)
    assert (kept["closing"], kept["closing_failed"], kept["closing_cause"]) == ("finish", True, "ci_red")

    revived = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=revived)  # a bare repeat replays the pending outcome
    assert revived.steps == [("ci", "failed"), ("run", "failed")]
    assert [e.cause_code for e in revived.events] == ["ci_red", "ci_red"]
    assert revived.events[-1].message == "CI vermelho" and revived.events[-1].run_id == run_id
    assert not _state_file(cli).exists()


def test_cancel_without_a_reachable_sink_keeps_the_state_and_is_replayed(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _open_with_active_phase(cli, RecordingSink())
    capsys.readouterr()
    assert cli("cancel", "--ticket", "USR-164", "--json", sink=BrokenSink()) == 0
    assert _outcome(capsys)["state_kept"] is True
    assert (_saved(cli)["closing"], _saved(cli)["run_id"]) == ("cancel", run_id)

    revived = RecordingSink()
    assert cli("cancel", "--ticket", "USR-164", sink=revived) == 0
    assert revived.steps == [("agent", "cancelled"), ("run", "cancelled")]
    assert revived.events[-1].cause_code == "owner_cancelled" and revived.events[-1].run_id == run_id
    assert not _state_file(cli).exists()


def test_a_different_closing_command_replaces_the_pending_one(cli: Any) -> None:
    _open_with_active_phase(cli, RecordingSink())
    cli("cancel", "--ticket", "USR-164", "--message", "desisti", sink=BrokenSink())
    revived = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=revived)  # explicit and different: nothing of the cancel leaks in
    assert revived.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert revived.events[-1].message == "" and revived.events[-1].cause_code is None


def test_the_machine_control_db_is_a_visibility_gap_and_keeps_the_state(
    cli: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _open_with_active_phase(cli, RecordingSink())
    capsys.readouterr()
    local = SqliteSink(tmp_path / "control.db")
    assert cli("finish", "--ticket", "USR-164", "--json", sink=local) == 0
    outcome = _outcome(capsys)
    assert outcome["published"] is False and outcome["state_kept"] is True
    assert _state_file(cli).is_file()  # the cloud Hub never reads that file

    cloud = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=cloud)
    assert cloud.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert {event.run_id for event in cloud.events} == {run_id}
    assert not _state_file(cli).exists()


def test_a_deliberately_local_control_db_settles_the_closing(
    cli: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {ENV_SWITCH: "local"}
    local = SqliteSink(tmp_path / "control.db")
    cli("open", "--ticket", "USR-164", sink=local, environ=env)
    capsys.readouterr()
    assert cli("finish", "--ticket", "USR-164", "--json", sink=local, environ=env) == 0
    outcome = _outcome(capsys)
    assert outcome["state_kept"] is False and outcome["published"] is False  # control.db never counts as published
    assert not _state_file(cli).exists()  # but a Hub on this machine reads it: nothing to replay


def test_publishing_switched_off_settles_the_closing_without_noise(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    warnings: list[str] = []
    env = {ENV_SWITCH: "off"}
    cli("open", "--ticket", "USR-164", warnings=warnings, environ=env)
    capsys.readouterr()
    assert cli("finish", "--ticket", "USR-164", "--json", warnings=warnings, environ=env) == 0
    assert _outcome(capsys)["state_kept"] is False
    assert warnings == [] and not _state_file(cli).exists()


def test_no_configured_sink_keeps_the_state_until_the_url_exists(
    cli: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "empty_state"))  # no control.db: no SQLite fallback
    cli("open", "--ticket", "USR-164")
    capsys.readouterr()
    assert cli("finish", "--ticket", "USR-164", "--json") == 0
    outcome = _outcome(capsys)
    assert outcome["published"] is False and outcome["state_kept"] is True
    assert _saved(cli)["closing"] == "finish"

    fixed = RecordingSink()
    assert cli("finish", "--ticket", "USR-164", sink=fixed) == 0
    assert fixed.steps[-1] == ("run", "succeeded") and not _state_file(cli).exists()


def test_a_publishing_exception_keeps_the_state(
    cli: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = _open_with_active_phase(cli, RecordingSink())
    capsys.readouterr()

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("publisher exploded")

    with monkeypatch.context() as patch:
        patch.setattr(live_run, "_apply", boom)
        assert cli("finish", "--ticket", "USR-164", "--json", sink=RecordingSink()) == 0
    outcome = _outcome(capsys)
    assert outcome["state_kept"] is True and "RuntimeError" in outcome["detail"]
    assert _saved(cli)["run_id"] == run_id

    revived = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=revived)
    assert revived.steps == [("agent", "succeeded"), ("run", "succeeded")] and not _state_file(cli).exists()


def test_a_partial_publish_is_not_a_settled_closing(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    _open_with_active_phase(cli, RecordingSink())
    capsys.readouterr()
    flaky = FlakySink(fail_first=1)  # the phase close is lost, the run close lands: failures goes back to 0
    assert cli("finish", "--ticket", "USR-164", "--json", sink=flaky) == 0
    assert flaky.steps == [("run", "succeeded")]
    outcome = _outcome(capsys)
    assert outcome["state_kept"] is True and outcome["published"] is False

    revived = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=revived)
    assert revived.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert not _state_file(cli).exists()


def test_the_single_warning_of_the_run_carries_the_state_kept_sentence(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    warnings: list[str] = []
    _open_with_active_phase(cli, RecordingSink())  # healthy so far: no warning yet
    assert cli("finish", "--ticket", "USR-164", "--json", sink=BrokenSink(), warnings=warnings) == 0
    capsys.readouterr()
    assert len(warnings) == 1  # ONE [AVISO], publisher reason and state-kept sentence together
    text = warnings[0]
    assert text.startswith("[AVISO]") and "inalcancavel" in text and "live_progress.md" in text
    assert "estado do run foi mantido" in text.lower() and "DARKFAC_HF02_DATABASE_URL" in text
    assert _saved(cli)["warned"] is True

    # the replay fails again: still one warning for the run, but the outcome keeps saying what happened
    assert cli("finish", "--ticket", "USR-164", "--json", sink=BrokenSink(), warnings=warnings) == 0
    again = _outcome(capsys)
    assert len(warnings) == 1
    assert again["state_kept"] is True and "mantido" in again["warning"]


def test_exit_code_stays_zero_and_the_notice_reaches_stdout_even_after_an_earlier_warning(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    warnings: list[str] = []
    cli("open", "--ticket", "USR-164", sink=BrokenSink(), warnings=warnings)  # the run's one warning
    capsys.readouterr()
    assert cli("finish", "--ticket", "USR-164", sink=BrokenSink(), warnings=warnings) == 0
    line = capsys.readouterr().out
    assert len(warnings) == 1 and "NAO publicado" in line and "estado do run foi mantido" in line


def test_open_and_phase_after_an_unpublished_finish_resume_the_run_and_drop_the_pending_closing(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    good = RecordingSink()
    run_id = _open_with_active_phase(cli, good)
    cli("finish", "--ticket", "USR-164", "--failed", "--cause", "ci_red", sink=BrokenSink())
    assert _saved(cli)["closing"] == "finish"
    capsys.readouterr()

    assert cli("open", "--ticket", "USR-164", "--json", sink=good) == 0
    reopened = _outcome(capsys)
    assert reopened["run_id"] == run_id and "descartado" in reopened["detail"]
    assert _saved(cli)["closing"] is None and _saved(cli)["closing_cause"] is None

    before = len(good.events)
    cli("phase", "gate", "--ticket", "USR-164", sink=good)
    assert {event.run_id for event in good.events} == {run_id}
    assert [(e.phase, e.status) for e in good.events[before:]] == [("agent", "succeeded"), ("gate", "running")]

    cli("finish", "--ticket", "USR-164", sink=good)  # no stale failure from the dropped pending finish
    assert [(e.phase, e.status) for e in good.events[-2:]] == [("gate", "succeeded"), ("run", "succeeded")]
    assert good.events[-1].cause_code is None and not _state_file(cli).exists()


def test_a_pending_closing_outlives_the_abandonment_window_but_a_plain_stale_run_does_not(cli: Any) -> None:
    run_id = _open_with_active_phase(cli, RecordingSink())
    cli("finish", "--ticket", "USR-164", sink=BrokenSink())
    path = _state_file(cli)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")

    revived = RecordingSink()
    cli("finish", "--ticket", "USR-164", sink=revived)  # the owner fixed the URL the next day
    assert revived.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert {event.run_id for event in revived.events} == {run_id}

    # without a pending closing a run that old is abandoned: finish stays a no-op (unchanged behaviour)
    _open_with_active_phase(cli, RecordingSink(), ticket="USR-165")
    stale = _state_file(cli, "USR-165")
    data = json.loads(stale.read_text(encoding="utf-8"))
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    stale.write_text(json.dumps(data), encoding="utf-8")
    ignored = RecordingSink()
    cli("finish", "--ticket", "USR-165", sink=ignored)
    assert ignored.events == []


def test_a_state_file_written_before_the_pending_closing_existed_still_loads(cli: Any) -> None:
    cli.state_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    old_format = {
        "run_id": "local-USR-164-old", "ticket_id": "USR-164", "project_id": "darkfac", "title": "antigo",
        "harness": "claude", "worker": "H", "active_phase": "agent", "last_phase": "agent",
        "last_status": "running", "last_message": "", "warned": False, "opened_at": now, "updated_at": now,
    }
    _state_file(cli).write_text(json.dumps(old_format), encoding="utf-8")
    sink = RecordingSink()
    assert cli("finish", "--ticket", "USR-164", sink=sink) == 0
    assert sink.steps == [("agent", "succeeded"), ("run", "succeeded")]
    assert {event.run_id for event in sink.events} == {"local-USR-164-old"} and not _state_file(cli).exists()


def test_every_outcome_reports_whether_the_state_was_kept(cli: Any, capsys: pytest.CaptureFixture[str]) -> None:
    sink = RecordingSink()
    cli("finish", "--ticket", "USR-164", "--json", sink=sink)  # no run open
    assert _outcome(capsys)["state_kept"] is False
    cli("open", "--ticket", "USR-164", "--json", sink=sink)
    assert _outcome(capsys)["state_kept"] is False
    cli("open", "--ticket", "USR-164", "--json", sink=sink)  # idempotent reuse
    assert _outcome(capsys)["state_kept"] is False
    cli("phase", "agent", "--ticket", "USR-164", "--json", sink=sink)
    assert _outcome(capsys)["state_kept"] is False


def test_replaying_a_closing_whose_write_had_landed_leaves_one_closed_run_in_the_hub(
    cli: Any, tmp_path: Path
) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, datetime.now(timezone.utc))
    sink = LostAckSink(SqliteSink(db))
    cli("open", "--ticket", "USR-164", "--title", "Esteira ao vivo", sink=sink)
    cli("phase", "agent", "--ticket", "USR-164", sink=sink)
    run_id = str(_saved(cli)["run_id"])

    cli("finish", "--ticket", "USR-164", sink=sink)  # the closing row is in the database, the ack is lost
    assert _saved(cli)["closing"] == "finish"
    sink.lose_ack = False
    cli("finish", "--ticket", "USR-164", sink=sink)  # the sink is append-only: the closing now exists twice
    assert not _state_file(cli).exists()

    with closing(sqlite3.connect(str(db))) as conn:
        closings = conn.execute(
            "SELECT COUNT(*) FROM local_run_events WHERE run_id = ? AND phase = 'run'", (run_id,)
        ).fetchone()[0]
    assert closings == 2  # duplicates are accepted by the sink ...
    snapshot = read_line_live(db, database_url="", now=datetime.now(timezone.utc))
    runs = [r for r in snapshot.runs if r.ticket_id == "USR-164"]
    assert len(runs) == 1 and runs[0].state == "succeeded"  # ... and folded into a single closed run
    assert {stage.stage: stage.status for stage in runs[0].stages}["agent"] == "succeeded"


# ---------------------------------------------------------------- shows up in the live line


def test_cli_runs_appear_in_the_live_line_with_harness_and_machine(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, datetime.now(timezone.utc))
    sink = SqliteSink(db)
    state = tmp_path / "live_runs"

    def call(*argv: str) -> int:
        return live_run.main(
            list(argv), environ={}, sink=sink, state_dir=state, hostname="NOTEBOOK-TEST", warn=None
        )

    assert call("open", "--ticket", "USR-164", "--title", "Esteira ao vivo local", "--harness", "claude") == 0
    assert call("phase", "workspace", "--ticket", "USR-164") == 0
    assert call("phase", "agent", "--ticket", "USR-164", "--message", "implementando por subagentes") == 0

    snapshot = read_line_live(db, database_url="", now=datetime.now(timezone.utc))
    run = next(r for r in snapshot.runs if r.ticket_id == "USR-164")
    assert run.mode == "run_ticket" and run.title == "Esteira ao vivo local"
    by_stage = {stage.stage: stage for stage in run.stages}
    assert by_stage["preflight"].status == "succeeded"
    assert by_stage["workspace"].status == "succeeded"
    assert by_stage["agent"].status == "running"
    assert (by_stage["agent"].worker, by_stage["agent"].route) == ("NOTEBOOK-TEST", "claude")
    assert by_stage["gate"].status == "not_reached"
    assert run.state == "running"

    assert call("finish", "--ticket", "USR-164") == 0
    done = next(r for r in read_line_live(db, database_url="", now=datetime.now(timezone.utc)).runs if r.ticket_id == "USR-164")
    assert done.state == "succeeded"
    assert {stage.stage: stage.status for stage in done.stages}["agent"] == "succeeded"


def test_published_means_visible_to_the_cloud_hub(cli: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli("open", "--ticket", "USR-164", "--json", sink=RecordingSink())
    assert json.loads(capsys.readouterr().out)["published"] is True
    cli("open", "--ticket", "USR-165", "--json", sink=SqliteSink(tmp_path / "control.db"))
    assert json.loads(capsys.readouterr().out)["published"] is False  # only the machine's own control.db
    cli("open", "--ticket", "USR-166", "--json", sink=BrokenSink())
    assert json.loads(capsys.readouterr().out)["published"] is False


# ---------------------------------------------------------------- documentation does not drift


def test_runbook_and_skill_document_the_cli_phases_and_the_provisioning_script() -> None:
    from core.line.local_progress import PHASES

    runbook = (REPO / "docs" / "runbooks" / "live_progress.md").read_text(encoding="utf-8")
    skill = (REPO / ".agents" / "skills" / "19-run-ticket" / "SKILL.md").read_text(encoding="utf-8")
    for command in ("open", "phase", "finish", "cancel"):
        assert f"live_run.py {command}" in runbook or f"`{command}`" in runbook
    for phase in PHASES:
        assert f"`{phase}`" in runbook and f"`{phase}`" in skill
    assert "provision_live_writer.py" in runbook and "DARKFAC_ADMIN_DATABASE_URL" in runbook
    assert "scripts/live_run.py" in skill and "docs/runbooks/live_progress.md" in skill
    assert (REPO / ".claude" / "skills" / "19-run-ticket" / "SKILL.md").read_text(encoding="utf-8") == skill


def test_runbook_documents_that_an_unpublished_closing_keeps_the_state() -> None:
    runbook = (REPO / "docs" / "runbooks" / "live_progress.md").read_text(encoding="utf-8")
    assert "mantem o estado" in runbook and "state_kept" in runbook and "USR-202" in runbook


# ---------------------------------------------------------------- the script entry point


def test_the_script_runs_from_any_working_directory_and_exits_zero(tmp_path: Path) -> None:
    script = REPO / "scripts" / "live_run.py"
    assert script.is_file()
    env = {
        **__import__("os").environ,
        ENV_SWITCH: "off",
        "DARKFAC_STATE_ROOT": str(tmp_path / "state"),
        "PYTHONIOENCODING": "utf-8",
    }
    done = subprocess.run(
        [sys.executable, str(script), "open", "--ticket", "USR-164", "--json"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["ticket_id"] == "USR-164"
