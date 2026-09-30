"""Agent failures must be visible, recoverable and never silently terminal.

- DevelopmentStage excludes a crashing (harness, model) for the rest of the ticket and publishes the
  per-iteration logs (context dir only, redacted) to the run branch;
- grill/planning retry bad JSON on another harness before failing, and tolerate fences/prose;
- the Codex sandbox switch maps to the flags of codex-cli 0.48.0;
- the cloud worker logs a redacted WARNING for every non-success stage.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.line import agent_cli, stage_grill
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage
from core.line.stage_grill import extract_json_object, parse_grill_plan, run_grill
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult
from tests.line.conftest import copy_bare_origin
from tests.line.test_stage_build import (
    _CHECK_COMMANDS,
    _CHECK_STATUS_SCRIPT,
    _init_bare_origin,
    _project,
    _write_tickets,
)

SECRET = "sk-ant-api03-TOPSECRETTOKENVALUE1234567890"


def _git_out(origin: Path, *args: str) -> str:
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    proc = subprocess.run(
        ["git", "--git-dir", str(origin), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class _Agent:
    """Fake write agent: `script(harness, call_no, req) -> AgentResult` decides each call."""

    def __init__(self, script) -> None:
        self.script = script
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        return self.script(req.harness, len(self.calls), req)


def _ok(req: AgentRequest, *, edit: bool) -> AgentResult:
    if edit:
        (req.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")
    return AgentResult(ok=True, text="done", harness=req.harness, model=req.model, duration_s=0.5)


def _crash(req: AgentRequest, text: str = "codex exited 1: sandbox unavailable") -> AgentResult:
    return AgentResult(ok=False, text=text, harness=req.harness, model=req.model, duration_s=0.2, error_kind="crash")


@pytest.fixture
def dev_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    origin = _init_bare_origin(
        tmp_path, extra_files={"requirements.txt": "", "check_status.py": _CHECK_STATUS_SCRIPT}
    )
    project = _project(str(origin), commands=_CHECK_COMMANDS)
    run_id = "run-vis"
    _write_tickets(tmp_path / "root", monkeypatch, project, run_id, [{"id": "T1", "title": "Ticket"}])
    return project, run_id, origin


def _route_picker(available: list[tuple[str, str | None]]):
    """pick_func double honouring `exclude` like routing.pick; records every call's exclude set."""
    calls: list[set] = []

    def _pick(stage, caps, exclude=(), **kwargs):
        calls.append(set(exclude))
        for route in available:
            if route not in set(exclude):
                return route
        return None

    _pick.calls = calls  # type: ignore[attr-defined]
    return _pick


# --------------------------------------------------------------------------
# 2. Harness fallback in DevelopmentStage
# --------------------------------------------------------------------------


def test_a_crashing_harness_is_excluded_for_the_rest_of_the_ticket(dev_project) -> None:
    project, run_id, _origin = dev_project
    picker = _route_picker([("codex", "gpt"), ("claude", "sonnet")])
    agent = _Agent(lambda harness, n, req: _crash(req) if harness == "codex" else _ok(req, edit=True))
    stage = DevelopmentStage(run_agent_func=agent, pick_func=picker, routing_config=load_routing_config())

    result = stage.run(project, run_id)

    assert result.outcome == "success"
    assert [c.harness for c in agent.calls] == ["codex", "claude"]  # codex crashed once, never retried
    assert ("codex", "gpt") in picker.calls[-1]


def test_when_every_route_crashed_the_ticket_keeps_iterating_and_fails_normally(dev_project) -> None:
    project, run_id, _origin = dev_project
    picker = _route_picker([("codex", "gpt")])  # the only route
    agent = _Agent(lambda harness, n, req: _crash(req))
    config = load_routing_config()
    stage = DevelopmentStage(run_agent_func=agent, pick_func=picker, routing_config=config)

    result = stage.run(project, run_id)

    # Exclusion leaves nothing routable, so it falls back to the same route: no bogus waiting_human.
    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert len(agent.calls) == config.run_caps.validate_iterations_per_ticket


# --------------------------------------------------------------------------
# 1. Visibility: logs on the run branch, redacted, context dir only
# --------------------------------------------------------------------------


