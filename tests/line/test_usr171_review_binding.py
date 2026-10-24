"""USR-171: a review approval is reusable only for the same candidate digest and policy (fail-closed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.line import workspace as ws_mod
from core.line.routing import load_routing_config
from core.line.stage_review import ReviewStage, _load_review_state, _write_review_state
from tests.line.test_stage_review import (
    _ScriptedAgent, _init_bare_origin, _isolated_pick, _prepare_run, _project,
)

_APPROVE = {"verdict": "approve", "blocking": [], "non_blocking": []}


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = _project(str(_init_bare_origin(tmp_path)))
    ws = _prepare_run(tmp_path, monkeypatch, project, "run-b", harness="claude")
    agent = _ScriptedAgent([_APPROVE])
    stage = ReviewStage(
        run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config()
    )
    assert stage.run(project, "run-b").outcome == "success"
    assert len(agent.calls) == 1
    return project, ws, agent, stage


def test_same_digest_and_policy_reuses_approval(tmp_path, monkeypatch) -> None:
    project, _ws, agent, stage = _setup(tmp_path, monkeypatch)
    assert stage.run(project, "run-b").outcome == "success"
    assert len(agent.calls) == 1


def test_changed_candidate_requires_new_review(tmp_path, monkeypatch) -> None:
    project, ws, agent, stage = _setup(tmp_path, monkeypatch)
    (ws.path / "feature.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    ws_mod.commit(ws, "fix: change after approval", job_key="run-b:T2")
    ws_mod.push(ws)
    assert stage.run(project, "run-b").outcome == "success"
    assert len(agent.calls) == 2
    state = _load_review_state(ws)
    assert state.rounds[0].verdict == "invalidated"
    assert state.rounds[-1].verdict == "approve"


def test_changed_policy_requires_new_review(tmp_path, monkeypatch) -> None:
    project, _ws, agent, _stage = _setup(tmp_path, monkeypatch)
    cfg = load_routing_config()
    cfg.run_caps.review_rounds = cfg.run_caps.review_rounds + 1
    stage2 = ReviewStage(run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=cfg)
    assert stage2.run(project, "run-b").outcome == "success"
    assert len(agent.calls) == 2


@pytest.mark.parametrize("mode", ["legacy_missing", "corrupt"])
def test_missing_or_corrupt_evidence_requires_new_review(tmp_path, monkeypatch, mode) -> None:
    project, ws, agent, stage = _setup(tmp_path, monkeypatch)
    if mode == "legacy_missing":
        state = _load_review_state(ws)
        state.rounds[-1].candidate_digest = None
        state.rounds[-1].policy_digest = None
        _write_review_state(ws, state)
    else:
        (ws_mod.context_dir(ws) / "review_state.json").write_text("{not json", encoding="utf-8")
    assert stage.run(project, "run-b").outcome == "success"
    assert len(agent.calls) == 2
