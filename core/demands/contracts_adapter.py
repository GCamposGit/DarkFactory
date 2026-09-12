"""Adapters bridging User Demands to HF-04 strict workflow contracts.

Converts UserTicket and Grill session data into auditable, Pydantic v2
contracts (GrillRecord, EnvironmentManifest, ManualDependency, WorkflowHandoff)
required by ReadinessGate and the hybrid autonomous loop.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from core.demands.models import DemandInput, UserTicket
from core.workflow.contracts import (
    AlternativeAttempt,
    EnvironmentEndpoint,
    EnvironmentKind,
    EnvironmentManifest,
    EnvironmentTool,
    EvidenceRequirement,
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
from core.workflow.verification import (
    EvidenceReceipt,
    GatePolicy,
    PlanApproval,
    ValidationMode,
    VerificationContext,
)


def _digest_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_grill_record(
    ticket: UserTicket,
    *,
    doc_insights: Sequence[str] | None = None,
    decisions: Sequence[GrillDecision] | None = None,
    pending_questions: Sequence[GrillPendingQuestion] | None = None,
    assumptions: Sequence[str] | None = None,
    ready_for_spec: bool | None = None,
    readiness_justification: str | None = None,
    demand_version: int = 1,
) -> GrillRecord:
    """Construct an auditable, strict GrillRecord for a UserTicket."""
    facts: list[GrillFact] = []

    # Inject baseline facts from project doc insights
    if doc_insights:
        for idx, insight in enumerate(doc_insights, start=1):
            facts.append(
                GrillFact(
                    fact_id=f"fact_doc_{idx}",
                    statement=insight[:240],
                    source="project_documentation",
                    locator=f"doc_insight_{idx}",
                )
            )

    # Fact for problem statement
    if ticket.problem_statement:
        facts.append(
            GrillFact(
                fact_id="fact_problem_statement",
                statement=ticket.problem_statement[:240],
                source="user_demand",
                locator=f"{ticket.id}/problem",
            )
        )

    # Invariants for criteria
    example_criteria = [c[:240] for c in ticket.acceptance_criteria if c.strip()]
    if not example_criteria:
        example_criteria = [f"Validação headless determinística de {ticket.title[:80]}"]

    # Assumptions
    effective_assumptions = list(assumptions or [])
    if not effective_assumptions:
        effective_assumptions = [
            "Execução local com custo zero ($0.00) por padrão",
            "Separação estrita entre lógica de negócios e apresentação",
        ]

    # Decisions and pending questions
    effective_decisions = list(decisions or [])
    effective_pending = list(pending_questions or [])

    # If readiness is not explicitly passed, evaluate deterministically
    is_ready = ready_for_spec
    if is_ready is None:
        unanswered_materials = [
            d for d in effective_decisions if d.is_material and not d.is_answered()
        ]
        is_ready = (
            len(effective_pending) == 0
            and len(unanswered_materials) == 0
            and bool(ticket.problem_statement.strip())
            and len(ticket.non_goals) > 0
            and len(example_criteria) > 0
        )

    # Guardrails: if ready_for_spec is True, pending questions MUST be empty
    if is_ready:
        effective_pending = []
        justification = (
            readiness_justification
            or "Demanda clara e contextualizada; critérios, escopo e non-goals validados sem pendências."
        )
    else:
        justification = (
            readiness_justification
            or f"Grill em andamento; {len(effective_pending)} questão(ões) pendente(s) de esclarecimento."
        )

    return GrillRecord(
        demand_id=ticket.id,
        demand_version=demand_version,
        intent_summary=f"{ticket.title}: {ticket.problem_statement or 'Implementação orientada a testes'}"[:240],
        known_facts=facts,
        decisions=effective_decisions,
        assumptions=effective_assumptions,
        pending_questions=effective_pending,
        example_criteria=example_criteria,
        ready_for_spec=is_ready,
        readiness_justification=justification[:240],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def build_environment_manifest(
    ticket_id: str | UserTicket,
    *,
    environment_ref: str | None = None,
    kind: EnvironmentKind = EnvironmentKind.TARGET_ENVIRONMENT,
    system: str = "windows",
    architecture: str = "x86_64",
    network_policy: str = "restricted_local",
    worker_identity: SanitizedIdentity | None = None,
    extra_tools: Sequence[EnvironmentTool] | None = None,
    secret_refs: Sequence[SecretReference] | None = None,
    endpoints: Sequence[EnvironmentEndpoint] | None = None,
    required_env_vars: Sequence[str] | None = None,
) -> EnvironmentManifest:
    """Build a sanitized EnvironmentManifest adhering strictly to HF-04 schema."""
    tid = ticket_id.id if hasattr(ticket_id, "id") else str(ticket_id)
    ref = environment_ref or f"env_{tid.lower().replace('-', '_')}"

    tools: list[EnvironmentTool] = [
        EnvironmentTool(name="python", version="3.12+", source="runtime"),
        EnvironmentTool(name="git", version="2.x", source="system"),
        EnvironmentTool(name="pytest", version="8.x", source="python_package"),
    ]
    if extra_tools:
        tools.extend(extra_tools)

    worker = worker_identity or SanitizedIdentity(
        subject="worker_local",
        role="developer",
        host="localhost",
    )

    clean_vars: list[str] = []
    if required_env_vars:
        for var in required_env_vars:
            clean = re.sub(r"[^A-Za-z0-9_]", "", var).strip()
            if clean and clean not in clean_vars:
                clean_vars.append(clean)

    return EnvironmentManifest(
        environment_ref=ref,
        ticket_id=tid,
        kind=kind,
        tools=tools,
        system=system,
        architecture=architecture,
        services=["sqlite3", "ollama_local"],
        network_policy=network_policy,
        worker_identity=worker,
        secret_refs=list(secret_refs or []),
        endpoints=list(endpoints or []),
        required_env_vars=clean_vars,
        probes=["python core/harness/runner.py --quick"],
    )


def build_manual_dependency(
    dependency_id: str,
    *,
    ticket_ids: Sequence[str],
    reason: str,
    configuration_location: str,
    steps: Sequence[tuple[str, str]],
    final_probe: str,
    resume_criteria: str,
    help_route: str = "docs/TROUBLESHOOTING.md",
    alternatives_attempted: Sequence[AlternativeAttempt],
    blocked_stages: Sequence[WorkflowState] | None = None,
    status: ManualDependencyStatus = ManualDependencyStatus.WAITING,
    resolved_at: datetime | None = None,
    resolution_receipt_ref: str | None = None,
) -> ManualDependency:
    """Build a strict, non-bypassable ManualDependency with safe steps and probe."""
    manual_steps: list[ManualStep] = []
    for idx, (instruction, outcome) in enumerate(steps, start=1):
        manual_steps.append(
            ManualStep(
                number=idx,
                instruction=instruction[:240],
                expected_result=outcome[:240],
            )
        )

    effective_blocked = list(blocked_stages or [WorkflowState.READY_FOR_HANDOFF, WorkflowState.DELIVERED])

    return ManualDependency(
        dependency_id=dependency_id,
        ticket_ids=list(ticket_ids),
        status=status,
        reason=reason[:240],
        alternatives_attempted=list(alternatives_attempted),
        configuration_location=configuration_location[:240],
        prerequisites=["Acesso autenticado do operador"],
        steps=manual_steps,
        final_probe=final_probe[:240],
        resume_criteria=resume_criteria[:240],
        help_route=help_route[:240],
        blocked_stages=effective_blocked,
        resolved_at=resolved_at,
        resolution_receipt_ref=resolution_receipt_ref,
    )


def build_workflow_handoff(
    ticket: UserTicket,
    grill: GrillRecord,
    environment: EnvironmentManifest,
    *,
    manual_dependencies: Sequence[ManualDependency] | None = None,
    required_evidence: Sequence[EvidenceRequirement] | None = None,
    allowed_paths: Sequence[str] | None = None,
    read_only_paths: Sequence[str] | None = None,
    validate_commands: Sequence[str] | None = None,
    state: WorkflowState = WorkflowState.READY_FOR_HANDOFF,
    planner_tier: PlannerTier = PlannerTier.HIGH,
    approval_reference: str = "appr_hf08_supervisor",
    baseline_sha: str = "0000000000000000000000000000000000000000",
) -> WorkflowHandoff:
    """Assemble a complete, auditable WorkflowHandoff ready for gate evaluation."""
    if state == WorkflowState.READY_FOR_HANDOFF and not grill.ready_for_spec:
        raise ValueError("Cannot create READY_FOR_HANDOFF handoff when GrillRecord is not ready_for_spec")

    evidence: list[EvidenceRequirement] = list(required_evidence or [])
    if not evidence:
        evidence = [
            EvidenceRequirement(
                evidence_id="req_unit_tests",
                description="Suíte de testes unitários determinísticos verdes",
                required_for=ReadinessState.VALIDATED_IN_SIMULATION,
            ),
            EvidenceRequirement(
                evidence_id="req_harness_pass",
                description="Portão determinístico do harness DarkFac global aprovado",
                required_for=ReadinessState.OPERATIONALLY_VERIFIED,
            ),
        ]

    effective_allowed = list(allowed_paths or ticket.suggested_files)
    if not effective_allowed:
        effective_allowed = ["core/", "tests/"]

    effective_readonly = list(read_only_paths or ["MISSION.md", "FACTORY_RULES.md", "AGENTS.md"])

    effective_validate = list(validate_commands or [
        f"python -m pytest tests -v -k {ticket.id}",
        "python core/harness/runner.py --quick",
    ])

    return WorkflowHandoff(
        ticket_id=ticket.id,
        parent_id=ticket.id,
        objective=ticket.problem_statement or ticket.title,
        origin=HandoffOrigin.USER_DEMAND,
        plan_version="1.0.0",
        planner_id="planner_gemini_flash",
        planner_tier=planner_tier,
        approval_reference=approval_reference,
        baseline_sha=baseline_sha,
        grill=grill,
        environment=environment,
        manual_dependencies=list(manual_dependencies or []),
        required_evidence=evidence,
        state=state,
        allowed_paths=effective_allowed,
        read_only_paths=effective_readonly,
        non_goals=list(ticket.non_goals) if ticket.non_goals else ["Sem non-goals extras"],
        acceptance_criteria=list(ticket.acceptance_criteria) if ticket.acceptance_criteria else ["Critério padrão de teste"],
        validate_commands=effective_validate,
        trigger_events=["demand.intake_completed"],
        successor_event="handoff.approved",
        conflict_keys=[f"ticket:{ticket.id}"],
        resource_requirements=["cpu:1", "ram:1gb"],
        retry_policy="max_attempts=3;backoff=exponential",
        resume_strategy="resume_from_last_checkpoint",
        rollback_plan="revert_working_tree",
    )


def create_testing_verification_context(
    handoff: WorkflowHandoff,
    *,
    approver_role: str = "supervisor",
    plan_digest: str | None = None,
    candidate_digest: str | None = None,
    additional_receipts: Sequence[EvidenceReceipt] | None = None,
) -> VerificationContext:
    """Convenience helper to create a valid VerificationContext with PlanApproval for gate evaluation."""
    now = datetime.now(UTC)
    digest = plan_digest or f"sha256:{_digest_text(handoff.model_dump_json())}"

    approval = PlanApproval(
        approval_ref=handoff.approval_reference,
        planner_tier=handoff.planner_tier,
        plan_digest=digest,
        approved_by=SanitizedIdentity(
            subject="supervisor_antigravity",
            role=approver_role,
            host="local_worktree",
        ),
        approved_at=now,
        enabled_capabilities=("run_tests", "emit_evidence", "inspect_filesystem"),
    )

    receipts_dict: dict[str, EvidenceReceipt] = {}
    if additional_receipts:
        for r in additional_receipts:
            receipts_dict[r.receipt_id] = r

    worker_id = handoff.environment.worker_identity

    cand_digest = candidate_digest or f"sha256:{handoff.baseline_sha}"

    return VerificationContext(
        now=now,
        policy_version="1",
        plan_digest=digest,
        candidate_digest=cand_digest,
        config_version="v1",
        expected_environment_ref=handoff.environment.environment_ref,
        expected_identity=worker_id,
        expected_route=handoff.allowed_paths[0] if handoff.allowed_paths else "core/",
        approvals={handoff.approval_reference: approval},
        receipts=receipts_dict,
    )
