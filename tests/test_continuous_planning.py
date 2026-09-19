"""Comprehensive deterministic tests for HF-08-04: Continuous Planning of Known Scope.

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-08-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py

Validates:
1. StageHandler protocol compliance and descriptor registration.
2. Valid WorkflowHandoff generation adhering to strict Pydantic contracts.
3. Rigid composite deduplication anti-infinite-loop invariants.
4. Architectural floor protection: escalation to high_architecture on missing bindings.
5. Independent leaf and project progression across the portfolio.
6. Failsafe on invalid context, divergent digest, and expired lease.
7. IntermediateDAG extraction and stopping conditions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import pytest

from core.demands.models import DemandInput, UserTicket
from core.demands.specifier import (
    IntermediateDAG,
    PlannedLeaf,
    build_handoff_from_ticket,
    extract_intermediate_dag,
)
from core.workflow.contracts import (
    HandoffOrigin,
    PlannerTier,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import (
    HandlerRegistry,
    StageHandler,
    dispatch_stage,
)
from core.workflow.planning_jobs import (
    DEFAULT_BASELINE_SHA,
    PlanningHandler,
    compute_composite_planning_digest,
)

NOW = datetime(2026, 9, 18, 22, 0, 0, tzinfo=UTC)


def make_context(
    *,
    ticket_id: str = "HF-08-04",
    stage: str = "planning",
    plan_version: str = "1.0",
    iteration: int = 0,
    plan_digest: str = "sha256:" + "a" * 64,
    route_ref: str = "route-economy-local",
    expires_at: str | None = None,
) -> StageContext:
    jk = JobKey(
        run_id="run-planning-01",
        ticket_id=ticket_id,
        plan_version=plan_version,
        stage=stage,
        iteration=iteration,
    )
    claim = Claim(
        job_key=jk,
        lease_id="lease-planning-test-1",
        owner="worker-economy-local",
        fencing_token=1,
        expires_at=expires_at or (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-hf08",
        plan_digest=plan_digest,
        config_version="1.0",
        environment_ref="env-local",
        identity="worker-economy-local",
        route_ref=route_ref,
        memory_version="1.0",
        input_refs=[f"ref://grill/{ticket_id}"],
    )


# ==============================================================================
# 1. Conformidade com StageHandler
# ==============================================================================


def test_planning_handler_conforms_to_stage_handler_protocol() -> None:
    """PlanningHandler must conform to StageHandler protocol with economy role."""
    handler = PlanningHandler()
    assert isinstance(handler, StageHandler)
    assert handler.descriptor.stage == "planning"
    assert handler.descriptor.version == "v1"
    assert handler.descriptor.role == "economy"
    assert "planning" in handler.descriptor.required_capabilities


def test_planning_handler_dispatch_via_registry() -> None:
    """PlanningHandler can be registered and dispatched through HandlerRegistry."""
    registry = HandlerRegistry()
    handler = PlanningHandler()
    registry[("planning", "v1")] = handler

    ctx = make_context()
    result = registry.dispatch(ctx, version="v1")
    assert result.outcome == "success"
    assert len(result.output_refs) > 0


def test_planning_handler_dispatch_helper() -> None:
    """PlanningHandler works seamlessly with dispatch_stage helper."""
    handlers = {("planning", "v1"): PlanningHandler()}
    ctx = make_context(ticket_id="USR-DISPATCH-01")
    result = dispatch_stage(handlers, ctx, version="v1")
    assert result.outcome == "success"
    assert len(result.output_refs) > 0


# ==============================================================================
# 2. Geração Válida de WorkflowHandoff
# ==============================================================================


def test_valid_workflow_handoff_generation() -> None:
    """PlanningHandler generates fully valid WorkflowHandoff conforming to Pydantic contracts."""
    ticket = UserTicket(
        id="HF-08-04",
        project_id="darkfac",
        title="Planejamento contínuo do escopo conhecido",
        problem_statement="Decompor demandas conhecidas em DAG e handoffs idempotentes.",
        non_goals=["Não modificar arquivos fora dos allowed paths."],
        acceptance_criteria=["Todos os testes em test_continuous_planning.py passam."],
        reachability_contract="python -m pytest tests/test_continuous_planning.py -v",
        suggested_files=[
            "core/workflow/planning_jobs.py",
            "core/demands/specifier.py",
            "tests/test_continuous_planning.py",
        ],
    )

    handler = PlanningHandler(demand_provider=lambda tid: ticket)
    ctx = make_context(ticket_id="HF-08-04")
    result = handler.handle(ctx)

    assert result.outcome == "success"
    assert len(result.output_refs) > 0

    handoff = handler.get_handoff("HF-08-04")
    assert handoff is not None
    assert isinstance(handoff, WorkflowHandoff)
    assert handoff.ticket_id == "HF-08-04"
    assert handoff.grill.demand_id == "HF-08-04"
    assert handoff.environment.ticket_id == "HF-08-04"
    assert handoff.baseline_sha == DEFAULT_BASELINE_SHA
    assert handoff.origin == HandoffOrigin.USER_DEMAND
    assert handoff.state == WorkflowState.READY_FOR_HANDOFF
    assert handoff.planner_tier == PlannerTier.ECONOMY
    assert "core/workflow/planning_jobs.py" in handoff.allowed_paths
    assert len(handoff.validate_commands) > 0


def test_build_handoff_from_ticket_sanitizes_identifiers() -> None:
    """build_handoff_from_ticket cleans characters and preserves valid contract IDs."""
    ticket = UserTicket(
        id="USR 01/special!item",
        project_id="darkfac",
        title="Demanda com caracteres especiais",
    )
    handoff = build_handoff_from_ticket(ticket)
    assert handoff.ticket_id.startswith("USR")
    assert handoff.grill.demand_id == handoff.ticket_id
    assert handoff.environment.ticket_id == handoff.ticket_id


# ==============================================================================
# 3. Deduplicação Rígida Anti-Loop Infinito
# ==============================================================================


def test_rigid_composite_deduplication_anti_infinite_loop() -> None:
    """Identical demand version + plan digest does NOT decompose infinitely; returns idempotent success."""
    handler = PlanningHandler()
    ctx = make_context(ticket_id="HF-DEDUPE-01", plan_digest="sha256:" + "b" * 64)

    # First execution: plans and generates handoff
    res1 = handler.handle(ctx)
    assert res1.outcome == "success"
    assert len(res1.output_refs) > 0
    primary_ref = res1.output_refs[0]

    # Second execution with identical version and plan_digest:
    # Must return idempotent success without re-running decomposition
    res2 = handler.handle(ctx)
    assert res2.outcome == "success"
    assert res2.cause_code == "idempotent_dedupe"
    assert res2.output_refs == [primary_ref]

    # Third execution: still idempotent
    res3 = handler.handle(ctx)
    assert res3.outcome == "success"
    assert res3.cause_code == "idempotent_dedupe"
    assert res3.output_refs == [primary_ref]


def test_composite_digest_differentiates_on_version_or_digest() -> None:
    """Changing demand version or plan digest produces different composite hash and re-plans."""
    handler = PlanningHandler()
    ctx1 = make_context(ticket_id="HF-DEDUPE-02", plan_digest="sha256:" + "1" * 64)
    ctx2 = make_context(ticket_id="HF-DEDUPE-02", plan_digest="sha256:" + "2" * 64)

    res1 = handler.handle(ctx1)
    res2 = handler.handle(ctx2)

    assert res1.outcome == "success"
    assert res2.outcome == "success"
    # Neither was an idempotent dedupe hit because digests differed
    assert res1.cause_code != "idempotent_dedupe"
    assert res2.cause_code != "idempotent_dedupe"
    assert res1.output_refs != res2.output_refs


# ==============================================================================
# 4. Proteção de Piso Arquitetural & needs_architecture_binding
# ==============================================================================


def test_architectural_floor_protection_missing_binding_escalates_to_high_architecture() -> None:
    """Missing architectural binding emits needs_architecture_binding and escalates to high_architecture."""
    ticket_payload = {
        "id": "HF-ARCH-GAP-01",
        "title": "Nova persistência distribuída sem binding",
        "architecture_binding": "HF-UNKNOWN-BINDING",
        "requires_binding": True,
    }

    handler = PlanningHandler(
        bindings_registry={"HF-07-01": "executors", "HF-05-02": "control"},
        demand_provider=lambda tid: ticket_payload,
    )
    ctx = make_context(ticket_id="HF-ARCH-GAP-01")
    result = handler.handle(ctx)

    assert result.outcome == "replan"
    assert result.cause_code == "needs_architecture_binding"
    assert "route://role/high_architecture" in result.output_refs
    assert any("needs_architecture_binding" in ref for ref in result.output_refs)


def test_architectural_floor_protection_subtask_gap_escalates() -> None:
    """An architectural gap in any leaf of a decomposed DAG triggers needs_architecture_binding."""
    demand = {
        "id": "PRJ-DAG-01",
        "title": "Demanda com subtasks",
        "subtasks": [
            {
                "id": "PRJ-DAG-01-SUB-1",
                "title": "Subtask econômica",
                "dependencies": [],
            },
            {
                "id": "PRJ-DAG-01-SUB-2",
                "title": "Subtask que requer binding não homologado",
                "architecture_binding": "HF-MISSING-99",
                "requires_binding": True,
                "dependencies": [],
            },
        ],
    }

    handler = PlanningHandler(
        bindings_registry={"HF-07-01": "executors"},
        demand_provider=lambda tid: demand,
    )
    ctx = make_context(ticket_id="PRJ-DAG-01")
    result = handler.handle(ctx)

    assert result.outcome == "replan"
    assert result.cause_code == "needs_architecture_binding"
    assert "route://role/high_architecture" in result.output_refs


def test_counter_proof_present_binding_proceeds_to_ready() -> None:
    """Counter-proof: when required architectural binding exists, planning succeeds normally."""
    ticket_payload = {
        "id": "HF-BOUND-01",
        "title": "Demanda com binding satisfeito",
        "architecture_binding": "HF-07-01",
        "requires_binding": True,
    }

    handler = PlanningHandler(
        bindings_registry={"HF-07-01": "executor-binding"},
        demand_provider=lambda tid: ticket_payload,
    )
    ctx = make_context(ticket_id="HF-BOUND-01")
    result = handler.handle(ctx)

    assert result.outcome == "success"
    assert result.cause_code != "needs_architecture_binding"
    assert len(result.output_refs) > 0


# ==============================================================================
# 5. Avanço Independente de Folhas Prontas por Projeto
# ==============================================================================


def test_portfolio_independent_leaf_and_project_progression() -> None:
    """Ready leaves in one project advance immediately without being blocked by other projects."""
    # Project Alpha: ready for development
    ticket_alpha = UserTicket(
        id="PRJ-ALPHA-01",
        project_id="alpha",
        title="Módulo de autenticação local",
    )
    # Project Beta: blocked by missing architectural binding
    ticket_beta = {
        "id": "PRJ-BETA-01",
        "project_id": "beta",
        "title": "Cloud multi-region cluster",
        "architecture_binding": "HF-UNAPPROVED-CLOUD",
        "requires_binding": True,
    }
    # Project Gamma: blocked waiting on dependencies
    ticket_gamma = UserTicket(
        id="PRJ-GAMMA-01",
        project_id="gamma",
        title="Serviço downstream",
        dependencies=["PRJ-GAMMA-UPSTREAM-PENDING"],
    )

    demands_map = {
        "PRJ-ALPHA-01": ticket_alpha,
        "PRJ-BETA-01": ticket_beta,
        "PRJ-GAMMA-01": ticket_gamma,
    }

    handler = PlanningHandler(
        bindings_registry={"HF-07-01": "executors"},
        demand_provider=lambda tid: demands_map.get(tid),
    )

    ctx_alpha = make_context(ticket_id="PRJ-ALPHA-01")
    ctx_beta = make_context(ticket_id="PRJ-BETA-01")
    ctx_gamma = make_context(ticket_id="PRJ-GAMMA-01")

    portfolio_results = handler.plan_portfolio([ctx_alpha, ctx_beta, ctx_gamma])

    # Invariant: Alpha succeeds and advances independently
    assert portfolio_results["PRJ-ALPHA-01"].outcome == "success"
    assert len(portfolio_results["PRJ-ALPHA-01"].output_refs) > 0
    assert handler.get_handoff("PRJ-ALPHA-01") is not None

    # Beta escalates to high_architecture
    assert portfolio_results["PRJ-BETA-01"].outcome == "replan"
    assert portfolio_results["PRJ-BETA-01"].cause_code == "needs_architecture_binding"

    # Gamma waits on dependency
    assert portfolio_results["PRJ-GAMMA-01"].outcome == "waiting_dependency"


def test_single_dag_ready_leaf_independence() -> None:
    """Within a single DAG, ready leaves advance even if sibling leaves have pending dependencies."""
    demand = {
        "id": "PRJ-MULTI-01",
        "title": "Demanda com leaves independentes e dependentes",
        "subtasks": [
            {
                "id": "PRJ-MULTI-01-READY",
                "title": "Leaf pronta",
                "dependencies": [],
            },
            {
                "id": "PRJ-MULTI-01-BLOCKED",
                "title": "Leaf bloqueada",
                "dependencies": ["NON-EXISTENT-EXTERNAL-DEP"],
            },
        ],
    }

    dag = extract_intermediate_dag(demand)
    ready = dag.ready_leaves()
    blocked = dag.blocked_leaves()

    assert len(ready) == 1
    assert ready[0].ticket_id == "PRJ-MULTI-01-READY"
    assert ready[0].state == "ready_for_handoff"
    assert ready[0].handoff is not None

    assert len(blocked) == 1
    assert blocked[0].ticket_id == "PRJ-MULTI-01-BLOCKED"
    assert blocked[0].state == "waiting_dependency"


# ==============================================================================
# 6. Failsafe em Caso de Contexto Inválido ou Digest Divergente
# ==============================================================================


def test_failsafe_invalid_plan_digest() -> None:
    """Empty or blank plan_digest fails safely."""
    handler = PlanningHandler()
    ctx = make_context(plan_digest="   ")
    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "invalid_plan_digest"


def test_failsafe_digest_mismatch() -> None:
    """Digest divergent from approved/expected plan_digest fails closed without side-effects."""
    handler = PlanningHandler(expected_plan_digest="sha256:" + "f" * 64)
    ctx = make_context(plan_digest="sha256:" + "0" * 64)
    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "digest_mismatch"


def test_failsafe_stale_lease() -> None:
    """Expired lease on claim triggers stale_lease failure."""
    handler = PlanningHandler()
    past_iso = (NOW - timedelta(days=1)).isoformat()
    ctx = make_context(expires_at=past_iso)
    result = handler.handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "stale_lease"


# ==============================================================================
# 7. IntermediateDAG e Critérios de Parada
# ==============================================================================


def test_intermediate_dag_stopping_conditions() -> None:
    """DAG stops decomposition when all leaves reach verified terminal states."""
    demand = {
        "id": "PRJ-STOP-01",
        "title": "Demanda com parada justificada",
        "subtasks": [
            {"id": "L1", "title": "Folha 1 pronta", "dependencies": []},
            {"id": "L2", "title": "Folha 2 com dependência", "dependencies": ["L1"]},
        ],
    }

    dag = extract_intermediate_dag(demand)
    assert dag.is_complete()
    assert len(dag.leaves) == 2
    assert "L1" in dag.leaves
    assert "L2" in dag.leaves
    assert dag.leaves["L1"].state == "ready_for_handoff"
    assert dag.leaves["L2"].state == "waiting_dependency"
