"""Tests for F6 (USR-285): Exclusão de integração e deploy por recurso.

Acceptance criteria:
1. Integração adquire lease compartilhado por repositório/branch antes de sincronizar base e mergear;
   candidato alterado exige evidência correspondente e CI verde.
2. Deploy é exclusivo por destino, projetos independentes prosseguem;
   perda de posse impede efeitos tardios e deploy/backup continuam autônomos.
3. python -m pytest tests/test_line_resource_lease.py -q passa para mesmo recurso,
   recursos distintos, crash e retomada sem duplicar merge/deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.line.resource_lease import (
    LeaseLostError,
    ResourceBusyError,
    ResourceLeaseCoordinator,
    deploy_resource_id,
    integration_resource_id,
)
from core.line.stage_integration import IntegrationStageHandler
from core.line.stage_release import ReleaseStageHandler
from core.projects.models import (
    DeployConfig,
    DeployTargetType,
    ProjectDescriptor,
    ProjectKind,
    SmokeCheck,
)
from core.workflow.control_contracts import Claim, JobKey, StageContext


def _make_context(
    run_id: str,
    *,
    stage: str = "integration",
    iteration: int = 0,
    ticket_id: str = "USR-44",
    input_refs: list[str] | None = None,
) -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id=ticket_id, plan_version="1", stage=stage, iteration=iteration)
    claim = Claim(
        job_key=jk,
        lease_id=f"claim-lease-{run_id}",
        fencing_token=1,
        owner="worker-1",
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan://line",
        plan_digest="a" * 64,
        config_version="v1",
        environment_ref="test",
        identity="worker-1",
        route_ref=f"route://line/{stage}",
        memory_version="mem_v1",
        input_refs=input_refs or [],
    )


# ---------------------------------------------------------------------------
# Coordinator unit tests: mesmo recurso, recursos distintos, crash e fencing
# ---------------------------------------------------------------------------


def test_coordinator_acquire_same_resource_conflicts(tmp_path: Path) -> None:
    """Acquiring the same resource with different holders raises ResourceBusyError until released."""
    db_file = tmp_path / "lease_test.db"
    coord = ResourceLeaseCoordinator(db_path=db_file)

    now = datetime.now(UTC)
    res_id = "repo:darkfac:main"

    # Holder 1 acquires successfully
    lease1 = coord.acquire(res_id, "repo", "run-101", ttl_s=60, now=now)
    assert lease1.resource_id == res_id
    assert lease1.holder_id == "run-101"
    assert lease1.fencing_token == 1
    assert lease1.status == "active"

    # Holder 2 attempts to acquire the same resource -> ResourceBusyError
    with pytest.raises(ResourceBusyError) as exc_info:
        coord.acquire(res_id, "repo", "run-102", ttl_s=60, now=now)

    assert exc_info.value.resource_id == res_id
    assert exc_info.value.holder_id == "run-101"

    # Holder 1 releases
    released = coord.release(lease1, now=now)
    assert released is True

    # Holder 2 can now acquire successfully
    lease2 = coord.acquire(res_id, "repo", "run-102", ttl_s=60, now=now)
    assert lease2.resource_id == res_id
    assert lease2.holder_id == "run-102"
    # Fencing token incremented after previous lease release
    assert lease2.fencing_token == 2


def test_coordinator_different_resources_succeed_concurrently(tmp_path: Path) -> None:
    """Distinct resources (repos or deploy targets) can be held concurrently without contention."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    now = datetime.now(UTC)

    # Two distinct repository integration resources
    lease_repo1 = coord.acquire("repo:darkfac:main", "repo", "run-A", ttl_s=60, now=now)
    lease_repo2 = coord.acquire("repo:client_portal:main", "repo", "run-B", ttl_s=60, now=now)

    assert lease_repo1.resource_id == "repo:darkfac:main"
    assert lease_repo2.resource_id == "repo:client_portal:main"

    # Two distinct deploy destination resources
    lease_deploy1 = coord.acquire("deploy:dokploy:darkfac-core", "deploy", "run-A", ttl_s=60, now=now)
    lease_deploy2 = coord.acquire("deploy:hostinger_ftp:client_portal", "deploy", "run-B", ttl_s=60, now=now)

    assert lease_deploy1.resource_id == "deploy:dokploy:darkfac-core"
    assert lease_deploy2.resource_id == "deploy:hostinger_ftp:client_portal"

    # Both holders maintain their distinct leases
    assert coord.is_held_by("repo:darkfac:main", "run-A", now=now)
    assert coord.is_held_by("repo:client_portal:main", "run-B", now=now)
    assert coord.is_held_by("deploy:dokploy:darkfac-core", "run-A", now=now)
    assert coord.is_held_by("deploy:hostinger_ftp:client_portal", "run-B", now=now)


