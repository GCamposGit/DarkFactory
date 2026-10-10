"""A cancelled edit of a guard-protected path is a human block, not a transient crash (USR-205).

The fake Grok CLI prints ``stopReason=cancelled`` after ``search_replace`` on
``core/harness/remote_worker.py``. The launcher must try once, classify
``protected_path``, keep the worktree and register one owner action.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import run_ticket
from core.git.autonomy import GitAutonomyManager
from core.git.ticket_workspace import TicketWorkspace
from core.line import agent_cli, agent_retry, routing
from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.agent_retry import backoff_delay, run_with_retry
from core.line.protected_edit import (
    diagnose_grok_cancellation,
    grok_session_dirname,
    register_protected_path_owner_action,
)
from core.line.routing import _HARNESS_TO_PROVIDER
from core.orchestrator.guard import PROTECTED_PATTERNS
from core.owner_actions.store import OwnerActionStore

_SESSION = "sess-protected-1"
_PROTECTED = "core/harness/remote_worker.py"
_TICKET = run_ticket.UserTicket(
    id="USR-99",
    project_id="darkfac",
    title="Ticket de teste",
    problem_statement="problema de teste",
)


def _edit_call(call_id: str, path: str, name: str = "search_replace") -> dict[str, Any]:
    return {
        "id": call_id,
        "name": name,
        "arguments": json.dumps({"file_path": path, "old_string": "a", "new_string": "b"}),
    }


def _assistant(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "content": "Vou editar.", "tool_calls": list(calls)}


def _result(call_id: str, content: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_call_id": call_id, "content": content}


def _cancelled(call_id: str, *, earlier: bool = False) -> dict[str, Any]:
    if earlier:
        content = "Tool execution cancelled due to earlier user cancellation for tool `search_replace`"
    else:
        content = "User cancelled the execution for tool `search_replace`"
    return _result(call_id, content)


def _protected_rows(cwd: Path) -> list[dict[str, Any]]:
    target = str(cwd / "core" / "harness" / "remote_worker.py")
    ordinary = str(cwd / "tests" / "test_remote_dispatch.py")
    return [
        _assistant(_edit_call("read-1", target, name="read_file")),
        _result("read-1", "1→def _terminate_process_tree"),
        _assistant(
            _edit_call("edit-1", target),
            _edit_call("edit-2", ordinary),
        ),
        _cancelled("edit-1"),
        _cancelled("edit-2", earlier=True),
    ]


def _write_session(root: Path, cwd: Path, rows: list[dict[str, Any]], session_id: str = _SESSION) -> None:
    directory = root / grok_session_dirname(cwd) / session_id
    directory.mkdir(parents=True)
    history = directory / "chat_history.jsonl"
    history.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _short_cwd() -> Path:
    """A cwd whose ``quote(cwd)`` fits in one Windows path component (255).

    Pytest's ``tmp_path`` is already long. Grok names the session folder with
    that quoted cwd, so a deep temp path makes the folder name illegal.
    """

    base = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "C:/Temp") / "df205"
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex[:8]
    path.mkdir()
    return path


def _grok_stdout(text: str = "Nao consegui editar o arquivo.") -> str:
    return json.dumps({"text": text, "stopReason": "cancelled", "sessionId": _SESSION})


class _FakeGrok:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[list[str]] = []
        self.prompts: list[str] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "--prompt-file" in argv:
            prompt_path = Path(argv[argv.index("--prompt-file") + 1])
            self.prompts.append(prompt_path.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(argv, 0, self.stdout, "")


def _install_grok(monkeypatch: pytest.MonkeyPatch, stdout: str, factory_root: Path) -> _FakeGrok:
    fake = _FakeGrok(stdout)
    monkeypatch.setattr(agent_cli, "REPO_ROOT", factory_root)
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: "grok-fake")
    monkeypatch.setattr(agent_cli.subprocess, "run", fake)
    return fake


def _quotas() -> dict[str, dict[str, Any]]:
    quotas: dict[str, dict[str, Any]] = {}
    for harness, provider in _HARNESS_TO_PROVIDER.items():
        quotas[harness] = {
            "provider": provider,
            "headroom": 98.7,
            "is_critical": False,
            "status": "SAUDAVEL",
        }
    return quotas


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_a_cancelled_search_replace_on_a_protected_path_is_protected_path(tmp_path: Path) -> None:
    rows = _protected_rows(tmp_path)
    diagnosis = diagnose_grok_cancellation(
        cwd=tmp_path, session_id=_SESSION, permission_mode="auto", agent_text="parcial", rows=rows,
    )

    assert diagnosis.error_kind == "protected_path"
    assert diagnosis.protected_path == _PROTECTED
    assert _PROTECTED in diagnosis.text and "protected_path" in diagnosis.text and "parcial" in diagnosis.text
    assert "test_remote_dispatch.py" not in diagnosis.protected_path


def test_a_cancelled_edit_of_an_ordinary_file_stays_a_crash(tmp_path: Path) -> None:
    rows = [
        _assistant(_edit_call("edit-1", str(tmp_path / "docs" / "foo.py"))),
        _cancelled("edit-1"),
    ]
    diagnosis = diagnose_grok_cancellation(
        cwd=tmp_path, session_id=_SESSION, permission_mode="acceptEdits", agent_text="Vou criar o arquivo.", rows=rows,
    )

    assert diagnosis.error_kind == "crash" and diagnosis.protected_path is None
    assert "acceptEdits" in diagnosis.text and "Vou criar o arquivo." in diagnosis.text


def test_reading_a_protected_file_does_not_count_as_an_edit(tmp_path: Path) -> None:
    target = str(tmp_path / "core" / "harness" / "remote_worker.py")
    rows = [
        _assistant(_edit_call("read-1", target, name="read_file")),
        _result("read-1", "User cancelled the execution for tool `read_file`"),
    ]
    diagnosis = diagnose_grok_cancellation(
        cwd=tmp_path, session_id=None, permission_mode="auto", agent_text="", rows=rows,
    )

    assert diagnosis.error_kind == "crash"


def test_run_agent_reads_the_session_of_the_cancelled_protected_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("DARKFAC_GROK_SESSIONS_DIR", str(sessions))
    cwd = _short_cwd()
    try:
        _write_session(sessions, cwd, _protected_rows(cwd))
        _install_grok(monkeypatch, _grok_stdout(), tmp_path / "factory")

        result = run_agent(AgentRequest(prompt="implemente", cwd=cwd, mode="write", harness="grok", timeout_s=30))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)

    assert result.ok is False
    assert result.error_kind == "protected_path"
    assert result.protected_path == _PROTECTED
    assert _PROTECTED in result.text


def test_run_agent_keeps_a_crash_when_the_cancelled_edit_is_not_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("DARKFAC_GROK_SESSIONS_DIR", str(sessions))
    cwd = _short_cwd()
    try:
        rows = [
            _assistant(_edit_call("edit-1", str(cwd / "src" / "app.py"))),
            _cancelled("edit-1"),
        ]
        _write_session(sessions, cwd, rows)
        _install_grok(monkeypatch, _grok_stdout("Vou criar o arquivo."), tmp_path / "factory")

        result = run_agent(AgentRequest(prompt="implemente", cwd=cwd, mode="write", harness="grok", timeout_s=30))
    finally:
        shutil.rmtree(cwd, ignore_errors=True)

    assert result.error_kind == "crash" and result.protected_path is None


def test_the_development_preamble_lists_protected_patterns_and_proposals() -> None:
    text = agent_cli.headless_development_preamble("grok", 98.7)

    assert "harness grok" in text.splitlines()[0]
    assert "docs/proposals" in text
    assert "USR-141-147-harness-resilience.md" in text
    for pattern in PROTECTED_PATTERNS:
        assert pattern in text


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


def test_protected_path_stops_on_the_first_attempt_and_does_not_change_harness() -> None:
    calls: list[str] = []

    def run_func(req: AgentRequest) -> AgentResult:
        calls.append(req.harness)
        return AgentResult(
            ok=False,
            text=f"edit of protected path {_PROTECTED} was cancelled",
            harness=req.harness,
            duration_s=1.0,
            error_kind="protected_path",
            protected_path=_PROTECTED,
        )

    def pick(*_args: Any, **_kwargs: Any) -> tuple[str, Optional[str]]:
        raise AssertionError("must not pick another harness")

    sleeps: list[float] = []
    report = run_with_retry(
        lambda harness, model: AgentRequest(prompt="x", cwd=Path("."), mode="write", harness=harness, model=model),
        ("grok", None),
        host_caps=["harness:claude", "harness:codex", "harness:grok"],
        run_func=run_func,
        pick_func=pick,
        sleep_fn=sleeps.append,
        record_func=lambda *_a, **_k: None,
        pinned=False,
    )

    assert calls == ["grok"]
    assert sleeps == []
    assert report.ok is False and report.preserve_worktree is True
    assert report.result is not None and report.result.error_kind == "protected_path"
    assert len(report.attempts) == 1
    assert backoff_delay(report.result, 0) is None


def test_owner_action_is_idempotent_and_tells_the_owner_not_to_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "owner_actions.json"
    store = OwnerActionStore(path)
    notes: list[str] = []
    monkeypatch.setattr(
        "core.owner_actions.notify.notify_owner_action",
        lambda action, **_k: notes.append(action.id) or {"sent": False},
    )
    workspace = tmp_path / "worktree"
    checkout = tmp_path / "main"

    first = register_protected_path_owner_action(
        ticket_id="USR-99",
        protected_path=_PROTECTED,
        workspace_path=workspace,
        checkout_root=checkout,
        store=store,
    )
    second = register_protected_path_owner_action(
        ticket_id="USR-99",
        protected_path=_PROTECTED,
        workspace_path=workspace,
        checkout_root=checkout,
        store=store,
    )

    assert first.id == second.id == "OA-001"
    assert notes == ["OA-001"]  # the repeat does not notify again
    record = json.loads(path.read_text(encoding="utf-8"))["actions"][0]
    assert record["priority"] == "high"
    assert record["blocks"] == ["USR-99"]
    assert _PROTECTED in record["title"] and _PROTECTED in record["why"]
    steps = "\n".join(
        f"{step.get('text', '')}\n{step.get('command') or ''}" for step in record["steps"]
    )
    blob = steps + "\n" + record["why"] + "\n" + record["verify"]
    assert "docs/proposals" in blob
    assert "USR-141-147-harness-resilience.md" in blob
    assert str(workspace) in steps and str(checkout) in steps
    assert "sem editar" in blob and "Nao peca" in blob


# --------------------------------------------------------------------------
# Launcher: one fake Grok run, one owner action, worktree kept
# --------------------------------------------------------------------------


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str):
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self):
        return [_TICKET]


def test_run_ticket_treats_one_cancelled_protected_edit_as_a_human_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Focal: stopReason=cancelled after editing core/harness/remote_worker.py, once, with an OA."""
    sessions = tmp_path / "sessions"
    actions = tmp_path / "owner_actions.json"
    monkeypatch.setenv("DARKFAC_GROK_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(actions))
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", _quotas)
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    sleeps: list[float] = []
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=sleeps.append))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *_a, **_k: (True, []))
    monkeypatch.setattr(
        run_ticket,
        "record_ticket_quota_cost",
        lambda **kwargs: {
            "ticket_id": kwargs["ticket_id"],
            "harness": kwargs["harness"],
            "before_headroom": kwargs.get("before_headroom"),
            "after_headroom": kwargs.get("after_headroom"),
            "delta_headroom": 0.0,
            "duration_s": kwargs.get("duration_s", 0.0),
        },
    )
    monkeypatch.setattr(
        "core.owner_actions.notify.notify_owner_action",
        lambda *_a, **_k: {"sent": False},
    )

    workspace_dir = _short_cwd()
    kept = workspace_dir / "src" / "kept.py"
    kept.parent.mkdir()
    kept.write_text("x = 1\n", encoding="utf-8")
    workspace = TicketWorkspace(
        ticket_id=_TICKET.id,
        path=workspace_dir,
        branch="ticket/usr-99",
        main_root=tmp_path / "main",
        base_ref="origin/main",
        base_sha="0" * 40,
    )
    monkeypatch.setattr(run_ticket, "_prepare_workspace", lambda _args, _ticket: workspace)
    monkeypatch.setattr(GitAutonomyManager, "changed_paths", lambda self, cwd: ["src/kept.py"])

    def _discard(*_a: Any, **_k: Any) -> bool:
        raise AssertionError("the worktree must stay after a protected-path block")

    monkeypatch.setattr(run_ticket, "_discard_untouched_workspace", _discard)
    _write_session(sessions, workspace_dir, _protected_rows(workspace_dir))
    fake = _install_grok(monkeypatch, _grok_stdout(), tmp_path / "factory")

    try:
        exit_code = run_ticket.main(["USR-99", "--harness", "grok", "--skip-validation", "--no-commit"])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert fake.calls and len(fake.calls) == 1  # a single Grok process, no retry
        assert sleeps == []
        assert fake.prompts and "docs/proposals" in fake.prompts[0] and "core/harness/*" in fake.prompts[0]
        assert "error_kind=protected_path" in captured.err
        assert _PROTECTED in captured.err
        assert "[BLOQUEIO HUMANO]" in captured.err
        assert "Worktree preservada" in captured.err
        assert kept.is_file()
        payload = json.loads(actions.read_text(encoding="utf-8"))
        action = payload["actions"][0]
        assert action["id"] == "OA-001"
        assert action["priority"] == "high"
        assert action["blocks"] == ["USR-99"]
        assert _PROTECTED in action["title"]
        assert "docs/proposals" in json.dumps(action, ensure_ascii=False)
        assert action["id"] in captured.err
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
