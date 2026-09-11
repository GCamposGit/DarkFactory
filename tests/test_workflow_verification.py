"""Tests for the HF-04 verification context, gate policy, and trusted receipts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pytest
from pydantic import ValidationError

from core.workflow.contracts import (
    EnvironmentEvidence,
    EnvironmentKind,
    EnvironmentManifest,
    EvidenceFreshness,
    EvidenceRequirement,
    EvidenceResult,
    GrillAlternative,
    GrillDecision,
    GrillRecord,
    ManualDependency,
    ManualDependencyStatus,
    PlannerTier,
    ReadinessState,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import (
    ReadinessError,
    ReadinessGate,
    mark_delivered,
    validate_transition,
)
from core.workflow.verification import (
    EvidenceReceipt,
    GatePolicy,
    PlanApproval,
    PolicyExemption,
    ValidationMode,
    VerificationContext,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
SUPERVISOR_IDENTITY = SanitizedIdentity(subject="supervisor-agent", role="orchestrator", host="trusted-host")
WORKER_IDENTITY = SanitizedIdentity(subject="worker-1", role="implementer", host="worker-host")


def make_approval(ref: str = "approval-1", digest: str = "sha256:plan-123") -> PlanApproval:
    return PlanApproval(
        approval_ref=ref,
        planner_tier=PlannerTier.HIGH,
        plan_digest=digest,
        approved_by=SUPERVISOR_IDENTITY,
        approved_at=NOW,
        enabled_capabilities=("core_execution", "sqlite_storage"),
    )


def make_receipt(
    receipt_id: str = "receipt-1",
    requirement: str = "req-test-suite",
    result: EvidenceResult = EvidenceResult.PASSED,
    observed_at: datetime = NOW,
    subject: str = "HF-04-01",
    producer: SanitizedIdentity | None = None,
    mode: ValidationMode = ValidationMode.TARGET_ENVIRONMENT,
    plan_digest: str | None = "sha256:plan-123",
    candidate_digest: str | None = "sha256:cand-456",
    config_version: str | None = "config-v1",
    environment_ref: str | None = "hf04-target-local",
    route: str | None = "core/workflow",
    capabilities: tuple[str, ...] = ("core_execution",),
    artifact_hash: str = "sha256:" + "a" * 64,
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=receipt_id,
        producer=producer or SUPERVISOR_IDENTITY,
        subject=subject,
        requirement=requirement,
        artifact_hash=artifact_hash,
        result=result,
        mode=mode,
        observed_at=observed_at,
        plan_digest=plan_digest,
        candidate_digest=candidate_digest,
        config_version=config_version,
        environment_ref=environment_ref,
        route=route,
        capabilities=capabilities,
    )



def make_exemption(
    exemption_id: str = "exemption-1",
    requirement: str = "req-docs",
    is_operational: bool = False,
    expires_at: datetime | None = None,
) -> PolicyExemption:
    return PolicyExemption(
        exemption_id=exemption_id,
        ticket_id="HF-04-01",
        version="1.0",
        requirement=requirement,
        authorized_by=SUPERVISOR_IDENTITY,
        reason="Exemption approved for documentation check in local dev.",
        is_operational=is_operational,
        expires_at=expires_at,
    )


def make_context(
    *,
    approvals: dict[str, PlanApproval] | None = None,
    receipts: dict[str, EvidenceReceipt] | None = None,
    exemptions: dict[str, PolicyExemption] | None = None,
    policy: GatePolicy | None = None,
    now: datetime = NOW,
) -> VerificationContext:
    return VerificationContext(
        now=now,
        policy_version="1",
        plan_digest="sha256:plan-123",
        candidate_digest="sha256:cand-456",
        config_version="config-v1",
        expected_environment_ref="hf04-target-local",
        expected_identity=WORKER_IDENTITY,
        expected_route="core/workflow",
        approvals=approvals if approvals is not None else {"approval-1": make_approval()},
        receipts=receipts if receipts is not None else {"req-test-suite": make_receipt()},
        exemptions=exemptions,
        policy=policy,
    )


def make_test_handoff(
    *,
    ticket_id: str = "HF-04-01",
    state: WorkflowState = WorkflowState.INDEPENDENT_REVIEW,
    planner_tier: PlannerTier = PlannerTier.HIGH,
    approval_ref: str = "approval-1",
    plan_digest: str = "sha256:plan-123",
    environment_kind: EnvironmentKind = EnvironmentKind.TARGET_ENVIRONMENT,
    required_evidence: list[EvidenceRequirement] | None = None,
    environment_evidence: list[EnvironmentEvidence] | None = None,
    manual_dependencies: list[ManualDependency] | None = None,
) -> WorkflowHandoff:
    return WorkflowHandoff(
        ticket_id=ticket_id,
        parent_id="HF-04",
        objective="Verification gate test handoff",
        origin="code-review",
        plan_version="1",
        planner_id="planner-1",
        planner_tier=planner_tier,
        approval_reference=approval_ref,
        baseline_sha="a" * 40,
        grill=GrillRecord(
            demand_id=ticket_id,
            intent_summary="Verify gate rules",
            ready_for_spec=True,
            readiness_justification="Scope known",
            example_criteria=["Must pass"],
        ),
        environment=EnvironmentManifest(
            environment_ref="hf04-target-local",
            ticket_id=ticket_id,
            kind=environment_kind,
            system="Linux",
            architecture="x86_64",
            network_policy="restricted",
            worker_identity=WORKER_IDENTITY,
            target_differences=[] if environment_kind is not EnvironmentKind.MOCK_ONLY else ["mocked sandbox"],
        ),
        state=state,
        successor_event="workflow.done",
        retry_policy="bounded",
        resume_strategy="reconcile",
        rollback_plan="workspace cleanup only",
        required_evidence=required_evidence
        if required_evidence is not None
        else [
            EvidenceRequirement(
                evidence_id="req-test-suite",
                description="Core test suite passes",
                required_for=ReadinessState.READY_FOR_RELEASE,
            )
        ],
        environment_evidence=environment_evidence
        if environment_evidence is not None
        else [
            EnvironmentEvidence(
                evidence_id="req-test-suite",
                requirement="Core test suite passes",
                environment_ref="hf04-target-local",
                identity=WORKER_IDENTITY,
                origin="local-runner",
                build_digest="sha256:cand-456",
                config_version="config-v1",
                test_name="test_core",
                expected="passed",
                observed="passed",
                result=EvidenceResult.PASSED,
                freshness=EvidenceFreshness.CURRENT,
                observed_at=NOW,
                evidence_ref="artifact://hf04/receipt-1",
            )
        ],
        manual_dependencies=manual_dependencies or [],
    )


def test_verification_context_immutability() -> None:
    context = make_context()

    # Attribute reassignment or deletion is prevented
    with pytest.raises(AttributeError, match="VerificationContext is immutable"):
        context.plan_digest = "tampered"  # type: ignore[misc]

    with pytest.raises(AttributeError, match="VerificationContext is immutable"):
        del context.plan_digest  # type: ignore[misc]

    # Mapping proxy prevents mutation of the underlying collections
    with pytest.raises(TypeError):
        context.approvals["approval-1"] = make_approval("tampered")  # type: ignore[index]

    with pytest.raises(TypeError):
        context.receipts["req-test-suite"] = make_receipt("tampered")  # type: ignore[index]


def test_models_are_frozen() -> None:
    approval = make_approval()
    with pytest.raises(ValidationError):
        approval.planner_tier = PlannerTier.ECONOMY  # type: ignore[misc]

    receipt = make_receipt()
    with pytest.raises(ValidationError):
        receipt.result = EvidenceResult.FAILED  # type: ignore[misc]

    exemption = make_exemption()
    with pytest.raises(ValidationError):
        exemption.is_operational = True  # type: ignore[misc]


def test_rejection_of_naive_datetimes() -> None:
    naive_dt = datetime(2026, 9, 9, 12, 0)

    with pytest.raises(ValueError, match="explicit timezone"):
        make_context(now=naive_dt)

    with pytest.raises(ValidationError):
        PlanApproval(
            approval_ref="approval-naive",
            planner_tier=PlannerTier.HIGH,
            plan_digest="sha256:plan-123",
            approved_by=SUPERVISOR_IDENTITY,
            approved_at=naive_dt,
        )

    with pytest.raises(ValidationError):
        EvidenceReceipt(
            receipt_id="receipt-naive",
            producer=SUPERVISOR_IDENTITY,
            subject="HF-04-01",
            requirement="req-1",
            artifact_hash="sha256:abc",
            result=EvidenceResult.PASSED,
            mode=ValidationMode.TARGET_ENVIRONMENT,
            observed_at=naive_dt,
        )

    with pytest.raises(ValidationError):
        PolicyExemption(
            exemption_id="ex-naive",
            ticket_id="HF-04-01",
            version="1.0",
            requirement="req-1",
            authorized_by=SUPERVISOR_IDENTITY,
            reason="reason",
            expires_at=naive_dt,
        )


def test_rejection_of_conflicting_references() -> None:
    appr1 = make_approval("ref-1", digest="sha256:digest-A")
    appr2 = make_approval("ref-1", digest="sha256:digest-B")

    # In Python dict literals, keys cannot be duplicated, but passing non-conforming mapping or conflicting logic
    # Rejection of invalid types in mapping:
    with pytest.raises(TypeError, match="must be a PlanApproval instance"):
        make_context(approvals={"ref-1": "not-an-approval"})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="must be an EvidenceReceipt instance"):
        make_context(receipts={"req-1": "not-a-receipt"})  # type: ignore[dict-item]


def test_expected_metadata_independent_of_candidate() -> None:
    context = make_context()
    assert context.expected_environment_ref == "hf04-target-local"
    assert context.expected_identity == WORKER_IDENTITY
    assert context.expected_route == "core/workflow"
    assert context.plan_digest == "sha256:plan-123"
    assert context.candidate_digest == "sha256:cand-456"


def test_trusted_producer_distinguished_from_imported_declaration() -> None:
    receipt = make_receipt()
    assert receipt.producer == SUPERVISOR_IDENTITY
    assert receipt.mode == ValidationMode.TARGET_ENVIRONMENT
    assert receipt.result == EvidenceResult.PASSED
    assert receipt.artifact_hash.startswith("sha256:")


def test_planning_stage_permits_zero_operational_proofs() -> None:
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.PLANNING_HIGH: ("req-spec-review",),
            WorkflowState.READY_FOR_HANDOFF: ("req-spec-review", "req-security-design"),
            WorkflowState.DELIVERED: ("req-target-test", "req-independent-audit"),
        },
        enabled_roles=frozenset({"orchestrator", "staff_reviewer"}),
        max_age_seconds={"req-target-test": 3600},
    )

    # Planning has 0 operational proofs
    planning_reqs = policy.requirements_for_stage(WorkflowState.PLANNING_HIGH)
    assert "req-target-test" not in planning_reqs
    assert policy.is_role_enabled("orchestrator")
    assert not policy.is_role_enabled("untrusted_role")


def test_delivery_policy_rejects_operational_evidence_exemption() -> None:
    # A policy attempting to allow an operational exemption for delivery requirements is rejected
    op_exemption = make_exemption(
        exemption_id="ex-op",
        requirement="req-target-test",
        is_operational=True,
    )
    with pytest.raises(ValueError, match="delivery policy cannot permit operational exemption"):
        GatePolicy(
            policy_version="1",
            stage_requirements={
                WorkflowState.DELIVERED: ("req-target-test",),
            },
            exemptions=(op_exemption,),
        )


def test_diagnostic_summary_sanitizes_credentials() -> None:
    context = make_context()
    diag = context.diagnostic_summary()
    assert "now" in diag
    assert "policy_version" in diag
    assert "expected_environment_ref" in diag
    assert diag["plan_digest"].endswith("...")
    assert diag["candidate_digest"].endswith("...")
    # Ensure no secrets leak
    diag_str = str(diag).lower()
    assert "password" not in diag_str
    assert "secret" not in diag_str
    assert "token" not in diag_str


def test_context_resolvers() -> None:
    approval = make_approval("appr-99")
    receipt = make_receipt("rcpt-99", requirement="req-focal")
    exemption = make_exemption("ex-99", requirement="req-optional")
    context = make_context(
        approvals={"appr-99": approval},
        receipts={"rcpt-99": receipt},
        exemptions={"ex-99": exemption},
    )

    assert context.resolve_approval("appr-99") == approval
    assert context.resolve_approval("unknown") is None

    # Resolve receipt by key or requirement
    assert context.resolve_receipt("rcpt-99") == receipt
    assert context.resolve_receipt("req-focal") == receipt
    assert context.resolve_receipt("missing") is None

    # Resolve exemption by key or requirement
    assert context.resolve_exemption("ex-99") == exemption
    assert context.resolve_exemption("req-optional") == exemption
    assert context.resolve_exemption("missing") is None


def test_exemption_expiration() -> None:
    past_time = NOW - timedelta(seconds=60)
    future_time = NOW + timedelta(seconds=60)

    expired_ex = make_exemption(expires_at=past_time)
    assert not expired_ex.is_valid_at(NOW)

    valid_ex = make_exemption(expires_at=future_time)
    assert valid_ex.is_valid_at(NOW)

    permanent_ex = make_exemption(expires_at=None)
    assert permanent_ex.is_valid_at(NOW)


def test_contradictory_receipts_rejected() -> None:
    receipt_pass = make_receipt(receipt_id="r-1", requirement="req-security", result=EvidenceResult.PASSED)
    receipt_fail = make_receipt(receipt_id="r-2", requirement="req-security", result=EvidenceResult.FAILED)

    with pytest.raises(ValueError, match="conflicting receipts detected for requirement"):
        make_context(receipts={"r-1": receipt_pass, "r-2": receipt_fail})


def test_workflow_handoff_rejects_context_field() -> None:
    # Context cannot be a field of WorkflowHandoff nor deserialized from candidate JSON
    assert "verification_context" not in WorkflowHandoff.model_fields
    assert "context" not in WorkflowHandoff.model_fields


def test_diagnostic_summary_redacts_sensitive_endpoint() -> None:
    context = VerificationContext(
        now=NOW,
        policy_version="1",
        plan_digest="sha256:plan-123",
        candidate_digest="sha256:cand-456",
        config_version="config-v1",
        expected_environment_ref="password=123",
        expected_identity=WORKER_IDENTITY,
        expected_route="postgres://user:token123@host/db",
        approvals={"approval-1": make_approval()},
        receipts={"req-test-suite": make_receipt()},
    )
    diag = context.diagnostic_summary()
    assert diag["expected_environment_ref"] == "[REDACTED]"
    assert diag["expected_route"] == "[REDACTED]"


# ==============================================================================
# CR-05 Acceptance Tests: Gate Policy, Authority, and Stage Enforcement
# ==============================================================================


def test_planning_does_not_require_future_operational_evidence() -> None:
    """Acceptance (1): Valid planning control does not require future operational proofs."""
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.READY_FOR_HANDOFF: ("req-plan-review",),
            WorkflowState.DELIVERED: ("req-target-proof",),
        },
        enabled_roles=frozenset({"orchestrator", "supervisor"}),
    )
    plan_receipt = make_receipt(
        receipt_id="r-plan",
        requirement="req-plan-review",
        result=EvidenceResult.PASSED,
    )
    context = make_context(
        receipts={"req-plan-review": plan_receipt},
        policy=policy,
    )
    handoff = make_test_handoff(
        state=WorkflowState.READY_FOR_HANDOFF,
        required_evidence=[
            EvidenceRequirement(
                evidence_id="req-plan-review",
                description="Planning review",
                required_for=ReadinessState.READY_FOR_HANDOFF,
            ),
            EvidenceRequirement(
                evidence_id="req-target-proof",
                description="Operational delivery proof",
                required_for=ReadinessState.OPERATIONALLY_VERIFIED,
            ),
        ],
    )

    gate = ReadinessGate()
    report = gate.evaluate(handoff, context=context, target_state=WorkflowState.READY_FOR_HANDOFF)
    assert report.eligible is True
    assert report.readiness is ReadinessState.READY_FOR_HANDOFF
    assert "req-target-proof" not in report.missing_evidence
    assert "req-plan-review" in report.verified_evidence_ids


def test_empty_candidate_evidence_list_does_not_bypass_policy_requirements() -> None:
    """Acceptance (2): Empty candidate evidence list cannot eliminate policy requirements."""
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.INDEPENDENT_REVIEW: ("req-security-scan", "req-focal-suite"),
        },
    )
    context = make_context(policy=policy, receipts={})
    handoff = make_test_handoff(
        state=WorkflowState.INDEPENDENT_REVIEW,
        required_evidence=[],
        environment_evidence=[],
    )

    gate = ReadinessGate()
    report = gate.evaluate(handoff, context=context, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert "req-security-scan" in report.missing_evidence
    assert "req-focal-suite" in report.missing_evidence


def test_plan_approval_and_candidate_binding_rejections() -> None:
    """Acceptance (3): False approval, self-declared economy, or mismatched candidate digest blocks."""
    gate = ReadinessGate()

    # 3a: Approval reference not registered in supervisor context
    ctx_no_approval = make_context(approvals={})
    handoff = make_test_handoff()
    rep1 = gate.evaluate(handoff, context=ctx_no_approval)
    assert rep1.eligible is False
    assert any("plan approval not found" in r for r in rep1.reasons)

    # 3b: Plan approval digest mismatch
    mismatched_appr = make_approval(digest="sha256:different-plan-digest")
    ctx_bad_digest = make_context(approvals={"approval-1": mismatched_appr})
    rep2 = gate.evaluate(handoff, context=ctx_bad_digest)
    assert rep2.eligible is False
    assert any("plan approval digest mismatch" in r for r in rep2.reasons)

    # 3c: Self-declared economy planner tier in handoff
    ctx_valid = make_context()
    handoff_economy = make_test_handoff(planner_tier=PlannerTier.ECONOMY)
    rep3 = gate.evaluate(handoff_economy, context=ctx_valid)
    assert rep3.eligible is False
    assert any("economy planner tier cannot produce approved plan" in r for r in rep3.reasons)

    # 3d: Approval planner tier mismatch
    appr_economy = PlanApproval(
        approval_ref="approval-1",
        planner_tier=PlannerTier.ECONOMY,
        plan_digest="sha256:plan-123",
        approved_by=SUPERVISOR_IDENTITY,
        approved_at=NOW,
    )
    ctx_tier_mismatch = make_context(approvals={"approval-1": appr_economy})
    rep4 = gate.evaluate(handoff, context=ctx_tier_mismatch)
    assert rep4.eligible is False
    assert any("planner_tier mismatch" in r for r in rep4.reasons)

    # 3e: Receipt bound to different candidate digest
    receipt_diff_candidate = EvidenceReceipt(
        receipt_id="r-diff",
        producer=SUPERVISOR_IDENTITY,
        subject="HF-04-01",
        requirement="req-test-suite",
        artifact_hash="sha256:art",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=NOW,
        candidate_digest="sha256:other-candidate",
    )
    ctx_diff_cand = make_context(receipts={"req-test-suite": receipt_diff_candidate})
    rep5 = gate.evaluate(handoff, context=ctx_diff_cand)
    assert rep5.eligible is False
    assert any("receipt candidate_digest mismatch" in r for r in rep5.reasons)


def test_cancelled_and_failed_workflows_cannot_advance() -> None:
    """Acceptance (4): Cancelled or failed workflows cannot advance or be evaluated as eligible."""
    gate = ReadinessGate()
    ctx = make_context()

    # Cancelled evaluated in-place
    handoff_cancelled = make_test_handoff(state=WorkflowState.CANCELLED)
    rep_cancelled = gate.evaluate(handoff_cancelled, context=ctx)
    assert rep_cancelled.eligible is False
    assert rep_cancelled.readiness is ReadinessState.BLOCKED
    assert any("cancelled workflow cannot advance" in r for r in rep_cancelled.reasons)

    # Cancelled attempting illegal transition to implementing_economy
    rep_advance = gate.evaluate(
        handoff_cancelled,
        context=ctx,
        target_state=WorkflowState.IMPLEMENTING_ECONOMY,
    )
    assert rep_advance.eligible is False
    assert rep_advance.readiness is ReadinessState.BLOCKED
    assert any(
        "illegal workflow transition cancelled -> implementing_economy" in r for r in rep_advance.reasons
    )

    # Failed workflow
    handoff_failed = make_test_handoff(state=WorkflowState.FAILED)
    rep_failed = gate.evaluate(handoff_failed, context=ctx)
    assert rep_failed.eligible is False
    assert rep_failed.readiness is ReadinessState.BLOCKED
    assert any("failed workflow cannot advance" in r for r in rep_failed.reasons)


def test_valid_progressive_workflow_stages_flow() -> None:
    """Acceptance (5): Valid progressive workflow transitions work across all stages."""
    gate = ReadinessGate()
    reviewer_identity = SanitizedIdentity(subject="reviewer-carol", role="reviewer", host="trusted-host")

    review_receipt = EvidenceReceipt(
        receipt_id="receipt-review-1",
        producer=reviewer_identity,
        subject="HF-04-01",
        requirement="independent_review",
        artifact_hash="sha256:review-ok",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=NOW,
        candidate_digest="sha256:cand-456",
        plan_digest="sha256:plan-123",
        environment_ref="hf04-target-local",
    )
    target_proof = make_receipt()

    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.READY_FOR_HANDOFF: (),
            WorkflowState.IMPLEMENTING_ECONOMY: (),
            WorkflowState.VALIDATING: (),
            WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",),
            WorkflowState.DELIVERED: ("req-test-suite", "independent_review"),
        },
        enabled_roles=frozenset({"orchestrator", "implementer", "reviewer"}),
    )
    context = make_context(
        receipts={
            "req-test-suite": target_proof,
            "independent_review": review_receipt,
        },
        policy=policy,
    )

    # 1. READY_FOR_HANDOFF -> target IMPLEMENTING_ECONOMY
    h1 = make_test_handoff(state=WorkflowState.READY_FOR_HANDOFF)
    r1 = gate.evaluate(h1, context=context, target_state=WorkflowState.IMPLEMENTING_ECONOMY)
    assert r1.eligible is True

    # 2. IMPLEMENTING_ECONOMY -> target VALIDATING
    h2 = h1.model_copy(update={"state": WorkflowState.IMPLEMENTING_ECONOMY})
    r2 = gate.evaluate(h2, context=context, target_state=WorkflowState.VALIDATING)
    assert r2.eligible is True

    # 3. VALIDATING -> target INDEPENDENT_REVIEW
    h3 = h2.model_copy(update={"state": WorkflowState.VALIDATING})
    r3 = gate.evaluate(h3, context=context, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert r3.eligible is True

    # 4. INDEPENDENT_REVIEW -> target DELIVERED
    h4 = h3.model_copy(update={"state": WorkflowState.INDEPENDENT_REVIEW})
    r4 = gate.evaluate(h4, context=context, target_state=WorkflowState.DELIVERED)
    assert r4.eligible is True
    assert r4.readiness is ReadinessState.OPERATIONALLY_VERIFIED

    # 5. mark_delivered succeeds
    delivered = mark_delivered(h4, gate, context=context)
    assert delivered.state is WorkflowState.DELIVERED


def test_delivery_fails_closed_without_independent_review_or_target_proof() -> None:
    """Acceptance (6): Delivery without distinct review receipt or target proof fails closed."""
    gate = ReadinessGate()

    # 6a: Missing review receipt (only worker/runner receipts exist)
    target_proof = make_receipt()
    ctx_no_review = make_context(receipts={"req-test-suite": target_proof})
    handoff = make_test_handoff(state=WorkflowState.INDEPENDENT_REVIEW)
    r1 = gate.evaluate(handoff, context=ctx_no_review, target_state=WorkflowState.DELIVERED)
    assert r1.eligible is False
    assert any("independent review receipt" in r for r in r1.reasons)

    # 6b: Reviewer identity equals worker identity (producer.subject == expected_identity.subject)
    bad_review_receipt = EvidenceReceipt(
        receipt_id="receipt-bad-review",
        producer=WORKER_IDENTITY,  # Same as context.expected_identity!
        subject="HF-04-01",
        requirement="independent_review",
        artifact_hash="sha256:bad-rev",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=NOW,
        candidate_digest="sha256:cand-456",
    )
    ctx_same_identity = make_context(
        receipts={"req-test-suite": target_proof, "independent_review": bad_review_receipt}
    )
    r2 = gate.evaluate(handoff, context=ctx_same_identity, target_state=WorkflowState.DELIVERED)
    assert r2.eligible is False
    assert any("distinct from the implementer" in r for r in r2.reasons)

    # 6c: Review receipt exists, but only in SIMULATION mode (no TARGET_ENVIRONMENT receipt)
    reviewer_identity = SanitizedIdentity(subject="reviewer-carol", role="reviewer", host="trusted-host")
    sim_proof = EvidenceReceipt(
        receipt_id="r-sim",
        producer=SUPERVISOR_IDENTITY,
        subject="HF-04-01",
        requirement="req-test-suite",
        artifact_hash="sha256:sim",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.SIMULATION,
        observed_at=NOW,
        candidate_digest="sha256:cand-456",
    )
    sim_review = EvidenceReceipt(
        receipt_id="r-sim-rev",
        producer=reviewer_identity,
        subject="HF-04-01",
        requirement="independent_review",
        artifact_hash="sha256:sim-rev",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.SIMULATION,
        observed_at=NOW,
        candidate_digest="sha256:cand-456",
    )
    ctx_sim_only = make_context(receipts={"req-test-suite": sim_proof, "independent_review": sim_review})
    r3 = gate.evaluate(handoff, context=ctx_sim_only, target_state=WorkflowState.DELIVERED)
    assert r3.eligible is False
    assert any("passed operational proof in target_environment" in r for r in r3.reasons)

    # 6d: Delivery attempted directly from VALIDATING state
    handoff_validating = make_test_handoff(state=WorkflowState.VALIDATING)
    with pytest.raises(ReadinessError, match="delivery rejected"):
        gate.require_delivery(handoff_validating, context=ctx_no_review)


def test_missing_context_fails_closed_with_stable_code() -> None:
    """Missing context returns ineligible with stable CONTEXT_REQUIRED code."""
    gate = ReadinessGate()
    handoff = make_test_handoff(state=WorkflowState.READY_FOR_HANDOFF)
    report = gate.evaluate(handoff, context=None)
    assert report.eligible is False
    assert "missing verification context: CONTEXT_REQUIRED" in report.reasons
    assert report.readiness is ReadinessState.NOT_READY

    with pytest.raises(ReadinessError, match="CONTEXT_REQUIRED"):
        gate.require_delivery(handoff, context=None)


# ==============================================================================
# CR-06 Acceptance Tests: TTL Validity and Strict Evidence Subject Binding
# ==============================================================================


def test_ttl_positive_control_and_exact_boundary() -> None:
    """Acceptance: fully bound positive control passes at exact limit (age == max_age) and age == 0."""
    gate = ReadinessGate()
    max_age = 3600
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",),
        },
        enabled_roles=frozenset({"orchestrator"}),
        max_age_seconds={"req-test-suite": max_age},
    )

    # 1. Fresh evidence at age == 0
    receipt_fresh = make_receipt(observed_at=NOW)
    ctx_fresh = make_context(receipts={"req-test-suite": receipt_fresh}, policy=policy)
    h_fresh = make_test_handoff()
    rep_fresh = gate.evaluate(h_fresh, context=ctx_fresh, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert rep_fresh.eligible is True
    assert "req-test-suite" in rep_fresh.verified_evidence_ids
    assert not rep_fresh.reasons

    # 2. Exact boundary: age == max_age
    boundary_time = NOW - timedelta(seconds=max_age)
    receipt_boundary = make_receipt(observed_at=boundary_time)
    ctx_boundary = make_context(receipts={"req-test-suite": receipt_boundary}, policy=policy)
    h_boundary = make_test_handoff()
    rep_boundary = gate.evaluate(h_boundary, context=ctx_boundary, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert rep_boundary.eligible is True
    assert "req-test-suite" in rep_boundary.verified_evidence_ids
    assert not rep_boundary.reasons


def test_ttl_rejection_one_second_past_boundary() -> None:
    """Acceptance: evidence one second past TTL boundary fails closed with isolated reason."""
    gate = ReadinessGate()
    max_age = 3600
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",),
        },
        enabled_roles=frozenset({"orchestrator"}),
        max_age_seconds={"req-test-suite": max_age},
    )
    expired_time = NOW - timedelta(seconds=max_age + 1)
    receipt_expired = make_receipt(observed_at=expired_time)
    ctx = make_context(receipts={"req-test-suite": receipt_expired}, policy=policy)
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert "req-test-suite" in report.missing_evidence
    assert any("evidence receipt expired: req-test-suite" in r for r in report.reasons)
    # Ensure this is the ONLY failure reason (positive control is otherwise valid)
    assert len(report.reasons) == 1


def test_ttl_rejection_future_timestamp() -> None:
    """Acceptance: evidence with future timestamp (now - observed_at < 0) fails closed with isolated reason."""
    gate = ReadinessGate()
    future_time = NOW + timedelta(seconds=1)
    receipt_future = make_receipt(observed_at=future_time)
    ctx = make_context(receipts={"req-test-suite": receipt_future})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert "req-test-suite" in report.missing_evidence
    assert any("evidence receipt observed in the future: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_ttl_candidate_current_flag_does_not_bypass_expired_receipt() -> None:
    """Acceptance: candidate declaring freshness=CURRENT cannot bypass an expired trusted receipt."""
    gate = ReadinessGate()
    max_age = 600
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",),
        },
        enabled_roles=frozenset({"orchestrator"}),
        max_age_seconds={"req-test-suite": max_age},
    )
    expired_time = NOW - timedelta(seconds=max_age + 10)
    receipt_expired = make_receipt(observed_at=expired_time)
    ctx = make_context(receipts={"req-test-suite": receipt_expired}, policy=policy)

    # Candidate explicitly declares freshness=CURRENT in handoff payload
    handoff = make_test_handoff()
    assert handoff.environment_evidence[0].freshness is EvidenceFreshness.CURRENT

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("evidence receipt expired: req-test-suite" in r for r in report.reasons)


def test_strict_binding_subject_mismatch() -> None:
    """Acceptance: evidence receipt bound to another subject (ticket) fails closed."""
    gate = ReadinessGate()
    receipt_other_subject = make_receipt(subject="HF-99-99")
    ctx = make_context(receipts={"req-test-suite": receipt_other_subject})
    handoff = make_test_handoff(ticket_id="HF-04-01")

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert "req-test-suite" in report.missing_evidence
    assert any("evidence receipt subject mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_strict_binding_route_mismatch() -> None:
    """Acceptance: evidence receipt bound to a different execution route fails closed."""
    gate = ReadinessGate()
    receipt_other_route = make_receipt(route="unauthorized/driver/route")
    ctx = make_context(receipts={"req-test-suite": receipt_other_route})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("receipt route mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_strict_binding_build_digest_mismatch() -> None:
    """Acceptance: evidence receipt bound to a different candidate / build digest fails closed."""
    gate = ReadinessGate()
    receipt_diff_digest = make_receipt(candidate_digest="sha256:" + "f" * 64)
    ctx = make_context(receipts={"req-test-suite": receipt_diff_digest})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("receipt candidate_digest mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_strict_binding_environment_ref_mismatch() -> None:
    """Acceptance: evidence receipt bound to another environment ref fails closed."""
    gate = ReadinessGate()
    receipt_diff_env = make_receipt(environment_ref="hf04-mock-sandbox")
    ctx = make_context(receipts={"req-test-suite": receipt_diff_env})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("receipt environment_ref mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_strict_binding_config_version_mismatch() -> None:
    """Acceptance: changed config version invalidates receipt even when fresh within TTL."""
    gate = ReadinessGate()
    receipt_diff_config = make_receipt(observed_at=NOW, config_version="config-v999")
    ctx = make_context(receipts={"req-test-suite": receipt_diff_config})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("receipt config_version mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_strict_binding_plan_digest_mismatch() -> None:
    """Acceptance: evidence receipt bound to a different plan digest fails closed."""
    gate = ReadinessGate()
    receipt_diff_plan = make_receipt(plan_digest="sha256:" + "e" * 64)
    ctx = make_context(receipts={"req-test-suite": receipt_diff_plan})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("receipt plan_digest mismatch: req-test-suite" in r for r in report.reasons)
    assert len(report.reasons) == 1


def test_producer_role_not_enabled_by_policy() -> None:
    """Acceptance: receipt produced by a role not enabled in gate policy fails closed."""
    gate = ReadinessGate()
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",),
        },
        enabled_roles=frozenset({"orchestrator", "trusted_supervisor"}),
    )
    unauthorized_producer = SanitizedIdentity(subject="agent-bob", role="untrusted_runner", host="local")
    receipt_unauthorized = make_receipt(producer=unauthorized_producer)
    ctx = make_context(receipts={"req-test-suite": receipt_unauthorized}, policy=policy)
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any(
        "receipt producer role 'untrusted_runner' is not enabled by policy: req-test-suite" in r
        for r in report.reasons
    )
    assert len(report.reasons) == 1


def test_receipt_unauthorized_capabilities() -> None:
    """Acceptance: receipt declaring capabilities not authorized by approved plan fails closed."""
    gate = ReadinessGate()
    # Approved plan only authorizes ("core_execution", "sqlite_storage")
    receipt_unauthorized_cap = make_receipt(capabilities=("unauthorized_network_egress",))
    ctx = make_context(receipts={"req-test-suite": receipt_unauthorized_cap})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any(
        "receipt capabilities not enabled by approved plan: req-test-suite" in r
        for r in report.reasons
    )
    assert len(report.reasons) == 1


def test_delivery_requires_fresh_review_and_matching_subject() -> None:
    """Acceptance: delivery fails closed if independent review receipt is expired or bound to another subject."""
    gate = ReadinessGate()
    reviewer_identity = SanitizedIdentity(subject="reviewer-carol", role="reviewer", host="trusted-host")
    target_proof = make_receipt()

    policy = GatePolicy(
        policy_version="1",
        stage_requirements={
            WorkflowState.DELIVERED: ("req-test-suite",),
        },
        enabled_roles=frozenset({"orchestrator", "reviewer"}),
        max_age_seconds={"independent_review": 3600},
    )

    # 1. Expired review receipt
    expired_review = EvidenceReceipt(
        receipt_id="receipt-review-1",
        producer=reviewer_identity,
        subject="HF-04-01",
        requirement="independent_review",
        artifact_hash="sha256:review-ok",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=NOW - timedelta(seconds=3601),
        candidate_digest="sha256:cand-456",
        plan_digest="sha256:plan-123",
        environment_ref="hf04-target-local",
    )
    ctx_expired_rev = make_context(
        receipts={"req-test-suite": target_proof, "independent_review": expired_review},
        policy=policy,
    )
    handoff = make_test_handoff(state=WorkflowState.INDEPENDENT_REVIEW)
    rep1 = gate.evaluate(handoff, context=ctx_expired_rev, target_state=WorkflowState.DELIVERED)
    assert rep1.eligible is False
    assert any("delivery requires independent review receipt" in r for r in rep1.reasons)

    # 2. Review receipt bound to different subject
    review_diff_subject = EvidenceReceipt(
        receipt_id="receipt-review-1",
        producer=reviewer_identity,
        subject="OTHER-TICKET-99",
        requirement="independent_review",
        artifact_hash="sha256:review-ok",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=NOW,
        candidate_digest="sha256:cand-456",
        plan_digest="sha256:plan-123",
        environment_ref="hf04-target-local",
    )
    ctx_diff_subject = make_context(
        receipts={"req-test-suite": target_proof, "independent_review": review_diff_subject},
        policy=policy,
    )
    rep2 = gate.evaluate(handoff, context=ctx_diff_subject, target_state=WorkflowState.DELIVERED)
    assert rep2.eligible is False
    assert any("delivery requires independent review receipt" in r for r in rep2.reasons)


def test_error_reasons_contain_ids_and_redact_secrets() -> None:
    """Acceptance: error reasons contain requirement and ticket IDs without leaking credentials."""
    gate = ReadinessGate()

    # Receipt with mismatched subject
    receipt = make_receipt(subject="UNBOUND-TICKET")
    ctx = make_context(receipts={"req-test-suite": receipt})
    handoff = make_test_handoff()

    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False

    # Verify IDs are in reasons
    reasons_text = " ".join(report.reasons)
    assert "req-test-suite" in reasons_text
    assert "HF-04-01" in reasons_text
    assert "UNBOUND-TICKET" in reasons_text


def make_test_dependency(
    status: ManualDependencyStatus = ManualDependencyStatus.WAITING,
    *,
    dependency_id: str = "dep-1",
    ticket_ids: list[str] | None = None,
    final_probe: str = "python -m core.workflow.probe --check dep-1",
    receipt_ref: str | None = None,
    created_at: datetime = NOW,
    resolved_at: datetime | None = None,
    blocked_stages: list[WorkflowState] | None = None,
) -> ManualDependency:
    return ManualDependency(
        dependency_id=dependency_id,
        ticket_ids=ticket_ids or ["HF-04-01"],
        status=status,
        reason="Manual dependency for testing",
        alternatives_attempted=[
            {
                "alternative_id": "alt-1",
                "description": "Alt 1",
                "tested": True,
                "equivalent": False,
                "reason_unusable": "Not equivalent",
            }
        ],
        configuration_location="secret://target",
        steps=[{"number": 1, "instruction": "Do step 1", "expected_result": "Success"}],
        final_probe=final_probe,
        resume_criteria="Probe passes",
        help_route="runbook/manual",
        created_at=created_at,
        resolved_at=resolved_at if resolved_at is not None else (created_at if status is ManualDependencyStatus.RESOLVED else None),
        resolution_receipt_ref=(
            receipt_ref
            if receipt_ref is not None
            else ("receipt-dep-1" if status is ManualDependencyStatus.RESOLVED else None)
        ),
        blocked_stages=blocked_stages or [],
    )


def test_grill_unanswered_material_decision_blocks_readiness() -> None:
    gate = ReadinessGate()
    ctx = make_context()
    decision = GrillDecision(
        decision_id="dec-material-1",
        question="Pergunta?",
        alternatives=[
            GrillAlternative(alternative_id="a", label="A", consequence="CA"),
            GrillAlternative(alternative_id="b", label="B", consequence="CB"),
        ],
        decision_source="owner",
        selected_alternative_id=None,
        response=None,
        is_material=True,
    )
    grill = GrillRecord(
        demand_id="HF-04-01",
        intent_summary="Teste",
        decisions=[decision],
        pending_questions=[],
        example_criteria=["Critério"],
        ready_for_spec=False,
        readiness_justification="Pendente resposta",
    )
    handoff = make_test_handoff().model_copy(update={"grill": grill})
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert any("dec-material-1" in r and "unanswered" in r for r in report.reasons)


def test_grill_reused_decision_with_origin_passes_readiness() -> None:
    gate = ReadinessGate()
    ctx = make_context()
    decision = GrillDecision(
        decision_id="dec-reused-1",
        question="Pergunta?",
        alternatives=[
            GrillAlternative(alternative_id="a", label="A", consequence="CA"),
            GrillAlternative(alternative_id="b", label="B", consequence="CB"),
        ],
        decision_source="session-prior",
        reused_decision_ref="decision-prior-1",
        is_material=True,
    )
    grill = GrillRecord(
        demand_id="HF-04-01",
        intent_summary="Teste",
        decisions=[decision],
        pending_questions=[],
        example_criteria=["Critério"],
        ready_for_spec=True,
        readiness_justification="Reuso aprovado",
    )
    handoff = make_test_handoff().model_copy(update={"grill": grill})
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is True


def test_grill_assumptions_and_optional_preferences_do_not_create_waiting_human() -> None:
    gate = ReadinessGate()
    ctx = make_context()
    decision = GrillDecision(
        decision_id="dec-optional-1",
        question="Preferência opcional?",
        alternatives=[
            GrillAlternative(alternative_id="opt-a", label="A", consequence="CA"),
            GrillAlternative(alternative_id="opt-b", label="B", consequence="CB"),
        ],
        decision_source="owner",
        selected_alternative_id=None,
        response=None,
        is_material=False,
    )
    grill = GrillRecord(
        demand_id="HF-04-01",
        intent_summary="Teste",
        decisions=[decision],
        assumptions=["Preferência opcional por modo headless assumida"],
        pending_questions=[],
        example_criteria=["Critério"],
        ready_for_spec=True,
        readiness_justification="Assunções explícitas",
    )
    handoff = make_test_handoff().model_copy(update={"grill": grill})
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is True
    assert report.readiness is not ReadinessState.WAITING_HUMAN


def test_manual_dependency_resolved_at_alone_does_not_unblock() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        receipt_ref="receipt-dep-1",
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt()})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.readiness is ReadinessState.WAITING_HUMAN
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("resolution receipt 'receipt-dep-1' not found" in r for r in report.reasons)


def test_manual_dependency_failed_resolution_receipt_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        receipt_ref="receipt-dep-failed",
    )
    failed_receipt = make_receipt(
        receipt_id="receipt-dep-failed",
        requirement="python -m core.workflow.probe --check dep-1",
        result=EvidenceResult.FAILED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-failed": failed_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("resolution receipt is not passed" in r for r in report.reasons)


def test_manual_dependency_probe_mismatch_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        final_probe="probe-declared",
        receipt_ref="receipt-dep-1",
    )
    mismatched_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="probe-different",
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-1": mismatched_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("probe mismatch" in r for r in report.reasons)


def test_manual_dependency_subject_mismatch_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        ticket_ids=["HF-04-01"],
        receipt_ref="receipt-dep-1",
    )
    mismatched_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="python -m core.workflow.probe --check dep-1",
        subject="WRONG-TICKET-888",
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-1": mismatched_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("subject mismatch" in r for r in report.reasons)


def test_manual_dependency_environment_mismatch_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        receipt_ref="receipt-dep-1",
    )
    mismatched_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="python -m core.workflow.probe --check dep-1",
        environment_ref="other-env",
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-1": mismatched_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("environment mismatch" in r for r in report.reasons)


def test_manual_dependency_config_version_mismatch_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        receipt_ref="receipt-dep-1",
    )
    mismatched_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="python -m core.workflow.probe --check dep-1",
        config_version="old-config-v0",
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-1": mismatched_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("config_version mismatch" in r for r in report.reasons)


def test_manual_dependency_receipt_older_than_dependency_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        created_at=NOW,
        resolved_at=NOW,
        receipt_ref="receipt-dep-1",
    )
    older_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="python -m core.workflow.probe --check dep-1",
        observed_at=NOW - timedelta(minutes=30),
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-dep-1": older_receipt})
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("older than dependency creation" in r for r in report.reasons)


def test_manual_dependency_expired_receipt_blocks() -> None:
    gate = ReadinessGate()
    dep = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        created_at=NOW - timedelta(days=2),
        resolved_at=NOW - timedelta(days=2),
        receipt_ref="receipt-dep-1",
    )
    expired_receipt = make_receipt(
        receipt_id="receipt-dep-1",
        requirement="python -m core.workflow.probe --check dep-1",
        observed_at=NOW - timedelta(days=2),
        result=EvidenceResult.PASSED,
    )
    policy = GatePolicy(
        policy_version="1",
        stage_requirements={WorkflowState.INDEPENDENT_REVIEW: ("req-test-suite",)},
        default_max_age_seconds=3600,
    )
    ctx = make_context(
        receipts={"req-test-suite": make_receipt(), "receipt-dep-1": expired_receipt},
        policy=policy,
    )
    handoff = make_test_handoff(manual_dependencies=[dep])
    report = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report.eligible is False
    assert report.blocking_dependency_ids == ["dep-1"]
    assert any("resolution receipt expired" in r for r in report.reasons)


def test_manual_dependency_unblocks_only_dependent_tickets() -> None:
    gate = ReadinessGate()
    dep_hf04 = make_test_dependency(
        ManualDependencyStatus.RESOLVED,
        dependency_id="dep-hf04",
        ticket_ids=["HF-04-01"],
        final_probe="probe-hf04",
        receipt_ref="receipt-hf04",
    )
    dep_other = make_test_dependency(
        ManualDependencyStatus.WAITING,
        dependency_id="dep-other",
        ticket_ids=["OTHER-TICKET-99"],
        final_probe="probe-other",
    )
    valid_receipt = make_receipt(
        receipt_id="receipt-hf04",
        requirement="probe-hf04",
        subject="HF-04-01",
        result=EvidenceResult.PASSED,
    )
    ctx = make_context(receipts={"req-test-suite": make_receipt(), "receipt-hf04": valid_receipt})

    # 1. Handoff for HF-04-01 has both dependencies declared, but dep_other only blocks OTHER-TICKET-99
    handoff_hf04 = make_test_handoff(
        ticket_id="HF-04-01",
        manual_dependencies=[dep_hf04, dep_other],
    )
    report_hf04 = gate.evaluate(handoff_hf04, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report_hf04.eligible is True
    assert report_hf04.blocking_dependency_ids == []

    # 2. Handoff for OTHER-TICKET-99 IS blocked by dep_other
    handoff_other = make_test_handoff(
        ticket_id="OTHER-TICKET-99",
        manual_dependencies=[dep_hf04, dep_other],
    )
    report_other = gate.evaluate(handoff_other, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert report_other.eligible is False
    assert report_other.blocking_dependency_ids == ["dep-other"]


def test_manual_dependency_stage_scoping_and_replay_determinism() -> None:
    gate = ReadinessGate()
    ctx = make_context()
    # Dependency only blocks DELIVERED stage
    dep_delivery_only = make_test_dependency(
        ManualDependencyStatus.WAITING,
        dependency_id="dep-delivery-gate",
        ticket_ids=["HF-04-01"],
        blocked_stages=[WorkflowState.DELIVERED],
    )
    handoff = make_test_handoff(
        state=WorkflowState.INDEPENDENT_REVIEW,
        manual_dependencies=[dep_delivery_only],
    )

    # 1. Evaluation at INDEPENDENT_REVIEW stage is NOT blocked by delivery-only dependency
    rep_review = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert rep_review.eligible is True
    assert "dep-delivery-gate" not in rep_review.blocking_dependency_ids

    # 2. Replay yields identical result
    rep_review_replay = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.INDEPENDENT_REVIEW)
    assert rep_review.eligible == rep_review_replay.eligible
    assert rep_review.readiness == rep_review_replay.readiness
    assert rep_review.blocking_dependency_ids == rep_review_replay.blocking_dependency_ids
    assert rep_review.reasons == rep_review_replay.reasons

    # 3. Evaluation at DELIVERED stage IS blocked
    rep_delivery = gate.evaluate(handoff, context=ctx, target_state=WorkflowState.DELIVERED)
    assert rep_delivery.eligible is False
    assert "dep-delivery-gate" in rep_delivery.blocking_dependency_ids