def test_coordinator_crash_recovery_increments_fencing_token(tmp_path: Path) -> None:
    """When a holder crashes and lease expires, new holder reclaims with incremented fencing token."""
    coord = ResourceLeaseCoordinator(db_path=tmp_path / "crash_lease.db")
    start_time = datetime.now(UTC)
    res_id = "deploy:dokploy:app-1"

    # Holder 1 acquires lease with 10s TTL
    lease1 = coord.acquire(res_id, "deploy", "run-crash-1", ttl_s=10, now=start_time)
    assert lease1.fencing_token == 1

    # Time passes past expiry (e.g. 15 seconds later)
    after_expiry = start_time + timedelta(seconds=15)

    # Holder 2 acquires the expired resource -> reclaims it
    lease2 = coord.acquire(res_id, "deploy", "run-survivor-2", ttl_s=60, now=after_expiry)
    assert lease2.holder_id == "run-survivor-2"
    assert lease2.fencing_token == 2

    # If Holder 1 wakes up from partition/crash and tries to heartbeat with token 1 -> LeaseLostError
    with pytest.raises(LeaseLostError):
        coord.heartbeat(lease1, now=after_expiry)

    # Holder 1 tries to release -> fails
    assert coord.release(lease1, now=after_expiry) is False


def test_coordinator_reentrant_acquire_by_same_holder(tmp_path: Path) -> None:
    """Same holder re-acquiring an active lease refreshes expiry without raising."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    now1 = datetime.now(UTC)
    res_id = "repo:my_repo:main"

    lease1 = coord.acquire(res_id, "repo", "run-reentrant", ttl_s=30, now=now1)
    assert lease1.fencing_token == 1

    now2 = now1 + timedelta(seconds=10)
    lease2 = coord.acquire(res_id, "repo", "run-reentrant", ttl_s=45, now=now2)

    assert lease2.fencing_token == 1
    assert lease2.expires_at > lease1.expires_at


def test_coordinator_context_manager_hold(tmp_path: Path) -> None:
    """coord.hold() automatically acquires and releases the lease on exit."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    res_id = "deploy:local_service:svc1"

    with coord.hold(res_id, "deploy", "run-ctx", ttl_s=60) as lease:
        assert lease.resource_id == res_id
        assert coord.is_held_by(res_id, "run-ctx")

    # After context exit, lease is released
    assert not coord.is_held_by(res_id, "run-ctx")


# ---------------------------------------------------------------------------
# Integration stage tests: lease, CI requirement for altered candidate, retry
# ---------------------------------------------------------------------------


