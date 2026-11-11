"""Negacao ao editar o motor entra na via expressa (USR-494 T2).

Self-Modification, ou permission denied num caminho do motor, grava a causa
``self_modification`` e a tag ``line-block`` uma vez. ``protected_path`` e
permission denied de banco ou ACL ficam de fora. O run nao relanca a edicao.
``classify_log_cause`` le a causa na linha oficial, nao na cauda do log.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import run_ticket
from core.demands.models import TAG_LINE_BLOCK, UserTicket
from core.demands.queue_store import get_ticket, register_ticket
from core.line import agent_cli, agent_retry
from core.line.admission_store import TICKET_CAUSES, AdmissionStore, normalize_error
from core.line.agent_cli import AgentRequest, AgentResult, _classify_error, run_agent
from core.line.agent_retry import RetryReport, run_with_retry
from core.line.express_lane import (
    CAUSE_SELF_MODIFICATION,
    ExpressRepeatAlert,
    HostGap,
    cancelled_edit_kind,
    classify_engine_denial,
    collect_host_gaps,
    main as express_main,
    notify_repeated_self_modification,
    official_cause_line,
    quota_floor_gap,
    record_self_modification,
    register_express_owner_action,
    run_express,
    transcript_denial,
)
from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
from core.owner_actions.store import OwnerActionStore
from core.line.fleet_dispatcher import DispatcherConfig, FleetDispatcher, InFlight, classify_log_cause
from core.line.local_progress import ProgressPublisher
from core.line.protected_edit import grok_session_dirname
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage
from core.projects.models import ProjectCommands, ProjectDescriptor

_TICKET = "USR-9494"
_OTHER = "USR-9495"
_ENGINE_TEXT = "permission denied: search_replace core/line/fleet_dispatcher.py"


@pytest.fixture
def control_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Control store isolado. Nada aqui grava o ``control.db`` do checkout."""
    state = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKFAC_DEMANDS_PATH", raising=False)
    from core.demands import queue_store

    queue_store._store.cache_clear()
    yield state
    queue_store._store.cache_clear()


def _ticket(ticket_id: str = _TICKET, **overrides: Any) -> UserTicket:
    payload: dict[str, Any] = {
        "id": ticket_id,
        "title": "Motor denial",
        "problem_statement": "o chat negou a edicao do motor",
    }
    payload.update(overrides)
    return UserTicket(**payload)


class _MemoryPort:
    def __init__(self, ticket: UserTicket | None, *, boom: bool = False) -> None:
        self.ticket = ticket
        self.boom = boom
        self.saves = 0

    def get(self, ticket_id: str) -> UserTicket | None:
        if self.ticket is None or self.ticket.id != ticket_id:
            return None
        return self.ticket

    def save(self, ticket: UserTicket) -> None:
        self.saves += 1
        if self.boom:
            raise RuntimeError("store offline")
        self.ticket = ticket


class _Agent:
    def __init__(self, script: dict[str, list[AgentResult]]) -> None:
        self.script = {harness: list(results) for harness, results in script.items()}
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        queue = self.script[req.harness]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class _Picker:
    def __init__(self, routes: list[tuple[str, Optional[str]]]) -> None:
        self.routes = routes
        self.calls: list[set] = []

    def __call__(self, stage, caps, config=None, exclude=(), mode=None, **kwargs):
        self.calls.append(set(exclude))
        for route in self.routes:
            if route not in set(exclude):
                return route
        return None


def _fail(kind: str, harness: str, text: str = "") -> AgentResult:
    return AgentResult(
        ok=False, text=text, harness=harness, duration_s=0.2, error_kind=kind,  # type: ignore[arg-type]
        exit_code=1,
    )


def _run(agent: _Agent, picker: _Picker, **kwargs: Any) -> RetryReport:
    return run_with_retry(
        lambda harness, model: AgentRequest(
            prompt="implement", cwd=Path("."), mode="write", harness=harness, model=model,
        ),
        ("grok", None),
        host_caps=["harness:grok", "harness:claude"],
        run_func=agent,
        pick_func=picker,
        sleep_fn=lambda _delay: None,
        record_func=lambda *args, **kw: None,
        transient_retries=0,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Classificacao
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Permission denied: Self-Modification",
        "SELF-MODIFICATION blocked the edit",
        "permission denied writing core/line/fleet_dispatcher.py",
        "permission denied writing core/harness/runner.py",
        "permission denied writing run_ticket.py",
        "permission denied writing scripts/line_validate.py",
        r"permission denied writing C:\dev\DarkFac\core\line\foo.py",
        r"permission denied writing scripts\line_validate.py",
        "permission denied writing ./core/line/foo.py",
        "permission denied writing run_ticket.py:88",
        "permission denied writing scripts/line_validate.py.",
    ],
)
def test_engine_denial_is_self_modification(text: str) -> None:
    assert classify_engine_denial(text) == CAUSE_SELF_MODIFICATION


