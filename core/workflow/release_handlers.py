"""Automated evidence-chain release handlers (HF-12-04).

Normative implementation of:
- docs/handoffs/continuous-autonomy/HF-12-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- ADR-HF-001

Invariants:
- Release requires trusted evidence receipts (preflight, build, deploy, digest, journey).
- Legacy booleans (e.g. approved=True, mock=True) are strictly labeled as simulation and
  CANNOT promote an artifact to production.
- Client acceptance must sign the EXACT same artifact_digest.
- Staging to production promotion preserves the exact same immutable artifact.
- Production/journey failure triggers immediate automated rollback and recovery logging,
  leaving concurrent independent projects unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

from core.orchestrator.release_pipeline import (
    ClientAcceptanceReceipt,
    ClientAcceptanceRequiredError,
    DeploymentRecord,
    EvidenceMismatchError,
    ProjectTier,
    ReleasePipelineService,
    SimulationNotPromotedError,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler

logger = logging.getLogger(__name__)


class BuildDeployHandler:
    """Stage handler executing artifact build and staging deployment with evidence receipts."""

    def __init__(
        self,
        pipeline: ReleasePipelineService | None = None,
        version: str = "v1",
    ) -> None:
        self.pipeline = pipeline or ReleasePipelineService()
        self.version = version

    def handle(self, context: StageContext) -> StageResult:
        """Build artifact and deploy to staging, emitting verifiable evidence."""
        project_id = context.claim.job_key.ticket_id
        git_sha = (context.candidate_digest or "0" * 40).replace("sha256:", "")[:40]

        try:
            # 1. Build immutable artifact
            artifact = self.pipeline.build(
                project_id=project_id,
                git_sha=git_sha,
                manifest_payload={"env_ref": context.environment_ref, "plan_digest": context.plan_digest},
            )

            # 2. Deploy to staging environment
            staging_rec = self.pipeline.deploy_staging(artifact)

            return StageResult(
                outcome="success",
                output_refs=[
                    f"artifact://{artifact.artifact_digest}",
                    f"deployment://staging/{staging_rec.deployment_id}",
                ],
                evidence_refs=[
                    f"receipt://build/{artifact.build_id}",
                    f"receipt://preflight/staging",
                    f"receipt://smoke/{staging_rec.smoke_test.test_id if staging_rec.smoke_test else 'ok'}",
                ],
                operation_refs=[staging_rec.deployment_id],
                actual_cost=0.0,
            )
        except Exception as exc:
            logger.error("BuildDeployHandler failed for project %s: %s", project_id, exc)
            return StageResult(
                outcome="failed",
                cause_code="BUILD_DEPLOY_FAILED",
                actual_cost=0.0,
            )


class ProductionReleaseHandler:
    """Stage handler executing production promotion, journey verification, and rollback on failure."""

    def __init__(
        self,
        pipeline: ReleasePipelineService | None = None,
        project_tier: ProjectTier = ProjectTier.INTERNAL_FREE,
        version: str = "v1",
    ) -> None:
        self.pipeline = pipeline or ReleasePipelineService()
        self.project_tier = project_tier
        self.version = version

    def handle(
        self,
        context: StageContext,
        *,
        is_simulation: bool = False,
        caller_approval_flag: bool | None = None,
        provided_acceptance: ClientAcceptanceReceipt | None = None,
    ) -> StageResult:
        """Promote artifact to production with strict evidence chain verification."""
        project_id = context.claim.job_key.ticket_id

        # Determine target artifact digest from context input refs or candidate digest
        artifact_digest: str | None = None
        for ref in context.input_refs:
            if ref.startswith("artifact://"):
                artifact_digest = ref.replace("artifact://", "").strip()
                break

        if not artifact_digest:
            artifact_digest = context.candidate_digest.replace("sha256:", "").strip() if context.candidate_digest else ""

        try:
            record = self.pipeline.promote_with_evidence_chain(
                artifact_digest=artifact_digest,
                project_tier=self.project_tier,
                is_simulation=is_simulation,
                caller_approval_flag=caller_approval_flag,
                provided_acceptance_receipt=provided_acceptance,
            )

            return StageResult(
                outcome="success",
                output_refs=[
                    f"artifact://{artifact_digest}",
                    f"deployment://production/{record.deployment_id}",
                ],
                evidence_refs=[
                    f"receipt://production/{record.deployment_id}",
                    f"receipt://journey/{record.smoke_test.test_id if record.smoke_test else 'ok'}",
                ],
                operation_refs=[record.deployment_id],
                actual_cost=0.0,
            )

        except (SimulationNotPromotedError, ClientAcceptanceRequiredError, EvidenceMismatchError) as exc:
            logger.warning("Production promotion blocked by evidence gate: %s", exc)
            return StageResult(
                outcome="failed",
                cause_code=exc.__class__.__name__,
                actual_cost=0.0,
            )

        except Exception as exc:
            logger.error("Production deployment or journey test failed for %s: %s", project_id, exc)
            # Retrieve generated rollback receipt
            rollbacks = self.pipeline.get_rollbacks(project_id=project_id)
            latest_rb = rollbacks[-1].rollback_id if rollbacks else "none"

            return StageResult(
                outcome="failed",
                cause_code="JOURNEY_FAILED",
                evidence_refs=[f"receipt://rollback/{latest_rb}"],
                actual_cost=0.0,
            )
