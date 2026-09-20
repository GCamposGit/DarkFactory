"""Canonical Evolution Stage Handlers and bindings (HF-25-01).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-25-01.md
- docs/handoffs/continuous-autonomy/bindings/HANDLERS.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py
- core/evolution/engine.py

Key Invariants:
1. Conforms strictly to StageHandler protocol: handle(StageContext) -> StageResult.
2. Failsafe for stale leases: expired leases or mismatched fencing tokens fail closed.
3. Separation of roles: candidates cannot be evaluated/approved by their own author.
4. Protected boundaries: verifier and governance mutation attempts are rejected fail-closed.
5. Isolated holdout evaluation: bad syntax or failing holdout tests reject candidates.
6. Post-restart application: promoted mutations do not alter running jobs; mutations apply stably post-restart.
7. Regression rollback: operational regression restores exact backup snapshot and blocks re-application.
8. Canonical refs: output_refs and evidence_refs link to proposal and snapshot URIs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.evolution.engine import FactoryEvolutionEngine
from core.evolution.models import (
    EvolutionProposal,
    EvolutionStatus,
    RollbackSnapshot,
    SecurityViolationError,
)
from core.workflow.control_contracts import (
    HandlerDescriptor,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler
from core.workflow.learning_handlers import ObservationHandler as MemoryObservationHandler

logger = logging.getLogger("darkfac.workflow.evolution_handlers")

STAGE: str = "learning_eval"
VERSION: str = "v1"


class EvolutionStageHandler:
    """StageHandler for continuous self-evolution and learning evaluation (HF-25-01).

    Orchestrates the evaluation of evolution proposals in an isolated holdout sandbox,
    enforces verifier security boundaries, ensures independent review, manages safe
    post-restart promotion, and supports regression rollback.
    """

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/EvolutionProposal",
        output_schema_ref="schema://contracts/RollbackSnapshot",
        role="evaluator",
        required_capabilities=["sandbox_evaluate", "run_holdout", "promote_skill", "rollback_skill"],
        timeout_seconds=900,
        conflict_scope="job",
    )

    def __init__(
        self,
        engine: FactoryEvolutionEngine | None = None,
        expected_fencing_token: int | None = None,
        defer_to_restart: bool = True,
        holdout_cmd: str | None = None,
    ) -> None:
        self.engine = engine or FactoryEvolutionEngine()
        self.expected_fencing_token = expected_fencing_token
        self.defer_to_restart = defer_to_restart
        self.holdout_cmd = holdout_cmd

    def _resolve_proposal_id(self, context: StageContext) -> str | None:
        """Resolve target proposal ID from stage context input_refs or ticket_id."""
        for ref in context.input_refs:
            if "ref://evolution/proposal/" in ref:
                return ref.split("ref://evolution/proposal/")[-1].strip("/")
            if ref.startswith("evo_"):
                return ref.strip()

        # Check if ticket_id or plan_ref maps to an existing proposal
        ticket_id = context.claim.job_key.ticket_id
        if ticket_id in self.engine._proposals:
            return ticket_id

        # Fallback to most recent proposed candidate if only one exists
        proposed = self.engine.list_proposals(status=EvolutionStatus.PROPOSED)
        if len(proposed) == 1:
            return proposed[0].proposal_id

        return None

    def handle(self, context: StageContext) -> StageResult:
        """Execute evolutionary evaluation and promotion within the given stage context."""
        jk = context.claim.job_key
        canonical_key = jk.canonical_key()

        # 1. Failsafe: Stale lease verification (time expiration)
        if context.claim and context.claim.expires_at:
            try:
                exp_clean = context.claim.expires_at.replace("Z", "+00:00")
                exp_dt = datetime.fromisoformat(exp_clean)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=UTC)
                if exp_dt < datetime.now(UTC):
                    logger.warning(
                        "EvolutionStageHandler rejected due to stale lease: expires_at %s < now for %s",
                        context.claim.expires_at,
                        canonical_key,
                    )
                    return StageResult(
                        outcome="failed",
                        cause_code="stale_lease",
                        output_refs=[],
                        evidence_refs=[],
                        actual_cost=0.0,
                    )
            except Exception as exc:
                logger.warning("Invalid lease expires_at '%s': %s", context.claim.expires_at, exc)
                return StageResult(
                    outcome="failed",
                    cause_code="stale_lease",
                    output_refs=[],
                    evidence_refs=[],
                    actual_cost=0.0,
                )

        # 2. Failsafe: Fencing token verification
        if (
            self.expected_fencing_token is not None
            and context.claim
            and context.claim.fencing_token != self.expected_fencing_token
        ):
            logger.warning(
                "EvolutionStageHandler rejected due to stale lease (fencing token mismatch: %s != %s)",
                context.claim.fencing_token,
                self.expected_fencing_token,
            )
            return StageResult(
                outcome="failed",
                cause_code="stale_lease",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # 3. Resolve proposal
        proposal_id = self._resolve_proposal_id(context)
        if not proposal_id or proposal_id not in self.engine._proposals:
            logger.error("EvolutionStageHandler: unknown or missing proposal in context %s", canonical_key)
            return StageResult(
                outcome="failed",
                cause_code="missing_proposal",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        proposal = self.engine._proposals[proposal_id]

        # 4. Inviolable Role Separation: proposer != evaluator
        proposer_id = proposal.metadata.get("proposer_identity") or proposal.metadata.get("author")
        if proposer_id and context.identity and proposer_id.strip().lower() == context.identity.strip().lower():
            logger.error(
                "Security violation: proposer '%s' cannot evaluate their own candidate '%s'",
                proposer_id,
                proposal_id,
            )
            return StageResult(
                outcome="failed",
                cause_code="role_separation_violation",
                output_refs=[],
                evidence_refs=[f"ref://evolution/evidence/{proposal_id}"],
                actual_cost=0.0,
            )

        # 5. Inviolable boundary check against verifiers and governance
        try:
            self.engine.sandbox.audit_boundaries(proposal.target_path)
        except SecurityViolationError as sec_exc:
            logger.error("Security boundary violation for proposal %s: %s", proposal_id, sec_exc)
            return StageResult(
                outcome="failed",
                cause_code="security_violation",
                output_refs=[],
                evidence_refs=[f"ref://evolution/evidence/{proposal_id}"],
                actual_cost=0.0,
            )

        # 6. Isolated Holdout Evaluation in Sandbox
        eval_result = self.engine.evaluate_candidate(proposal_id, holdout_cmd=self.holdout_cmd)
        if not eval_result.passed:
            logger.warning(
                "Proposal %s failed holdout evaluation (tampering=%s, steps=%s/%s)",
                proposal_id,
                eval_result.tampering_detected,
                eval_result.passed_steps,
                eval_result.discovered_steps,
            )
            return StageResult(
                outcome="failed",
                cause_code="security_violation" if eval_result.tampering_detected else "holdout_failed",
                output_refs=[],
                evidence_refs=[f"ref://evolution/evidence/{proposal_id}"],
                actual_cost=0.0,
            )

        # 7. Safe Promotion (deferring mutation to restart if configured)
        try:
            snapshot = self.engine.promote_candidate(
                proposal_id,
                defer_to_restart=self.defer_to_restart,
            )
        except Exception as prom_exc:
            logger.error("Promotion failed for proposal %s: %s", proposal_id, prom_exc)
            return StageResult(
                outcome="failed",
                cause_code="promotion_failed",
                output_refs=[],
                evidence_refs=[f"ref://evolution/evidence/{proposal_id}"],
                actual_cost=0.0,
            )

        # 8. Success: Emit canonical output_refs and evidence_refs
        output_refs = [
            f"ref://evolution/proposal/{proposal.proposal_id}",
            f"ref://evolution/snapshot/{snapshot.snapshot_id}",
        ]
        evidence_refs = [
            f"ref://evolution/evidence/{proposal.proposal_id}",
            f"ref://evidence/{jk.stage}/{canonical_key}",
        ]

        logger.info(
            "Evolution proposal %s successfully evaluated and promoted (defer_to_restart=%s, snapshot=%s)",
            proposal_id,
            self.defer_to_restart,
            snapshot.snapshot_id,
        )

        return StageResult(
            outcome="success",
            output_refs=output_refs,
            evidence_refs=evidence_refs,
            operation_refs=[],
            actual_cost=0.0,
            cause_code=None,
        )

    def handle_regression(self, proposal_id: str, reason: str = "") -> StageResult:
        """Handle detected operational regression by restoring backup snapshot and blocking re-application."""
        try:
            snapshot = self.engine.record_regression(proposal_id, reason=reason)
            return StageResult(
                outcome="failed",
                cause_code="regression_rolled_back",
                output_refs=[],
                evidence_refs=[
                    f"ref://evolution/proposal/{proposal_id}",
                    f"ref://evolution/snapshot/{snapshot.snapshot_id}",
                ],
                actual_cost=0.0,
            )
        except Exception as exc:
            logger.error("Regression rollback failed for proposal %s: %s", proposal_id, exc)
            return StageResult(
                outcome="failed",
                cause_code="rollback_error",
                output_refs=[],
                evidence_refs=[f"ref://evolution/proposal/{proposal_id}"],
                actual_cost=0.0,
            )


class LearningEvalHandler(EvolutionStageHandler):
    """Specialized LearningEvalHandler for learning_eval stage."""


def create_evolution_bindings(
    engine: FactoryEvolutionEngine | None = None,
    handler: EvolutionStageHandler | None = None,
    expected_fencing_token: int | None = None,
    defer_to_restart: bool = True,
    holdout_cmd: str | None = None,
) -> dict[tuple[str, str], StageHandler]:
    """Create canonical stage bindings for Factory Evolution subsystem.

    Integrates directly with build_handlers(bindings=...) and dispatch_stage().
    """
    evo_handler = handler or EvolutionStageHandler(
        engine=engine,
        expected_fencing_token=expected_fencing_token,
        defer_to_restart=defer_to_restart,
        holdout_cmd=holdout_cmd,
    )
    return {
        ("learning_eval", "v1"): evo_handler,
        ("evolution", "v1"): evo_handler,
    }


__all__ = [
    "EvolutionStageHandler",
    "LearningEvalHandler",
    "MemoryObservationHandler",
    "create_evolution_bindings",
    "STAGE",
    "VERSION",
]