def test_integration_stage_retries_when_resource_busy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """IntegrationStageHandler returns retry when another run holds repo:branch lease."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        kind=ProjectKind.CORE,
        repo_url="https://github.com/GCamposGit/DarkFactory",
        default_branch="main",
    )
    res_id = integration_resource_id(project, "main")

    # Run A currently holds the integration lease
    coord.acquire(res_id, "repo", "run-other-active", ttl_s=120)

    handler = IntegrationStageHandler(project, resource_coordinator=coord)

    ws_mock = MagicMock()
    ws_mock.path = tmp_path
    monkeypatch.setattr("core.line.workspace.checkout", lambda p, r: ws_mock)
    monkeypatch.setattr("core.line.workspace.find_commit_by_job", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_find_merged_pr", lambda ws: None)

    context = _make_context("run-contender", stage="integration", ticket_id="USR-44", input_refs=["pr_url"])

    result = handler.handle(context)

    # Must return retry with resource_busy cause_code without attempting rebase/merge
    assert result.outcome == "retry"
    assert "resource_busy:" in (result.cause_code or "")
    assert res_id in (result.cause_code or "")


def test_integration_stage_merges_within_lease_and_releases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """IntegrationStageHandler acquires lease, verifies green CI on candidate, merges and releases."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        kind=ProjectKind.CORE,
        repo_url="https://github.com/GCamposGit/DarkFactory",
        default_branch="main",
    )
    res_id = integration_resource_id(project, "main")

    handler = IntegrationStageHandler(project, resource_coordinator=coord)

    ws_mock = MagicMock()
    ws_mock.path = tmp_path
    monkeypatch.setattr("core.line.workspace.checkout", lambda p, r: ws_mock)
    monkeypatch.setattr("core.line.workspace.find_commit_by_job", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_find_merged_pr", lambda ws: None)
    monkeypatch.setattr(handler, "_rebase_onto_default", lambda ws, branch: None)
    monkeypatch.setattr(handler, "_push_force_with_lease", lambda ws: None)
    monkeypatch.setattr(handler, "_strip_context", lambda ws, ctx: ("Title", "Body", None))
    monkeypatch.setattr(handler, "_find_or_create_pr", lambda ws, b, t, body: {"number": 123, "url": "https://pr/123"})
    # CI passes on candidate head
    monkeypatch.setattr(handler, "_evaluate_checks", lambda ws, pr_num, ctx: None)
    monkeypatch.setattr(handler, "_merge_pr", lambda ws, pr_num: ("merge_sha_abc", None))
    monkeypatch.setattr(handler, "_confirm_remote_ancestry", lambda ws, b, sha: None)

    context = _make_context("run-success-merge", stage="integration", ticket_id="USR-45", input_refs=["pr_url"])

    result = handler.handle(context)

    assert result.outcome == "success"
    assert result.output_refs == ["https://pr/123", "merge_sha_abc"]

    # Verifies lease is released upon completion
    assert not coord.is_held_by(res_id, "run-success-merge")


def test_integration_stage_aborts_merge_if_lease_lost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If lease was lost before merging, IntegrationStageHandler fails-closed without merging."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        kind=ProjectKind.CORE,
        repo_url="https://github.com/GCamposGit/DarkFactory",
        default_branch="main",
    )
    res_id = integration_resource_id(project, "main")

    handler = IntegrationStageHandler(project, resource_coordinator=coord)

    ws_mock = MagicMock()
    ws_mock.path = tmp_path
    monkeypatch.setattr("core.line.workspace.checkout", lambda p, r: ws_mock)
    monkeypatch.setattr("core.line.workspace.find_commit_by_job", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_find_merged_pr", lambda ws: None)
    monkeypatch.setattr(handler, "_rebase_onto_default", lambda ws, branch: None)
    monkeypatch.setattr(handler, "_push_force_with_lease", lambda ws: None)
    monkeypatch.setattr(handler, "_strip_context", lambda ws, ctx: ("Title", "Body", None))
    monkeypatch.setattr(handler, "_find_or_create_pr", lambda ws, b, t, body: {"number": 123, "url": "https://pr/123"})
    merge_called = False

    def _mock_merge(ws, pr_num):
        nonlocal merge_called
        merge_called = True
        return ("merge_sha", None)

    monkeypatch.setattr(handler, "_merge_pr", _mock_merge)

    def _eval_checks_and_steal_lease(ws, pr_num, ctx):
        # Simulate fence loss / lease stolen by another worker
        coord.acquire(res_id, "repo", "run-stolen", ttl_s=60, now=datetime.now(UTC) + timedelta(seconds=200))
        return None

    monkeypatch.setattr(handler, "_evaluate_checks", _eval_checks_and_steal_lease)

    context = _make_context("run-lease-lost", stage="integration", ticket_id="USR-46", input_refs=["pr_url"])

    result = handler.handle(context)

    # Returns retry:lease_lost and NEVER executes merge
    assert result.outcome == "retry"
    assert result.cause_code == "lease_lost"
    assert merge_called is False


# ---------------------------------------------------------------------------
# Deploy stage tests: destination exclusivity, independent projects, fencing
# ---------------------------------------------------------------------------


def test_deploy_stage_destination_exclusivity(tmp_path: Path) -> None:
    """Two projects deploying to the same destination contend for the deploy lease."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")

    proj1 = ProjectDescriptor(
        id="app1",
        name="App 1",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={"compose_id": "shared-vps-cluster"}),
    )
    proj2 = ProjectDescriptor(
        id="app2",
        name="App 2",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={"compose_id": "shared-vps-cluster"}),
    )

    dest1 = deploy_resource_id(proj1)
    dest2 = deploy_resource_id(proj2)
    assert dest1 == dest2 == "deploy:dokploy:shared-vps-cluster"

    # App 1 acquires destination lease
    coord.acquire(dest1, "deploy", "run-app1", ttl_s=60)

    # App 2 attempts to acquire -> ResourceBusyError
    with pytest.raises(ResourceBusyError):
        coord.acquire(dest2, "deploy", "run-app2", ttl_s=60)


def test_deploy_stage_independent_projects_proceed_concurrently(tmp_path: Path) -> None:
    """Projects with independent destinations proceed concurrently without contention."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")

    darkfac_proj = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={"compose_id": "darkfac-core"}),
    )
    client_proj = ProjectDescriptor(
        id="client_portal",
        name="Client Portal",
        deploy=DeployConfig(type=DeployTargetType.HOSTINGER_FTP, params={"host": "ftp.client.com"}),
    )

    dest_darkfac = deploy_resource_id(darkfac_proj)
    dest_client = deploy_resource_id(client_proj)

    assert dest_darkfac != dest_client
    assert dest_darkfac == "deploy:dokploy:darkfac-core"
    assert dest_client == "deploy:hostinger_ftp:ftp.client.com"

    # Both acquire their respective deploy destination leases concurrently
    lease1 = coord.acquire(dest_darkfac, "deploy", "run-darkfac", ttl_s=60)
    lease2 = coord.acquire(dest_client, "deploy", "run-client", ttl_s=60)

    assert lease1.resource_id == dest_darkfac
    assert lease2.resource_id == dest_client