def test_failed_development_publishes_redacted_iteration_logs_without_the_agents_edits(dev_project) -> None:
    project, run_id, origin = dev_project
    long_tail = "x" * 3000

    def script(harness, n, req):
        (req.cwd / "half_made_edit.txt").write_text("junk\n", encoding="utf-8")  # uncommitted agent edit
        return _crash(req, f"boom {SECRET} {long_tail}")

    stage = DevelopmentStage(
        run_agent_func=_Agent(script), pick_func=_route_picker([("codex", "gpt")]), routing_config=load_routing_config()
    )
    result = stage.run(project, run_id)
    assert result.outcome == "failed"

    branch = f"df/{run_id}"
    tree = _git_out(origin, "ls-tree", "-r", "--name-only", branch)
    assert f".darkfac/runs/{run_id}/validate-T1-1.log.md" in tree
    assert "half_made_edit.txt" not in tree  # only the context dir was committed
    log = _git_out(origin, "show", f"{branch}:.darkfac/runs/{run_id}/validate-T1-1.log.md")
    assert "- harness: codex" in log and "- model: gpt" in log
    assert "- error_kind: crash" in log and "- duration_s: 0.2" in log
    assert SECRET not in log and "[REDACTED]" in log
    assert "truncated" in log and len(log) < 3000  # first ~2000 chars only


def test_successful_iterations_still_commit_normally_and_carry_the_header(dev_project) -> None:
    project, run_id, origin = dev_project
    agent = _Agent(lambda harness, n, req: _ok(req, edit=n >= 2))
    stage = DevelopmentStage(
        run_agent_func=agent, pick_func=_route_picker([("claude", "sonnet")]), routing_config=load_routing_config()
    )
    assert stage.run(project, run_id).outcome == "success"
    log = _git_out(origin, "show", f"df/{run_id}:.darkfac/runs/{run_id}/validate-T1-1.log.md")
    assert "- harness: claude" in log and "validate failed" in log


# --------------------------------------------------------------------------
# 4. grill: tolerant JSON parsing + retry instead of terminal failure
# --------------------------------------------------------------------------

_PLAN = {"questions": [], "assumptions": ["a"], "is_product_scale": False}


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(_PLAN),
        "```json\n" + json.dumps(_PLAN) + "\n```",
        "Claro! Aqui esta o plano:\n\n" + json.dumps(_PLAN) + "\n\nEspero que ajude.",
        "Usei {placeholders} e {} vazio.\n```\n" + json.dumps(_PLAN) + "\n```\nfim {nao json}",
        "```json\n" + json.dumps(_PLAN, indent=2) + "\n```\n\nnota: {\"outro\": 1}",
        "<think>{\"draft\": true}</think>" + json.dumps(_PLAN),
    ],
)
def test_parse_grill_plan_tolerates_fences_prose_and_stray_braces(text: str) -> None:
    plan = parse_grill_plan(text)
    assert plan.assumptions == ["a"]


def test_extract_json_object_prefers_the_object_with_the_expected_keys() -> None:
    text = 'antes {"x": 1} depois ' + json.dumps(_PLAN)
    assert json.loads(extract_json_object(text, ("questions", "assumptions"))) == _PLAN
    assert json.loads(extract_json_object(text)) == {"x": 1}  # no keys: first decodable object
    with pytest.raises(ValueError):
        extract_json_object("sem json nenhum { aqui")
    with pytest.raises(ValueError):
        extract_json_object("")


@pytest.fixture
def grill_project(tmp_path, monkeypatch) -> ProjectDescriptor:
    origin = copy_bare_origin(tmp_path / "origin.git")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setattr(stage_grill, "default_telegram_sender", lambda: None)
    return ProjectDescriptor(id="acme", name="Acme", repo_url=str(origin))


def test_invalid_json_from_the_grill_is_retried_then_fails_and_is_visible(grill_project, monkeypatch) -> None:
    seen: list[set] = []

    def _fake(*args, **kwargs):
        seen.append(set(kwargs.get("exclude") or ()))
        return AgentResult(ok=True, text=f"oops {SECRET} not json", harness="codex", model="gpt-x", duration_s=0.3)

    monkeypatch.setattr(stage_grill, "run_read_agent", _fake)

    first = run_grill(grill_project, "run-g1", "demanda")
    second = run_grill(grill_project, "run-g1", "demanda")
    third = run_grill(grill_project, "run-g1", "demanda")

    assert first.outcome == "retry" and first.cause_code.startswith("grill_invalid_json not_before=")
    assert second.outcome == "retry"
    assert third.outcome == "failed" and third.cause_code == "grill_invalid_json"  # only after 3 bad attempts
    assert seen[0] == set() and ("codex", "gpt-x") in seen[1]  # the offending harness is excluded next time

    origin = Path(grill_project.repo_url)
    attempts = json.loads(_git_out(origin, "show", "df/run-g1:.darkfac/runs/run-g1/agent-attempts-grill.json"))
    assert [a["note"] for a in attempts] == ["invalid_json"] * 3
    assert attempts[0]["harness"] == "codex" and attempts[0]["error_kind"] == "invalid_json"
    assert SECRET not in json.dumps(attempts) and "[REDACTED]" in attempts[0]["output"]


