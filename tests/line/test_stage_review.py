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
from tests.line.conftest import copy_bare_origin


def _isolated_pick(tmp_path: Path):
    """Wrap `routing.pick` with a cooldown store/quota lookup isolated to `tmp_path`.

    Keeps the "other family" routing test deterministic regardless of any
    real `.factory/usage/cooldowns.json` or quota snapshots on the host.
    """

    def _pick(*args, **kwargs):
        kwargs.setdefault("cooldown_path", tmp_path / "cooldowns.json")
        kwargs.setdefault("quota_lookup", lambda _provider: 90.0)
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
    """Fresh local "origin" remote: a directory copy of the shared
    tests/line/conftest.py template (branch main, one README.md seed
    commit) instead of ~8 real git subprocess calls every time.
    """
    return copy_bare_origin(tmp_path / "origin.git")


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


def test_changes_required_retries_then_fails_on_exhaustion_without_another_review(
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
    # HF-27-08 review item 1: routes back to development, not another
    # review round; the blocking log is review-<round>.md, not the
    # cause_code (already asserted further below).
    assert result1.cause_code.startswith("retry:development\n")

    result2 = stage.run(project, run_id)
    assert result2.outcome == "retry"
    assert result2.cause_code.startswith("retry:development\n")

    # Round 3 exceeds run_caps.review_rounds (2). A green validation does
    # not override the independent reviewer's blocking finding.
    result3 = stage.run(project, run_id)
    assert result3.outcome == "failed"
    assert result3.cause_code == "review_exhausted_changes_required:round=3"
    assert len(agent.calls) == 3

    ws = ws_mod.checkout(project, run_id)
    ctx = ws_mod.context_dir(ws)
    assert (ctx / "review-1.md").is_file()
    assert (ctx / "review-2.md").is_file()
    review_3 = (ctx / "review-3.md").read_text(encoding="utf-8")
    assert "Verdict: changes_required" in review_3
    assert "no tests" in review_3
    assert "aprovado automaticamente" not in review_3
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state["rounds"][-1]["verdict"] == "changes_required"
    assert state["rounds"][-1]["forced"] is False

    replay = stage.run(project, run_id)
    assert replay.outcome == "failed"
    assert replay.cause_code == result3.cause_code
    assert replay.output_refs == result3.output_refs
    assert len(agent.calls) == 3


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
    assert result3.cause_code == "review_exhausted_changes_required:round=3"
    ws = ws_mod.checkout(project, run_id)
    assert (ws_mod.context_dir(ws) / "review-3.md").is_file()


def test_review_may_approve_on_final_independent_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-final-approval"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    changes_required = {"verdict": "changes_required", "blocking": [], "non_blocking": []}
    approved = {"verdict": "approve", "blocking": [], "non_blocking": []}
    agent = _ScriptedAgent([changes_required, changes_required, approved])
    stage = ReviewStage(run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config())

    assert stage.run(project, run_id).outcome == "retry"
    assert stage.run(project, run_id).outcome == "retry"
    assert stage.run(project, run_id).outcome == "success"
    assert len(agent.calls) == 3


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


def test_consecutive_agent_errors_fail_after_max_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-agent-fail"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    def failing_agent(req: AgentRequest) -> AgentResult:
        return AgentResult(ok=False, text="", error_kind="empty_output", harness=req.harness, duration_s=0.01)

    stage = ReviewStage(
        run_agent_func=failing_agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )

    ctx = ws_mod.context_dir(ws_mod.checkout(project, run_id))

    # 5 consecutive retries allowed
    for attempt in range(1, 6):
        res = stage.run(project, run_id)
        assert res.outcome == "retry"
        assert res.cause_code == "review_agent_empty_output"
        state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
        assert state["agent_error_streak"] == attempt

    # 6th attempt: ceiling exceeded (5 retries already exhausted)
    res = stage.run(project, run_id)
    assert res.outcome == "failed"
    assert res.cause_code == "review_agent_exhausted"


def test_agent_error_streak_resets_on_agent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-agent-reset"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    call_count = 0

    def flaky_agent(req: AgentRequest) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return AgentResult(ok=False, text="", error_kind="timeout", harness=req.harness, duration_s=0.01)
        return AgentResult(
            ok=True,
            text=json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),
            harness=req.harness,
            duration_s=0.01,
        )

    stage = ReviewStage(
        run_agent_func=flaky_agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )

    ctx = ws_mod.context_dir(ws_mod.checkout(project, run_id))

    # 2 failures
    res1 = stage.run(project, run_id)
    assert res1.outcome == "retry"
    assert res1.cause_code == "review_agent_timeout"

    res2 = stage.run(project, run_id)
    assert res2.outcome == "retry"

    state_mid = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state_mid["agent_error_streak"] == 2

    # 3rd attempt: success -> streak resets to 0
    res3 = stage.run(project, run_id)
    assert res3.outcome == "success"

    state_final = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state_final["agent_error_streak"] == 0


def test_agent_error_streak_resets_on_round_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-round-reset"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    call_count = 0

    def round_agent(req: AgentRequest) -> AgentResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AgentResult(ok=False, text="", error_kind="empty_output", harness=req.harness, duration_s=0.01)
        if call_count == 2:
            return AgentResult(
                ok=True,
                text=json.dumps({
                    "verdict": "changes_required",
                    "blocking": [{"file": "f.py", "line": 1, "issue": "bug", "fix": "fix"}],
                    "non_blocking": [],
                }),
                harness=req.harness,
                duration_s=0.01,
            )
        # Round 2: fail again once
        return AgentResult(ok=False, text="", error_kind="empty_output", harness=req.harness, duration_s=0.01)

    stage = ReviewStage(
        run_agent_func=round_agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )

    ctx = ws_mod.context_dir(ws_mod.checkout(project, run_id))

    # Call 1: fails in round 1
    res1 = stage.run(project, run_id)
    assert res1.outcome == "retry"

    # Call 2: succeeds in round 1 with changes_required
    res2 = stage.run(project, run_id)
    assert res2.outcome == "retry"
    assert "retry:development" in res2.cause_code

    state_r1 = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state_r1["agent_error_streak"] == 0
    assert len(state_r1["rounds"]) == 1

    # Call 3: round 2 starts, fails once -> streak should be 1 (not 2 or more)
    res3 = stage.run(project, run_id)
    assert res3.outcome == "retry"

    state_r2 = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state_r2["agent_error_streak"] == 1



def test_review_prompt_judges_final_tree_not_process_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canary 2026-10-04/05: the reviewer blocked 3 rounds in a row on a missing recorded 'red' run
    (process evidence the final tree can never show). The prompt now says process is non-blocking."""
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-process-nit"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    agent = _ScriptedAgent([{"verdict": "approve", "blocking": [], "non_blocking": []}])
    stage = ReviewStage(
        run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config()
    )
    assert stage.run(project, run_id).outcome == "success"

    prompt = agent.calls[0].prompt
    assert "Julgue a arvore final" in prompt
    assert "NAO e bloqueante" in prompt and "red" in prompt
    assert "{{" not in prompt and '"verdict"' in prompt  # JSON example rendered, braces unescaped
