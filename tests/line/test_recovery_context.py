"""A rollback can read the run's planning inputs after the product merge."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.line import recovery_context, workspace
from core.line.stage_build import DevelopmentStage
from core.line.stage_integration import IntegrationStageHandler
from core.line.stage_release import ReleaseStageHandler, ReleaseState, ReleaseStateStore
from core.line.workspace import WorkspaceError
from core.orchestrator.deployment_adapter import (
    DeploymentOperation,
    DeploymentStatus,
    RollbackResult,
    TargetConfig,
)
from core.projects.models import DeployConfig, DeployTargetType, SmokeCheck
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import Claim, JobKey, StageContext, StageResult


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
        if args and args[0] == "push" and any("refs/heads/darkfac-context/" in arg for arg in args):
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


def test_stripped_merged_deleted_branch_rolls_back_and_develops_one_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second worker resumes the full integration-to-release failure path."""
    project = _project(tmp_path).model_copy(update={
        "deploy": DeployConfig(type=DeployTargetType.DOKPLOY, params={}),
        "smoke": [SmokeCheck(url="https://sample.invalid/health", expect_status=200)],
    })
    run_id = "run-e2e-recovery"
    seed = tmp_path / "seed"
    previous_good_sha = _git(["rev-parse", "HEAD"], seed)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-integration"))
    planned = _planned_workspace(project, run_id)
    (planned.path / "app.txt").write_text("new product\n", encoding="utf-8")
    workspace.commit(planned, "test: candidate", f"{run_id}:T1")
    workspace.push(planned)

    claim = Claim(
        job_key=JobKey(run_id=run_id, ticket_id="T1", plan_version="1.0", stage="integration", iteration=0),
        lease_id="lease-e2e", owner="worker-integration", fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    context = StageContext(
        claim=claim, plan_ref="plan://e2e", plan_digest="a" * 64,
        config_version="v1", environment_ref=project.id, identity="worker-integration",
        route_ref="route://line/integration", memory_version="mem_v1", input_refs=[],
    )
    _, _, strip_error = IntegrationStageHandler(project)._strip_context(planned, context)
    assert strip_error is None
    assert not workspace.context_dir(planned).exists()
    assert _git(["ls-remote", "origin", recovery_context.context_ref(run_id)], planned.path)

    # Simulate GitHub's squash merge and branch deletion using the bare remote.
    _git(["fetch", "origin"], seed)
    _git(["merge", "--squash", f"origin/{planned.branch}"], seed)
    _git(["-c", "user.name=test", "-c", "user.email=test@example.invalid",
          "commit", "-m", "merge candidate"], seed)
    merged_sha = _git(["rev-parse", "HEAD"], seed)
    _git(["push", "origin", "main"], seed)
    _git(["push", "origin", "--delete", planned.branch], seed)
    assert ".darkfac/runs/" not in _git(["ls-tree", "-r", "--name-only", "HEAD"], seed)

    class Deployment:
        def __init__(self) -> None:
            self.installed = previous_good_sha
            self.rollbacks: list[str] = []

        def start(self, artifact_ref, target_config: TargetConfig, claim=None) -> DeploymentOperation:
            self.installed = artifact_ref.byte_digest
            return DeploymentOperation(
                operation_id="deploy-candidate", external_operation_id="external-candidate",
                project_id=target_config.project_id, target_type=target_config.target_type,
                artifact_ref=artifact_ref, status=DeploymentStatus.SUCCEEDED,
            )

        def installed_digest(self, target_config: TargetConfig) -> str:
            return self.installed

        def rollback(self, target_config: TargetConfig, failed_digest: str, reason: str, claim=None) -> RollbackResult:
            self.installed = target_config.last_known_good_digest
            self.rollbacks.append(failed_digest)
            return RollbackResult(
                rollback_id="rollback-candidate", project_id=target_config.project_id,
                failed_digest=failed_digest, restored_digest=self.installed,
                status=DeploymentStatus.ROLLED_BACK, reason=reason,
            )

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-release"))
    deployment = Deployment()
    smoke_responses = iter([(500, b"candidate unhealthy"), (200, b"previous version healthy")])
    state_store = ReleaseStateStore(state_dir=tmp_path / "release-state")
    state_store.save(project.id, ReleaseState(last_good_sha=previous_good_sha))
    release = ReleaseStageHandler(
        project, dokploy_adapter=deployment, state_store=state_store,
        smoke_opener=lambda _url, _timeout: next(smoke_responses),
        smoke_retries=1, smoke_delay_s=0, sleep=lambda _seconds: None,
    )
    release_context = context.model_copy(update={
        "input_refs": ["", merged_sha],
        "claim": claim.model_copy(update={
            "job_key": claim.job_key.model_copy(update={"stage": "release"}),
        }),
    })
    outcome = release.handle(release_context)
    assert outcome.outcome == "retry"
    assert (outcome.cause_code or "").startswith("retry:development\nprod_smoke_failed_rolled_back_to_")
    assert deployment.rollbacks == [merged_sha]
    assert deployment.installed == previous_good_sha

    recovered = workspace.checkout(project, run_id)
    directory = workspace.context_dir(recovered)
    assert (directory / "SPEC.md").read_text(encoding="utf-8") == "# Original specification\n"
    assert json.loads((directory / "tickets.json").read_text(encoding="utf-8"))[0]["id"] == "T1"
    assert not (directory / "progress.json").exists()
    feedback = (directory / "PROD_SMOKE_FAILURE.md").read_text(encoding="utf-8")
    assert merged_sha in feedback and "500 (expected 200)" in feedback

    development = DevelopmentStage()
    developed: list[str] = []

    def develop_fix(_ws, _run_id, _project, ticket, _index, _progress, *, initial_feedback=None):
        assert initial_feedback == feedback
        assert ticket.id.startswith("fix-prod-smoke-")
        assert ticket.id != "T1"
        developed.append(ticket.id)
        (_ws.path / "smoke_fix.txt").write_text("candidate corrected\n", encoding="utf-8")
        fix_sha = workspace.commit(_ws, "test: repair production smoke", f"{_run_id}:{ticket.id}")
        workspace.push(_ws)
        return StageResult(outcome="success", output_refs=[fix_sha])

    monkeypatch.setattr(development, "_develop_ticket", develop_fix)
    development_outcome = development.run(project, run_id)
    assert development_outcome.outcome == "success"
    assert development_outcome.cause_code != "no_tickets"
    assert len(developed) == 1
    fix_sha = development_outcome.output_refs[0]
    assert fix_sha != merged_sha
    assert _git(["rev-parse", f"refs/heads/{recovered.branch}"], recovered.path) == fix_sha
    assert _git(["ls-remote", "origin", f"refs/heads/{recovered.branch}"], recovered.path).startswith(fix_sha)