@pytest.mark.parametrize(
    "text",
    [
        "permission denied writing README.md",
        "permission denied writing core/lineage/foo.py",
        "permission denied writing core/line.py",
        "permission denied writing my_run_ticket.py",
        "permission denied writing run_ticket.py.bak",
        "permission denied writing scripts/line_validate.py.bak",
        "causa: self_modification",
        "Self-Modification mentioned, but permission denied for schema public",
        "permission denied for table local_run_events while editing core/line/foo.py",
        "insufficient privilege on core/harness/runner.py",
        "must be owner of run_ticket.py",
        "ACL denied the edit of core/line/foo.py",
        "error_kind=protected_path Self-Modification on core/line/foo.py",
        "error_kind: protected_path",
        "Vou criar o arquivo.",
    ],
)
def test_protected_database_and_other_paths_stay_out(text: str) -> None:
    assert classify_engine_denial(text) is None
    assert classify_engine_denial(text, error_kind="protected_path") is None


def test_classify_error_keeps_rate_limit_ahead_of_the_motor_denial() -> None:
    assert _classify_error("Permission denied: Self-Modification") == "self_modification"
    assert _classify_error("permission denied writing core/line/fleet_dispatcher.py") == "self_modification"
    assert _classify_error("permission denied for schema public") == "crash"
    assert _classify_error("rate limit exceeded\nSelf-Modification") == "rate_limited"


def _edit(call_id: str, path: str, name: str = "search_replace") -> dict[str, Any]:
    return {
        "type": "assistant",
        "tool_calls": [{
            "id": call_id,
            "name": name,
            "arguments": json.dumps({"file_path": path, "old_string": "a", "new_string": "b"}),
        }],
    }


def _tool_result(call_id: str, content: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_call_id": call_id, "content": content}


def test_transcript_denial_counts_only_an_engine_edit() -> None:
    denied = [
        _edit("e1", "core/harness/runner.py"),
        _tool_result("e1", "permission denied"),
    ]
    assert transcript_denial(denied) == CAUSE_SELF_MODIFICATION

    read = [
        _edit("r1", "core/harness/runner.py", name="read_file"),
        _tool_result("r1", "permission denied"),
    ]
    assert transcript_denial(read) is None

    other = [
        _edit("e2", "README.md"),
        _tool_result("e2", "permission denied"),
    ]
    assert transcript_denial(other) is None

    phrase = [
        _edit("e3", "core/line/fleet_dispatcher.py", name=""),
        _tool_result("e3", "Self-Modification"),
    ]
    assert transcript_denial(phrase) == CAUSE_SELF_MODIFICATION

    database = [
        _edit("e4", "core/line/fleet_dispatcher.py"),
        _tool_result("e4", "permission denied for schema public"),
    ]
    assert transcript_denial(database) is None


def test_cancelled_edit_keeps_protected_path_and_a_plain_cancel() -> None:
    assert cancelled_edit_kind(
        diagnosis_kind="protected_path",
        diagnosis_text="Self-Modification on core/line/foo.py",
    ) == "protected_path"
    assert cancelled_edit_kind(
        diagnosis_kind="crash",
        diagnosis_text="Vou criar o arquivo.",
    ) == "crash"
    assert cancelled_edit_kind(
        diagnosis_kind="crash",
        diagnosis_text="cancelled",
        rows=[
            _edit("e1", "core/line/express_lane.py"),
            _tool_result("e1", "Permission denied: Self-Modification"),
        ],
    ) == "self_modification"


def _short_cwd() -> Path:
    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "C:/Temp") / "df494"
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex[:8]
    path.mkdir()
    return path


def _write_session(root: Path, cwd: Path, rows: list[dict[str, Any]]) -> None:
    directory = root / grok_session_dirname(cwd) / "sess-494"
    directory.mkdir(parents=True)
    (directory / "chat_history.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
    )


class _FakeGrok:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


def test_chat_cancel_on_an_engine_edit_is_self_modification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("DARKFAC_GROK_SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(agent_cli, "REPO_ROOT", tmp_path / "factory")
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: "grok-fake")
    monkeypatch.setattr(
        agent_cli.subprocess, "run",
        _FakeGrok(json.dumps({
            "text": "Vou criar o arquivo.",
            "stopReason": "cancelled",
            "sessionId": "sess-494",
        })),
    )
    cwd = _short_cwd()
    try:
        _write_session(sessions, cwd, [
            _edit("e1", "core/line/fleet_dispatcher.py"),
            _tool_result("e1", "Permission denied: Self-Modification"),
        ])
        result = run_agent(AgentRequest(
            prompt="implemente", cwd=cwd, mode="write", harness="grok", timeout_s=30,
        ))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)

    assert result.ok is False
    assert result.error_kind == "self_modification"


# --------------------------------------------------------------------------
# Gravacao idempotente no control store
# --------------------------------------------------------------------------


