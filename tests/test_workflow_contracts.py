"""Deterministic HF-04 tests for strict workflow contracts and readiness gates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.workflow.contracts import (
    AlternativeAttempt,
    EnvironmentEndpoint,
    EnvironmentEvidence,
    EnvironmentKind,
    EnvironmentManifest,
    EvidenceFreshness,
    EvidenceRequirement,
    EvidenceResult,
    GrillAlternative,
    GrillDecision,
    GrillFact,
    GrillPendingQuestion,
    GrillRecord,
    HandoffOrigin,
    ManualDependency,
    ManualDependencyStatus,
    ManualStep,
    PlannerTier,
    ReadinessState,
    SanitizedIdentity,
    SecretReference,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import ReadinessError, ReadinessGate, mark_delivered, validate_transition
from core.workflow.verification import (
    EvidenceReceipt,
    GatePolicy,
    PlanApproval,
    ValidationMode,
    VerificationContext,
)


NOW = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


def make_grill(*, ready: bool = True, pending: list[GrillPendingQuestion] | None = None) -> GrillRecord:
    return GrillRecord(
        demand_id="HF-04-01",
        demand_version=1,
        intent_summary="Congelar contratos e impedir que uma entrega sem prova avance.",
        known_facts=[
            GrillFact(
                fact_id="fact-1",
                statement="O workflow precisa ser exercitável sem interface gráfica.",
                source="docs",
                locator="docs/HYBRID_AUTONOMY_REQUIREMENTS.md#3",
            )
        ],
        decisions=[
            GrillDecision(
                decision_id="decision-1",
                question="Qual é o driver primário?",
                alternatives=[
                    GrillAlternative(
                        alternative_id="library",
                        label="Biblioteca Python",
                        consequence="Permite testes rápidos e determinísticos.",
                    ),
                    GrillAlternative(
                        alternative_id="http",
                        label="HTTP local",
                        consequence="Adiciona uma fronteira de transporte.",
                    ),
                ],
                selected_alternative_id="library" if ready else None,
                response="Executar a lógica como biblioteca.",
                decision_source="owner-demand",
            )
        ],
        assumptions=["A evidência real será fornecida pelo ambiente de destino."],
        pending_questions=pending or [],
        example_criteria=["Entrega sem evidência atual deve falhar."],
        ready_for_spec=ready,
        readiness_justification="A intenção e os limites do contrato estão explícitos.",
        created_at=NOW,
        updated_at=NOW,
    )


def make_environment(kind: EnvironmentKind = EnvironmentKind.TARGET_ENVIRONMENT) -> EnvironmentManifest:
    return EnvironmentManifest(
        environment_ref="hf04-target-local",
        ticket_id="HF-04-01",
        kind=kind,
        tools=[{"name": "python", "version": "3.12", "source": "runtime"}],
        system="Windows 11 with WSL2",
        architecture="x86_64",
        services=["local workflow runner"],
        accounts=["local-operator"],
        secret_refs=[
            SecretReference(
                ref_id="secret-ref-1",
                provider="environment",
                locator="HF04_TOKEN_REF",
                variable_name="HF04_TOKEN",
            )
        ],
        permission_scopes=["filesystem:workspace"],
        required_env_vars=["HF04_TOKEN"],
        endpoints=[
            EnvironmentEndpoint(
                endpoint_id="endpoint-1",
                url="https://example.invalid/health",
                protocol="https",
            )
        ],
        network_policy="Only declared endpoints; no implicit egress.",
        connection_origins=["local-test-process"],
        ports=[{"port": 8443, "direction": "outbound", "purpose": "health probe"}],
        worker_identity=SanitizedIdentity(subject="worker-local", role="test-runner", host="local"),
        installation=["Use the repository environment."],
        probes=["python -m pytest tests/test_workflow_contracts.py -v"],
        rollback=["Discard the candidate worktree."],
        cleanup=["Remove temporary evidence fixtures."],
        target_differences=["None for this local target."],
        observed_at=NOW,
    )


def make_evidence(
    *,
    evidence_id: str = "evidence-1",
    environment_ref: str = "hf04-target-local",
    result: EvidenceResult = EvidenceResult.PASSED,
    freshness: EvidenceFreshness = EvidenceFreshness.CURRENT,
) -> EnvironmentEvidence:
    return EnvironmentEvidence(
        evidence_id=evidence_id,
        requirement="Focal suite passes in the declared environment.",
        environment_ref=environment_ref,
        identity=SanitizedIdentity(subject="worker-local", role="test-runner", host="local"),
        origin="local-test-process",
        build_digest="sha256:" + "a" * 64,
        config_version="config-v1",
        test_name="hf04_focal_suite",
        expected="All focal assertions pass.",
        observed="All focal assertions pass.",
        result=result,
        freshness=freshness,
        observed_at=NOW,
        evidence_ref="artifact://hf04/evidence-1",
    )


def make_dependency(
    status: ManualDependencyStatus = ManualDependencyStatus.WAITING,
    *,
    receipt_ref: str | None = None,
    blocked_stages: list[WorkflowState] | None = None,
) -> ManualDependency:
    return ManualDependency(
        dependency_id="access-1",
        ticket_ids=["HF-04-01"],
        status=status,
        reason="Only the owner can authorize the final target credential.",
        alternatives_attempted=[
            AlternativeAttempt(
                alternative_id="local-equivalent",
                description="Local equivalent without target credential.",
                tested=True,
                equivalent=False,
                reason_unusable="It cannot prove target identity.",
            )
        ],
        configuration_location="Target secret manager entry HF04_TOKEN_REF",
        prerequisites=["Owner has access to the target account."],
        steps=[
            ManualStep(
                number=1,
                instruction="Add the secret reference in the approved secure field.",
                expected_result="The reference is visible without its value.",
            ),
            ManualStep(
                number=2,
                instruction="Run the final probe as the worker identity.",
                expected_result="The probe returns a sanitized success receipt.",
            ),
        ],
        secret_ref=SecretReference(
            ref_id="secret-ref-2", provider="environment", locator="HF04_TOKEN_REF"
        ),
        final_probe="python -m core.workflow.probe --check hf04-target-local",
        resume_criteria="Resume only after the probe returns passed for this environment_ref.",
        help_route="Owner runbook section HF-04 access",
        created_at=NOW,
        resolved_at=NOW if status is ManualDependencyStatus.RESOLVED else None,
        resolution_receipt_ref=(
            receipt_ref
            if receipt_ref is not None
            else ("receipt-access-1" if status is ManualDependencyStatus.RESOLVED else None)
        ),
        blocked_stages=blocked_stages or [],
    )


def make_handoff(
    *,
    environment: EnvironmentManifest | None = None,
    evidence: list[EnvironmentEvidence] | None = None,
    dependencies: list[ManualDependency] | None = None,
    state: WorkflowState = WorkflowState.INDEPENDENT_REVIEW,
) -> WorkflowHandoff:
    return WorkflowHandoff(
        ticket_id="HF-04-01",
        parent_id="HF-04",
        objective="Congelar os contratos e bloquear entregas sem evidência.",
        origin=HandoffOrigin.USER_DEMAND,
        plan_version="1.0",
        planner_id="hf04-planning-pass",
        planner_tier="high",
        approval_reference="session-hf04-local",
        baseline_sha="b" * 40,
        grill=make_grill(),
        environment=environment if environment is not None else make_environment(),
        environment_evidence=evidence if evidence is not None else [make_evidence()],
        manual_dependencies=dependencies or [],
        required_evidence=[
            EvidenceRequirement(
                evidence_id="evidence-1",
                description="Focal validation in the declared target.",
                required_for=ReadinessState.OPERATIONALLY_VERIFIED,
            )
        ],
        state=state,
        allowed_paths=["core/workflow", "tests/test_workflow_contracts.py"],
        read_only_paths=["docs/local-demos"],
        non_goals=["No runtime or external service installation."],
        acceptance_criteria=["Delivery gate rejects missing evidence."],
        validate_commands=["python -m pytest tests/test_workflow_contracts.py -v"],
        trigger_events=["handoff.ready"],
        successor_event="workflow.hf04.validated",
        conflict_keys=["workflow-contracts:HF-04"],
        resource_requirements=["local Python 3.12"],
        retry_policy="Retry only after a classified validation or environment failure.",
        resume_strategy="Re-read the handoff and reconcile evidence by ID.",
        rollback_plan="Remove only the candidate files from the isolated worktree.",
    )


def make_simulation_context(
    *,
    plan_digest: str = "plan-sha-1",
    candidate_digest: str = "sha256:" + "a" * 64,
    config_version: str = "config-v1",
    expected_environment_ref: str = "hf04-target-local",
    expected_identity: SanitizedIdentity | None = None,
    expected_route: str = "local-test-process",
    now: datetime = NOW,
    approvals: dict[str, PlanApproval] | None = None,
    receipts: dict[str, EvidenceReceipt] | None = None,
    policy: GatePolicy | None = None,
) -> VerificationContext:
    identity = expected_identity or SanitizedIdentity(
        subject="worker-local", role="test-runner", host="local"
    )
    if approvals is None:
        approvals = {
            "session-hf04-local": PlanApproval(
                approval_ref="session-hf04-local",
                planner_tier=PlannerTier.HIGH,
                plan_digest=plan_digest,
                approved_by=SanitizedIdentity(subject="supervisor-lead", role="supervisor", host="local"),
                approved_at=now,
                enabled_capabilities=("planning", "specification"),
            )
        }
    if receipts is None:
        receipts = {
            "evidence-1": EvidenceReceipt(
                receipt_id="receipt-evidence-1",
                producer=SanitizedIdentity(subject="test-runner", role="test-runner", host="local"),
                subject="HF-04-01",
                requirement="evidence-1",
                artifact_hash="artifact-sha-1",
                result=EvidenceResult.PASSED,
                mode=ValidationMode.TARGET_ENVIRONMENT,
                observed_at=now,
                plan_digest=plan_digest,
                candidate_digest=candidate_digest,
                config_version=config_version,
                environment_ref=expected_environment_ref,
                route=expected_route,
            ),
            "review": EvidenceReceipt(
                receipt_id="receipt-review-1",
                producer=SanitizedIdentity(subject="reviewer-alice", role="reviewer", host="local"),
                subject="HF-04-01",
                requirement="review",
                artifact_hash="review-sha-1",
                result=EvidenceResult.PASSED,
                mode=ValidationMode.TARGET_ENVIRONMENT,
                observed_at=now,
                plan_digest=plan_digest,
                candidate_digest=candidate_digest,
                config_version=config_version,
                environment_ref=expected_environment_ref,
                route=expected_route,
            ),
            "receipt-access-1": EvidenceReceipt(
                receipt_id="receipt-access-1",
                producer=SanitizedIdentity(subject="owner", role="supervisor", host="local"),
                subject="HF-04-01",
                requirement="python -m core.workflow.probe --check hf04-target-local",
                artifact_hash="probe-sha-1",
                result=EvidenceResult.PASSED,
                mode=ValidationMode.TARGET_ENVIRONMENT,
                observed_at=now,
                plan_digest=plan_digest,
                candidate_digest=candidate_digest,
                config_version=config_version,
                environment_ref=expected_environment_ref,
                route=expected_route,
            ),
        }
    if policy is None:
        policy = GatePolicy(
            policy_version="1",
            stage_requirements={
                WorkflowState.READY_FOR_HANDOFF: (),
                WorkflowState.INDEPENDENT_REVIEW: ("evidence-1",),
                WorkflowState.DELIVERED: ("evidence-1",),
            },
            enabled_roles=frozenset({"supervisor", "reviewer", "test-runner"}),
        )

    return VerificationContext(
        now=now,
        policy_version="1",
        plan_digest=plan_digest,
        candidate_digest=candidate_digest,
        config_version=config_version,
        expected_environment_ref=expected_environment_ref,
        expected_identity=identity,
        expected_route=expected_route,
        approvals=approvals,
        receipts=receipts,
        policy=policy,
    )


def test_contracts_round_trip_json_and_reject_unknown_fields() -> None:
    record = make_grill()
    restored = GrillRecord.model_validate_json(record.model_dump_json())
    assert restored == record

    with pytest.raises(ValidationError):
        GrillRecord.model_validate({**record.model_dump(), "unexpected": True})


def test_grill_ready_state_rejects_pending_questions() -> None:
    pending = [
        GrillPendingQuestion(
            question_id="question-1",
            question="Qual identidade autoriza o alvo?",
            impact="Muda a evidência necessária.",
        )
    ]
    with pytest.raises(ValidationError, match="pending questions"):
        make_grill(pending=pending)


def test_grill_decisions_and_identifiers_are_closed() -> None:
    with pytest.raises(ValidationError, match="selected alternative"):
        GrillDecision(
            decision_id="decision-1",
            question="Escolha uma opção.",
            alternatives=[
                {"alternative_id": "a", "label": "A", "consequence": "C"},
                {"alternative_id": "b", "label": "B", "consequence": "C"},
            ],
            selected_alternative_id="unknown",
            decision_source="owner",
        )


def test_manifest_rejects_secret_values_and_mock_without_differences() -> None:
    with pytest.raises(ValidationError, match="embedded credentials"):
        EnvironmentEndpoint(
            endpoint_id="bad-endpoint",
            url="https://user:password@example.invalid/health",
        )

    with pytest.raises(ValidationError, match="secret values"):
        SecretReference(ref_id="bad-ref", provider="env", locator="token=plain-text")

    with pytest.raises(ValidationError, match="target differences"):
        mock_payload = make_environment(EnvironmentKind.MOCK_ONLY).model_dump()
        mock_payload["target_differences"] = []
        EnvironmentManifest.model_validate(mock_payload)


def test_manual_dependency_requires_safe_steps_and_resolution_timestamp() -> None:
    with pytest.raises(ValidationError, match="consecutively"):
        ManualDependency(
            **make_dependency().model_dump(exclude={"steps"}),
            steps=[
                {"number": 1, "instruction": "One", "expected_result": "Ok"},
                {"number": 3, "instruction": "Three", "expected_result": "Ok"},
            ],
        )

    with pytest.raises(ValidationError, match="resolved_at"):
        ManualDependency(**make_dependency().model_dump(exclude={"status", "resolved_at"}), status="resolved")

    with pytest.raises(ValidationError, match="resolution_receipt_ref"):
        ManualDependency(
            **make_dependency(ManualDependencyStatus.RESOLVED).model_dump(exclude={"resolution_receipt_ref"}),
            resolution_receipt_ref=None,
        )

    with pytest.raises(ValidationError, match="only resolved manual dependencies"):
        ManualDependency(
            **make_dependency().model_dump(exclude={"status", "resolution_receipt_ref"}),
            status="waiting",
            resolution_receipt_ref="receipt-unresolved",
        )


def test_grill_material_decision_unanswered_rejects_ready_for_spec() -> None:
    decision = GrillDecision(
        decision_id="dec-unanswered",
        question="Qual o framework?",
        alternatives=[
            GrillAlternative(alternative_id="a", label="A", consequence="CA"),
            GrillAlternative(alternative_id="b", label="B", consequence="CB"),
        ],
        decision_source="owner",
        selected_alternative_id=None,
        response=None,
        is_material=True,
    )
    with pytest.raises(ValidationError, match="unanswered material decisions"):
        GrillRecord(
            demand_id="HF-04-01",
            intent_summary="Teste",
            decisions=[decision],
            pending_questions=[],
            example_criteria=["Critério 1"],
            ready_for_spec=True,
            readiness_justification="Scope known",
        )


def test_grill_reused_decision_with_origin_passes() -> None:
    decision = GrillDecision(
        decision_id="dec-reused",
        question="Qual o framework?",
        alternatives=[
            GrillAlternative(alternative_id="a", label="A", consequence="CA"),
            GrillAlternative(alternative_id="b", label="B", consequence="CB"),
        ],
        decision_source="prior-session-session-123",
        reused_decision_ref="decision-prev-1",
        is_material=True,
    )
    record = GrillRecord(
        demand_id="HF-04-01",
        intent_summary="Teste reuso",
        decisions=[decision],
        pending_questions=[],
        example_criteria=["Critério 1"],
        ready_for_spec=True,
        readiness_justification="Scope known via reuse",
    )
    assert record.ready_for_spec is True
    assert record.decisions[0].reused_decision_ref == "decision-prev-1"


def test_grill_optional_assumption_does_not_block() -> None:
    optional_decision = GrillDecision(
        decision_id="dec-optional",
        question="Preferência cosmética?",
        alternatives=[
            GrillAlternative(alternative_id="opt1", label="Opt 1", consequence="C1"),
            GrillAlternative(alternative_id="opt2", label="Opt 2", consequence="C2"),
        ],
        decision_source="owner",
        selected_alternative_id=None,
        response=None,
        is_material=False,
    )
    record = GrillRecord(
        demand_id="HF-04-01",
        intent_summary="Teste opcional",
        decisions=[optional_decision],
        assumptions=["Preferência opcional assumida como tema escuro"],
        pending_questions=[],
        example_criteria=["Critério 1"],
        ready_for_spec=True,
        readiness_justification="Scope known with assumed optional preference",
    )
    assert record.ready_for_spec is True


def test_handoff_cross_references_and_duplicate_ids_are_rejected() -> None:
    handoff = make_handoff()
    with pytest.raises(ValidationError, match="environment ticket_id"):
        make_handoff(environment=make_environment().model_copy(update={"ticket_id": "HF-04-02"}))

    with pytest.raises(ValidationError, match="duplicate evidence IDs"):
        make_handoff(evidence=[make_evidence(), make_evidence()])

    assert handoff.ticket_id == handoff.grill.demand_id


def test_readiness_blocks_unresolved_manual_dependency() -> None:
    ctx = make_simulation_context()
    report = ReadinessGate().evaluate(make_handoff(dependencies=[make_dependency()]), context=ctx)
    assert report.eligible is False
    assert report.readiness is ReadinessState.WAITING_HUMAN
    assert report.blocking_dependency_ids == ["access-1"]


def test_resolved_dependency_does_not_block_readiness() -> None:
    ctx = make_simulation_context()
    report = ReadinessGate().evaluate(
        make_handoff(dependencies=[make_dependency(ManualDependencyStatus.RESOLVED)]),
        context=ctx,
    )
    assert report.eligible is True
    assert report.readiness is ReadinessState.READY_FOR_RELEASE


def test_stale_failed_mismatched_and_missing_evidence_fail_closed() -> None:
    gate = ReadinessGate()
    for evidence in [
        make_evidence(result=EvidenceResult.FAILED),
        make_evidence(freshness=EvidenceFreshness.STALE),
        make_evidence(environment_ref="other-environment"),
    ]:
        report = gate.evaluate(make_handoff(evidence=[evidence]))
        assert report.eligible is False

    report = gate.evaluate(make_handoff(evidence=[]))
    assert report.missing_evidence == ["evidence-1"]


def test_mock_evidence_can_validate_simulation_but_not_release() -> None:
    mock_environment = make_environment(EnvironmentKind.MOCK_ONLY)
    ctx = make_simulation_context()
    report = ReadinessGate().evaluate(make_handoff(environment=mock_environment), context=ctx)
    assert report.readiness is ReadinessState.VALIDATED_IN_SIMULATION

    release = ReadinessGate().evaluate(
        make_handoff(environment=mock_environment), context=ctx, target_state=WorkflowState.DELIVERED
    )
    assert release.eligible is False
    assert any("mock_only" in reason for reason in release.reasons)


def test_delivery_requires_review_target_and_current_evidence() -> None:
    gate = ReadinessGate()
    ctx = make_simulation_context()
    report = gate.require_delivery(make_handoff(), context=ctx)
    assert report.readiness is ReadinessState.OPERATIONALLY_VERIFIED

    delivered = mark_delivered(make_handoff(), gate, context=ctx)
    assert delivered.state is WorkflowState.DELIVERED
    assert make_handoff().state is WorkflowState.INDEPENDENT_REVIEW

    with pytest.raises(ReadinessError, match="independent_review"):
        gate.require_delivery(make_handoff(state=WorkflowState.VALIDATING), context=ctx)


def test_delivery_rejects_required_evidence_not_declared() -> None:
    with pytest.raises(ValidationError, match="required evidence"):
        make_handoff(evidence=[], state=WorkflowState.DELIVERED)


def test_workflow_transitions_are_closed_and_non_mutating() -> None:
    validate_transition(WorkflowState.READY_FOR_HANDOFF, WorkflowState.IMPLEMENTING_ECONOMY)
    validate_transition(WorkflowState.VALIDATING, WorkflowState.INDEPENDENT_REVIEW)

    with pytest.raises(ReadinessError, match="illegal workflow transition"):
        validate_transition(WorkflowState.READY_FOR_HANDOFF, WorkflowState.DELIVERED)
