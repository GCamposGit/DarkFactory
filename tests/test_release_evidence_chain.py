"""Deterministic unit tests for HF-12-04: Evidence-Chain Automated Release.

Governed by:
- docs/handoffs/continuous-autonomy/HF-12-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/orchestrator/release_pipeline.py
- core/workflow/release_handlers.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.orchestrator.release_pipeline import (
    CANONICAL_TARGET_PROFILES,
    ClientAcceptanceRequiredError,
    DeploymentStage,
    EvidenceMismatchError,
    ProjectTier,
    ProductionDeploymentFailedError,
    ReleasePipelineService,
    SimulationNotPromotedError,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
)
from core.workflow.handlers import (
    build_handlers,
    dispatch_stage,
)
from core.workflow.release_handlers import (
    BuildDeployHandler,
    ProductionReleaseHandler,
    create_release_bindings,
)

BASE_TIME = datetime(2026, 9, 18, 16, 0, 0, tzinfo=UTC)


def _make_context(
    ticket_id: str,
    stage: str = "build_deploy",
    candidate_digest: str | None = None,
    input_refs: list[str] | None = None,
    expires_at: str | None = None,
    environment_ref: str = "env-release",
) -> StageContext:
    jk = JobKey(
        run_id="run-rel-01",
        ticket_id=ticket_id,
        plan_version="1.0",
        stage=stage,
        iteration=0,
    )
    claim = Claim(
        job_key=jk,
        lease_id="lease-rel-01",
        owner="release-worker",
        fencing_token=1,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-1",
        plan_digest="a" * 64,
        candidate_digest=candidate_digest or ("b" * 64),
        config_version="1.0",
        environment_ref=environment_ref,
        identity="worker-release",
        route_ref="default",
        memory_version="1.0",
        input_refs=input_refs or [],
    )


def test_caller_supplied_boolean_or_simulation_does_not_promote() -> None:
    """Validate that simulation or bare caller flags cannot promote to production."""
    pipeline = ReleasePipelineService()
    artifact = pipeline.build("proj-sim", "gitsha1234567890abcdef")

    # Flagged as simulation must fail closed
    with pytest.raises(SimulationNotPromotedError, match="cannot promote simulation"):
        pipeline.promote_with_evidence_chain(
            artifact_digest=artifact.artifact_digest,
            project_tier=ProjectTier.INTERNAL_FREE,
            is_simulation=True,
        )

    # Caller approval flag False must fail closed
    with pytest.raises(SimulationNotPromotedError, match="Caller approval flag is False"):
        pipeline.promote_with_evidence_chain(
            artifact_digest=artifact.artifact_digest,
            project_tier=ProjectTier.INTERNAL_FREE,
            caller_approval_flag=False,
        )


def test_stale_or_mismatched_digest_fails_closed() -> None:
    """Validate that mismatched artifact digest raises EvidenceMismatchError."""
    pipeline = ReleasePipelineService()
    artifact = pipeline.build("proj-mismatch", "gitsha1234567890abcdef")

    # Mismatched receipt digest
    wrong_digest = "c" * 64
    with pytest.raises(EvidenceMismatchError):
        pipeline.promote_with_evidence_chain(
            artifact_digest=wrong_digest,
            project_tier=ProjectTier.INTERNAL_FREE,
        )


def test_commercial_paid_project_requires_signed_client_acceptance() -> None:
    """Validate that commercial paid projects require signed client acceptance for exact digest."""
    pipeline = ReleasePipelineService()
    artifact = pipeline.build("client-alpha", "gitsha1234567890abcdef")

    # Attempting promotion without acceptance fails closed
    with pytest.raises(ClientAcceptanceRequiredError):
        pipeline.promote_with_evidence_chain(
            artifact_digest=artifact.artifact_digest,
            project_tier=ProjectTier.COMMERCIAL_PAID,
        )

    # Record client acceptance with matching digest
    receipt = pipeline.record_client_acceptance(
        project_id="client-alpha",
        artifact_digest=artifact.artifact_digest,
        client_id="acme-corp",
        approved_by="alice@acme.com",
    )
    assert receipt.artifact_digest == artifact.artifact_digest

    # Now promotion succeeds
    record = pipeline.promote_with_evidence_chain(
        artifact_digest=artifact.artifact_digest,
        project_tier=ProjectTier.COMMERCIAL_PAID,
    )
    assert record.stage == DeploymentStage.DELIVERED
    assert record.artifact_digest == artifact.artifact_digest


def test_production_failure_triggers_automatic_rollback_without_affecting_other_projects() -> None:
    """Validate that journey test failure triggers rollback and leaves concurrent project intact."""
    pipeline = ReleasePipelineService()

    # Project 1: successfully deployed to production
    art_1 = pipeline.build("proj-healthy", "gitsha1111111111111111")
    rec_1 = pipeline.deploy_production(art_1, project_tier=ProjectTier.INTERNAL_FREE)
    assert rec_1.stage == DeploymentStage.DELIVERED
    assert pipeline.get_current_stable_artifact("proj-healthy") == art_1.artifact_digest

    # Project 2: fails journey smoke test in production
    art_2 = pipeline.build("proj-faulty", "gitsha2222222222222222")
    with pytest.raises(ProductionDeploymentFailedError):
        pipeline.deploy_production(art_2, project_tier=ProjectTier.INTERNAL_FREE, smoke_passed=False)

    # Verify rollback recorded for proj-faulty
    rollbacks_2 = pipeline.get_rollbacks("proj-faulty")
    assert len(rollbacks_2) == 1
    assert rollbacks_2[0].failed_artifact_digest == art_2.artifact_digest

    # Invariant: Project 1 remains fully stable and delivered
    assert pipeline.get_current_stable_artifact("proj-healthy") == art_1.artifact_digest
    assert len(pipeline.get_rollbacks("proj-healthy")) == 0


def test_end_to_end_evidence_chain_release_handlers() -> None:
    """Validate StageHandler execution flow: build_deploy -> acceptance -> target_journey."""
    pipeline = ReleasePipelineService()
    build_handler = BuildDeployHandler(pipeline=pipeline)
    release_handler = ProductionReleaseHandler(pipeline=pipeline, project_tier=ProjectTier.COMMERCIAL_PAID)

    # 1. Build & staging deploy stage
    ctx_build = _make_context("client-beta", stage="build_deploy")
    res_build = build_handler.handle(ctx_build)
    assert res_build.outcome == "success"
    assert any("artifact://" in ref for ref in res_build.output_refs)

    artifact_ref = next(ref for ref in res_build.output_refs if ref.startswith("artifact://"))
    artifact_digest = artifact_ref.replace("artifact://", "")

    # 2. Production release handler blocked before client acceptance
    ctx_release = _make_context(
        "client-beta",
        stage="target_journey",
        candidate_digest=artifact_digest,
        input_refs=[artifact_ref],
    )
    res_blocked = release_handler.handle(ctx_release)
    assert res_blocked.outcome == "failed"
    assert res_blocked.cause_code == "ClientAcceptanceRequiredError"

    # 3. Client records approval
    pipeline.record_client_acceptance(
        project_id="client-beta",
        artifact_digest=artifact_digest,
        client_id="beta-corp",
        approved_by="bob@beta.com",
    )

    # 4. Production release handler succeeds with verified operational proof
    res_promoted = release_handler.handle(ctx_release)
    assert res_promoted.outcome == "success"
    assert any("deployment://production/" in ref for ref in res_promoted.output_refs)


# ---------------------------------------------------------------------------
# 6. Canonical Factory Targets Matrix (darkfac, site-ggcampos, segundo-cerebro, jarvis)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "project_id,expected_type,expected_endpoint",
    [
        ("darkfac", "dokploy_docker_compose", "https://darkhub.ggcampos.com/health"),
        ("site-ggcampos", "static_web", "https://ggcampos.com"),
        ("segundo-cerebro", "local_mcp_service", "stdio://python -m segundocerebro.mcp.server"),
        ("jarvis", "local_daemon_service", "http://127.0.0.1:8088/health"),
    ],
)
def test_canonical_targets_matrix_profile_and_smoke_journey_verification(
    project_id: str,
    expected_type: str,
    expected_endpoint: str,
) -> None:
    """Verifies that all 4 canonical targets have normative profiles and execute target-specific journeys."""
    assert project_id in CANONICAL_TARGET_PROFILES
    profile = CANONICAL_TARGET_PROFILES[project_id]
    assert profile.target_type == expected_type
    assert profile.health_endpoint == expected_endpoint
    assert len(profile.preflight_checks) >= 3
    assert len(profile.production_scenarios) >= 3

    pipeline = ReleasePipelineService()
    git_sha = f"sha_{project_id}_0123456789abcdef"
    art = pipeline.build(project_id, git_sha)

    # 1. Staging deployment: uses canonical target staging scenarios
    stg_rec = pipeline.deploy_staging(art)
    assert stg_rec.stage == DeploymentStage.STAGING_DEPLOYED
    assert stg_rec.preflight.checks == profile.preflight_checks
    assert stg_rec.smoke_test is not None
    assert stg_rec.smoke_test.scenarios_executed == profile.staging_scenarios

    # 2. Production promotion: uses canonical target production scenarios
    prd_rec = pipeline.promote_with_evidence_chain(
        artifact_digest=art.artifact_digest,
        project_tier=ProjectTier.INTERNAL_FREE,
    )
    assert prd_rec.stage == DeploymentStage.DELIVERED
    assert prd_rec.preflight.checks == profile.preflight_checks
    assert prd_rec.smoke_test is not None
    assert prd_rec.smoke_test.scenarios_executed == profile.production_scenarios
    assert pipeline.get_current_stable_artifact(project_id) == art.artifact_digest


@pytest.mark.parametrize(
    "project_id",
    ["darkfac", "site-ggcampos", "segundo-cerebro", "jarvis"],
)
def test_canonical_targets_matrix_production_smoke_failure_triggers_isolated_rollback(
    project_id: str,
) -> None:
    """Verifies that journey smoke failure on any of the 4 canonical targets triggers isolated rollback."""
    pipeline = ReleasePipelineService()

    # Initial stable version
    art_v1 = pipeline.build(project_id, f"sha_{project_id}_v1")
    pipeline.deploy_production(art_v1, project_tier=ProjectTier.INTERNAL_FREE)
    assert pipeline.get_current_stable_artifact(project_id) == art_v1.artifact_digest

    # New version failing smoke
    art_v2 = pipeline.build(project_id, f"sha_{project_id}_v2")
    with pytest.raises(ProductionDeploymentFailedError, match="Automatic rollback executed"):
        pipeline.deploy_production(art_v2, project_tier=ProjectTier.INTERNAL_FREE, smoke_passed=False)

    rollbacks = pipeline.get_rollbacks(project_id)
    assert len(rollbacks) == 1
    rb = rollbacks[0]
    assert rb.project_id == project_id
    assert rb.failed_artifact_digest == art_v2.artifact_digest
    assert rb.restored_artifact_digest == art_v1.artifact_digest
    assert "Production journey smoke test failed" in rb.trigger_reason
    # Stable version restored
    assert pipeline.get_current_stable_artifact(project_id) == art_v1.artifact_digest


# ---------------------------------------------------------------------------
# 7. Failsafe of stale_lease in Handlers
# ---------------------------------------------------------------------------

def test_build_deploy_handler_stale_lease_fails_closed() -> None:
    """Validate that BuildDeployHandler immediately aborts on expired lease (stale_lease)."""
    pipeline = ReleasePipelineService()
    handler = BuildDeployHandler(pipeline=pipeline)

    past_time = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    ctx_stale = _make_context("darkfac", stage="build_deploy", expires_at=past_time)

    res = handler.handle(ctx_stale)
    assert res.outcome == "failed"
    assert res.cause_code == "stale_lease"
    assert res.output_refs == []
    assert res.evidence_refs == []

    # Verify no artifact or deployment was created in pipeline
    assert len(pipeline.get_deployments("darkfac")) == 0


def test_production_release_handler_stale_lease_fails_closed() -> None:
    """Validate that ProductionReleaseHandler immediately aborts on expired lease (stale_lease)."""
    pipeline = ReleasePipelineService()
    art = pipeline.build("darkfac", "sha_darkfac_0123456789")

    handler = ProductionReleaseHandler(pipeline=pipeline, project_tier=ProjectTier.INTERNAL_FREE)

    past_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    ctx_stale = _make_context(
        "darkfac",
        stage="target_journey",
        candidate_digest=art.artifact_digest,
        input_refs=[f"artifact://{art.artifact_digest}"],
        expires_at=past_time,
    )

    res = handler.handle(ctx_stale)
    assert res.outcome == "failed"
    assert res.cause_code == "stale_lease"
    assert res.output_refs == []
    assert res.evidence_refs == []

    # Verify no production deployment occurred
    assert pipeline.get_current_stable_artifact("darkfac") is None


def test_fresh_lease_succeeds_both_handlers() -> None:
    """Validate that fresh (non-expired) lease succeeds through both BuildDeploy and ProductionRelease handlers."""
    pipeline = ReleasePipelineService()
    build_handler = BuildDeployHandler(pipeline=pipeline)
    release_handler = ProductionReleaseHandler(pipeline=pipeline, project_tier=ProjectTier.INTERNAL_FREE)

    future_time = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    ctx_build = _make_context("darkfac", stage="build_deploy", expires_at=future_time)
    res_build = build_handler.handle(ctx_build)
    assert res_build.outcome == "success"

    art_ref = next(r for r in res_build.output_refs if r.startswith("artifact://"))
    art_digest = art_ref.replace("artifact://", "")

    ctx_release = _make_context(
        "darkfac",
        stage="target_journey",
        candidate_digest=art_digest,
        input_refs=[art_ref],
        expires_at=future_time,
    )
    res_release = release_handler.handle(ctx_release)
    assert res_release.outcome == "success"
    assert pipeline.get_current_stable_artifact("darkfac") == art_digest


# ---------------------------------------------------------------------------
# 8. Multi-Version Chained Rollback
# ---------------------------------------------------------------------------

def test_multi_version_chained_rollback_restores_preceding_stable_preserving_other_projects() -> None:
    """Validate: v1 stable -> v2 promoted -> v3 fails smoke -> deterministic rollback restores v2.

    Also ensures concurrent project B is completely unaffected.
    """
    pipeline = ReleasePipelineService()

    # 1. Independent concurrent project B ("jarvis") establishes stable baseline
    art_b1 = pipeline.build("jarvis", "sha_jarvis_v1_baseline")
    pipeline.deploy_production(art_b1, project_tier=ProjectTier.INTERNAL_FREE)
    assert pipeline.get_current_stable_artifact("jarvis") == art_b1.artifact_digest

    # 2. Target project A ("darkfac"): deploy v1 (stable)
    art_a1 = pipeline.build("darkfac", "sha_darkfac_v1_1111111111111111")
    rec_a1 = pipeline.deploy_production(art_a1, project_tier=ProjectTier.INTERNAL_FREE)
    assert rec_a1.stage == DeploymentStage.DELIVERED
    assert pipeline.get_current_stable_artifact("darkfac") == art_a1.artifact_digest
    assert pipeline.get_stable_history("darkfac") == [art_a1.artifact_digest]

    # 3. Target project A: deploy v2 (successfully promoted to production)
    art_a2 = pipeline.build("darkfac", "sha_darkfac_v2_2222222222222222")
    rec_a2 = pipeline.deploy_production(art_a2, project_tier=ProjectTier.INTERNAL_FREE)
    assert rec_a2.stage == DeploymentStage.DELIVERED
    assert pipeline.get_current_stable_artifact("darkfac") == art_a2.artifact_digest
    assert pipeline.get_stable_history("darkfac") == [art_a1.artifact_digest, art_a2.artifact_digest]

    # 4. Target project A: deploy v3 (fails production journey smoke test)
    art_a3 = pipeline.build("darkfac", "sha_darkfac_v3_3333333333333333")
    with pytest.raises(ProductionDeploymentFailedError) as exc_info:
        pipeline.deploy_production(art_a3, project_tier=ProjectTier.INTERNAL_FREE, smoke_passed=False)

    assert art_a2.artifact_digest in str(exc_info.value)

    # 5. Deterministic rollback verified: v2 restored, NOT v1!
    rollbacks_a = pipeline.get_rollbacks("darkfac")
    assert len(rollbacks_a) == 1
    rb_a = rollbacks_a[0]
    assert rb_a.project_id == "darkfac"
    assert rb_a.failed_artifact_digest == art_a3.artifact_digest
    assert rb_a.restored_artifact_digest == art_a2.artifact_digest
    assert pipeline.get_current_stable_artifact("darkfac") == art_a2.artifact_digest

    # 6. Invariant check: Project B ("jarvis") is completely intact
    assert pipeline.get_current_stable_artifact("jarvis") == art_b1.artifact_digest
    assert len(pipeline.get_rollbacks("jarvis")) == 0

    # 7. Chained rollback: subsequent rollback of v2 to v1
    rb_prev = pipeline.rollback_to_previous("darkfac", reason="Emergency rollback of v2 to v1")
    assert rb_prev.failed_artifact_digest == art_a2.artifact_digest
    assert rb_prev.restored_artifact_digest == art_a1.artifact_digest
    assert pipeline.get_current_stable_artifact("darkfac") == art_a1.artifact_digest

    # Project B remains intact
    assert pipeline.get_current_stable_artifact("jarvis") == art_b1.artifact_digest
    assert len(pipeline.get_rollbacks("jarvis")) == 0


# ---------------------------------------------------------------------------
# 9. Canonical Integration with build_handlers() and dispatch_stage()
# ---------------------------------------------------------------------------

def test_canonical_dispatch_stage_integration_with_release_handlers() -> None:
    """Validate canonical integration with build_handlers() and dispatch_stage()."""
    pipeline = ReleasePipelineService()
    bindings = create_release_bindings(pipeline=pipeline, project_tier=ProjectTier.INTERNAL_FREE)

    registry = build_handlers(bindings=bindings)
    assert ("build_deploy", "v1") in registry
    assert ("target_journey", "v1") in registry
    assert isinstance(registry[("build_deploy", "v1")], BuildDeployHandler)
    assert isinstance(registry[("target_journey", "v1")], ProductionReleaseHandler)

    # 1. Dispatch build_deploy stage
    ctx_build = _make_context("site-ggcampos", stage="build_deploy")
    res_build = dispatch_stage(registry, ctx_build)
    assert res_build.outcome == "success"
    assert any("artifact://" in r for r in res_build.output_refs)
    assert any("deployment://staging/" in r for r in res_build.output_refs)

    art_ref = next(r for r in res_build.output_refs if r.startswith("artifact://"))
    art_digest = art_ref.replace("artifact://", "")

    # 2. Dispatch target_journey stage
    ctx_release = _make_context(
        "site-ggcampos",
        stage="target_journey",
        candidate_digest=art_digest,
        input_refs=[art_ref],
    )
    res_release = dispatch_stage(registry, ctx_release)
    assert res_release.outcome == "success"
    assert any("deployment://production/" in r for r in res_release.output_refs)
    assert any("receipt://production/" in r for r in res_release.evidence_refs)
    assert any("receipt://journey/" in r for r in res_release.evidence_refs)
    assert pipeline.get_current_stable_artifact("site-ggcampos") == art_digest


def test_canonical_dispatch_stage_commercial_paid_with_acceptance() -> None:
    """Validate dispatch_stage for COMMERCIAL_PAID tier enforcing client acceptance gate."""
    pipeline = ReleasePipelineService()
    bindings = create_release_bindings(pipeline=pipeline, project_tier=ProjectTier.COMMERCIAL_PAID)
    registry = build_handlers(bindings=bindings)

    # 1. Build & staging
    ctx_build = _make_context("segundo-cerebro", stage="build_deploy")
    res_build = dispatch_stage(registry, ctx_build)
    assert res_build.outcome == "success"

    art_ref = next(r for r in res_build.output_refs if r.startswith("artifact://"))
    art_digest = art_ref.replace("artifact://", "")

    # 2. Target journey before client acceptance -> fails closed
    ctx_journey = _make_context(
        "segundo-cerebro",
        stage="target_journey",
        candidate_digest=art_digest,
        input_refs=[art_ref],
    )
    res_blocked = dispatch_stage(registry, ctx_journey)
    assert res_blocked.outcome == "failed"
    assert res_blocked.cause_code == "ClientAcceptanceRequiredError"

    # 3. Client records approval
    pipeline.record_client_acceptance(
        project_id="segundo-cerebro",
        artifact_digest=art_digest,
        client_id="acme-enterprise",
        approved_by="security-officer@acme.com",
    )

    # 4. Target journey after client acceptance -> succeeds
    res_success = dispatch_stage(registry, ctx_journey)
    assert res_success.outcome == "success"
    assert pipeline.get_current_stable_artifact("segundo-cerebro") == art_digest


def test_canonical_dispatch_stage_with_stale_lease_fails_closed() -> None:
    """Validate that dispatch_stage with stale lease fails closed on both release stages."""
    pipeline = ReleasePipelineService()
    bindings = create_release_bindings(pipeline=pipeline)
    registry = build_handlers(bindings=bindings)

    past_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    # Stale lease on build_deploy
    ctx_build = _make_context("jarvis", stage="build_deploy", expires_at=past_time)
    res_build = dispatch_stage(registry, ctx_build)
    assert res_build.outcome == "failed"
    assert res_build.cause_code == "stale_lease"

    # Stale lease on target_journey
    ctx_journey = _make_context("jarvis", stage="target_journey", expires_at=past_time)
    res_journey = dispatch_stage(registry, ctx_journey)
    assert res_journey.outcome == "failed"
    assert res_journey.cause_code == "stale_lease"


def test_canonical_dispatch_stage_simulation_environment_fails_closed() -> None:
    """Validate that dispatch_stage blocks promotion when environment is simulation."""
    pipeline = ReleasePipelineService()
    bindings = create_release_bindings(pipeline=pipeline)
    registry = build_handlers(bindings=bindings)

    # Build artifact
    art = pipeline.build("jarvis", "sha_jarvis_sim_test")

    ctx_sim = _make_context(
        "jarvis",
        stage="target_journey",
        candidate_digest=art.artifact_digest,
        input_refs=[f"artifact://{art.artifact_digest}"],
        environment_ref="simulation",
    )
    res = dispatch_stage(registry, ctx_sim)
    assert res.outcome == "failed"
    assert res.cause_code == "SimulationNotPromotedError"
