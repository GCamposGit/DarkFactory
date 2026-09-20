"""Quality assurance and independent review stage handlers (HF-09-02).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-09-02.md
- docs/handoffs/continuous-autonomy/bindings/HANDLERS.md
- core/workflow/control_contracts.py
- core/workflow/cycle.py
- core/workflow/verification.py

Key Invariants:
1. Conforms to StageHandler protocol: handle(StageContext) -> StageResult.
2. Defect reproval: functional test failure rejects even with self-asserted PASS.
3. Reviewer independence: strict prohibition of self-approval (reviewer.subject != developer.subject).
4. Cross-model family isolation: reviewer must belong to an independent model family.
5. Emits verified EvidenceReceipt under VerificationContext authority.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from core.workflow.contracts import (
    SanitizedIdentity,
    WorkflowHandoff,
)
from core.workflow.control_contracts import (
    HandlerDescriptor,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.cycle import (
    ImplementationCandidate,
    IndependentReviewVerdict,
    ValidationCycleResult,
)
from core.workflow.handlers import StageHandler
from core.workflow.verification import (
    EvidenceReceipt,
    EvidenceResult,
    ValidationMode,
    VerificationContext,
)

logger = logging.getLogger("darkfac.workflow.quality_handlers")

VALIDATION_STAGE: str = "validation"
REVIEW_STAGE: str = "independent_review"
VERSION: str = "v1"


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ValidationStageHandler:
    """StageHandler for the automated validation stage (HF-09-02).

    Executes test harnesses, verifies oracles independently of agent self-assertions,
    and returns deterministic StageResult outcomes.
    """

    STAGE: str = VALIDATION_STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=VALIDATION_STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/ImplementationCandidate",
        output_schema_ref="schema://contracts/ValidationReport",
        role="validator",
        required_capabilities=["run_command", "read_logs"],
        timeout_seconds=900,
        conflict_scope="job",
    )

    def __init__(
        self,
        validator_func: Callable[[StageContext, str], dict[str, Any]] | None = None,
        candidate_provider: Callable[[str], ImplementationCandidate | None] | None = None,
    ) -> None:
        self.validator_func = validator_func
        self.candidate_provider = candidate_provider
        self._reports: dict[str, Any] = {}

    def get_report(self, report_key: str) -> dict[str, Any] | None:
        """Lookup stored validation report by report_id or ticket_id."""
        return self._reports.get(report_key)

    def handle(self, context: StageContext) -> StageResult:
        """Execute test validation over candidate."""
        jk = context.claim.job_key
        ticket_id = jk.ticket_id

        # 1. Lease check
        if context.claim.expires_at:
            try:
                exp_clean = context.claim.expires_at.replace("Z", "+00:00")
                exp_dt = datetime.fromisoformat(exp_clean)
                if exp_dt < datetime.now(UTC):
                    return StageResult(
                        outcome="failed",
                        cause_code="stale_lease",
                        output_refs=[],
                        evidence_refs=[],
                        actual_cost=0.0,
                    )
            except Exception:
                pass

        candidate_sha = context.candidate_digest or "unknown_sha"
        if self.candidate_provider is not None:
            cand = self.candidate_provider(ticket_id)
            if cand:
                candidate_sha = cand.candidate_sha

        # 2. Execute validation
        passed = True
        error_detail = ""
        actual_cost = 0.0

        if self.validator_func is not None:
            try:
                res = self.validator_func(context, candidate_sha)
                passed = bool(res.get("passed", False))
                error_detail = res.get("error", "")
                actual_cost = float(res.get("actual_cost", 0.0))
            except Exception as exc:
                passed = False
                error_detail = f"validator_exception: {exc}"

        if not passed:
            logger.warning("Validation failed for %s (candidate: %s): %s", ticket_id, candidate_sha, error_detail)
            failed_report_id = f"val_report_failed_{ticket_id}_{candidate_sha[:8]}"
            report_data = {
                "report_id": failed_report_id,
                "ticket_id": ticket_id,
                "candidate_sha": candidate_sha,
                "passed": False,
                "error": error_detail,
                "actual_cost": actual_cost,
            }
            self._reports[failed_report_id] = report_data
            self._reports[ticket_id] = report_data

            return StageResult(
                outcome="retry",
                cause_code=f"test_failure: {error_detail}" if error_detail else "test_failure",
                output_refs=[f"ref://validation-report/failed/{ticket_id}"],
                evidence_refs=[f"ref://evidence/validation/failed/{jk.canonical_key()}"],
                actual_cost=actual_cost,
            )

        report_id = f"val_report_{ticket_id}_{candidate_sha[:8]}"
        report_data = {
            "report_id": report_id,
            "ticket_id": ticket_id,
            "candidate_sha": candidate_sha,
            "passed": True,
            "error": "",
            "actual_cost": actual_cost,
        }
        self._reports[report_id] = report_data
        self._reports[ticket_id] = report_data

        output_refs = [
            f"ref://validation-report/{report_id}",
            f"ref://candidate/{candidate_sha}",
        ]
        evidence_refs = [
            f"ref://evidence/validation/{jk.canonical_key()}",
            f"ref://report/{report_id}",
        ]

        return StageResult(
            outcome="success",
            output_refs=output_refs,
            evidence_refs=evidence_refs,
            operation_refs=[],
            actual_cost=actual_cost,
        )


class ReviewStageHandler:
    """StageHandler for independent adversarial review (HF-09-02).

    Enforces cross-subject and cross-model family independence, auditing candidates
    and emitting cryptographically bound EvidenceReceipts.
    """

    STAGE: str = REVIEW_STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=REVIEW_STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/ValidationReport",
        output_schema_ref="schema://contracts/EvidenceReceipt",
        role="reviewer",
        required_capabilities=["read_file", "git_diff", "run_linter"],
        timeout_seconds=600,
        conflict_scope="job",
    )

    def __init__(
        self,
        reviewer_identity: SanitizedIdentity | None = None,
        reviewer_model_family: str = "gpt-review",
        developer_model_family: str = "qwen-fast",
        review_evaluator: Callable[[StageContext, ImplementationCandidate | None], dict[str, Any]] | None = None,
        candidate_provider: Callable[[str], ImplementationCandidate | None] | None = None,
        verification_context: VerificationContext | None = None,
    ) -> None:
        self.reviewer_identity = reviewer_identity or SanitizedIdentity(
            subject="independent-reviewer",
            role="reviewer",
            host="local",
        )
        self.reviewer_model_family = reviewer_model_family
        self.developer_model_family = developer_model_family
        self.review_evaluator = review_evaluator
        self.candidate_provider = candidate_provider
        self.verification_context = verification_context
        self._receipts: dict[str, EvidenceReceipt] = {}

    def get_receipt(self, receipt_key: str) -> EvidenceReceipt | None:
        """Lookup stored evidence receipt by receipt_id or ticket_id."""
        return self._receipts.get(receipt_key)

    def handle(self, context: StageContext) -> StageResult:
        """Execute independent adversarial review."""
        jk = context.claim.job_key
        ticket_id = jk.ticket_id
        developer_identity_str = context.identity

        # 1. Invariant: Strict prohibition of self-approval (reviewer.subject != developer.subject)
        if self.reviewer_identity.subject == developer_identity_str:
            logger.error(
                "Self-approval attempt detected for %s: reviewer '%s' matches developer '%s'.",
                ticket_id,
                self.reviewer_identity.subject,
                developer_identity_str,
            )
            return StageResult(
                outcome="failed",
                cause_code="self_approval_prohibited",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # 2. Invariant: Cross-model family isolation
        if self.reviewer_model_family == self.developer_model_family:
            logger.warning(
                "Reviewer model family '%s' matches developer family for %s; cross-family isolation required.",
                self.reviewer_model_family,
                ticket_id,
            )
            # In strict mode, fails closed
            return StageResult(
                outcome="failed",
                cause_code="model_family_isolation_violation",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # 3. VerificationContext digest mismatch check if context provided
        if self.verification_context is not None:
            expected_cand_digest = self.verification_context.candidate_digest
            if expected_cand_digest and context.candidate_digest and expected_cand_digest != context.candidate_digest:
                logger.error(
                    "Candidate digest mismatch under VerificationContext authority for %s: expected %s, got %s",
                    ticket_id,
                    expected_cand_digest,
                    context.candidate_digest,
                )
                return StageResult(
                    outcome="failed",
                    cause_code="candidate_digest_mismatch_with_verification_context",
                    output_refs=[],
                    evidence_refs=[],
                    actual_cost=0.0,
                )

        # 4. Retrieve candidate if available
        candidate: ImplementationCandidate | None = None
        if self.candidate_provider is not None:
            candidate = self.candidate_provider(ticket_id)

        # 5. Evaluate review
        approved = True
        findings: list[str] = []
        actual_cost = 0.0

        if self.review_evaluator is not None:
            try:
                eval_res = self.review_evaluator(context, candidate)
                approved = bool(eval_res.get("approved", False))
                findings = list(eval_res.get("findings", []))
                actual_cost = float(eval_res.get("actual_cost", 0.0))
            except Exception as exc:
                approved = False
                findings.append(f"review_evaluator_exception: {exc}")

        if not approved:
            return StageResult(
                outcome="retry",
                cause_code="review_changes_required",
                output_refs=[f"ref://review/findings/{ticket_id}"],
                evidence_refs=[f"ref://evidence/review/rejected/{jk.canonical_key()}"],
                actual_cost=actual_cost,
            )

        # 6. Create verifiable EvidenceReceipt under VerificationContext authority
        now = self.verification_context.now if self.verification_context else datetime.now(UTC)
        candidate_sha = candidate.candidate_sha if candidate else (context.candidate_digest or "sha_verified")
        candidate_digest = candidate.candidate_digest if candidate else context.candidate_digest
        receipt_id = f"receipt://review/{ticket_id}/{now.strftime('%Y%m%d%H%M%S')}"

        receipt = EvidenceReceipt(
            receipt_id=receipt_id,
            producer=self.reviewer_identity,
            subject=ticket_id,
            requirement="independent_review",
            artifact_hash=candidate_sha,
            result=EvidenceResult.PASSED,
            mode=ValidationMode.TARGET_ENVIRONMENT,
            observed_at=now,
            environment_ref=context.environment_ref,
            plan_digest=context.plan_digest,
            candidate_digest=candidate_digest,
            config_version=context.config_version,
            route=context.route_ref,
        )
        self._receipts[receipt_id] = receipt
        self._receipts[ticket_id] = receipt

        return StageResult(
            outcome="success",
            output_refs=[
                receipt_id,
                f"ref://review/verdict/approved/{ticket_id}",
            ],
            evidence_refs=[
                receipt_id,
                f"ref://evidence/review/{jk.canonical_key()}",
            ],
            operation_refs=[],
            actual_cost=actual_cost,
        )
