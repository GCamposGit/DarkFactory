"""Deterministic unit tests for HF-12-04: Evidence-Chain Automated Release.

Governed by:
- docs/handoffs/continuous-autonomy/HF-12-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/orchestrator/release_pipeline.py
- core/workflow/release_handlers.py
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.orchestrator.release_pipeline import (
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
from core.workflow.release_handlers import (
    BuildDeployHandler,
    ProductionReleaseHandler,
)

BASE_TIME = datetime(2026, 9, 18, 16, 0, 0, tzinfo=UTC)


def _make_context(
    ticket_id: str,
    stage: str = "build_deploy",
    candidate_digest: str | None = None,
    input_refs: list[str] | None = None,
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
        expires_at="2026-09-18T18:00:00+00:00",
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-1",
        plan_digest="a" * 64,
        candidate_digest=candidate_digest or ("b" * 64),
        config_version="1.0",
        environment_ref="env-release",
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
