"""Tests for core.line.stage_review — cross-family review stage (HF-27-05).

Uses a local `git init --bare` origin (no network) and an in-process fake
read-mode agent returning canned JSON verdicts, per the HF-27-05
acceptance note. Routing uses the real `core.line.routing.pick` cascade so
the "different family than development" rule is exercised for real.
"""

from __future__ import annotations

import json
import hashlib
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
from tests.line.conftest import copy_bare_origin, make_fake_cli, set_fake_response


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
    ws_mod.write_context(ws, "SPEC.md", "# SPEC\naceite da rodada\n")
    ws_mod.write_context(
        ws, "tickets.json",
        json.dumps([{"id": "T1", "acceptance": ["o aceite da rodada permanece verificavel"]}]),
    )
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
    ws = _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")
    from core.line.recovery_receipt import record_implementation
    record_implementation(ws, "claude", None)
    record_implementation(ws, "codex", None)
    ws_mod.write_context(ws, "SPEC.md", "Actual acceptance: persist recovery across restart")

    agent = _ScriptedAgent([{"verdict": "approve", "blocking": [], "non_blocking": []}])
    stage = ReviewStage(
        run_agent_func=agent, pick_func=_isolated_pick(tmp_path), routing_config=load_routing_config()
    )

    result = stage.run(project, run_id)

    assert result.outcome == "success"
    assert len(agent.calls) == 1
    assert agent.calls[0].harness not in {"claude", "codex"}
    assert agent.calls[0].mode == "read"
    assert "Actual acceptance: persist recovery across restart" in agent.calls[0].prompt


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


def test_claude_invalid_structured_response_records_sanitized_diagnostic_and_returns_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_fake_cli, caplog: pytest.LogCaptureFixture
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-invalid-claude-envelope"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="codex")
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr("core.line.agent_cli.find_claude_binary", lambda: cmd_path)
    secret = "sentinel-secret-value-426"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    raw_response = f"not JSON: OPENAI_API_KEY={secret}"
    record_path = tmp_path / "claude-record.json"
    set_fake_response(
        monkeypatch,
        env_var,
        {"record_path": str(record_path), "stdout": json.dumps({"result": raw_response}), "returncode": 0},
    )

    stage = ReviewStage(
        pick_func=lambda *_args, **_kwargs: ("claude", None),
        routing_config=load_routing_config(),
    )
    result = stage.run(project, run_id)

    assert result.outcome == "retry"
    assert result.cause_code.startswith("review_invalid_json:harness=claude:next=")
    ctx = ws_mod.context_dir(ws_mod.checkout(project, run_id))
    diagnostic_path = ctx / "review_invalid_response.json"
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["harness"] == "claude"
    assert diagnostic["reason"] == "json_decode_error"
    assert diagnostic["size_bytes"] == len(raw_response.encode("utf-8"))
    assert diagnostic["sha256"] == hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
    assert "[REDACTED]" in diagnostic["sanitized_excerpt"]
    assert secret not in diagnostic_path.read_text(encoding="utf-8")
    assert secret not in caplog.text
    assert not (ctx / "review_verdict.json").exists()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    argv = record["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert "--json-schema" in argv


def test_invalid_review_response_excludes_harness_for_same_candidate_and_fails_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-invalid-review-failover"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="codex")

    choices = ["claude", "codex"]
    picks: list[list[str]] = []

    def picking(_stage, _caps, *, exclude=(), **_kwargs):
        excluded = list(exclude)
        picks.append(excluded)
        return next(((name, None) for name in choices if name not in excluded), None)

    def agent(req: AgentRequest) -> AgentResult:
        if req.harness == "claude":
            return AgentResult(ok=True, text="invalid", harness=req.harness, duration_s=0.01)
        return AgentResult(
            ok=True,
            text=json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),
            harness=req.harness,
            duration_s=0.01,
        )

    stage = ReviewStage(run_agent_func=agent, pick_func=picking, routing_config=load_routing_config())
    first = stage.run(project, run_id)
    assert first.outcome == "retry"
    assert first.cause_code == "review_invalid_json:harness=claude:next=codex"
    ctx = ws_mod.context_dir(ws_mod.checkout(project, run_id))
    assert not (ctx / "review_verdict.json").exists()
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state["invalid_response_routes"][0]["harness"] == "claude"

    second = stage.run(project, run_id)
    assert second.outcome == "success"
    assert picks[0] == []
    assert picks[2] == ["claude"]
    assert (ctx / "review_verdict.json").is_file()