def test_recording_marks_line_block_once_and_skips_a_missing_ticket(control_store: Path) -> None:
    register_ticket(_ticket())
    first = record_self_modification(_TICKET, project_id="darkfac")
    stored = get_ticket(_TICKET)
    assert stored is not None
    assert first.written is True
    assert stored.tags.count(TAG_LINE_BLOCK) == 1
    assert stored.express_causes == [CAUSE_SELF_MODIFICATION]
    assert str(getattr(stored.status, "value", stored.status)) == "planned"

    second = record_self_modification(_TICKET, project_id="darkfac")
    again = get_ticket(_TICKET)
    assert again is not None
    assert second.written is False
    assert again.updated_at == stored.updated_at
    assert again.tags.count(TAG_LINE_BLOCK) == 1
    assert again.express_causes == [CAUSE_SELF_MODIFICATION]

    missing = record_self_modification("USR-9498")
    assert missing.written is False and missing.tagged is False
    assert get_ticket("USR-9498") is None


def test_an_already_marked_ticket_is_not_rewritten(control_store: Path) -> None:
    register_ticket(_ticket(tags=["user-demand", TAG_LINE_BLOCK], express_causes=[CAUSE_SELF_MODIFICATION]))
    before = get_ticket(_TICKET)
    mark = record_self_modification(_TICKET)
    after = get_ticket(_TICKET)
    assert before is not None and after is not None
    assert mark.written is False
    assert after.updated_at == before.updated_at
    assert after.express_causes == [CAUSE_SELF_MODIFICATION]


def test_old_ticket_json_loads_without_express_causes() -> None:
    loaded = UserTicket.model_validate({"id": "USR-1", "title": "Old row"})
    assert loaded.express_causes == []
    duplicated = _ticket(express_causes=["self_modification", " self_modification ", ""])
    assert duplicated.express_causes == ["self_modification"]


def test_self_modification_is_a_ticket_cause_only_on_the_official_line() -> None:
    assert CAUSE_SELF_MODIFICATION in TICKET_CAUSES
    assert normalize_error("self_modification") == "self_modification"
    tail = (
        "Self-Modification no fim do log\n"
        '{"ok": false, "cause": "self_modification"}\n'
        '{"cause": "self_modification"}\n'
    )
    assert classify_log_cause(tail) == ""
    official = official_cause_line(_TICKET) + "\n" + tail
    assert classify_log_cause(official) == "self_modification"
    assert classify_log_cause(
        "[ERRO NA ENTREGA] causa: gate_failed; erro: x\n" + tail
    ) == "gate_failed"


def test_tail_only_failures_do_not_hold_as_self_modification(tmp_path: Path) -> None:
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    dispatcher = FleetDispatcher(
        DispatcherConfig(
            hosts=[],
            state_path=tmp_path / "fleet.json",
            recovery_failure_threshold=3,
        ),
        admission_store=store,
        demands_loader=lambda: [],
        express_alert=lambda _alert: True,
    )
    tail = 'Self-Modification\n{"ok": false, "cause": "self_modification"}\n'
    for _ in range(3):
        dispatcher._record_failure(
            InFlight(_TICKET, "grokbox", object()), 1, "failure", tail,
        )
    assert [row["cause"] for row in store.ticket_holds()] != [CAUSE_SELF_MODIFICATION]

    for _ in range(3):
        dispatcher._record_failure(
            InFlight(_OTHER, "grokbox", object()), 1, "failure", official_cause_line(_OTHER) + "\n" + tail,
        )
    held = [row for row in store.ticket_holds() if row["ticket_id"] == _OTHER]
    assert len(held) == 1 and held[0]["cause"] == CAUSE_SELF_MODIFICATION


# --------------------------------------------------------------------------
# O run nao relanca a edicao
# --------------------------------------------------------------------------


def test_retry_loop_does_not_repeat_a_motor_denial() -> None:
    denial = _fail("crash", "grok", text="permission denied writing core/harness/runner.py")
    preset = _fail("self_modification", "grok", text="")
    for result in (denial, preset):
        agent = _Agent({"grok": [result], "claude": [_fail("crash", "claude", text="ok")]})
        picker = _Picker([("grok", None), ("claude", "sonnet")])
        report = _run(agent, picker)
        assert report.ok is False
        assert [call.harness for call in agent.calls] == ["grok"]
        assert picker.calls == []
        assert report.preserve_worktree is False
        assert report.result is not None and report.result.error_kind == result.error_kind


def test_other_permission_denials_still_move_to_the_next_harness() -> None:
    texts = [
        "permission denied writing README.md",
        "permission denied for schema public while editing core/line/fleet_dispatcher.py",
        "error_kind=protected_path Self-Modification",
    ]
    for text in texts:
        agent = _Agent({
            "grok": [_fail("crash", "grok", text=text)],
            "claude": [AgentResult(ok=True, text="done", harness="claude", duration_s=0.1, exit_code=0)],
        })
        picker = _Picker([("grok", None), ("claude", "sonnet")])
        report = _run(agent, picker)
        assert report.ok is True
        assert [call.harness for call in agent.calls] == ["grok", "claude"]