def test_agent_crash_in_the_grill_is_recorded_on_the_branch_but_login_problems_are_not(grill_project, monkeypatch) -> None:
    monkeypatch.setattr(
        stage_grill, "run_read_agent",
        lambda *a, **k: AgentResult(ok=False, text="segfault", harness="codex", duration_s=0.1, error_kind="crash"),
    )
    assert run_grill(grill_project, "run-g2", "demanda").outcome == "retry"
    origin = Path(grill_project.repo_url)
    assert "crash" in _git_out(origin, "show", "df/run-g2:.darkfac/runs/run-g2/agent-attempts-grill.json")

    monkeypatch.setattr(
        stage_grill, "run_read_agent",
        lambda *a, **k: AgentResult(ok=False, text="please login", harness="codex", duration_s=0.1, error_kind="auth_expired"),
    )
    before = _git_out(origin, "rev-list", "--count", "df/run-g2").strip()
    run_grill(grill_project, "run-g2", "demanda")
    assert _git_out(origin, "rev-list", "--count", "df/run-g2").strip() == before  # no commit spam for login retries


def test_run_read_agent_retries_a_fully_excluded_cascade_instead_of_giving_up(monkeypatch, tmp_path) -> None:
    picks: list[set] = []

    def _pick(stage, caps, exclude=(), config=None):
        picks.append(set(exclude))
        return None if exclude else ("claude", "sonnet")

    monkeypatch.setattr(stage_grill, "pick", _pick)
    monkeypatch.setattr(
        stage_grill, "run_agent",
        lambda req: AgentResult(ok=True, text="{}", harness=req.harness, model=req.model, duration_s=0.1),
    )
    monkeypatch.setattr(stage_grill, "record_result", lambda *a, **k: None)
    result = stage_grill.run_read_agent(
        "grill", "p", tmp_path, host_caps=["harness:claude"], routing_config=None, exclude={("claude", "sonnet")}
    )
    assert result.ok and picks == [{("claude", "sonnet")}, set()]


# --------------------------------------------------------------------------
# 3. Codex sandbox switch (flags verified against codex-cli 0.48.0)
# --------------------------------------------------------------------------


def _argv(mode: str, monkeypatch, env: str | None):
    if env is None:
        monkeypatch.delenv(agent_cli.CODEX_SANDBOX_ENV, raising=False)
    else:
        monkeypatch.setenv(agent_cli.CODEX_SANDBOX_ENV, env)
    req = AgentRequest(prompt="p", cwd=Path("."), mode=mode, harness="codex", model="gpt-x")  # type: ignore[arg-type]
    return agent_cli.build_codex_argv("codex", req, Path("out.json"))


@pytest.mark.parametrize("env", [None, "", "auto", "AUTO", "nonsense"])
def test_codex_default_keeps_the_current_sandbox_flags(monkeypatch, env) -> None:
    write = _argv("write", monkeypatch, env)
    read = _argv("read", monkeypatch, env)
    assert write == ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json", "-m", "gpt-x", "-o", "out.json", "-"]
    assert read[3] == "read-only" and "--dangerously-bypass-approvals-and-sandbox" not in read


def test_codex_danger_full_access_mode_for_both_modes(monkeypatch) -> None:
    for mode in ("write", "read"):
        argv = _argv(mode, monkeypatch, "danger-full-access")
        assert argv[2:4] == ["--sandbox", "danger-full-access"]
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_codex_bypass_mode_uses_the_documented_flag_and_no_sandbox_flag(monkeypatch) -> None:
    for mode in ("write", "read"):
        argv = _argv(mode, monkeypatch, "bypass")
        assert argv[:3] == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        assert "--sandbox" not in argv
        assert argv[-1] == "-" and "--skip-git-repo-check" in argv and "--json" in argv


# --------------------------------------------------------------------------
# 1b. Cloud worker WARNING per non-success stage
# --------------------------------------------------------------------------


def test_worker_logs_a_redacted_warning_for_every_non_success_stage(caplog) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    claim = SimpleNamespace(job_key=SimpleNamespace(stage="development", run_id="run-w1"))
    with caplog.at_level(logging.WARNING):
        CloudWorker._log_stage_outcome(claim, StageResult(outcome="failed", cause_code=f"validate_exhausted {SECRET} " + "y" * 900))
        CloudWorker._log_stage_outcome(claim, StageResult(outcome="success", output_refs=["sha"]))
        CloudWorker._log_stage_outcome(claim, StageResult(outcome="waiting_human", cause_code="no_route_available"))
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(messages) == 2  # success is silent
    assert "development" in messages[0] and "run-w1" in messages[0] and "failed" in messages[0]
    assert "validate_exhausted" in messages[0] and SECRET not in messages[0]
    assert len(messages[0]) < 500 and messages[0].endswith("...")
    assert "waiting_human" in messages[1] and "no_route_available" in messages[1]