def test_invalid_review_exclusion_expires_when_candidate_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-invalid-review-new-candidate"
    ws = _prepare_run(tmp_path, monkeypatch, project, run_id, harness="codex")
    ctx = ws_mod.context_dir(ws)
    picks: list[list[str]] = []

    def picking(_stage, _caps, *, exclude=(), **_kwargs):
        picks.append(list(exclude))
        return "claude", None

    agent = lambda req: AgentResult(ok=True, text="invalid", harness=req.harness, duration_s=0.01)
    stage = ReviewStage(run_agent_func=agent, pick_func=picking, routing_config=load_routing_config())
    stage.run(project, run_id)
    first_state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    first_digest = first_state["invalid_response_routes"][0]["candidate_digest"]

    (ws.path / "feature.py").write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    ws_mod.commit(ws, "change candidate", job_key=f"{run_id}:candidate-change")
    stage.run(project, run_id)

    assert picks[-2] == []
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    # The stale exclusion is discarded before routing, so Claude is attempted again against the new tree.
    assert picks[-1] == ["claude"]
    assert state["invalid_response_routes"][0]["candidate_digest"] != first_digest


def test_review_agent_request_includes_review_verdict_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-review-schema"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="codex")
    agent = _ScriptedAgent([{"verdict": "approve", "blocking": [], "non_blocking": []}])
    stage = ReviewStage(
        run_agent_func=agent,
        pick_func=lambda *_args, **_kwargs: ("claude", None),
        routing_config=load_routing_config(),
    )

    result = stage.run(project, run_id)

    assert result.outcome == "success"
    schema = agent.calls[0].output_schema
    assert schema is not None
    assert schema["properties"]["verdict"]["enum"] == ["approve", "changes_required"]


def test_candidate_digest_is_none_when_stdout_is_not_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-text diff (the continuation test stubs git this way) is unavailable, not a crash."""
    from types import SimpleNamespace

    from core.line.stage_review import _candidate_digest
    from core.line.workspace import RunWorkspace

    ws = RunWorkspace(
        project_id="darkfac", run_id="USR-94", branch="ticket/usr-94", path=tmp_path, base_sha="0" * 40,
    )
    calls: dict[str, SimpleNamespace] = {}

    def fake_git(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return calls["proc"]

    monkeypatch.setattr(ws_mod, "_run_git", fake_git)

    calls["proc"] = SimpleNamespace(returncode=0, stdout="diff --git a/core/foo.py")
    assert _candidate_digest(ws, "main") == hashlib.sha256(b"diff --git a/core/foo.py").hexdigest()

    calls["proc"] = SimpleNamespace(returncode=0, stdout=None)
    assert _candidate_digest(ws, "main") == hashlib.sha256(b"").hexdigest()

    calls["proc"] = SimpleNamespace(returncode=0, stdout=b"diff")
    assert _candidate_digest(ws, "main") == hashlib.sha256(b"diff").hexdigest()

    calls["proc"] = SimpleNamespace(returncode=0, stdout=object())
    assert _candidate_digest(ws, "main") is None

    calls["proc"] = SimpleNamespace(returncode=1, stdout="diff")
    assert _candidate_digest(ws, "main") is None


def _candidate_diff(cwd: Path) -> str:
    return _git(
        ["diff", "origin/main...HEAD", "--", ".", ":(exclude).darkfac/runs"],
        cwd,
    ).stdout


def test_get_diff_returns_the_full_diff_when_it_exceeds_60kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large candidate is still the raw git diff. No size banner replaces it."""
    from core.line import stage_review

    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-6"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    (ws.path / "a.py").write_text("alpha = 1\n" * 7000, encoding="utf-8")
    (ws.path / "b.py").write_text("beta = 2\n", encoding="utf-8")
    ws_mod.commit(ws, "add files", job_key=f"{run_id}:T1")

    raw = _candidate_diff(ws.path)
    assert len(raw.encode("utf-8")) > 60 * 1024
    assert stage_review._get_diff(ws, "main") == raw
    assert "acima do limite" not in raw
    assert "mostrando apenas o resumo" not in raw
    assert stage_review._changed_paths(ws, "main") == ["a.py", "b.py"]


