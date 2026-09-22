"""Tests for core.line.stage_review — cross-family review stage (HF-27-05).

Uses a local `git init --bare` origin (no network) and an in-process fake
read-mode agent returning canned JSON verdicts, per the HF-27-05
acceptance note. Routing uses the real `core.line.routing.pick` cascade so
the "different family than development" rule is exercised for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.routing import pick as real_pick
from core.line.stage_review import ReviewStage
from core.projects.models import ProjectDescriptor


def _isolated_pick(tmp_path: Path):
    """Wrap `routing.pick` with a cooldown store/quota lookup isolated to `tmp_path`.

    Keeps the "other family" routing test deterministic regardless of any
    real `.factory/usage/cooldowns.json` or quota snapshots on the host.
    """

    def _pick(*args, **kwargs):
        kwargs.setdefault("cooldown_path", tmp_path / "cooldowns.json")
        kwargs.setdefault("quota_lookup", lambda _provider: None)
        return real_pick(*args, **kwargs)

    return _pick


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


def _init_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


def _project(repo_url: str) -> ProjectDescriptor:
    return ProjectDescriptor(id="acme", name="Acme Project", repo_url=repo_url, default_branch="main")


def _prepare_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project, run_id: str, *, harness: str = "claude", green: bool = True):
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    (ws.path / "feature.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ws_mod.write_context(ws, "progress.json", json.dumps({"harness": harness, "tickets_done": ["T1"]}))
    if green:
        ws_mod.write_context(
            ws, "validation.json",
            json.dumps({"sha": "deadbeef", "commands": {"validate": {"ran": True, "ok": True}}}),
        )
    ws_mod.commit(ws, "feat: add feature", job_key=f"{run_id}:T1")
    ws_mod.push(ws)
    return ws


class _ScriptedAgent:
    def __init__(self, verdicts: list[dict]) -> None:
        self.verdicts = verdicts
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        verdict = self.verdicts[min(len(self.calls) - 1, len(self.verdicts) - 1)]
        return AgentResult(ok=True, text=json.dumps(verdict), harness=req.harness, model=req.model, duration_s=0.01)


def test_review_uses_family_different_from_development(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-1"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    agent = _ScriptedAgent([{"verdict": "approve", "blocking": [], "non_blocking": []}])
    stage = ReviewStage(
        run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config()
    )

    result = stage.run(project, run_id)

    assert result.outcome == "success"
    assert len(agent.calls) == 1
    assert agent.calls[0].harness != "claude"
    assert agent.calls[0].mode == "read"


def test_changes_required_retries_with_review_log_then_approves_on_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-2"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude", green=True)

    changes_required = {
        "verdict": "changes_required",
        "blocking": [{"file": "feature.py", "line": 1, "issue": "no tests", "fix": "add tests"}],
        "non_blocking": [],
    }
    agent = _ScriptedAgent([changes_required, changes_required, changes_required])
    routing_config = load_routing_config()
    stage = ReviewStage(run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=routing_config)

    result1 = stage.run(project, run_id)
    assert result1.outcome == "retry"
    assert result1.cause_code == "changes_required"

    result2 = stage.run(project, run_id)
    assert result2.outcome == "retry"
    assert result2.cause_code == "changes_required"

    # Round 3 exceeds run_caps.review_rounds (2); validation is green, so it
    # force-approves instead of looping forever between two opinionated models.
    result3 = stage.run(project, run_id)
    assert result3.outcome == "success"
    assert len(agent.calls) == 3

    ws = ws_mod.checkout(project, run_id)
    ctx = ws_mod.context_dir(ws)
    assert (ctx / "review-1.md").is_file()
    assert (ctx / "review-2.md").is_file()
    review_3 = (ctx / "review-3.md").read_text(encoding="utf-8")
    assert "aprovado automaticamente" in review_3


def test_review_exhausted_without_green_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-3"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude", green=False)

    changes_required = {"verdict": "changes_required", "blocking": [], "non_blocking": []}
    agent = _ScriptedAgent([changes_required] * 3)
    stage = ReviewStage(run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config())

    stage.run(project, run_id)
    stage.run(project, run_id)
    result3 = stage.run(project, run_id)

    assert result3.outcome == "failed"
    assert result3.cause_code == "review_exhausted_not_green"


def test_resume_after_approval_is_idempotent_and_does_not_call_agent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-4"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    agent = _ScriptedAgent([{"verdict": "approve", "blocking": [], "non_blocking": []}])
    stage = ReviewStage(run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config())

    result1 = stage.run(project, run_id)
    assert result1.outcome == "success"

    result2 = stage.run(project, run_id)
    assert result2.outcome == "success"
    assert result2.output_refs == result1.output_refs
    assert len(agent.calls) == 1  # second call short-circuits on persisted state


def test_invalid_json_verdict_returns_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-5"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    def bad_agent(req: AgentRequest) -> AgentResult:
        return AgentResult(ok=True, text="not json at all", harness=req.harness, duration_s=0.01)

    stage = ReviewStage(run_agent_func=bad_agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config())
    result = stage.run(project, run_id)

    assert result.outcome == "retry"
    assert result.cause_code == "review_invalid_json"


def test_get_diff_summarizes_when_over_60kb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.line.stage_review import _get_diff

    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-6"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    (ws.path / "big.py").write_text("x = 1\n" * 20000, encoding="utf-8")
    ws_mod.commit(ws, "add big file", job_key=f"{run_id}:T1")

    diff_text = _get_diff(ws, "main")

    assert "diff completo tem" in diff_text
    assert "big.py" in diff_text
