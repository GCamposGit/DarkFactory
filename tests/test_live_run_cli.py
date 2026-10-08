"""USR-164: interactive Claude Code sessions publish their ticket progress to the live line board.

``scripts/live_run.py`` (``core.line.live_run``) is the thin, stable CLI a chat session calls to open,
advance, finish or cancel one local run per ticket. Everything here is hermetic: a recording sink or a
scratch SQLite control store, a scratch state directory, no network and no real database.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line import live_run
from core.line.local_progress import ENV_RUN_ID, ENV_SWITCH, ENV_WARNED, LocalRunEvent, SqliteSink
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