def test_protected_path_still_stops_without_becoming_self_modification() -> None:
    agent = _Agent({
        "grok": [_fail(
            "protected_path", "grok", text="Self-Modification on core/line/express_lane.py",
        )],
        "claude": [AgentResult(ok=True, text="done", harness="claude", duration_s=0.1, exit_code=0)],
    })
    picker = _Picker([("grok", None), ("claude", "sonnet")])
    report = _run(agent, picker)
    assert report.ok is False and report.preserve_worktree is True
    assert [call.harness for call in agent.calls] == ["grok"]
    assert report.result is not None and report.result.error_kind == "protected_path"


def _git(args: list[str], cwd: Path) -> None:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, proc.stderr


def _seed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectDescriptor:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], tmp_path)
    _git(["checkout", "-B", "main"], seed)
    _git(["config", "user.email", "seed@example.com"], seed)
    _git(["config", "user.name", "Seed"], seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    script = seed / "check_status.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\nsys.exit(0 if Path('STATUS_OK').is_file() else 1)\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "seed"], seed)
    _git(["push", "origin", "main"], seed)
    project = ProjectDescriptor(
        id="acme",
        name="Acme",
        repo_url=str(origin),
        default_branch="main",
        commands=ProjectCommands(setup=["python -c \"pass\""], validate=["python check_status.py"]),
    )
    from core.line import workspace as ws_mod

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, "run-494")
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": _TICKET, "title": "Motor denial"}]))
    ws_mod.commit(ws, "planning: tickets", job_key="run-494:planning")
    ws_mod.push(ws)
    return project


def _silence_owner_notify(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.owner_actions.notify.notify_owner_action",
        lambda *args, **kwargs: {"sent": False},
    )


def _commands(action: Any) -> list[str]:
    return [step.command for step in action.steps if step.command]


def test_development_stage_records_the_denial_and_does_not_edit_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control_store: Path,
) -> None:
    monkeypatch.setattr("core.line.base_probe.probe_base_failures", lambda *args, **kwargs: None)
    _silence_owner_notify(monkeypatch)
    actions = tmp_path / "owner_actions.json"
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(actions))
    project = _seed_stage(tmp_path, monkeypatch)
    register_ticket(_ticket())
    calls: list[str] = []

    def blocked(req: AgentRequest) -> AgentResult:
        calls.append(req.harness)
        return AgentResult(
            ok=False,
            text=_ENGINE_TEXT,
            harness=req.harness,
            model=req.model,
            duration_s=0.01,
            error_kind="crash",
        )

    stage = DevelopmentStage(
        run_agent_func=blocked,
        pick_func=lambda *args, **kwargs: ("claude", "sonnet"),
        routing_config=load_routing_config(),
    )
    result = stage.run(project, "run-494")

    assert calls == ["claude"]
    assert result.outcome == "failed"
    assert result.cause_code == CAUSE_SELF_MODIFICATION
    stored = get_ticket(_TICKET)
    assert stored is not None
    assert stored.tags.count(TAG_LINE_BLOCK) == 1
    assert stored.express_causes == [CAUSE_SELF_MODIFICATION]
    opened = OwnerActionStore(actions, remote_ref=None, control=False).list_actions(include_done=False)
    assert len(opened) == 1
    assert _commands(opened[0]) == [f"python -m core.line.express_lane run --ticket {_TICKET}"]
    stamped = stored.updated_at
    again = record_self_modification(_TICKET, project_id=project.id)
    reread = get_ticket(_TICKET)
    assert reread is not None
    assert again.written is False and reread.updated_at == stamped
    still = OwnerActionStore(actions, remote_ref=None, control=False).list_actions(include_done=False)
    assert len(still) == 1 and still[0].id == opened[0].id


def test_stage_stops_even_when_the_store_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.line.base_probe.probe_base_failures", lambda *args, **kwargs: None)
    _silence_owner_notify(monkeypatch)
    actions = tmp_path / "owner_actions.json"
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(actions))
    project = _seed_stage(tmp_path, monkeypatch)
    calls: list[int] = []
    port = _MemoryPort(_ticket(), boom=True)

    def blocked(req: AgentRequest) -> AgentResult:
        calls.append(1)
        return AgentResult(
            ok=False, text=_ENGINE_TEXT, harness=req.harness, duration_s=0.01, error_kind="crash",
        )

    stage = DevelopmentStage(
        run_agent_func=blocked,
        pick_func=lambda *args, **kwargs: ("claude", "sonnet"),
        routing_config=load_routing_config(),
        express_port=port,
    )
    result = stage.run(project, "run-494")

    assert calls == [1]
    assert result.outcome == "failed" and result.cause_code == CAUSE_SELF_MODIFICATION
    assert port.saves == 1 and port.ticket is not None
    assert TAG_LINE_BLOCK not in port.ticket.tags
    opened = OwnerActionStore(actions, remote_ref=None, control=False).list_actions(include_done=False)
    assert len(opened) == 1
    assert _commands(opened[0]) == [f"python -m core.line.express_lane run --ticket {_TICKET}"]


