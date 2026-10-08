"""USR-165: leaving a `base_red` wait brings the run branch up to the (fixed) base before validating again.

USR-153 made `DevelopmentStage` wait (`retry("base_red not_before=...")`) when every failing test is red on
the base too. The retry then re-validated the developer's kept edits on top of the OLD base, so a base that
had since been fixed still looked red and only `integration` (rebase) could ever unblock the run. Now the
resume first merges `origin/<default>` into the run branch (`core.line.base_sync`), keeping the developer's
uncommitted edits. A conflict is structured (`base_sync_conflict:<path>`) and goes back to the developer.
All repositories here are throwaway local git repos: no network, no nested pytest.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from core.git import ci_checks
from core.line import base_sync
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage
from core.projects.models import ProjectCommands, ProjectDescriptor

BASE_TEST = "tests/test_base.py::test_red_on_main"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

# Fails (printing pytest's short summary) until `FIXED` exists in the validated tree.
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


def _git(args: list[str], cwd: Path) -> str:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


class _Origin:
    """A bare origin plus a seed clone used to land commits on `main` like another developer would."""

    def __init__(self, tmp_path: Path) -> None:
        self.bare = tmp_path / "origin.git"
        _git(["init", "--bare", str(self.bare)], cwd=tmp_path)
        self.seed = tmp_path / "_seed"
        _git(["clone", str(self.bare), str(self.seed)], cwd=tmp_path)
        _git(["checkout", "-B", "main"], cwd=self.seed)
        _git(["config", "user.email", "seed@example.com"], cwd=self.seed)
        _git(["config", "user.name", "Seed"], cwd=self.seed)
        self.land({
            "README.md": "seed\n",
            "requirements.txt": "",
            "validate_stub.py": _VALIDATE_SCRIPT,
            "FAILED_LINES": BASE_TEST + "\n",
        }, "seed")

    def land(self, files: dict[str, str], message: str) -> None:
        for name, content in files.items():
            target = self.seed / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _git(["add", "-A"], cwd=self.seed)
        _git(["commit", "-m", message], cwd=self.seed)
        _git(["push", "origin", "main"], cwd=self.seed)


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> tuple[_Origin, ProjectDescriptor]:
    origin = _Origin(tmp_path)
    project = ProjectDescriptor(
        id="acme", name="Acme", repo_url=str(origin.bare), default_branch="main", commands=_COMMANDS
    )
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Add calc"}]))
    ws_mod.commit(ws, "planning: tickets", job_key=f"{run_id}:planning")
    ws_mod.push(ws)
    return origin, project


class _Agent:
    """Writes `agent_work.txt` on every call and remembers what it was asked and what it found on disk."""

    def __init__(self, *, content: str = "developer edit\n", resolve: bool = False) -> None:
        self.calls: list[AgentRequest] = []
        self.seen_work: list[str] = []
        self.content = content
        self.resolve = resolve

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls.append(req)
        work = req.cwd / "agent_work.txt"
        self.seen_work.append(work.read_text(encoding="utf-8") if work.is_file() else "")
        if self.resolve and len(self.calls) > 1:
            work.write_text("resolved by the developer\n", encoding="utf-8")
            (req.cwd / "FIXED").write_text("green\n", encoding="utf-8")
        else:
            work.write_text(self.content, encoding="utf-8")
        return AgentResult(ok=True, text="ok", harness=req.harness, model=req.model, duration_s=0.01)


def _stage(agent: _Agent, red_on_base: set[str]) -> DevelopmentStage:
    return DevelopmentStage(
        run_agent_func=agent,
        pick_func=lambda *a, **k: ("claude", "sonnet"),
        routing_config=load_routing_config(),
        base_probe_func=lambda ws, project, ids: set(red_on_base),
        clock=lambda: NOW,
    )


def _is_ancestor(ws: ws_mod.RunWorkspace, ref: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, "HEAD"], cwd=str(ws.path), capture_output=True, text=True
    )
    return proc.returncode == 0


# --------------------------------------------------------------------------
# the stage: base fixed while waiting
# --------------------------------------------------------------------------


def test_base_fixed_during_the_wait_is_merged_in_before_validating_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-sync")
    agent = _Agent()
    stage = _stage(agent, {BASE_TEST})

    waiting = stage.run(project, "run-sync")  # base red -> wait
    assert waiting.outcome == "retry" and ci_checks.is_base_red_cause(waiting.cause_code)

    origin.land({"FIXED": "main is green again\n"}, "fix: repair the base")  # somebody fixes the base
    result = stage.run(project, "run-sync")

    assert result.outcome == "success", result
    assert len(agent.calls) == 1  # the developer's kept edits were re-validated, no second agent call
    ws = ws_mod.checkout(project, "run-sync")
    assert (ws.path / "FIXED").is_file()  # validated against the NEW base
    assert _is_ancestor(ws, "origin/main")
    ticket_commit = ws_mod.find_commit_by_job(ws, "run-sync:T1")
    assert ticket_commit == result.output_refs[0]
    changed = _git(["show", "--name-only", "--format=", ticket_commit], cwd=ws.path).split()
    assert "agent_work.txt" in changed and "FIXED" not in changed  # the base's file is not the ticket's work
    assert not (ws_mod.context_dir(ws) / "base-red-T1.json").exists()


def test_a_resume_with_the_base_still_red_syncs_and_waits_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-still-red")
    agent = _Agent()
    stage = _stage(agent, {BASE_TEST})
    assert stage.run(project, "run-still-red").outcome == "retry"

    origin.land({"CHANGELOG.md": "unrelated base change\n"}, "docs: unrelated")  # base moved, still red
    result = stage.run(project, "run-still-red")

    assert result.outcome == "retry" and ci_checks.is_base_red_cause(result.cause_code)
    assert len(agent.calls) == 1
    ws = ws_mod.checkout(project, "run-still-red")
    assert (ws.path / "CHANGELOG.md").is_file() and _is_ancestor(ws, "origin/main")
    assert (ws.path / "agent_work.txt").read_text(encoding="utf-8") == "developer edit\n"  # edits survive the sync


# --------------------------------------------------------------------------
# conflicts
# --------------------------------------------------------------------------


def test_conflict_between_the_developers_edits_and_the_base_goes_back_to_the_developer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-conflict")
    agent = _Agent(resolve=True)
    stage = _stage(agent, {BASE_TEST})
    assert stage.run(project, "run-conflict").outcome == "retry"

    # The base now ships its own agent_work.txt (add/add conflict with the developer's edit) AND the fix.
    origin.land({"agent_work.txt": "base version\n", "FIXED": "green\n"}, "feat: base gets agent_work.txt")
    result = stage.run(project, "run-conflict")

    assert result.outcome == "success", result
    assert len(agent.calls) == 2  # the conflict is the developer's to resolve: one more agent call
    assert "<<<<<<<" in agent.seen_work[1]  # it found the conflict markers in its own edit
    prompt = agent.calls[1].prompt
    assert "base_sync_conflict" in prompt and "agent_work.txt" in prompt and "origin/main" in prompt
    ws = ws_mod.checkout(project, "run-conflict")
    assert _is_ancestor(ws, "origin/main")
    assert (ws.path / "agent_work.txt").read_text(encoding="utf-8") == "resolved by the developer\n"


def test_an_unresolved_conflict_ends_with_the_structured_cause_in_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-conflict2")
    agent = _Agent()  # never resolves anything
    stage = _stage(agent, set())  # not base-red any more: plain ticket failures from here on
    assert stage.run(project, "run-conflict2").outcome == "failed"  # first run: ticket failure, edits kept

    ws = ws_mod.checkout(project, "run-conflict2")
    workspace_marker = ws_mod.context_dir(ws) / "base-red-T1.json"
    workspace_marker.write_text(json.dumps({"harness": "claude", "model": "sonnet", "tests": [BASE_TEST]}))
    origin.land({"agent_work.txt": "base version\n"}, "feat: base gets agent_work.txt")

    result = stage.run(project, "run-conflict2")

    assert result.outcome == "failed" and result.cause_code == "validate_exhausted"
    assert "base_sync_conflict:agent_work.txt" in result.evidence_refs


def test_a_conflict_inside_the_branch_commits_is_a_structured_failure_and_keeps_the_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-branchconf")
    agent = _Agent()
    stage = _stage(agent, {BASE_TEST})
    assert stage.run(project, "run-branchconf").outcome == "retry"

    # The base rewrites a file the RUN BRANCH itself committed (planning's tickets.json): no edit of the
    # developer can fix that, so the stage ends with a structured cause instead of looping.
    origin.land({".darkfac/runs/run-branchconf/tickets.json": "[]\n"}, "chore: touch the run context")
    result = stage.run(project, "run-branchconf")

    assert result.outcome == "failed" and result.cause_code == "base_sync_conflict"
    assert any(ref.startswith("base_sync_conflict:") for ref in result.evidence_refs)
    assert len(agent.calls) == 1
    ws = ws_mod.checkout(project, "run-branchconf")
    status = _git(["status", "--porcelain"], cwd=ws.path)
    assert "UU" not in status and "AA" not in status  # no merge left half-done
    assert (ws.path / "agent_work.txt").read_text(encoding="utf-8") == "developer edit\n"


# --------------------------------------------------------------------------
# the helper itself
# --------------------------------------------------------------------------


def test_sync_is_a_no_op_when_the_branch_already_contains_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project = _prepare(tmp_path, monkeypatch, "run-noop")
    ws = ws_mod.checkout(project, "run-noop")
    head = _git(["rev-parse", "HEAD"], cwd=ws.path)

    result = base_sync.sync_with_base(ws, project)

    assert result.status == "up_to_date" and not result.conflicts
    assert _git(["rev-parse", "HEAD"], cwd=ws.path) == head


def test_sync_keeps_uncommitted_and_untracked_edits_and_deletions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project = _prepare(tmp_path, monkeypatch, "run-keep")
    ws = ws_mod.checkout(project, "run-keep")
    (ws.path / "README.md").write_text("edited readme\n", encoding="utf-8")  # tracked, modified
    (ws.path / "FAILED_LINES").unlink()  # tracked, deleted
    (ws.path / "new_dir").mkdir()
    (ws.path / "new_dir" / "x.txt").write_text("untracked\n", encoding="utf-8")
    origin.land({"CHANGELOG.md": "base moved\n"}, "docs: base moved")

    result = base_sync.sync_with_base(ws, project)

    assert result.status == "synced"
    assert (ws.path / "CHANGELOG.md").is_file()
    assert (ws.path / "README.md").read_text(encoding="utf-8") == "edited readme\n"
    assert not (ws.path / "FAILED_LINES").exists()
    assert (ws.path / "new_dir" / "x.txt").read_text(encoding="utf-8") == "untracked\n"
    status = _git(["status", "--porcelain", "--untracked-files=all"], cwd=ws.path).splitlines()
    assert " M README.md" in status and " D FAILED_LINES" in status and "?? new_dir/x.txt" in status
    assert _git(["log", "-1", "--format=%s"], cwd=ws.path).startswith("Merge")  # no "wip" commit leaks


def test_sync_cannot_tell_without_a_reachable_remote_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project = _prepare(tmp_path, monkeypatch, "run-unavail")
    ws = ws_mod.checkout(project, "run-unavail")
    unknown = project.model_copy(update={"default_branch": "no-such-branch"})

    result = base_sync.sync_with_base(ws, unknown)

    assert result.status == "unavailable"
    assert _git(["status", "--porcelain"], cwd=ws.path) == ""
