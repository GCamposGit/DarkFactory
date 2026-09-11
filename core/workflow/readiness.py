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

        # 1. State transition validation for explicit target_state
        if target_state is not None and target_state != handoff.state:
            try:
                validate_transition(handoff.state, target_state)
            except ReadinessError as err:
                reasons.append(str(err))

        # Terminal and failed states cannot advance or be eligible
        if handoff.state is WorkflowState.CANCELLED:
            reasons.append("cancelled workflow cannot advance or be evaluated as eligible")
        elif handoff.state is WorkflowState.FAILED:
            reasons.append("failed workflow cannot advance or be evaluated as eligible")

        # 2. Verification context presence
        if context is None:
            reasons.append("missing verification context: CONTEXT_REQUIRED")

        # 3. Grill integrity
        if not handoff.grill.ready_for_spec:
            reasons.append("Grill is not ready_for_spec")
        for decision in handoff.grill.decisions:
            if getattr(decision, "is_material", True):
                if not decision.is_answered():
                    reasons.append(
                        f"grill decision '{decision.decision_id}' is unanswered or missing origin"
                    )

        # 4. Manual dependencies
        for dependency in handoff.manual_dependencies:
            # Check ticket scope: only blocks tickets declared in dependency.ticket_ids
            if handoff.ticket_id not in dependency.ticket_ids:
                continue

            # Check stage scope: if blocked_stages is specified, only blocks if requested_state is in blocked_stages
            if dependency.blocked_stages and requested_state not in dependency.blocked_stages:
                continue

            if dependency.status is not ManualDependencyStatus.RESOLVED:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(f"manual dependency unresolved: {dependency.dependency_id}")
                continue

            # Resolved dependency requires resolution_receipt_ref verification via context
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

            # 1. Result must be PASSED
            if receipt.result is not EvidenceResult.PASSED:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution receipt is not passed"
                )
                continue

            # 2. Probe matching: requirement or probe attribute must match final_probe (or dependency_id)
            receipt_probe = getattr(receipt, "probe", None) or receipt.requirement
            if (
                receipt_probe != dependency.final_probe
                and receipt.requirement != dependency.dependency_id
            ):
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' probe mismatch: expected '{dependency.final_probe}', got '{receipt_probe}'"
                )
                continue

            # 3. Subject matching: receipt.subject must be the dependency_id, ticket_id or one of dependency.ticket_ids
            valid_subjects = {handoff.ticket_id, dependency.dependency_id, *dependency.ticket_ids}
            if receipt.subject not in valid_subjects:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' receipt subject mismatch: '{receipt.subject}' not in {sorted(valid_subjects)}"
                )
                continue

            # 4. Environment matching
            if (
                receipt.environment_ref
                and context.expected_environment_ref
                and receipt.environment_ref != context.expected_environment_ref
            ):
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' receipt environment mismatch: '{receipt.environment_ref}' != '{context.expected_environment_ref}'"
                )
                continue

            # 5. Config version matching
            if (
                receipt.config_version
                and context.config_version
                and receipt.config_version != context.config_version
            ):
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' receipt config_version mismatch: '{receipt.config_version}' != '{context.config_version}'"
                )
                continue

            # 6. Producer role check against policy
            if (
                context.policy is not None
                and context.policy.enabled_roles
                and not context.policy.is_role_enabled(receipt.producer.role)
            ):
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' receipt producer role '{receipt.producer.role}' not enabled by policy"
                )
                continue

            # 7. Temporal ordering: receipt must not be older than dependency creation
            if receipt.observed_at < dependency.created_at:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution receipt is older than dependency creation"
                )
                continue

            # 8. TTL and temporal validity (UTC): 0 <= now - observed_at <= max_age
            max_age = (
                context.policy.get_max_age(dependency.dependency_id)
                if context.policy is not None
                else 86400
            )
            age_seconds = (context.now - receipt.observed_at).total_seconds()
            if age_seconds < 0:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution receipt observed in the future: {age_seconds}s"
                )
                continue
            if age_seconds > max_age:
                blocking_dependencies.append(dependency.dependency_id)
                reasons.append(
                    f"manual dependency '{dependency.dependency_id}' resolution receipt expired: age={age_seconds}s > max_age={max_age}s"
                )
                continue

        # 5. Plan approval verification via context
        if context is not None:
            approval = context.resolve_approval(handoff.approval_reference)
            if approval is None:
                reasons.append(f"plan approval not found: {handoff.approval_reference}")
            else:
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

        # 6. Evidence requirements resolution per stage
        applicable_readiness = STAGE_READINESS_APPLICABILITY.get(requested_state, frozenset())

        policy_req_ids: set[str] = set()
        if context is not None and context.policy is not None:
            policy_req_ids = set(context.policy.requirements_for_stage(requested_state))

        candidate_reqs_for_stage = [
            req for req in handoff.required_evidence if req.required_for in applicable_readiness
        ]

        all_stage_req_ids = sorted(
            policy_req_ids.union(req.evidence_id for req in candidate_reqs_for_stage)
        )

        # In delivery, empty evidence policy/declarations fail closed
        if requested_state is WorkflowState.DELIVERED:
            if not all_stage_req_ids and not handoff.environment_evidence:
                reasons.append(
                    "delivery requires operational evidence policy; empty evidence cannot authorize release"
                )

        evidence_by_id = {item.evidence_id: item for item in handoff.environment_evidence}

        for req_id in all_stage_req_ids:
            if context is not None:
                exemption = context.resolve_exemption(req_id)
                if exemption is not None:
                    if requested_state is WorkflowState.DELIVERED and exemption.is_operational:
                        reasons.append(
                            f"delivery policy cannot permit operational exemption for '{req_id}'"
                        )
                        missing_evidence.append(req_id)
                        continue
                    if not exemption.is_valid_at(context.now):
                        reasons.append(f"exemption for '{req_id}' has expired")
                        missing_evidence.append(req_id)
                        continue
                    verified_evidence.append(req_id)
                    continue

                receipt = context.resolve_receipt(req_id)
                if receipt is not None:
                    # 1. Result must be PASSED
                    if receipt.result is not EvidenceResult.PASSED:
                        reasons.append(f"evidence receipt is not passed: {req_id}")
                        missing_evidence.append(req_id)
                        continue

                    # 2. TTL and temporal validity calculation (UTC): 0 <= now - observed_at <= max_age
                    max_age = (
                        context.policy.get_max_age(req_id)
                        if context.policy is not None
                        else 86400
                    )
                    age_seconds = (context.now - receipt.observed_at).total_seconds()
                    if age_seconds < 0:
                        reasons.append(
                            f"evidence receipt observed in the future: {req_id} "
                            f"(observed_at={receipt.observed_at.isoformat()} > now={context.now.isoformat()})"
                        )
                        missing_evidence.append(req_id)
                        continue
                    if age_seconds > max_age:
                        reasons.append(
                            f"evidence receipt expired: {req_id} "
                            f"(age={int(age_seconds)}s > max_age={max_age}s)"
                        )
                        missing_evidence.append(req_id)
                        continue

                    # 3. Strict Subject binding: receipt.subject must match handoff.ticket_id
                    if receipt.subject != handoff.ticket_id:
                        reasons.append(
                            f"evidence receipt subject mismatch: {req_id} "
                            f"(receipt.subject='{receipt.subject}' != ticket_id='{handoff.ticket_id}')"
                        )
                        missing_evidence.append(req_id)
                        continue

                    # 4. Strict Plan Digest binding
                    if (
                        receipt.plan_digest is not None
                        and receipt.plan_digest != context.plan_digest
                    ):
                        reasons.append(f"receipt plan_digest mismatch: {req_id}")
                        missing_evidence.append(req_id)
                        continue

                    # 5. Strict Candidate / Build Digest binding
                    if (
                        receipt.candidate_digest is not None
                        and receipt.candidate_digest != context.candidate_digest
                    ):
                        reasons.append(f"receipt candidate_digest mismatch: {req_id}")
                        missing_evidence.append(req_id)
                        continue
                    if (
                        requested_state is WorkflowState.DELIVERED
                        and receipt.candidate_digest is None
                    ):
                        reasons.append(f"delivery evidence '{req_id}' requires bound candidate_digest")
                        missing_evidence.append(req_id)
                        continue

                    # 6. Strict Config Version binding
                    if (
                        receipt.config_version is not None
                        and receipt.config_version != context.config_version
                    ):
                        reasons.append(f"receipt config_version mismatch: {req_id}")
                        missing_evidence.append(req_id)
                        continue

                    # 7. Strict Environment binding
                    if receipt.environment_ref is not None:
                        if receipt.environment_ref != context.expected_environment_ref:
                            reasons.append(f"receipt environment_ref mismatch: {req_id}")
                            missing_evidence.append(req_id)
                            continue
                        if receipt.environment_ref != handoff.environment.environment_ref:
                            reasons.append(
                                f"receipt environment does not match handoff environment: {req_id}"
                            )
                            missing_evidence.append(req_id)
                            continue

                    # 8. Strict Route binding
                    if (
                        receipt.route is not None
                        and receipt.route != context.expected_route
                    ):
                        reasons.append(f"receipt route mismatch: {req_id}")
                        missing_evidence.append(req_id)
                        continue

                    # 9. Producer Role authorization
                    if (
                        context.policy is not None
                        and context.policy.enabled_roles
                        and not context.policy.is_role_enabled(receipt.producer.role)
                    ):
                        reasons.append(
                            f"receipt producer role '{receipt.producer.role}' is not enabled by policy: {req_id}"
                        )
                        missing_evidence.append(req_id)
                        continue

                    # 10. Capability authorization via approved plan
                    if receipt.capabilities:
                        approval = context.resolve_approval(handoff.approval_reference)
                        if approval is not None:
                            unauthorized_caps = [
                                cap for cap in receipt.capabilities if cap not in approval.enabled_capabilities
                            ]
                            if unauthorized_caps:
                                reasons.append(
                                    f"receipt capabilities not enabled by approved plan: {req_id} ({unauthorized_caps})"
                                )
                                missing_evidence.append(req_id)
                                continue

                    # 11. Artifact Hash presence
                    if not receipt.artifact_hash or not str(receipt.artifact_hash).strip():
                        reasons.append(f"receipt artifact_hash is missing: {req_id}")
                        missing_evidence.append(req_id)
                        continue

                    # 12. Delivery mode check
                    if (
                        requested_state is WorkflowState.DELIVERED
                        and receipt.mode is not ValidationMode.TARGET_ENVIRONMENT
                    ):
                        reasons.append(
                            f"delivery evidence '{req_id}' must be target_environment, got {receipt.mode.value}"
                        )
                        missing_evidence.append(req_id)
                        continue

                    verified_evidence.append(req_id)
                    continue
                else:
                    missing_evidence.append(req_id)
                    reasons.append(f"missing supervisor receipt for required evidence: {req_id}")
                    continue
            else:
                # No context: evaluate against handoff.environment_evidence
                evidence = evidence_by_id.get(req_id)
                if evidence is None:
                    missing_evidence.append(req_id)
                    reasons.append(f"missing required evidence: {req_id}")
                    continue
                if evidence.environment_ref != handoff.environment.environment_ref:
                    reasons.append(f"evidence environment mismatch: {req_id}")
                    continue
                if evidence.result is not EvidenceResult.PASSED:
                    reasons.append(f"evidence is not passed: {req_id}")
                    continue
                if evidence.identity.subject != handoff.environment.worker_identity.subject:
                    reasons.append(f"evidence identity mismatch: {req_id}")
                    continue
                if evidence.freshness is not EvidenceFreshness.CURRENT:
                    reasons.append(f"evidence is not current: {req_id}")
                    continue
                # Calculate age even without context (using UTC)
                ev_now = datetime.now(UTC)
                ev_dt = _aware_utc(evidence.observed_at)
                assert ev_dt is not None
                ev_age = (ev_now - ev_dt).total_seconds()
                if ev_age < 0 or ev_age > 86400:
                    reasons.append(f"evidence is stale: {req_id}")
                    continue
                if not evidence.evidence_ref.strip():
                    reasons.append(f"evidence has no audit reference: {req_id}")
                    continue
                verified_evidence.append(req_id)

        # 7. Delivery-specific requirements
        if requested_state is WorkflowState.DELIVERED:
            if handoff.state not in {WorkflowState.INDEPENDENT_REVIEW, WorkflowState.DELIVERED}:
                reasons.append("delivery requires independent_review")
            if handoff.environment.kind is not EnvironmentKind.TARGET_ENVIRONMENT:
                reasons.append("delivery requires target_environment evidence")
            if handoff.environment.kind is EnvironmentKind.MOCK_ONLY:
                reasons.append("mock_only evidence cannot authorize release")

            if context is not None:
                has_independent_review = False
                has_target_proof = False
                for receipt in context.receipts.values():
                    if (
                        receipt.result is EvidenceResult.PASSED
                        and (
                            receipt.candidate_digest is None
                            or receipt.candidate_digest == context.candidate_digest
                        )
                    ):
                        # Review must be within TTL and bound to the ticket/subject
                        rev_max_age = (
                            context.policy.get_max_age(receipt.requirement)
                            if context.policy is not None
                            else 86400
                        )
                        rev_age = (context.now - receipt.observed_at).total_seconds()
                        if rev_age < 0 or rev_age > rev_max_age:
                            continue
                        if receipt.subject != handoff.ticket_id:
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

                if not has_independent_review:
                    reasons.append(
                        "delivery requires independent review receipt from a reviewer distinct from the implementer"
                    )
                if not has_target_proof:
                    reasons.append("delivery requires passed operational proof in target_environment")

        # 8. Eligibility and readiness determination
        eligible = not reasons
        if requested_state is WorkflowState.DELIVERED and eligible:
            readiness = ReadinessState.OPERATIONALLY_VERIFIED
        elif requested_state is WorkflowState.DELIVERED:
            readiness = ReadinessState.BLOCKED
        elif handoff.state in {WorkflowState.CANCELLED, WorkflowState.FAILED}:
            readiness = ReadinessState.BLOCKED
        elif not eligible and blocking_dependencies:
            readiness = ReadinessState.WAITING_HUMAN
        elif not eligible:
            readiness = ReadinessState.NOT_READY
        elif handoff.environment.kind is EnvironmentKind.MOCK_ONLY:
            readiness = ReadinessState.VALIDATED_IN_SIMULATION
        elif requested_state in {WorkflowState.NEEDS_SPEC, WorkflowState.PLANNING_HIGH}:
            readiness = ReadinessState.READY_FOR_SPEC
        elif requested_state is WorkflowState.READY_FOR_HANDOFF:
            readiness = ReadinessState.READY_FOR_HANDOFF
        elif requested_state is WorkflowState.INDEPENDENT_REVIEW:
            readiness = ReadinessState.READY_FOR_RELEASE
        else:
            readiness = ReadinessState.READY_FOR_RELEASE

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