def test_finish_prints_the_official_line_and_marks_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control_store: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    register_ticket(_ticket())
    _silence_owner_notify(monkeypatch)
    actions = tmp_path / "owner_actions.json"
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(actions))
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: {"grok": {"headroom": 68.2}})
    result = AgentResult(
        ok=False, text=_ENGINE_TEXT, harness="grok", duration_s=1.0, error_kind="self_modification",
    )
    report = agent_retry.RetryReport(
        ok=False,
        result=result,
        route=("grok", None),
        attempts=[agent_retry.AgentAttempt.from_result(1, result)],
    )
    workspace = SimpleNamespace(path=tmp_path / "wt", branch="ticket/usr-9494")
    progress = ProgressPublisher(ticket_id=_TICKET, sink=None)
    code = run_ticket._finish_self_modification(
        progress,
        get_ticket(_TICKET),  # type: ignore[arg-type]
        workspace,  # type: ignore[arg-type]
        report,
        as_json=True,
        harness="grok",
        before_headroom=68.2,
        ticket_size="M",
    )
    captured = capsys.readouterr()
    assert code == 1
    assert official_cause_line(_TICKET) in captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is False and payload["cause"] == CAUSE_SELF_MODIFICATION
    assert classify_log_cause(captured.out) == ""
    assert classify_log_cause(captured.err) == CAUSE_SELF_MODIFICATION
    stored = get_ticket(_TICKET)
    assert stored is not None and stored.express_causes == [CAUSE_SELF_MODIFICATION]
    stamped = stored.updated_at

    code_again = run_ticket._finish_self_modification(
        progress,
        stored,
        workspace,  # type: ignore[arg-type]
        report,
        as_json=False,
        harness="grok",
        before_headroom=68.2,
        ticket_size="M",
    )
    captured_again = capsys.readouterr()
    assert code_again == 1
    assert official_cause_line(_TICKET) in captured_again.err
    reread = get_ticket(_TICKET)
    assert reread is not None
    assert reread.updated_at == stamped
    assert reread.tags.count(TAG_LINE_BLOCK) == 1
    assert reread.express_causes == [CAUSE_SELF_MODIFICATION]
    assert str(getattr(reread.status, "value", reread.status)) == "planned"
    opened = OwnerActionStore(actions, remote_ref=None, control=False).list_actions(include_done=False)
    assert len(opened) == 1
    assert opened[0].status.value == "open"
    assert _commands(opened[0]) == [f"python -m core.line.express_lane run --ticket {_TICKET}"]


# --------------------------------------------------------------------------
# Comando unico que valida o host (USR-494 T3)
# --------------------------------------------------------------------------


def _owner_store(tmp_path: Path) -> OwnerActionStore:
    return OwnerActionStore(tmp_path / "owner_actions.json", remote_ref=None, control=False)


def _spawn_recorder() -> tuple[list[list[str]], Any]:
    spawned: list[list[str]] = []

    def spawn(argv: list[str]) -> int:
        spawned.append(list(argv))
        return 0

    return spawned, spawn


def test_owner_action_has_one_command_and_repeat_returns_the_same(tmp_path: Path) -> None:
    store = _owner_store(tmp_path)
    notes: list[str] = []
    command = f"python -m core.line.express_lane run --ticket {_TICKET}"
    first = register_express_owner_action(
        _TICKET, store=store, notifier=lambda action: notes.append(action.id),
    )
    second = register_express_owner_action(
        _TICKET, store=store, notifier=lambda action: notes.append(action.id),
    )

    assert first.id == second.id
    assert first.status.value == "open"
    assert notes == [first.id]
    assert _commands(first) == [command]
    assert len(first.steps) == 1
    assert len(store.list_actions()) == 1
    reloaded = OwnerActionStore(store.path, remote_ref=None, control=False).get(first.id)
    assert reloaded is not None
    assert _commands(reloaded) == [command]


def test_two_missing_requirements_are_printed_and_the_ticket_stays_put(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def git_gap() -> HostGap:
        seen.append("git")
        return HostGap("git", "git ausente no PATH")

    def quota_gap() -> HostGap:
        seen.append("quota_floor")
        return HostGap("quota_floor", "nenhum harness acima do piso de 15%")

    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[git_gap, quota_gap],
        running=set(),
        spawn=spawn,
        host_id="notebook",
    )
    out = capsys.readouterr().out

    assert code == 1
    assert spawned == []
    assert seen == ["git", "quota_floor"]
    assert "requisito ausente: git: git ausente no PATH" in out
    assert "requisito ausente: quota_floor: nenhum harness acima do piso de 15%" in out
    assert "lancado:" not in out


