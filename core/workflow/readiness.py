"""Pure readiness and state-transition gates for the hybrid workflow."""

from __future__ import annotations

from datetime import UTC, datetime

from core.workflow.contracts import (
    EnvironmentKind,
    EvidenceFreshness,
    EvidenceResult,
    ManualDependencyStatus,
    PlannerTier,
    ReadinessReport,
    ReadinessState,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.verification import (
    EvidenceReceipt,
    GatePolicy,
    ValidationMode,
    VerificationContext,
    _aware_utc,
)


class ReadinessError(ValueError):
    """Raised when a workflow cannot legally advance or be delivered."""


ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.NEEDS_SPEC: frozenset({WorkflowState.PLANNING_HIGH, WorkflowState.CANCELLED}),
    WorkflowState.PLANNING_HIGH: frozenset(
        {
            WorkflowState.READY_FOR_HANDOFF,
            WorkflowState.WAITING_HUMAN,
            WorkflowState.WAITING_DEPENDENCY,
            WorkflowState.NEEDS_REPLAN,
            WorkflowState.FAILED_VALIDATION,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.READY_FOR_HANDOFF: frozenset(
        {
            WorkflowState.IMPLEMENTING_ECONOMY,
            WorkflowState.BLOCKED_POLICY,
            WorkflowState.NEEDS_REPLAN,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.IMPLEMENTING_ECONOMY: frozenset(
        {
            WorkflowState.VALIDATING,
            WorkflowState.RETRYABLE,
            WorkflowState.RETRY_SCHEDULED,
            WorkflowState.WAITING_CAPACITY,
            WorkflowState.CANCELLED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.VALIDATING: frozenset(
        {
            WorkflowState.INDEPENDENT_REVIEW,
            WorkflowState.FAILED_VALIDATION,
            WorkflowState.RETRYABLE,
            WorkflowState.WAITING_DEPENDENCY,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.INDEPENDENT_REVIEW: frozenset(
        {
            WorkflowState.DELIVERED,
            WorkflowState.NEEDS_REPLAN,
            WorkflowState.FAILED_VALIDATION,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.WAITING_HUMAN: frozenset(
        {WorkflowState.PLANNING_HIGH, WorkflowState.READY_FOR_HANDOFF, WorkflowState.CANCELLED}
    ),
    WorkflowState.WAITING_DEPENDENCY: frozenset(
        {WorkflowState.IMPLEMENTING_ECONOMY, WorkflowState.VALIDATING, WorkflowState.CANCELLED}
    ),
    WorkflowState.WAITING_ACCESS: frozenset(
        {WorkflowState.PLANNING_HIGH, WorkflowState.CANCELLED}
    ),
    WorkflowState.WAITING_BUDGET: frozenset(
        {WorkflowState.READY_FOR_HANDOFF, WorkflowState.CANCELLED}
    ),
    WorkflowState.WAITING_CAPACITY: frozenset(
        {WorkflowState.IMPLEMENTING_ECONOMY, WorkflowState.CANCELLED}
    ),
    WorkflowState.RETRYABLE: frozenset(
        {
            WorkflowState.IMPLEMENTING_ECONOMY,
            WorkflowState.RETRY_SCHEDULED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.RETRY_SCHEDULED: frozenset(
        {WorkflowState.IMPLEMENTING_ECONOMY, WorkflowState.FAILED, WorkflowState.CANCELLED}
    ),
    WorkflowState.NEEDS_REPLAN: frozenset({WorkflowState.PLANNING_HIGH, WorkflowState.CANCELLED}),
    WorkflowState.FAILED_VALIDATION: frozenset(
        {
            WorkflowState.IMPLEMENTING_ECONOMY,
            WorkflowState.PLANNING_HIGH,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.BLOCKED_POLICY: frozenset({WorkflowState.PLANNING_HIGH, WorkflowState.CANCELLED}),
    WorkflowState.SUCCEEDED: frozenset({WorkflowState.INDEPENDENT_REVIEW, WorkflowState.CANCELLED}),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.DELIVERED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
    WorkflowState.SKIPPED_BY_POLICY: frozenset(),
}

STAGE_READINESS_APPLICABILITY: dict[WorkflowState, frozenset[ReadinessState]] = {
    WorkflowState.NEEDS_SPEC: frozenset({ReadinessState.READY_FOR_SPEC}),
    WorkflowState.PLANNING_HIGH: frozenset({ReadinessState.READY_FOR_SPEC}),
    WorkflowState.READY_FOR_HANDOFF: frozenset({
        ReadinessState.READY_FOR_SPEC,
        ReadinessState.READY_FOR_HANDOFF,
    }),
    WorkflowState.IMPLEMENTING_ECONOMY: frozenset({
        ReadinessState.READY_FOR_SPEC,
        ReadinessState.READY_FOR_HANDOFF,
    }),
    WorkflowState.VALIDATING: frozenset({
        ReadinessState.READY_FOR_SPEC,
        ReadinessState.READY_FOR_HANDOFF,
        ReadinessState.VALIDATED_IN_SIMULATION,
    }),
    WorkflowState.INDEPENDENT_REVIEW: frozenset({
        ReadinessState.READY_FOR_SPEC,
        ReadinessState.READY_FOR_HANDOFF,
        ReadinessState.VALIDATED_IN_SIMULATION,
        ReadinessState.READY_FOR_RELEASE,
        ReadinessState.OPERATIONALLY_VERIFIED,
    }),
    WorkflowState.DELIVERED: frozenset({
        ReadinessState.READY_FOR_SPEC,
        ReadinessState.READY_FOR_HANDOFF,
        ReadinessState.VALIDATED_IN_SIMULATION,
        ReadinessState.READY_FOR_RELEASE,
        ReadinessState.OPERATIONALLY_VERIFIED,
        ReadinessState.DELIVERED,
    }),
}


def validate_transition(current: WorkflowState, target: WorkflowState) -> None:
    """Reject a transition not present in the closed workflow graph."""

    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ReadinessError(f"illegal workflow transition {current.value} -> {target.value}")


class ReadinessGate:
    """Evaluate a handoff without filesystem, network, clock, or runtime I/O."""

    def evaluate(
        self,
        handoff: WorkflowHandoff,
        *,
        context: VerificationContext | None = None,
        target_state: WorkflowState | None = None,
    ) -> ReadinessReport:
        requested_state = target_state or handoff.state
        reasons: list[str] = []
        missing_evidence: list[str] = []
        blocking_dependencies: list[str] = []
        verified_evidence: list[str] = []

        self._check_state_and_grill(handoff, target_state, context, reasons)
        self._check_manual_dependencies(
            handoff, requested_state, context, reasons, blocking_dependencies
        )
        self._check_plan_approval(handoff, context, reasons)
        self._check_evidence(
            handoff,
            requested_state,
            context,
            reasons,
            missing_evidence,
            verified_evidence,
        )
        self._check_delivery(handoff, requested_state, context, reasons)
        eligible = not reasons
        readiness = self._readiness_state(
            handoff, requested_state, eligible, blocking_dependencies
        )

        return ReadinessReport(
            ticket_id=handoff.ticket_id,
            state=handoff.state,
            readiness=readiness,
            eligible=eligible,
            reasons=reasons,
            missing_evidence=missing_evidence,
            blocking_dependency_ids=blocking_dependencies,
            verified_evidence_ids=verified_evidence,
        )

    def _check_state_and_grill(
        self,
        handoff: WorkflowHandoff,
        target_state: WorkflowState | None,
        context: VerificationContext | None,
        reasons: list[str],
    ) -> None:
        if target_state is not None and target_state != handoff.state:
            try:
                validate_transition(handoff.state, target_state)
            except ReadinessError as err:
                reasons.append(str(err))
        if handoff.state is WorkflowState.CANCELLED:
            reasons.append("cancelled workflow cannot advance or be evaluated as eligible")
        elif handoff.state is WorkflowState.FAILED:
            reasons.append("failed workflow cannot advance or be evaluated as eligible")
        if context is None:
            reasons.append("missing verification context: CONTEXT_REQUIRED")
        if not handoff.grill.ready_for_spec:
            reasons.append("Grill is not ready_for_spec")
        for decision in handoff.grill.decisions:
            if getattr(decision, "is_material", True) and not decision.is_answered():
                reasons.append(
                    f"grill decision '{decision.decision_id}' is unanswered or missing origin"
                )

    def _check_manual_dependencies(
        self,
        handoff: WorkflowHandoff,
        requested_state: WorkflowState,
        context: VerificationContext | None,
        reasons: list[str],
        blocking_dependencies: list[str],
    ) -> None:
        for dependency in handoff.manual_dependencies:
            if handoff.ticket_id not in dependency.ticket_ids:
                continue
            if dependency.blocked_stages and requested_state not in dependency.blocked_stages:
                continue
            if dependency.status is not ManualDependencyStatus.RESOLVED:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(f"manual dependency unresolved: {dependency.dependency_id}")
                continue
            if context is None:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution cannot be verified without context"
                )
                continue
            receipt_ref = dependency.resolution_receipt_ref
            if not receipt_ref:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' missing resolution_receipt_ref"
                )
                continue
            receipt = context.resolve_receipt(receipt_ref)
            if receipt is None:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution receipt '{receipt_ref}' not found"
                )
                continue
            issue = self._manual_dependency_receipt_issue(handoff, dependency, context, receipt)
            if issue:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(issue)

    @staticmethod
    def _manual_dependency_receipt_issue(handoff, dependency, context, receipt) -> str | None:
        dep_id = dependency.dependency_id
        if receipt.result is not EvidenceResult.PASSED:
            return f"manual dependency '{dep_id}' resolution receipt is not passed"
        receipt_probe = getattr(receipt, "probe", None) or receipt.requirement
        if receipt_probe != dependency.final_probe and receipt.requirement != dep_id:
            return (
                f"manual dependency '{dep_id}' probe mismatch: expected '{dependency.final_probe}', got '{receipt_probe}'"
            )
        valid_subjects = {handoff.ticket_id, dep_id, *dependency.ticket_ids}
        if receipt.subject not in valid_subjects:
            return (
                f"manual dependency '{dep_id}' receipt subject mismatch: '{receipt.subject}' not in {sorted(valid_subjects)}"
            )
        if (
            receipt.environment_ref
            and context.expected_environment_ref
            and receipt.environment_ref != context.expected_environment_ref
        ):
            return (
                f"manual dependency '{dep_id}' receipt environment mismatch: '{receipt.environment_ref}' != '{context.expected_environment_ref}'"
            )
        if receipt.config_version and context.config_version and receipt.config_version != context.config_version:
            return (
                f"manual dependency '{dep_id}' receipt config_version mismatch: '{receipt.config_version}' != '{context.config_version}'"
            )
        if (
            context.policy is not None
            and context.policy.enabled_roles
            and not context.policy.is_role_enabled(receipt.producer.role)
        ):
            return (
                f"manual dependency '{dep_id}' receipt producer role '{receipt.producer.role}' not enabled by policy"
            )
        if receipt.observed_at < dependency.created_at:
            return f"manual dependency '{dep_id}' resolution receipt is older than dependency creation"
        max_age = (
            context.policy.get_max_age(dep_id) if context.policy is not None else 86400
        )
        age_seconds = (context.now - receipt.observed_at).total_seconds()
        if age_seconds < 0:
            return (
                f"manual dependency '{dep_id}' resolution receipt observed in the future: {age_seconds}s"
            )
        if age_seconds > max_age:
            return (
                f"manual dependency '{dep_id}' resolution receipt expired: age={age_seconds}s > max_age={max_age}s"
            )
        return None

    def _check_plan_approval(
        self,
        handoff: WorkflowHandoff,
        context: VerificationContext | None,
        reasons: list[str],
    ) -> None:
        if context is None:
            return
        approval = context.resolve_approval(handoff.approval_reference)
        if approval is None:
            reasons.append(f"plan approval not found: {handoff.approval_reference}")
            return
        if approval.plan_digest != context.plan_digest:
            reasons.append("plan approval digest mismatch")
        if handoff.planner_tier is PlannerTier.ECONOMY:
            reasons.append("economy planner tier cannot produce approved plan")
        if approval.planner_tier != handoff.planner_tier:
            reasons.append(
                f"plan approval planner_tier mismatch: {approval.planner_tier.value} != {handoff.planner_tier.value}"
            )
        if approval.planner_tier is PlannerTier.ECONOMY:
            reasons.append("approved plan cannot have economy planner tier")
        if (
            context.policy is not None
            and context.policy.enabled_roles
            and not context.policy.is_role_enabled(approval.approved_by.role)
        ):
            reasons.append(
                f"plan approver role '{approval.approved_by.role}' is not enabled by policy"
            )

    def _check_evidence(
        self,
        handoff: WorkflowHandoff,
        requested_state: WorkflowState,
        context: VerificationContext | None,
        reasons: list[str],
        missing_evidence: list[str],
        verified_evidence: list[str],
    ) -> None:
        applicable = STAGE_READINESS_APPLICABILITY.get(requested_state, frozenset())
        policy_req_ids = (
            set(context.policy.requirements_for_stage(requested_state))
            if context is not None and context.policy is not None
            else set()
        )
        candidate_ids = {
            req.evidence_id for req in handoff.required_evidence if req.required_for in applicable
        }
        req_ids = sorted(policy_req_ids.union(candidate_ids))
        if requested_state is WorkflowState.DELIVERED and not req_ids and not handoff.environment_evidence:
            reasons.append(
                "delivery requires operational evidence policy; empty evidence cannot authorize release"
            )
        evidence_by_id = {item.evidence_id: item for item in handoff.environment_evidence}
        for req_id in req_ids:
            if context is not None:
                self._check_context_evidence(
                    handoff, req_id, requested_state, context, reasons,
                    missing_evidence, verified_evidence,
                )
            else:
                self._check_handoff_evidence(
                    handoff, req_id, evidence_by_id, reasons, missing_evidence, verified_evidence
                )

    def _check_context_evidence(
        self, handoff, req_id, requested_state, context, reasons, missing_evidence, verified_evidence
    ) -> None:
        exemption = context.resolve_exemption(req_id)
        if exemption is not None:
            if requested_state is WorkflowState.DELIVERED and exemption.is_operational:
                reasons.append(f"delivery policy cannot permit operational exemption for '{req_id}'")
                missing_evidence.append(req_id)
            elif not exemption.is_valid_at(context.now):
                reasons.append(f"exemption for '{req_id}' has expired")
                missing_evidence.append(req_id)
            else:
                verified_evidence.append(req_id)
            return
        receipt = context.resolve_receipt(req_id)
        if receipt is None:
            missing_evidence.append(req_id)
            reasons.append(f"missing supervisor receipt for required evidence: {req_id}")
            return
        issue = self._context_receipt_issue(handoff, req_id, requested_state, context, receipt)
        if issue:
            reasons.append(issue)
            missing_evidence.append(req_id)
        else:
            verified_evidence.append(req_id)

    def _context_receipt_issue(self, handoff, req_id, requested_state, context, receipt) -> str | None:
        if receipt.result is not EvidenceResult.PASSED:
            return f"evidence receipt is not passed: {req_id}"
        max_age = context.policy.get_max_age(req_id) if context.policy is not None else 86400
        age_seconds = (context.now - receipt.observed_at).total_seconds()
        if age_seconds < 0:
            return (
                f"evidence receipt observed in the future: {req_id} "
                f"(observed_at={receipt.observed_at.isoformat()} > now={context.now.isoformat()})"
            )
        if age_seconds > max_age:
            return f"evidence receipt expired: {req_id} (age={int(age_seconds)}s > max_age={max_age}s)"
        if receipt.subject != handoff.ticket_id:
            return (
                f"evidence receipt subject mismatch: {req_id} "
                f"(receipt.subject='{receipt.subject}' != ticket_id='{handoff.ticket_id}')"
            )
        issue = self._receipt_binding_issue(handoff, req_id, requested_state, context, receipt)
        return issue

    def _receipt_binding_issue(self, handoff, req_id, requested_state, context, receipt) -> str | None:
        if receipt.plan_digest is not None and receipt.plan_digest != context.plan_digest:
            return f"receipt plan_digest mismatch: {req_id}"
        if receipt.candidate_digest is not None and receipt.candidate_digest != context.candidate_digest:
            return f"receipt candidate_digest mismatch: {req_id}"
        if requested_state is WorkflowState.DELIVERED and receipt.candidate_digest is None:
            return f"delivery evidence '{req_id}' requires bound candidate_digest"
        if receipt.config_version is not None and receipt.config_version != context.config_version:
            return f"receipt config_version mismatch: {req_id}"
        if receipt.environment_ref is not None:
            if receipt.environment_ref != context.expected_environment_ref:
                return f"receipt environment_ref mismatch: {req_id}"
            if receipt.environment_ref != handoff.environment.environment_ref:
                return f"receipt environment does not match handoff environment: {req_id}"
        if receipt.route is not None and receipt.route != context.expected_route:
            return f"receipt route mismatch: {req_id}"
        return self._receipt_authorization_issue(
            handoff, req_id, requested_state, context, receipt
        )

    def _receipt_authorization_issue(self, handoff, req_id, requested_state, context, receipt) -> str | None:
        if (
            context.policy is not None
            and context.policy.enabled_roles
            and not context.policy.is_role_enabled(receipt.producer.role)
        ):
            return f"receipt producer role '{receipt.producer.role}' is not enabled by policy: {req_id}"
        if receipt.capabilities:
            approval = context.resolve_approval(handoff.approval_reference)
            if approval is not None:
                unauthorized = [cap for cap in receipt.capabilities if cap not in approval.enabled_capabilities]
                if unauthorized:
                    return f"receipt capabilities not enabled by approved plan: {req_id} ({unauthorized})"
        if not receipt.artifact_hash or not str(receipt.artifact_hash).strip():
            return f"receipt artifact_hash is missing: {req_id}"
        if requested_state is WorkflowState.DELIVERED and receipt.mode is not ValidationMode.TARGET_ENVIRONMENT:
            return (
                f"delivery evidence '{req_id}' must be target_environment, got {receipt.mode.value}"
            )
        return None

    def _check_handoff_evidence(
        self, handoff, req_id, evidence_by_id, reasons, missing_evidence, verified_evidence
    ) -> None:
        evidence = evidence_by_id.get(req_id)
        if evidence is None:
            missing_evidence.append(req_id)
            reasons.append(f"missing required evidence: {req_id}")
            return
        if evidence.environment_ref != handoff.environment.environment_ref:
            reasons.append(f"evidence environment mismatch: {req_id}")
        elif evidence.result is not EvidenceResult.PASSED:
            reasons.append(f"evidence is not passed: {req_id}")
        elif evidence.identity.subject != handoff.environment.worker_identity.subject:
            reasons.append(f"evidence identity mismatch: {req_id}")
        elif evidence.freshness is not EvidenceFreshness.CURRENT:
            reasons.append(f"evidence is not current: {req_id}")
        else:
            ev_now = datetime.now(UTC)
            ev_dt = _aware_utc(evidence.observed_at)
            assert ev_dt is not None
            ev_age = (ev_now - ev_dt).total_seconds()
            if ev_age < 0 or ev_age > 86400:
                reasons.append(f"evidence is stale: {req_id}")
            elif not evidence.evidence_ref.strip():
                reasons.append(f"evidence has no audit reference: {req_id}")
            else:
                verified_evidence.append(req_id)

    def _check_delivery(self, handoff, requested_state, context, reasons) -> None:
        if requested_state is not WorkflowState.DELIVERED:
            return
        if handoff.state not in {WorkflowState.INDEPENDENT_REVIEW, WorkflowState.DELIVERED}:
            reasons.append("delivery requires independent_review")
        if handoff.environment.kind is not EnvironmentKind.TARGET_ENVIRONMENT:
            reasons.append("delivery requires target_environment evidence")
        if handoff.environment.kind is EnvironmentKind.MOCK_ONLY:
            reasons.append("mock_only evidence cannot authorize release")
        if context is None:
            return
        has_independent_review, has_target_proof = self._delivery_receipt_proofs(handoff, context)
        if not has_independent_review:
            reasons.append(
                "delivery requires independent review receipt from a reviewer distinct from the implementer"
            )
        if not has_target_proof:
            reasons.append("delivery requires passed operational proof in target_environment")

    def _delivery_receipt_proofs(self, handoff, context) -> tuple[bool, bool]:
        has_independent_review = False
        has_target_proof = False
        for receipt in context.receipts.values():
            if receipt.result is not EvidenceResult.PASSED:
                continue
            if receipt.candidate_digest is not None and receipt.candidate_digest != context.candidate_digest:
                continue
            max_age = context.policy.get_max_age(receipt.requirement) if context.policy is not None else 86400
            age = (context.now - receipt.observed_at).total_seconds()
            if age < 0 or age > max_age or receipt.subject != handoff.ticket_id:
                continue
            if (
                receipt.producer.subject != context.expected_identity.subject
                and (
                    receipt.requirement in {"review", "independent_review", "code_review"}
                    or receipt.producer.role in {"reviewer", "supervisor", "independent_reviewer"}
                )
            ):
                has_independent_review = True
            if receipt.mode is ValidationMode.TARGET_ENVIRONMENT:
                has_target_proof = True
        return has_independent_review, has_target_proof

    @staticmethod
    def _readiness_state(handoff, requested_state, eligible, blocking_dependencies):
        if requested_state is WorkflowState.DELIVERED and eligible:
            return ReadinessState.OPERATIONALLY_VERIFIED
        if requested_state is WorkflowState.DELIVERED:
            return ReadinessState.BLOCKED
        if handoff.state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
            return ReadinessState.BLOCKED
        if not eligible and blocking_dependencies:
            return ReadinessState.WAITING_HUMAN
        if not eligible:
            return ReadinessState.NOT_READY
        if handoff.environment.kind is EnvironmentKind.MOCK_ONLY:
            return ReadinessState.VALIDATED_IN_SIMULATION
        if requested_state in {WorkflowState.NEEDS_SPEC, WorkflowState.PLANNING_HIGH}:
            return ReadinessState.READY_FOR_SPEC
        if requested_state is WorkflowState.READY_FOR_HANDOFF:
            return ReadinessState.READY_FOR_HANDOFF
        return ReadinessState.READY_FOR_RELEASE

    def require_delivery(
        self,
        handoff: WorkflowHandoff,
        *,
        context: VerificationContext | None = None,
    ) -> ReadinessReport:
        """Return an operational proof or fail closed before delivery."""

        report = self.evaluate(handoff, context=context, target_state=WorkflowState.DELIVERED)
        if not report.eligible:
            raise ReadinessError("delivery rejected: " + "; ".join(report.reasons))
        return report


def mark_delivered(
    handoff: WorkflowHandoff,
    gate: ReadinessGate | None = None,
    *,
    context: VerificationContext | None = None,
) -> WorkflowHandoff:
    """Produce a new delivered handoff only after the external gate passes."""

    (gate or ReadinessGate()).require_delivery(handoff, context=context)
    return handoff.model_copy(update={"state": WorkflowState.DELIVERED})


__all__ = [
    "ALLOWED_TRANSITIONS",
    "STAGE_READINESS_APPLICABILITY",
    "ReadinessError",
    "ReadinessGate",
    "mark_delivered",
    "validate_transition",
]