def test_review_prompt_includes_full_diff_file_list_and_candidate_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer sees every byte, the changed paths, and candidate.diff while it runs."""
    from core.line import stage_review

    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-full-diff"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "progress.json", json.dumps({"harness": "claude", "tickets_done": ["T1"]}))
    ws_mod.write_context(ws, "SPEC.md", "# SPEC\naceite do ticket\n")
    ws_mod.write_context(
        ws, "tickets.json",
        json.dumps([{"id": "T1", "acceptance": ["o diff integral continua no prompt"]}]),
    )
    (ws.path / "big.py").write_text("value = 1\n" * 8000, encoding="utf-8")
    ws_mod.commit(ws, "add big", job_key=f"{run_id}:T1")
    ws_mod.push(ws)

    raw = _candidate_diff(ws.path)
    assert len(raw.encode("utf-8")) > 60 * 1024
    seen: dict[str, object] = {}

    def agent(req: AgentRequest) -> AgentResult:
        diff_path = req.cwd / ".darkfac" / "runs" / run_id / "candidate.diff"
        spec_path = req.cwd / ".darkfac" / "runs" / run_id / "SPEC.md"
        seen["diff"] = diff_path.read_bytes()
        seen["spec"] = spec_path.read_text(encoding="utf-8")
        seen["prompt"] = req.prompt
        return AgentResult(
            ok=True,
            text=json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []}),
            harness=req.harness,
            duration_s=0.01,
        )

    stage = ReviewStage(
        run_agent_func=agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )
    assert stage.run(project, run_id).outcome == "success"

    prompt = str(seen["prompt"])
    assert seen["diff"] == raw.encode("utf-8")
    assert raw in prompt
    assert "- `big.py`" in prompt
    assert "candidate.diff" in prompt
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() in prompt
    assert "acima do limite" not in prompt
    assert "mostrando apenas o resumo" not in prompt
    assert "aceite do ticket" in str(seen["spec"])
    assert not (ws.path / ".darkfac" / "runs" / run_id / "candidate.diff").exists()


def test_delivery_review_copies_spec_into_the_worktree_and_restores_it(
    tmp_path: Path,
) -> None:
    """Delivery artifacts live outside the worktree. The reviewer still opens them from cwd."""
    from core.line import stage_review
    from core.line.workspace import RunWorkspace

    artifact = RunWorkspace(
        project_id="darkfac", run_id="USR-424", branch="ticket/usr-424",
        path=tmp_path / "artifacts", base_sha="0" * 40,
    )
    reviewer = RunWorkspace(
        project_id="darkfac", run_id="USR-424", branch="ticket/usr-424",
        path=tmp_path / "worktree", base_sha="0" * 40,
    )
    ws_mod.write_context(artifact, "SPEC.md", "# SPEC\nfila duravel\n")
    ws_mod.write_context(artifact, "tickets.json", "{\"id\":\"USR-424\"}\n")
    diff_text = "diff --git a/core/demands/store.py b/core/demands/store.py\n+resolve\n"

    staged = stage_review._materialize_review_sources(reviewer, artifact, "USR-424", diff_text)
    directory = reviewer.path / ".darkfac" / "runs" / "USR-424"
    assert (directory / "candidate.diff").read_bytes() == diff_text.encode("utf-8")
    assert "fila duravel" in (directory / "SPEC.md").read_text(encoding="utf-8")
    assert "USR-424" in (directory / "tickets.json").read_text(encoding="utf-8")
    assert (artifact.path / ".darkfac" / "runs" / "USR-424" / "SPEC.md").is_file()

    stage_review._restore_staged(staged)
    assert not (directory / "candidate.diff").exists()
    assert not (directory / "SPEC.md").exists()
    assert "fila duravel" in (
        artifact.path / ".darkfac" / "runs" / "USR-424" / "SPEC.md"
    ).read_text(encoding="utf-8")


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
    assert res1.cause_code.startswith("review_timeout:")

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


def test_blocking_review_objection_across_all_rounds_prevents_success_and_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-129: Blocking objections across all review rounds MUST fail closed.
    
    1. Exhaustion returns structured failure cause without approving or integrating.
    2. Workflow successors do NOT route to integration stage.
    3. Run can proceed to fix/replanning without infinite loop.
    4. Re-running ReviewStage is idempotent and fails without invoking the agent again.
    """
    from core.workflow.control_contracts import JobKey
    from core.workflow.control_store import SQLiteControlStore
    from core.workflow.successors import materialize_result

    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-usr-129-blocking"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude", green=True)

    blocking_finding = {
        "verdict": "changes_required",
        "blocking": [{"file": "feature.py", "line": 1, "issue": "unhandled regression", "fix": "fix bug"}],
        "non_blocking": [],
    }
    agent = _ScriptedAgent([blocking_finding, blocking_finding, blocking_finding])
    stage = ReviewStage(
        run_agent_func=agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )

    # Round 1: blocking review objection -> retries back to development
    res1 = stage.run(project, run_id)
    assert res1.outcome == "retry"
    assert res1.cause_code.startswith("retry:development\nchanges_required:round=1")

    # Round 2: still blocking review objection -> retries back to development
    res2 = stage.run(project, run_id)
    assert res2.outcome == "retry"
    assert res2.cause_code.startswith("retry:development\nchanges_required:round=2")

    # Round 3 (exceeding review_rounds=2): blocking objection STILL persists
    # MUST fail terminal with structured cause, NOT approve, even with green validation
    res3 = stage.run(project, run_id)
    assert res3.outcome == "failed"
    assert res3.cause_code == "review_exhausted_changes_required:round=3"
    assert res3.outcome != "success"
    assert len(agent.calls) == 3

    # Check committed review state
    ws = ws_mod.checkout(project, run_id)
    ctx = ws_mod.context_dir(ws)
    review_3 = (ctx / "review-3.md").read_text(encoding="utf-8")
    assert "Verdict: changes_required" in review_3
    assert "unhandled regression" in review_3
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state["rounds"][-1]["verdict"] == "changes_required"
    assert state["rounds"][-1]["forced"] is False

    # Workflow DAG verification: successors must NEVER contain 'integration'
    store = SQLiteControlStore(tmp_path / "control.db")
    jk = JobKey(run_id=run_id, ticket_id="T1", plan_version="1", stage="independent_review", iteration=0)
    successors = materialize_result(jk, res3, store)
    assert all(s.stage != "integration" for s in successors)
    assert [s.stage for s in successors] == ["retrospective"]

    # Replay verification: idempotent fail without re-running agent
    replay = stage.run(project, run_id)
    assert replay.outcome == "failed"
    assert replay.cause_code == res3.cause_code
    assert replay.output_refs == res3.output_refs
    assert len(agent.calls) == 3