def test_missing_quota_floor_alone_does_not_launch(capsys: pytest.CaptureFixture[str]) -> None:
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[lambda: HostGap("quota_floor", "nenhum harness acima do piso de 15%")],
        running=set(),
        spawn=spawn,
    )
    out = capsys.readouterr().out

    assert code == 1
    assert spawned == []
    assert "requisito ausente: quota_floor:" in out
    assert "lancado:" not in out


def test_quota_floor_probe_joins_the_gap_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.line.feeder_cycle.default_quota_gate",
        lambda: (False, "quota_floor: no harness above the critical floor"),
    )
    gaps = collect_host_gaps([quota_floor_gap])
    assert [gap.code for gap in gaps] == ["quota_floor"]
    assert "15" in gaps[0].detail or "floor" in gaps[0].detail or "harness" in gaps[0].detail


def test_a_broken_probe_does_not_hide_the_quota_floor(capsys: pytest.CaptureFixture[str]) -> None:
    def boom() -> HostGap | None:
        raise RuntimeError("disk")

    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[boom, lambda: HostGap("quota_floor", "nenhum harness acima do piso de 15%")],
        running=set(),
        spawn=spawn,
    )
    out = capsys.readouterr().out

    assert code == 1
    assert spawned == []
    assert "requisito ausente: host_check:" in out
    assert "requisito ausente: quota_floor:" in out


def test_valid_host_launches_run_ticket_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = tmp_path / "run_ticket.py"
    script.write_text("# launcher\n", encoding="utf-8")
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[],
        host_id="notebook",
        running=set(),
        spawn=spawn,
        run_ticket=script,
    )
    out = capsys.readouterr().out

    assert code == 0
    assert spawned == [[sys.executable, str(script), _TICKET, "--fleet", "--host-id", "notebook"]]
    assert f"lancado: run_ticket.py {_TICKET} --fleet --host-id notebook" in out


def test_valid_host_uses_this_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.line.line_lock.resolve_host_id", lambda host_id=None: "desktop")
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[],
        running=set(),
        spawn=spawn,
        run_ticket=Path("run_ticket.py"),
    )

    assert code == 0
    assert spawned[0][2:] == [_TICKET, "--fleet", "--host-id", "desktop"]
    assert len(spawned) == 1
    assert len(spawned[0]) == 6


@pytest.mark.parametrize("status", ["implementing", "validating"])
def test_in_flight_ticket_succeeds_without_a_new_process(
    status: str, capsys: pytest.CaptureFixture[str],
) -> None:
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket(status=status)),
        checks=[lambda: HostGap("quota_floor", "ausente")],
        running=set(),
        spawn=spawn,
        host_id="notebook",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert spawned == []
    assert f"ticket {_TICKET} ja em voo" in out
    assert "requisito ausente" not in out


def test_a_live_process_counts_as_in_flight(capsys: pytest.CaptureFixture[str]) -> None:
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket()),
        checks=[],
        running={_TICKET},
        spawn=spawn,
        host_id="notebook",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert spawned == []
    assert "ja em voo" in out


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_ticket_succeeds_without_a_new_process(
    status: str, capsys: pytest.CaptureFixture[str],
) -> None:
    spawned, spawn = _spawn_recorder()
    code = run_express(
        _TICKET,
        port=_MemoryPort(_ticket(status=status)),
        checks=[],
        running={_TICKET},
        spawn=spawn,
        host_id="notebook",
    )
    out = capsys.readouterr().out

    assert code == 0
    assert spawned == []
    assert f"ticket {_TICKET} ja terminal ({status})" in out


def test_missing_ticket_is_listed_and_not_launched(capsys: pytest.CaptureFixture[str]) -> None:
    spawned, spawn = _spawn_recorder()
    code = run_express(
        "USR-9498",
        port=_MemoryPort(None),
        checks=[lambda: HostGap("git", "git ausente no PATH")],
        running=set(),
        spawn=spawn,
    )
    out = capsys.readouterr().out

    assert code == 1
    assert spawned == []
    assert "requisito ausente: git:" in out
    assert "requisito ausente: ticket:" in out


def test_cli_run_delegates_the_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake(ticket_id: str, **kwargs: Any) -> int:
        seen["id"] = ticket_id
        return 0

    monkeypatch.setattr("core.line.express_lane.run_express", fake)
    assert express_main(["run", "--ticket", _TICKET]) == 0
    assert seen["id"] == _TICKET


# --------------------------------------------------------------------------
# Alerta unico da falha repetida (USR-494 T4)
# --------------------------------------------------------------------------


_EPISODE = "express_episodes.json"


class _Exit:
    def __init__(self, rc: int) -> None:
        self._rc = rc

    def poll(self) -> int:
        return self._rc


