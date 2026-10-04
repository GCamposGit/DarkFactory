"""A rollback can read the run's planning inputs after the product merge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.line import recovery_context, workspace
from core.line.stage_build import DevelopmentStage
from core.line.workspace import WorkspaceError
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageResult


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", check=True,
    )
    return result.stdout.strip()


def _project(tmp_path: Path) -> ProjectDescriptor:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_path)
    seed = tmp_path / "seed"
    _git(["clone", str(origin), str(seed)], tmp_path)
    _git(["checkout", "-B", "main"], seed)
    (seed / "README.md").write_text("product\n", encoding="utf-8")
    _git(["add", "."], seed)
    _git(["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed"], seed)
    _git(["push", "origin", "main"], seed)
    return ProjectDescriptor(id="sample", name="Sample", repo_url=str(origin), default_branch="main")


def _planned_workspace(project: ProjectDescriptor, run_id: str) -> workspace.RunWorkspace:
    ws = workspace.checkout(project, run_id)
    workspace.write_context(ws, "SPEC.md", "# Original specification\n")
    workspace.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Repair smoke"}]))
    workspace.write_context(ws, "progress.json", '{"failed_candidate": true}')
    workspace.commit(ws, "test: planning context", f"{run_id}:planning")
    workspace.push(ws)
    return ws


def test_context_ref_survives_branch_deletion_and_new_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-a"))
    ws = _planned_workspace(project, "run-134")
    pinned_sha = recovery_context.preserve_context(ws)
    assert recovery_context.preserve_context(ws) == pinned_sha  # idempotent replay
    _git(["push", "origin", "--delete", ws.branch], ws.path)

    # A new worker has only the remote default branch and the recovery ref.
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-b"))
    resumed = workspace.checkout(project, "run-134")
    assert not workspace.context_dir(resumed).exists()
    assert recovery_context.restore_context(resumed)
    directory = workspace.context_dir(resumed)
    assert (directory / "SPEC.md").read_text(encoding="utf-8") == "# Original specification\n"
    assert json.loads((directory / "tickets.json").read_text(encoding="utf-8"))[0]["id"] == "T1"
    assert not (directory / "progress.json").exists()
    main_tree = _git(["ls-tree", "-r", "--name-only", "origin/main"], resumed.path)
    assert ".darkfac/runs/" not in main_tree


def test_archive_push_failure_leaves_context_in_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker"))
    ws = _planned_workspace(project, "run-push-fails")
    real_run_git = recovery_context.workspace._run_git

    def reject_context_push(args: list[str], **kwargs):
        if args and args[0] == "push" and any("refs/darkfac/context/" in arg for arg in args):
            raise WorkspaceError("simulated push rejection")
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(recovery_context.workspace, "_run_git", reject_context_push)
    with pytest.raises(WorkspaceError, match="simulated push rejection"):
        recovery_context.preserve_context(ws)
    assert (workspace.context_dir(ws) / "tickets.json").is_file()
    assert (workspace.context_dir(ws) / "SPEC.md").is_file()


def test_smoke_recovery_develops_one_fix_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker"))
    ws = _planned_workspace(project, "run-smoke")
    feedback = "# Production smoke failure\n\nSHA: badbad\nReason: failed health check\n"
    workspace.write_context(ws, "PROD_SMOKE_FAILURE.md", feedback)
    workspace.commit(ws, "test: smoke feedback", "run-smoke:release:smoke_failure")
    workspace.push(ws)

    stage = DevelopmentStage()
    calls: list[str] = []

    def develop(_ws, run_id, _project, ticket, _index, _progress, *, initial_feedback=None):
        calls.append(ticket.id)
        assert ticket.id.startswith("fix-prod-smoke-")
        assert initial_feedback == feedback
        assert ticket.title != "Repair smoke"  # original ticket is retained, not replayed
        (_ws.path / "smoke_fix.txt").write_text("fixed\n", encoding="utf-8")
        sha = workspace.commit(_ws, "test: smoke correction", f"{run_id}:{ticket.id}")
        workspace.push(_ws)
        return StageResult(outcome="success", output_refs=[sha])

    monkeypatch.setattr(stage, "_develop_ticket", develop)
    first = stage.run(project, "run-smoke")
    second = stage.run(project, "run-smoke")
    assert (first.outcome, second.outcome) == ("success", "success")
    assert first.output_refs == second.output_refs
    assert len(calls) == 1
    assert workspace.find_commit_by_job(ws, "run-smoke:T1") is None
