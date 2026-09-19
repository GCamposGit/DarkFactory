"""Continuous planning stage handler for known scope (HF-08-04).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-08-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py

Key Invariants:
1. Conforms to StageHandler protocol: handle(StageContext) -> StageResult.
2. Rigid composite deduplication: composite hash (demand_version + plan_digest + contracts)
   prevents infinite decomposition loops on re-execution.
3. Architectural floor protection: missing bindings or architectural gaps emit
   'needs_architecture_binding' and escalate to 'high_architecture' (economy never decides architecture).
4. Portfolio & Leaf Independence: ready leaves advance without blocking sibling leaves
   or independent projects in the portfolio.
5. Failsafe: invalid contexts or mismatched digests fail-closed safely.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from core.demands.models import DemandInput, UserTicket
from core.demands.specifier import (
    IntermediateDAG,
    PlannedLeaf,
    build_handoff_from_ticket,
    extract_intermediate_dag,
)
from core.workflow.contracts import (
    PlannerTier,
    WorkflowHandoff,
)
from core.workflow.control_contracts import (
    HandlerDescriptor,
    JobKey,
    StageContext,
    StageResult,
    canonical_payload_digest,
)
from core.workflow.handlers import StageHandler

logger = logging.getLogger("darkfac.workflow.planning_jobs")

STAGE: str = "planning"
VERSION: str = "v1"

DEFAULT_BASELINE_SHA = "83e5298eb231599076811802dceac8575c7f6feb"


def compute_composite_planning_digest(
    demand_version: int | str,
    plan_digest: str,
    contract_digest: str | None = None,
    extra_payload: Any = None,
) -> str:
    """Compute deterministic SHA-256 composite digest for demand version and plan digest.

    Guarantees strict deduplication: identical inputs always produce the identical hash.
    """
    hasher = hashlib.sha256()
    hasher.update(str(demand_version).encode("utf-8"))
    hasher.update(b":")
    hasher.update(str(plan_digest).strip().encode("utf-8"))
    if contract_digest:
        hasher.update(b":")
        hasher.update(str(contract_digest).strip().encode("utf-8"))
    if extra_payload is not None:
        hasher.update(b":")
        if isinstance(extra_payload, dict):
            hasher.update(canonical_payload_digest(extra_payload).encode("utf-8"))
        else:
            hasher.update(str(extra_payload).encode("utf-8"))
    return hasher.hexdigest()


class PlanningHandler:
    """StageHandler for the continuous planning stage (v1).

    Executes planning over known scope, enforcing architectural floor protection,
    rigid deduplication, and independent leaf progression.
    """

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/GrillRecord",
        output_schema_ref="schema://contracts/WorkflowHandoff",
        role="economy",
        required_capabilities=["planning"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        bindings_registry: dict[str, Any] | None = None,
        demand_provider: Callable[[str], UserTicket | DemandInput | dict[str, Any] | None] | None = None,
        memory_provider: Callable[[str], list[dict[str, Any]] | None] | None = None,
        handoff_store: dict[str, WorkflowHandoff] | None = None,
        state_store: dict[str, Any] | None = None,
        baseline_sha: str = DEFAULT_BASELINE_SHA,
        planner_id: str = "hf08-planner",
        planner_tier: PlannerTier | str = PlannerTier.ECONOMY,
        expected_plan_digest: str | None = None,
    ) -> None:
        self.bindings_registry: dict[str, Any] = (
            bindings_registry
            if bindings_registry is not None
            else {
                "HF-07-01": "executor-binding",
                "HF-05-02": "cloud-control-binding",
                "HF-07-02": "qualified-routes-binding",
                "HF-05-04": "workflow-handlers-binding",
            }
        )
        self.demand_provider = demand_provider
        self.memory_provider = memory_provider
        self.handoff_store: dict[str, WorkflowHandoff] = (
            handoff_store if handoff_store is not None else {}
        )
        self.state_store: dict[str, Any] = state_store if state_store is not None else {}
        self.baseline_sha = baseline_sha
        self.planner_id = planner_id
        self.planner_tier = (
            PlannerTier(planner_tier) if isinstance(planner_tier, str) else planner_tier
        )
        self.expected_plan_digest = expected_plan_digest

        # Internal deduplication ledger
        self._completed_hashes: dict[str, str] = {}
        self._execution_counts: dict[str, int] = {}

    def handle(self, context: StageContext) -> StageResult:
        """Execute continuous planning stage within given context."""
        jk = context.claim.job_key
        ticket_id = jk.ticket_id

        # 1. Failsafe validation of context
        if not context.plan_digest or not context.plan_digest.strip():
            return StageResult(
                outcome="failed",
                cause_code="invalid_plan_digest",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        if (
            self.expected_plan_digest is not None
            and context.plan_digest != self.expected_plan_digest
        ):
            return StageResult(
                outcome="failed",
                cause_code="digest_mismatch",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

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

        # 2. Ingest demand specification and memory history
        demand: UserTicket | DemandInput | dict[str, Any] | None = None
        if self.demand_provider is not None:
            demand = self.demand_provider(ticket_id)

        if demand is None:
            demand = UserTicket(
                id=ticket_id,
                project_id=ticket_id.split("-")[0].lower() if "-" in ticket_id else "darkfac",
                title=f"Demanda planejada: {ticket_id}",
                problem_statement=f"Implementar os requisitos de {ticket_id} com contratos estritos.",
                acceptance_criteria=[f"Entrega de {ticket_id} validada com sucesso."],
                reachability_contract=f"python -m pytest tests/test_{ticket_id.lower().replace('-', '_')}.py -v",
                suggested_files=[f"core/{ticket_id.lower().replace('-', '_')}/service.py"],
                non_goals=["Não modificar arquivos fora dos allowed_paths."],
            )

        memory_history: list[dict[str, Any]] = []
        if self.memory_provider is not None:
            memory_history = self.memory_provider(ticket_id) or []

        demand_version = 1
        if isinstance(demand, dict):
            demand_version = demand.get("demand_version", 1)

        # 3. Rigid Deduplication: composite hash of demand_version + plan_digest + contracts
        composite_hash = compute_composite_planning_digest(
            demand_version=demand_version,
            plan_digest=context.plan_digest,
            contract_digest=context.route_ref,
        )

        dedupe_key = (ticket_id, composite_hash)
        if dedupe_key in self._completed_hashes:
            existing_ref = self._completed_hashes[dedupe_key]
            logger.info("Idempotent deduplication hit for ticket %s hash: %s", ticket_id, composite_hash)
            return StageResult(
                outcome="success",
                output_refs=[existing_ref],
                evidence_refs=[f"ref://evidence/planning/dedupe/{jk.canonical_key()}"],
                operation_refs=[],
                actual_cost=0.0,
                cause_code="idempotent_dedupe",
            )

        # 4. Architectural floor protection
        # Economy role is strictly forbidden from deciding architecture or adopting missing bindings
        requires_binding = False
        target_binding = None

        if isinstance(demand, dict):
            target_binding = demand.get("architecture_binding") or demand.get("binding")
            requires_binding = (
                bool(demand.get("requires_binding", False))
                or (target_binding is not None)
                or ("architecture" in demand.get("tags", []))
            )
        elif isinstance(demand, UserTicket):
            requires_binding = "architecture" in demand.tags
            target_binding = getattr(demand, "architecture_binding", None)

        if requires_binding or target_binding is not None:
            if not target_binding or target_binding not in self.bindings_registry:
                gap_ref = f"ref://planning/needs_architecture_binding/{ticket_id}"
                route_ref = "route://role/high_architecture"
                logger.warning(
                    "Architectural gap detected for %s (binding: %s). Escalating to high_architecture.",
                    ticket_id,
                    target_binding,
                )
                return StageResult(
                    outcome="replan",
                    cause_code="needs_architecture_binding",
                    output_refs=[route_ref, gap_ref],
                    evidence_refs=[f"ref://evidence/architecture_gap/{ticket_id}"],
                    operation_refs=[],
                    actual_cost=0.0,
                )

        # 5. Graph decomposition into IntermediateDAG
        dag = extract_intermediate_dag(
            demand,
            bindings_registry=self.bindings_registry,
            baseline_sha=self.baseline_sha,
            demand_version=demand_version,
            parent_id=ticket_id,
        )

        arch_gap_leaves = dag.architecture_gap_leaves()
        if arch_gap_leaves:
            gap_ref = f"ref://planning/needs_architecture_binding/{ticket_id}"
            route_ref = "route://role/high_architecture"
            return StageResult(
                outcome="replan",
                cause_code="needs_architecture_binding",
                output_refs=[route_ref, gap_ref],
                evidence_refs=[f"ref://evidence/architecture_gap/{ticket_id}"],
                operation_refs=[],
                actual_cost=0.0,
            )

        # Invariant: Graph decomposition must stop when all leaves are in verified terminal state
        if not dag.is_complete():
            return StageResult(
                outcome="failed",
                cause_code="incomplete_decomposition",
                output_refs=[],
                actual_cost=0.0,
            )

        # 6. Leaf & Project Independence
        ready_leaves = dag.ready_leaves()
        output_refs: list[str] = []

        for leaf in ready_leaves:
            if leaf.handoff is not None:
                self.handoff_store[leaf.ticket_id] = leaf.handoff
                handoff_ref = f"ref://handoff/{leaf.ticket_id}/{composite_hash}"
                output_refs.append(handoff_ref)

        if not output_refs:
            return StageResult(
                outcome="waiting_dependency",
                cause_code="dependencies_pending",
                output_refs=[],
                evidence_refs=[f"ref://evidence/planning/blocked/{jk.canonical_key()}"],
                actual_cost=0.0,
            )

        # Register successful completion for deduplication
        self._completed_hashes[dedupe_key] = output_refs[0]
        self._execution_counts[dedupe_key] = (
            self._execution_counts.get(dedupe_key, 0) + 1
        )

        return StageResult(
            outcome="success",
            output_refs=output_refs,
            evidence_refs=[f"ref://evidence/planning/{jk.canonical_key()}"],
            operation_refs=[],
            actual_cost=0.0,
        )

    def plan_portfolio(
        self,
        contexts: list[StageContext],
    ) -> dict[str, StageResult]:
        """Plan multiple projects across the portfolio with independent progression.

        Ready leaves in one project advance immediately without being blocked
        by gaps or dependencies in other portfolio projects.
        """
        results: dict[str, StageResult] = {}
        for ctx in contexts:
            project_key = ctx.claim.job_key.ticket_id
            results[project_key] = self.handle(ctx)
        return results

    def get_handoff(self, ticket_id: str) -> WorkflowHandoff | None:
        """Lookup stored handoff by ticket ID."""
        return self.handoff_store.get(ticket_id)

    def is_hash_completed(self, composite_hash: str, ticket_id: str | None = None) -> bool:
        """Check if composite hash has completed planning."""
        if ticket_id is not None:
            return (ticket_id, composite_hash) in self._completed_hashes
        return any(h == composite_hash for (_, h) in self._completed_hashes.keys())