class _AlertService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def notify(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(notification_id="notif-494")


def _episode_key(ticket_id: str, cause: str = CAUSE_SELF_MODIFICATION) -> str:
    return f"{ticket_id}|{cause}"


def _episodes(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / _EPISODE
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _self_mod_log(ticket_id: str, n: int) -> str:
    return official_cause_line(ticket_id) + f"\ncauda {ticket_id} #{n} self_modification\n"


def _record_cause(dispatcher: FleetDispatcher, ticket_id: str, n: int) -> None:
    dispatcher._record_failure(
        InFlight(ticket_id, "grokbox", object()),
        1,
        "failure",
        _self_mod_log(ticket_id, n),
    )


def _lane_dispatcher(
    tmp_path: Path,
    alerts: list[ExpressRepeatAlert],
    *,
    threshold: int = 3,
    use_default: bool = False,
) -> tuple[FleetDispatcher, AdmissionStore]:
    store = AdmissionStore(tmp_path / "admission.sqlite3")
    notify = None if use_default else (lambda alert: alerts.append(alert) or True)
    dispatcher = FleetDispatcher(
        DispatcherConfig(
            hosts=[],
            state_path=tmp_path / "fleet.json",
            recovery_failure_threshold=threshold,
        ),
        admission_store=store,
        demands_loader=lambda: [],
        express_alert=notify,
    )
    return dispatcher, store


def _deliver(dispatcher: FleetDispatcher, ticket_id: str, tmp_path: Path) -> None:
    log = tmp_path / f"{ticket_id}-delivery.log"
    log.write_text("[DELIVERY_MERGED]\n", encoding="utf-8")
    dispatcher.in_flight["grokbox"] = InFlight(
        ticket_id, "grokbox", _Exit(0), log_path=log,
    )
    dispatcher._reap()


def test_the_second_occurrence_alerts_once_and_a_restart_stays_quiet(tmp_path: Path) -> None:
    alerts: list[ExpressRepeatAlert] = []
    dispatcher, _store = _lane_dispatcher(tmp_path, alerts)
    command = f"python -m core.line.express_lane run --ticket {_TICKET}"

    _record_cause(dispatcher, _TICKET, 1)
    stored = _episodes(tmp_path)
    assert alerts == []
    assert stored[_episode_key(_TICKET)]["occurrences"] == 1
    assert stored[_episode_key(_TICKET)]["alerted"] is False
    assert "cauda" not in (tmp_path / _EPISODE).read_text(encoding="utf-8")

    _record_cause(dispatcher, _TICKET, 2)
    assert len(alerts) == 1
    assert alerts[0].ticket_id == _TICKET
    assert alerts[0].cause == CAUSE_SELF_MODIFICATION
    assert alerts[0].command == command
    assert _episodes(tmp_path)[_episode_key(_TICKET)]["occurrences"] == 2
    assert _episodes(tmp_path)[_episode_key(_TICKET)]["alerted"] is True

    later: list[ExpressRepeatAlert] = []
    restarted, _again = _lane_dispatcher(tmp_path, later)
    _record_cause(restarted, _TICKET, 3)
    assert later == []
    assert len(alerts) == 1
    assert _episodes(tmp_path)[_episode_key(_TICKET)]["occurrences"] == 3
    assert _episodes(tmp_path)[_episode_key(_TICKET)]["alerted"] is True


def test_a_failed_second_send_does_not_alert_on_the_following_occurrence(tmp_path: Path) -> None:
    seen: list[ExpressRepeatAlert] = []

    def refuse(alert: ExpressRepeatAlert) -> bool:
        seen.append(alert)
        return False

    dispatcher, _store = _lane_dispatcher(tmp_path, [], use_default=True)
    dispatcher.express_alert = refuse
    for n in (1, 2, 3):
        _record_cause(dispatcher, _TICKET, n)

    assert len(seen) == 1
    assert seen[0].cause == CAUSE_SELF_MODIFICATION
    assert _episodes(tmp_path)[_episode_key(_TICKET)]["alerted"] is False


def test_success_or_another_cause_closes_the_episode(tmp_path: Path) -> None:
    alerts: list[ExpressRepeatAlert] = []
    dispatcher, _store = _lane_dispatcher(tmp_path, alerts)
    key = _episode_key(_TICKET)

    _record_cause(dispatcher, _TICKET, 1)
    dispatcher._record_failure(
        InFlight(_TICKET, "grokbox", object()),
        1,
        "failure",
        "[ERRO NA ENTREGA] causa: gate_failed; erro: outra\nSelf-Modification na cauda\n",
    )
    assert alerts == []
    assert key not in _episodes(tmp_path)

    _record_cause(dispatcher, _TICKET, 2)
    dispatcher._record_failure(
        InFlight(_OTHER, "notebook", object()),
        1,
        "failure",
        "[ERRO NA ENTREGA] causa: gate_failed; erro: outro ticket\n",
    )
    assert alerts == []
    assert _episodes(tmp_path)[key]["occurrences"] == 1

    _record_cause(dispatcher, _TICKET, 3)
    assert len(alerts) == 1
    assert alerts[0].command == f"python -m core.line.express_lane run --ticket {_TICKET}"

    _deliver(dispatcher, _TICKET, tmp_path)
    assert key not in _episodes(tmp_path)
    assert not dispatcher.recovery.get("active")

    _record_cause(dispatcher, _TICKET, 4)
    _record_cause(dispatcher, _TICKET, 5)
    assert len(alerts) == 2
    assert alerts[1].cause == CAUSE_SELF_MODIFICATION
    assert alerts[1].command.endswith(_TICKET)


def test_the_log_tail_does_not_open_or_count_an_episode(tmp_path: Path) -> None:
    alerts: list[ExpressRepeatAlert] = []
    dispatcher, _store = _lane_dispatcher(tmp_path, alerts)
    tail = 'Self-Modification\n{"ok": false, "cause": "self_modification"}\n'
    for _ in range(3):
        dispatcher._record_failure(
            InFlight(_TICKET, "grokbox", object()), 1, "failure", tail,
        )
    assert alerts == []
    assert not (tmp_path / _EPISODE).exists()

    _record_cause(dispatcher, _TICKET, 1)
    dispatcher._record_failure(
        InFlight(_TICKET, "grokbox", object()), 1, "failure", tail,
    )
    stored = _episodes(tmp_path)
    assert list(stored) == [_episode_key(_TICKET)]
    assert stored[_episode_key(_TICKET)]["occurrences"] == 1
    assert alerts == []

    _record_cause(dispatcher, _OTHER, 1)
    _record_cause(dispatcher, _OTHER, 2)
    assert [item.ticket_id for item in alerts] == [_OTHER]
    assert set(stored_keys := _episodes(tmp_path)) == {
        _episode_key(_TICKET),
        _episode_key(_OTHER),
    }
    assert stored_keys[_episode_key(_TICKET)]["occurrences"] == 1

    _record_cause(dispatcher, _TICKET, 2)
    assert [item.ticket_id for item in alerts] == [_OTHER, _TICKET]
    assert "cauda" not in (tmp_path / _EPISODE).read_text(encoding="utf-8")


def test_the_alert_leaves_global_recovery_off_and_keeps_the_hold_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts: list[ExpressRepeatAlert] = []
    dispatcher, store = _lane_dispatcher(tmp_path, alerts, threshold=3)

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("recovery global")

    monkeypatch.setattr(store, "set_recovery", refuse)
    _record_cause(dispatcher, _TICKET, 1)
    _record_cause(dispatcher, _TICKET, 2)

    assert len(alerts) == 1
    assert store.ticket_holds() == []
    assert not store.recovery_state()["active"]
    assert not dispatcher.recovery.get("active")
    assert dispatcher.config.recovery_failure_threshold == 3

    _record_cause(dispatcher, _TICKET, 3)
    held = store.ticket_holds()
    assert len(alerts) == 1
    assert len(held) == 1
    assert held[0]["ticket_id"] == _TICKET
    assert held[0]["cause"] == CAUSE_SELF_MODIFICATION
    assert held[0]["occurrences"] == 3
    assert not store.recovery_state()["active"]
    assert not dispatcher.recovery.get("active")


def test_repeat_alert_names_the_cause_and_the_command_on_channel_all() -> None:
    service = _AlertService()
    command = f"python -m core.line.express_lane run --ticket {_TICKET}"
    alert = ExpressRepeatAlert(
        ticket_id=_TICKET, cause=CAUSE_SELF_MODIFICATION, command=command,
    )
    assert notify_repeated_self_modification(alert, service=service) is True
    sent = service.calls[0]
    assert sent["channel"] == NotificationChannel.ALL
    assert sent["category"] == AlertCategory.SYSTEM_HEALTH
    assert sent["severity"] == AlertSeverity.CRITICAL
    assert CAUSE_SELF_MODIFICATION in sent["message"]
    assert command in sent["message"]
    assert sent["details"]["cause"] == CAUSE_SELF_MODIFICATION
    assert sent["details"]["command"] == command


def test_dispatcher_sends_the_default_alert_on_the_second_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ExpressRepeatAlert] = []

    def fake(alert: ExpressRepeatAlert) -> bool:
        seen.append(alert)
        return True

    monkeypatch.setattr("core.line.express_lane.notify_repeated_self_modification", fake)
    dispatcher, _store = _lane_dispatcher(tmp_path, [], use_default=True)
    _record_cause(dispatcher, _TICKET, 1)
    assert seen == []
    _record_cause(dispatcher, _TICKET, 2)
    assert len(seen) == 1
    assert seen[0].cause == CAUSE_SELF_MODIFICATION
    assert seen[0].command == f"python -m core.line.express_lane run --ticket {_TICKET}"
    _record_cause(dispatcher, _TICKET, 3)
    assert len(seen) == 1
