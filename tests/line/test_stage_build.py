"""Tests for core.line.stage_build — development + validate loop (HF-27-05).

Every test uses a local `git init --bare` repository under `tmp_path` as
"origin" (no network) and an in-process "fake agent" callable that edits
files directly under `req.cwd`, per the HF-27-05 acceptance note. No real
Claude/Codex/Grok/Antigravity CLI is ever invoked, and `resolve_commands`
runs for real (pure/offline autodetection) against the fake project files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage, ValidationStage, _MISSING_TESTS_INSTRUCTION
from core.projects.models import ProjectCommands, ProjectDescriptor


# --------------------------------------------------------------------------
# Helpers (mirrors tests/line/test_workspace.py's local git helpers)
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path, extra_files: dict[str, str] | None = None) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    for name, content in (extra_files or {}).items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


def _project(repo_url: str, *, commands: ProjectCommands | None = None) -> ProjectDescriptor:
    return ProjectDescriptor(
        id="acme",
        name="Acme Project",
        repo_url=repo_url,
        default_branch="main",
        commands=commands or ProjectCommands(),
    )


def _write_tickets(root_workspaces: Path, monkeypatch: pytest.MonkeyPatch, project, run_id, tickets) -> None:
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(root_workspaces))
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "tickets.json", json.dumps(tickets))
    ws_mod.commit(ws, "planning: tickets", job_key=f"{run_id}:planning")
    ws_mod.push(ws)


# A trivial, dependency-free "validate" command used instead of a real nested
# `pytest` run: it just checks whether the fake agent dropped a marker file.
# This keeps the iterative development-loop tests fast and avoids relying on
# pytest's own startup/collection timing under a loaded CI host.
_CHECK_COMMANDS = ProjectCommands(
    setup=["python -c \"pass\""], validate=["python check_status.py"]
)
_CHECK_STATUS_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.exit(0 if Path('STATUS_OK').is_file() else 1)\n"
)


class _RecordingFakeAgent:
    """A fake write-mode agent that edits files based on call count."""

    def __init__(self, behavior: Callable[[int, AgentRequest], None]) -> None:
        self.behavior = behavior
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        self.behavior(len(self.calls), req)
        return AgentResult(ok=True, text="ok", harness=req.harness, model=req.model, duration_s=0.01)


def _fixed_route(harness: str = "claude", model: str | None = "sonnet"):
    def _pick(*args, **kwargs):
        return (harness, model)
    return _pick


# --------------------------------------------------------------------------
# DevelopmentStage — passes on the 2nd iteration
# --------------------------------------------------------------------------


def test_ticket_fails_once_then_passes_commits_and_writes_two_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path,
        extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT},
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-1"
    _write_tickets(
        tmp_path / "root",
        monkeypatch,
        project,
        run_id,
        [{"id": "T1", "title": "Add calc", "goal": "add numbers", "acceptance": ["2+2==4"]}],
    )

    def behavior(call_no: int, req: AgentRequest) -> None:
        if call_no >= 2:
            (req.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")

    fake_agent = _RecordingFakeAgent(behavior)
    stage = DevelopmentStage(
        run_agent_func=fake_agent, pick_func=_fixed_route(), routing_config=load_routing_config()
    )

    result = stage.run(project, run_id)

    assert result.outcome == "success"
    assert len(fake_agent.calls) == 2

    ws = ws_mod.checkout(project, run_id)
    assert ws_mod.find_commit_by_job(ws, f"{run_id}:T1") is not None
    logs = sorted((ws_mod.context_dir(ws)).glob("validate-T1-*.log.md"))
    assert len(logs) == 2


# --------------------------------------------------------------------------
# DevelopmentStage — never passes, exhausts iterations
# --------------------------------------------------------------------------


def test_ticket_never_passes_fails_without_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path,
        extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT},
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-2"
    _write_tickets(
        tmp_path / "root",
        monkeypatch,
        project,
        run_id,
        [{"id": "T1", "title": "Broken ticket"}],
    )

    def behavior(call_no: int, req: AgentRequest) -> None:
        pass  # never creates STATUS_OK: validate keeps failing every iteration

    fake_agent = _RecordingFakeAgent(behavior)
    routing_config = load_routing_config()
    stage = DevelopmentStage(
        run_agent_func=fake_agent, pick_func=_fixed_route(), routing_config=routing_config
    )

    result = stage.run(project, run_id)

    assert result.outcome == "failed"
    assert result.cause_code == "validate_exhausted"
    assert len(fake_agent.calls) == routing_config.run_caps.validate_iterations_per_ticket

    ws = ws_mod.checkout(project, run_id)
    assert ws_mod.find_commit_by_job(ws, f"{run_id}:T1") is None
    logs = sorted(ws_mod.context_dir(ws).glob("validate-T1-*.log.md"))
    assert len(logs) == routing_config.run_caps.validate_iterations_per_ticket


# --------------------------------------------------------------------------
# DevelopmentStage — empty validate prompts for test infra
# --------------------------------------------------------------------------


def test_empty_validate_instructs_first_ticket_to_create_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)  # no manifest -> detect_commands() is always empty
    project = _project(str(origin))
    run_id = "run-3"
    _write_tickets(
        tmp_path / "root",
        monkeypatch,
        project,
        run_id,
        [{"id": "T1", "title": "First ticket"}],
    )

    def behavior(call_no: int, req: AgentRequest) -> None:
        pass  # never creates test infra: forces exhaustion, but we only check the prompt

    fake_agent = _RecordingFakeAgent(behavior)
    stage = DevelopmentStage(run_agent_func=fake_agent, pick_func=_fixed_route(), routing_config=load_routing_config())

    result = stage.run(project, run_id)

    assert result.outcome == "failed"
    assert result.cause_code == "validate_exhausted"
    assert len(fake_agent.calls) == 3
    for call in fake_agent.calls:
        assert _MISSING_TESTS_INSTRUCTION in call.prompt


# --------------------------------------------------------------------------
# DevelopmentStage — resumable: skips tickets already committed
# --------------------------------------------------------------------------


def test_resume_skips_tickets_already_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path,
        extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT},
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-4"
    _write_tickets(
        tmp_path / "root",
        monkeypatch,
        project,
        run_id,
        [
            {"id": "T1", "title": "First"},
            {"id": "T2", "title": "Second"},
        ],
    )

    def behavior(call_no: int, req: AgentRequest) -> None:
        (req.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")

    fake_agent = _RecordingFakeAgent(behavior)
    stage = DevelopmentStage(run_agent_func=fake_agent, pick_func=_fixed_route(), routing_config=load_routing_config())

    result1 = stage.run(project, run_id)
    assert result1.outcome == "success"
    calls_after_first_run = len(fake_agent.calls)
    assert calls_after_first_run == 2  # one agent call per ticket, both pass first try

    # Simulate a fresh run picking up the same branch: no new agent calls needed.
    fake_agent2 = _RecordingFakeAgent(behavior)
    stage2 = DevelopmentStage(run_agent_func=fake_agent2, pick_func=_fixed_route(), routing_config=load_routing_config())
    result2 = stage2.run(project, run_id)

    assert result2.outcome == "success"
    assert len(fake_agent2.calls) == 0  # both tickets were already committed


# --------------------------------------------------------------------------
# ValidationStage — clean checkout catches an uncommitted (gitignored) file
# --------------------------------------------------------------------------


_CHECK_SECRET_SCRIPT = (
    "from pathlib import Path\n"
    "import sys\n"
    "p = Path('secret.txt')\n"
    "sys.exit(0 if p.is_file() and p.read_text().strip() == 'expected' else 1)\n"
)


def test_clean_validation_catches_gitignored_file_needed_by_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path,
        extra_files={
            "requirements.txt": "",
            ".gitignore": "secret.txt\n",
            "check_secret.py": _CHECK_SECRET_SCRIPT,
        },
    )
    check_secret_commands = ProjectCommands(
        setup=["python -c \"pass\""], validate=["python check_secret.py"]
    )
    project = _project(str(origin), commands=check_secret_commands)
    run_id = "run-5"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = ws_mod.checkout(project, run_id)
    (ws.path / "secret.txt").write_text("expected\n", encoding="utf-8")
    sha_before = ws_mod.commit(ws, "add test needing gitignored file", job_key=f"{run_id}:T1")
    ws_mod.push(ws)

    # Sanity: git add -A really did not stage the gitignored file.
    log_files = _git(["show", "--stat", sha_before], cwd=ws.path).stdout
    assert "secret.txt" not in log_files

    result = ValidationStage().run(project, run_id)

    assert result.outcome == "retry"
    assert result.cause_code == "clean_validate_failed"

    ws2 = ws_mod.checkout(project, run_id)
    validation_path = ws_mod.context_dir(ws2) / "validation.json"
    assert validation_path.is_file()
    report = json.loads(validation_path.read_text(encoding="utf-8"))
    assert report["commands"]["validate"]["ok"] is False


def test_clean_validation_success_when_everything_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path, extra_files={"requirements.txt": ""})
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-6"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = ws_mod.checkout(project, run_id)
    (ws.path / "check_status.py").write_text(_CHECK_STATUS_SCRIPT, encoding="utf-8")
    (ws.path / "STATUS_OK").write_text("ok\n", encoding="utf-8")
    ws_mod.commit(ws, "add passing check", job_key=f"{run_id}:T1")
    ws_mod.push(ws)

    result = ValidationStage().run(project, run_id)

    assert result.outcome == "success"
    assert result.output_refs


# --------------------------------------------------------------------------
# Review/validation feedback reaches development (fix-up pass)
# --------------------------------------------------------------------------


def test_changes_required_triggers_one_fixup_pass_with_review_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path, extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT}
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-fix"
    _write_tickets(tmp_path / "root", monkeypatch, project, run_id, [{"id": "T1", "title": "Add calc"}])

    def behavior(call_no: int, req: AgentRequest) -> None:
        (req.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")
        if call_no == 2:
            (req.cwd / "calc.py").write_text("fixed = True\n", encoding="utf-8")

    fake_agent = _RecordingFakeAgent(behavior)
    stage = DevelopmentStage(
        run_agent_func=fake_agent, pick_func=_fixed_route(), routing_config=load_routing_config()
    )
    assert stage.run(project, run_id).outcome == "success"
    assert len(fake_agent.calls) == 1

    # Review round 1 asks for changes (as ReviewStage would persist it).
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "review-1.md", "# Review round 1\n\n- calc.py:1 — BUG_MARKER_42\n")
    ws_mod.write_context(
        ws, "review_state.json", json.dumps({"rounds": [{"round": 1, "verdict": "changes_required"}]})
    )
    ws_mod.commit(ws, "docs: review round 1", job_key=f"{run_id}:review:1")
    ws_mod.push(ws)

    result = stage.run(project, run_id)
    assert result.outcome == "success"
    assert len(fake_agent.calls) == 2
    assert "BUG_MARKER_42" in fake_agent.calls[1].prompt
    ws = ws_mod.checkout(project, run_id)
    assert ws_mod.find_commit_by_job(ws, f"{run_id}:fix-review-1") is not None

    # Replay: the feedback was already addressed, so no new agent call.
    assert stage.run(project, run_id).outcome == "success"
    assert len(fake_agent.calls) == 2


def test_rate_limited_agent_retries_without_burning_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(
        tmp_path, extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT}
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-rl"
    _write_tickets(tmp_path / "root", monkeypatch, project, run_id, [{"id": "T1", "title": "Add calc"}])

    calls: list[AgentRequest] = []

    def limited(req: AgentRequest) -> AgentResult:
        calls.append(req)
        return AgentResult(
            ok=False, text="429", harness=req.harness, duration_s=0.01, error_kind="rate_limited"
        )

    stage = DevelopmentStage(
        run_agent_func=limited, pick_func=_fixed_route(), routing_config=load_routing_config()
    )
    result = stage.run(project, run_id)
    assert result.outcome == "retry"
    assert result.cause_code == "agent_rate_limited"
    assert len(calls) == 1