def test_deploy_stage_aborts_when_lease_lost(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ReleaseStageHandler checks lease validity before deploying, aborting if lease was lost."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={"compose_id": "darkfac-core"}),
        smoke=[SmokeCheck(url="http://localhost:8080/health")],
    )
    handler = ReleaseStageHandler(project, resource_coordinator=coord)

    state_mock = MagicMock()
    state_mock.last_good_sha = "older_sha"
    state_mock.runs = {}
    monkeypatch.setattr(handler.state_store, "load", lambda p: state_mock)

    # Force is_held_by to return False before deploy
    monkeypatch.setattr(coord, "is_held_by", lambda *a, **k: False)

    deploy_called = False

    def _mock_deploy(*a, **k):
        nonlocal deploy_called
        deploy_called = True
        return (MagicMock(operation_id="op-1"), MagicMock(), None)

    monkeypatch.setattr(handler, "_deploy", _mock_deploy)

    context = _make_context("run-lost-deploy", stage="release", ticket_id="USR-47", input_refs=["https://pr/1", "sha_new"])

    result = handler.handle(context)

    # Returns retry:lease_lost and DOES NOT invoke deploy
    assert result.outcome == "retry"
    assert result.cause_code == "lease_lost"
    assert deploy_called is False


# ---------------------------------------------------------------------------
# Idempotent resume tests: no duplicate merge or deploy
# ---------------------------------------------------------------------------


def test_idempotent_integration_resume_does_not_duplicate_merge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When a run is resumed after PR was already merged, integration returns success without re-merging."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        repo_url="https://github.com/GCamposGit/DarkFactory",
        default_branch="main",
    )
    handler = IntegrationStageHandler(project, resource_coordinator=coord)

    ws_mock = MagicMock()
    ws_mock.path = tmp_path
    monkeypatch.setattr("core.line.workspace.checkout", lambda p, r: ws_mock)
    monkeypatch.setattr("core.line.workspace.find_commit_by_job", lambda *a, **k: None)

    # PR is already merged
    monkeypatch.setattr(
        handler,
        "_find_merged_pr",
        lambda ws: {"url": "https://pr/already-merged", "mergeCommit": {"oid": "sha_merged_already"}},
    )
    monkeypatch.setattr(handler, "_confirm_remote_ancestry", lambda ws, b, sha: None)

    rebase_called = False
    merge_called = False

    def _mock_rebase(*a, **k):
        nonlocal rebase_called
        rebase_called = True
        return None

    def _mock_merge(*a, **k):
        nonlocal merge_called
        merge_called = True
        return ("sha_merged_already", None)

    monkeypatch.setattr(handler, "_rebase_onto_default", _mock_rebase)
    monkeypatch.setattr(handler, "_merge_pr", _mock_merge)

    context = _make_context("run-already-merged", stage="integration", ticket_id="USR-48", input_refs=["pr_url"])

    result = handler.handle(context)

    assert result.outcome == "success"
    assert result.output_refs == ["https://pr/already-merged", "sha_merged_already"]
    assert rebase_called is False
    assert merge_called is False


def test_idempotent_deploy_resume_does_not_duplicate_deploy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When a run is resumed and merge_sha was already deployed, release returns success without re-deploying."""
    coord = ResourceLeaseCoordinator(db_path=":memory:")
    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={"compose_id": "darkfac-core"}),
    )
    handler = ReleaseStageHandler(project, resource_coordinator=coord)

    state_mock = MagicMock()
    state_mock.last_good_sha = "sha_deployed_already"
    state_mock.last_success_operation_id = "op-prev-123"
    state_mock.last_success_smoke_ref = "smoke:prev"
    state_mock.runs = {}
    monkeypatch.setattr(handler.state_store, "load", lambda p: state_mock)

    deploy_called = False

    def _mock_deploy(*a, **k):
        nonlocal deploy_called
        deploy_called = True
        return (MagicMock(), MagicMock(), None)

    monkeypatch.setattr(handler, "_deploy", _mock_deploy)

    context = _make_context("run-already-deployed", stage="release", ticket_id="USR-49", input_refs=["pr_url", "sha_deployed_already"])

    result = handler.handle(context)

    assert result.outcome == "success"
    assert result.output_refs == ["op-prev-123", "smoke:prev"]
    assert deploy_called is False
