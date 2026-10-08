"""USR-153: a validate that fails only where the base branch is already red must not burn the developer.

`DevelopmentStage` used to loop `run_caps.validate_iterations_per_ticket` times (3 x ~450 s on
run-46efca7e11d5) while the agent tried to fix a test that fails identically on `origin/main`. Now the
failing test ids are re-run on the base (`core.line.base_probe`): if EVERY one is red there too the stage
waits with `retry("base_red not_before=...")` (then `failed(base_red_exhausted)`) and no iteration is
consumed; one test that only the ticket breaks keeps the old loop. The probe, the wait counter and the
clock are injected, so nothing here touches the network or runs a nested pytest on the base.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pytest

from core.git import ci_checks
from core.line import base_probe
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage
from core.projects.models import ProjectCommands, ProjectDescriptor

BASE_TEST = "tests/test_base.py::test_red_on_main"
OTHER_BASE_TEST = "tests/test_base.py::test_also_red_on_main"
TICKET_TEST = "tests/test_ticket.py::test_new_behaviour"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

# Fails (printing pytest's short summary) until `FIXED` exists; the lines come from `FAILED_LINES`.
_VALIDATE_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "if Path('FIXED').is_file():\n"
    "    sys.exit(0)\n"
    "for line in Path('FAILED_LINES').read_text(encoding='utf-8').splitlines():\n"
    "    print('FAILED ' + line + ' - AssertionError')\n"
    "sys.exit(1)\n"
)
_COMMANDS = ProjectCommands(setup=['python -c "pass"'], validate=["python validate_stub.py"])


def _git(args: list[str], cwd: Path) -> None:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"


def _origin(tmp_path: Path, failed_lines: list[str]) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "requirements.txt").write_text("", encoding="utf-8")
    (seed / "validate_stub.py").write_text(_VALIDATE_SCRIPT, encoding="utf-8")
    (seed / "FAILED_LINES").write_text("\n".join(failed_lines) + "\n", encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


def _project(origin: Path) -> ProjectDescriptor:
    return ProjectDescriptor(
        id="acme", name="Acme", repo_url=str(origin), default_branch="main", commands=_COMMANDS
    )


def _prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_lines: list[str], run_id: str
) -> ProjectDescriptor:
    project = _project(_origin(tmp_path, failed_lines))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Add calc"}]))
    ws_mod.commit(ws, "planning: tickets", job_key=f"{run_id}:planning")
    ws_mod.push(ws)
    return project


class _Agent:
    def __init__(self) -> None:
        self.calls: list[AgentRequest] = []

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        (req.cwd / "agent_work.txt").write_text(f"call {len(self.calls)}\n", encoding="utf-8")
        return AgentResult(ok=True, text="ok", harness=req.harness, model=req.model, duration_s=0.01)


class _Probe:
    """Fake `BaseProbe`: the tests that also fail on the base branch."""

    def __init__(self, red_on_base: Optional[set[str]]) -> None:
        self.red_on_base = red_on_base
        self.calls: list[list[str]] = []

    def __call__(self, ws: Any, project: Any, test_ids: list[str]) -> Optional[set[str]]:
        self.calls.append(list(test_ids))
        return self.red_on_base


def _stage(agent: _Agent, probe: Any, **kwargs: Any) -> DevelopmentStage:
    return DevelopmentStage(
        run_agent_func=agent,
        pick_func=lambda *a, **k: ("claude", "sonnet"),
        routing_config=load_routing_config(),
        base_probe_func=probe,
        clock=lambda: NOW,
        **kwargs,
    )


def _iterations() -> int:
    return load_routing_config().run_caps.validate_iterations_per_ticket


# --------------------------------------------------------------------------
# failure only on the base
# --------------------------------------------------------------------------


def test_failure_only_on_the_base_waits_without_consuming_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST, OTHER_BASE_TEST], "run-base")
    agent, probe = _Agent(), _Probe({BASE_TEST, OTHER_BASE_TEST})

    result = _stage(agent, probe).run(project, "run-base")

    assert result.outcome == "retry"
    assert result.cause_code == ci_checks.base_red_cause_code(NOW + timedelta(minutes=ci_checks.BASE_RED_RETRY_MINUTES))
    assert ci_checks.is_base_red_cause(result.cause_code)
    assert result.cause_code != "validate_exhausted"
    assert result.evidence_refs == [
        f"base_red:{BASE_TEST}", f"base_red:{OTHER_BASE_TEST}", "validate_log:validate-T1-1.log.md",
    ]
    assert len(agent.calls) == 1  # one developer call, not validate_iterations_per_ticket
    assert probe.calls == [[BASE_TEST, OTHER_BASE_TEST]]
    ws = ws_mod.checkout(project, "run-base")
    assert ws_mod.find_commit_by_job(ws, "run-base:T1") is None  # nothing is committed as the ticket's work
    assert len(list(ws_mod.context_dir(ws).glob("validate-T1-*.log.md"))) == 1


def test_base_red_retry_validates_again_without_calling_the_agent_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-resume")
    agent, probe = _Agent(), _Probe({BASE_TEST})
    stage = _stage(agent, probe)
    assert stage.run(project, "run-resume").outcome == "retry"

    ws = ws_mod.checkout(project, "run-resume")
    (ws.path / "FIXED").write_text("base is green again\n", encoding="utf-8")
    result = stage.run(project, "run-resume")

    assert result.outcome == "success"
    assert len(agent.calls) == 1  # the developer's edits were kept; only validate ran again
    assert ws_mod.find_commit_by_job(ws, "run-resume:T1") == result.output_refs[0]
    assert not (ws_mod.context_dir(ws) / "base-red-T1.json").exists()


def test_a_resumed_validate_that_now_fails_on_the_ticket_calls_the_agent_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-resume2")
    agent = _Agent()
    probes = [_Probe({BASE_TEST}), _Probe(set())]
    stage = _stage(agent, lambda ws, p, ids: probes.pop(0)(ws, p, ids))
    assert stage.run(project, "run-resume2").outcome == "retry"

    result = stage.run(project, "run-resume2")  # the base is not what fails any more

    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert len(agent.calls) == _iterations()  # resumed validate (1) + the remaining agent iterations
    assert probes == []


def test_base_red_waits_are_capped_and_end_in_a_structured_failure_not_validate_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-cap")
    agent = _Agent()
    stage = _stage(agent, _Probe({BASE_TEST}), base_red_attempts=lambda run_id: ci_checks.BASE_RED_MAX_RETRIES)

    result = stage.run(project, "run-cap")

    assert result.outcome == "failed"
    assert result.cause_code == "base_red_exhausted"
    assert result.evidence_refs[0] == f"base_red:{BASE_TEST}"
    assert len(agent.calls) == 1


def test_an_unreadable_wait_counter_fails_closed_instead_of_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-counter")

    def broken(run_id: str) -> int:
        raise RuntimeError("store down")

    result = _stage(_Agent(), _Probe({BASE_TEST}), base_red_attempts=broken).run(project, "run-counter")

    assert result.outcome == "failed" and result.cause_code == "base_red_exhausted"


# --------------------------------------------------------------------------
# failure only on the ticket / mixed / unknown
# --------------------------------------------------------------------------


def test_failure_only_in_the_ticket_keeps_consuming_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [TICKET_TEST], "run-ticket")
    agent, probe = _Agent(), _Probe(set())

    result = _stage(agent, probe).run(project, "run-ticket")

    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert len(agent.calls) == _iterations()
    assert len(probe.calls) == _iterations()
    assert f"validate_failed:{TICKET_TEST}" in result.evidence_refs


def test_mixed_failure_keeps_consuming_iterations_and_tells_the_agent_which_tests_are_not_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST, TICKET_TEST], "run-mixed")
    agent = _Agent()

    result = _stage(agent, _Probe({BASE_TEST})).run(project, "run-mixed")

    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert len(agent.calls) == _iterations()
    assert "tambem falham na branch base" in agent.calls[1].prompt
    assert f"- {BASE_TEST}" in agent.calls[1].prompt
    assert f"- {TICKET_TEST}" not in agent.calls[1].prompt.split("tambem falham na branch base", 1)[1]


@pytest.mark.parametrize("probe", [_Probe(None), lambda ws, p, ids: (_ for _ in ()).throw(RuntimeError("boom"))])
def test_a_probe_that_cannot_tell_is_not_proof_the_base_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: Any
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-unknown")
    agent = _Agent()

    result = _stage(agent, probe).run(project, "run-unknown")

    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert len(agent.calls) == _iterations()


# --------------------------------------------------------------------------
# healthy path
# --------------------------------------------------------------------------


def test_a_passing_validate_never_probes_the_base_and_commits_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-ok")
    agent, probe = _Agent(), _Probe({BASE_TEST})
    ws = ws_mod.checkout(project, "run-ok")
    (ws.path / "FIXED").write_text("green\n", encoding="utf-8")

    result = _stage(agent, probe).run(project, "run-ok")

    assert result.outcome == "success" and result.cause_code is None
    assert probe.calls == []
    assert len(agent.calls) == 1
    assert ws_mod.find_commit_by_job(ws, "run-ok:T1") == result.output_refs[0]


# --------------------------------------------------------------------------
# the real probe (git worktree of the base branch; pytest itself is faked)
# --------------------------------------------------------------------------


def test_probe_reruns_only_the_failing_ids_in_a_detached_worktree_of_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-probe")
    ws = ws_mod.checkout(project, "run-probe")
    seen: dict[str, Any] = {}

    def fake_pytest(argv: Any, cwd: Path, timeout_s: int) -> Optional[str]:
        seen["argv"], seen["cwd"], seen["has_readme"] = list(argv), cwd, (cwd / "README.md").is_file()
        return f"FAILED {BASE_TEST} - AssertionError\n1 failed, 1 passed\n"

    failing = base_probe.probe_base_failures(ws, project, [BASE_TEST, TICKET_TEST], pytest_runner=fake_pytest)

    assert failing == {BASE_TEST}  # the ticket's test passes (or is absent) on the base
    assert seen["argv"][-2:] == [BASE_TEST, TICKET_TEST]
    assert seen["has_readme"] and seen["cwd"] != ws.path
    assert not seen["cwd"].exists()  # the temp worktree is gone
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(ws.path), capture_output=True, text=True
    ).stdout
    assert "darkfac-base-probe" not in worktrees


def test_probe_cannot_tell_when_pytest_does_not_finish_or_an_id_is_not_rerunnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _prepare(tmp_path, monkeypatch, [BASE_TEST], "run-probe2")
    ws = ws_mod.checkout(project, "run-probe2")

    assert base_probe.probe_base_failures(ws, project, [BASE_TEST], pytest_runner=lambda a, c, t: None) is None
    assert base_probe.probe_base_failures(ws, project, ["--collect-only::x"], pytest_runner=lambda a, c, t: "") is None
    assert base_probe.probe_base_failures(ws, project, [BASE_TEST, "src/app.test.js"], pytest_runner=lambda a, c, t: "") is None
    assert base_probe.probe_base_failures(ws, project, [], pytest_runner=lambda a, c, t: "") is None


def test_split_by_base_keeps_the_original_order() -> None:
    assert base_probe.split_by_base(["b", "a", "c"], {"c", "b"}) == (["b", "c"], ["a"])
    assert base_probe.split_by_base(["a"], None) == ([], ["a"])