def test_review_verdict_approve_with_blocking_items_fails_to_approve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reviewer mistakenly emits verdict 'approve' while specifying blocking items,
    it must NOT be approved and must be treated as changes_required."""
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    run_id = "run-usr-129-contradictory"
    _prepare_run(tmp_path, monkeypatch, project, run_id, harness="claude")

    contradictory_verdict = {
        "verdict": "approve",
        "blocking": [{"file": "vuln.py", "line": 10, "issue": "critical vuln", "fix": "sanitize"}],
        "non_blocking": [],
    }
    agent = _ScriptedAgent([contradictory_verdict])
    stage = ReviewStage(
        run_agent_func=agent,
        pick_func=_isolated_pick(tmp_path),
        routing_config=load_routing_config(),
    )

    res = stage.run(project, run_id)
    assert res.outcome == "retry"
    assert res.cause_code.startswith("review_invalid_json:harness=")
    assert ":next=" in res.cause_code

    ws = ws_mod.checkout(project, run_id)
    ctx = ws_mod.context_dir(ws)
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state["invalid_response_routes"][0]["harness"] in res.cause_code
    invalid_response = json.loads((ctx / "review_invalid_response.json").read_text(encoding="utf-8"))
    assert invalid_response["reason"] == "schema_validation_error"
    assert not (ctx / "review-1.md").exists()
    assert not (ctx / "review_verdict.json").exists()


def test_duplicate_json_keys_have_specific_invalid_response_reason() -> None:
    from core.line.stage_review import _parse_verdict_with_reason

    verdict, reason = _parse_verdict_with_reason(
        '{"verdict":"approve","verdict":"changes_required","blocking":[]}'
    )

    assert verdict is None
    assert reason == "duplicate_key"


_APPROVE_TEXT = json.dumps({"verdict": "approve", "blocking": [], "non_blocking": []})
_SPEC_SENTENCE = "FRASE_SPEC_474_UNICA"
_FILE_MARKER = "MARCADOR_TRECHO_474"
_CRITERION = "criterio verificavel USR-474 sem filesystem"
_SECRET = "sk-ant-" + ("Aa1_" * 4)


def _paid_model() -> str:
    from core.models.registry import resolve

    return resolve("distill.openrouter")


def _review_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    *,
    spec: str | None,
    tickets: str | None,
    source: str,
) -> tuple[object, object]:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "progress.json", json.dumps({"harness": "claude", "tickets_done": ["T1"]}))
    if spec is not None:
        ws_mod.write_context(ws, "SPEC.md", spec)
    if tickets is not None:
        ws_mod.write_context(ws, "tickets.json", tickets)
    (ws.path / "feature.py").write_text(source, encoding="utf-8")
    ws_mod.write_context(ws, "validation.json", "{\"passed\": true, \"marker\": \"gate\"}\n")
    ws_mod.commit(ws, "feat", job_key=f"{run_id}:T1")
    ws_mod.push(ws)
    return project, ws


def _install_paid_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    completion: int = 12,
    finish: str = "stop",
    truncated: bool = False,
    text: str = _APPROVE_TEXT,
) -> dict[str, object]:
    """Fake OpenRouter transport. While ``generate`` runs it cannot read the disk."""
    captured: dict[str, object] = {"calls": 0, "prompt": "", "ticket_id": None}
    reading = {"armed": False}
    real_bytes = Path.read_bytes
    real_text = Path.read_text

    def _guard_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        if reading["armed"]:
            raise AssertionError(f"transport read bytes: {self}")
        return real_bytes(self, *args, **kwargs)  # type: ignore[arg-type]

    def _guard_text(self: Path, *args: object, **kwargs: object) -> str:
        if reading["armed"]:
            raise AssertionError(f"transport read text: {self}")
        return real_text(self, *args, **kwargs)  # type: ignore[arg-type]

    class _Response:
        def __init__(self) -> None:
            self.text = text
            self.model = "paid-model"
            self.tokens_prompt = 20
            self.tokens_completion = completion
            self.total_tokens = 20 + completion
            self.measured_cost = 0.0
            self.estimated_cost = 0.0
            self.is_measured = True
            self.truncated = truncated
            self.metadata = {"backend": "openrouter", "finish_reason": finish}

    class _Transport:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def generate(self, prompt: str, *, model: str, **kwargs: object) -> _Response:
            reading["armed"] = True
            try:
                captured["calls"] = int(captured["calls"]) + 1
                captured["prompt"] = prompt
                captured["ticket_id"] = kwargs.get("ticket_id")
                captured["model"] = model
                return _Response()
            finally:
                reading["armed"] = False

    monkeypatch.setattr(Path, "read_bytes", _guard_bytes)
    monkeypatch.setattr(Path, "read_text", _guard_text)
    monkeypatch.setattr("core.execution.providers.OpenRouterModelProvider", _Transport)
    return captured


def _paid_stage(tmp_path: Path, captured_model: str) -> ReviewStage:
    from core.line.agent_cli import run_agent

    return ReviewStage(
        run_agent_func=run_agent,
        pick_func=lambda *_args, **_kwargs: ("openrouter", captured_model),
        routing_config=load_routing_config(),
    )


def test_openrouter_generate_prompt_carries_sanitized_context_without_reading_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paid transport proves SPEC, criteria, the redacted excerpt and the manifest hashes."""
    spec = f"{_SPEC_SENTENCE}\n{_SECRET}\n"
    tickets = json.dumps([{"id": "T1", "acceptance": [_CRITERION]}])
    source = f"{_FILE_MARKER} = 1\n"
    project, ws = _review_tree(
        tmp_path, monkeypatch, "USR-474", spec=spec, tickets=tickets, source=source,
    )
    ctx = ws_mod.context_dir(ws)
    validation = (ctx / "validation.json").read_bytes()
    spec_hash = hashlib.sha256((ctx / "SPEC.md").read_bytes()).hexdigest()
    tickets_hash = hashlib.sha256((ctx / "tickets.json").read_bytes()).hexdigest()
    file_hash = hashlib.sha256((ws.path / "feature.py").read_bytes()).hexdigest()
    monkeypatch.delenv("DARKFAC_TICKET_ID", raising=False)
    captured = _install_paid_transport(monkeypatch)
    recorded: list[object] = []

    class _Ledger:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def record(self, event: object) -> None:
            recorded.append(event)

    monkeypatch.setattr("core.usage.ledger.ModelUsageLedger", _Ledger)

    result = _paid_stage(tmp_path, _paid_model()).run(project, "USR-474")

    prompt = str(captured["prompt"])
    assert result.outcome == "success"
    assert captured["calls"] == 1
    assert _CRITERION in prompt
    assert _FILE_MARKER in prompt
    assert _SECRET not in prompt
    assert "[REDACTED]" in prompt
    assert prompt.count(_SPEC_SENTENCE) == 1
    assert spec_hash in prompt
    assert tickets_hash in prompt
    assert file_hash in prompt
    assert captured["ticket_id"] == "USR-474"
    assert recorded and all(getattr(event, "ticket_id", None) == "USR-474" for event in recorded)
    assert (ctx / "validation.json").read_bytes() == validation
    verdict = json.loads((ctx / "review_verdict.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == "approve"


def test_openrouter_ticket_id_comes_from_the_env_when_run_id_is_not_a_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ws = _review_tree(
        tmp_path, monkeypatch, "run-env-ticket",
        spec=f"{_SPEC_SENTENCE}\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    monkeypatch.setenv("DARKFAC_TICKET_ID", "USR-469")
    captured = _install_paid_transport(monkeypatch)

    result = _paid_stage(tmp_path, _paid_model()).run(project, "run-env-ticket")

    assert result.outcome == "success"
    assert captured["ticket_id"] == "USR-469"


def test_openrouter_review_without_ticket_id_keeps_a_null_event_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ws = _review_tree(
        tmp_path, monkeypatch, "run-no-ticket",
        spec=f"{_SPEC_SENTENCE}\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    monkeypatch.delenv("DARKFAC_TICKET_ID", raising=False)
    captured = _install_paid_transport(monkeypatch)
    recorded: list[object] = []

    class _Ledger:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def record(self, event: object) -> None:
            recorded.append(event)

    monkeypatch.setattr("core.usage.ledger.ModelUsageLedger", _Ledger)

    result = _paid_stage(tmp_path, _paid_model()).run(project, "run-no-ticket")

    assert result.outcome == "success"
    assert captured["ticket_id"] is None
    assert recorded
    assert all(getattr(event, "ticket_id", "missing") is None for event in recorded)


@pytest.mark.parametrize(
    ("completion", "finish", "truncated"),
    [(4096, "stop", False), (8, "length", False), (12, "stop", True)],
)
def test_truncated_openrouter_review_does_not_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completion: int,
    finish: str,
    truncated: bool,
) -> None:
    project, ws = _review_tree(
        tmp_path, monkeypatch, "USR-470",
        spec=f"{_SPEC_SENTENCE}\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    ctx = ws_mod.context_dir(ws)
    validation = (ctx / "validation.json").read_bytes()
    captured = _install_paid_transport(
        monkeypatch, completion=completion, finish=finish, truncated=truncated,
    )

    result = _paid_stage(tmp_path, _paid_model()).run(project, "USR-470")

    assert captured["calls"] == 1
    assert result.outcome == "failed"
    assert result.cause_code == "review_output_truncated"
    assert not (ctx / "review_verdict.json").exists()
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8")) if (ctx / "review_state.json").is_file() else {"rounds": []}
    assert not any(item.get("verdict") == "approve" for item in state.get("rounds", []))
    assert (ctx / "validation.json").read_bytes() == validation


def test_truncated_openrouter_review_can_only_approve_after_a_complete_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, ws = _review_tree(
        tmp_path, monkeypatch, "USR-474",
        spec=f"{_SPEC_SENTENCE}\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    ctx = ws_mod.context_dir(ws)
    validation = (ctx / "validation.json").read_bytes()
    stage = _paid_stage(tmp_path, _paid_model())

    first = _install_paid_transport(monkeypatch, truncated=True)
    failed = stage.run(project, "USR-474")

    assert failed.outcome == "failed"
    assert failed.cause_code == "review_output_truncated"
    assert not (ctx / "review_verdict.json").exists()

    second = _install_paid_transport(monkeypatch, truncated=False)
    retried = stage.run(project, "USR-474")

    assert retried.outcome == "success"
    assert first["calls"] == second["calls"] == 1
    assert (ctx / "review_verdict.json").is_file()
    assert (ctx / "validation.json").read_bytes() == validation


@pytest.mark.parametrize(
    "mutation",
    ["drop-spec", "drop-tickets", "empty-spec", "empty-tickets"],
)
def test_missing_or_empty_spec_or_tickets_do_not_call_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    spec = f"{_SPEC_SENTENCE}\n"
    tickets = json.dumps([{"id": "T1", "acceptance": [_CRITERION]}])
    project, ws = _review_tree(
        tmp_path, monkeypatch, "run-missing-context", spec=spec, tickets=tickets,
        source=f"{_FILE_MARKER} = 1\n",
    )
    ctx = ws_mod.context_dir(ws)
    if mutation == "drop-spec":
        (ctx / "SPEC.md").unlink()
    elif mutation == "drop-tickets":
        (ctx / "tickets.json").unlink()
    elif mutation == "empty-spec":
        (ctx / "SPEC.md").write_text("  \n", encoding="utf-8")
    else:
        (ctx / "tickets.json").write_text("[]", encoding="utf-8")
    captured = _install_paid_transport(monkeypatch)
    calls: list[AgentRequest] = []

    def _agent(req: AgentRequest) -> AgentResult:
        calls.append(req)
        return AgentResult(ok=True, text=_APPROVE_TEXT, harness=req.harness, duration_s=0.01)

    stage = ReviewStage(
        run_agent_func=_agent,
        pick_func=lambda *_args, **_kwargs: ("codex", None),
        routing_config=load_routing_config(),
    )
    result = stage.run(project, "run-missing-context")

    assert result.outcome == "failed"
    assert result.cause_code == "review_context_unavailable"
    assert calls == []
    assert captured["calls"] == 0
    assert not (ctx / "review_verdict.json").exists()


def test_review_uses_recorded_contingency_when_only_the_implementer_family_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line.recovery_receipt import record_implementation

    project, ws = _review_tree(
        tmp_path, monkeypatch, "run-same-family",
        spec=f"{_SPEC_SENTENCE}\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    record_implementation(ws, "claude", None)
    ctx = ws_mod.context_dir(ws)
    validation = (ctx / "validation.json").read_bytes()
    calls: list[AgentRequest] = []

    def _agent(req: AgentRequest) -> AgentResult:
        calls.append(req)
        return AgentResult(ok=True, text=_APPROVE_TEXT, harness=req.harness, duration_s=0.01)

    stage = ReviewStage(
        run_agent_func=_agent,
        pick_func=lambda *_args, **_kwargs: ("claude", None),
        routing_config=load_routing_config(),
    )

    result = stage.run(project, "run-same-family")

    assert result.outcome == "success"
    assert len(calls) == 1 and calls[0].mode == "read"
    assert (ctx / "validation.json").read_bytes() == validation
    route = json.loads((ctx / "review_route.json").read_text(encoding="utf-8"))
    assert route["level"] in (3, 4) and route["persona"] and route["reason"]
    execution = json.loads((ctx / "review_execution.json").read_text(encoding="utf-8"))
    assert execution["contingency"] == route
    state = json.loads((ctx / "review_state.json").read_text(encoding="utf-8"))
    assert state["rounds"][-1]["route"] == route and not state["rounds"][-1]["forced"]


def test_filesystem_review_keeps_the_full_diff_and_shows_the_spec_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, ws = _review_tree(
        tmp_path, monkeypatch, "run-fs-once",
        spec="FRASE_FS_474_UNICA\n",
        tickets=json.dumps([{"id": "T1", "acceptance": [_CRITERION]}]),
        source=f"{_FILE_MARKER} = 1\n",
    )
    seen: dict[str, object] = {}

    def _agent(req: AgentRequest) -> AgentResult:
        diff_path = req.cwd / ".darkfac" / "runs" / "run-fs-once" / "candidate.diff"
        seen["diff"] = diff_path.read_bytes()
        seen["prompt"] = req.prompt
        return AgentResult(ok=True, text=_APPROVE_TEXT, harness=req.harness, duration_s=0.01)

    stage = ReviewStage(
        run_agent_func=_agent,
        pick_func=lambda *_args, **_kwargs: ("codex", None),
        routing_config=load_routing_config(),
    )
    assert stage.run(project, "run-fs-once").outcome == "success"
    raw = _candidate_diff(ws.path)
    prompt = str(seen["prompt"])
    assert seen["diff"] == raw.encode("utf-8")
    assert raw in prompt
    assert prompt.count("FRASE_FS_474_UNICA") == 1
    assert "Contexto verificavel da revisao" not in prompt
    assert not (ws.path / ".darkfac" / "runs" / "run-fs-once" / "candidate.diff").exists()

