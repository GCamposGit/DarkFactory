"""Learning and memory stage handlers and context loader (HF-10-01).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-10-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py
- core/learning/service.py

Key Invariants:
1. Conforms to StageHandler protocol: handle(StageContext) -> StageResult.
2. ObservationHandler operates on memory_observation stage.
3. Idempotent processing by (event_id, consumer_version).
4. Anti-recursion: Events originating from LEARNING_STAGES (memory_observation, learning_eval, promotion)
   are treated as terminal no-ops and never re-enqueue memory_observation.
5. Non-blocking Owner Learning Pack generation: Failure or error during pack generation is logged
   and does NOT fail the StageResult or block the production pipeline (pack falhou não bloqueia).
6. ContextLoader.get(job):
   - Binds to PersistentMemoryService.get_selective_context.
   - Enforces strict project isolation (project_id vs global).
   - Excludes non-ACTIVE candidates (PROPOSED, EVALUATING, REJECTED).
   - Computes and fixes deterministic memory_version.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from core.execution.agent_executor import TaskSpec
from core.learning.service import PersistentMemoryService
from core.orchestrator.context import TaskContext
from core.workflow.contracts import ContractModel
from core.workflow.control_contracts import (
    HandlerDescriptor,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler
from core.workflow.successors import LEARNING_STAGES

logger = logging.getLogger("darkfac.workflow.learning_handlers")

STAGE: str = "memory_observation"
VERSION: str = "v1"


class LoadedContext(ContractModel):
    """Context loaded and bounded by ContextLoader with deterministic memory version."""

    task_context: TaskContext
    memory_version: str
    project_id: str
    active_rules: list[str] = Field(default_factory=list)


class ContextLoader:
    """Loads selective context for jobs with project isolation and fixed memory version."""

    def __init__(self, memory_service: PersistentMemoryService | None = None) -> None:
        self.memory_service = memory_service or PersistentMemoryService()

    def get(
        self,
        job: JobKey | StageContext,
        project_id: str | None = None,
        task_spec: TaskSpec | None = None,
        checkpoint: dict[str, Any] | None = None,
        max_summary_tokens: int = 500,
    ) -> LoadedContext:
        """Retrieve task context with strict project isolation and deterministic memory_version."""
        resolved_project = project_id
        if not resolved_project:
            if isinstance(job, StageContext):
                resolved_project = (
                    getattr(job.claim.job_key, "project_id", None)
                    or getattr(job, "project_id", None)
                    or job.claim.job_key.ticket_id
                )
            elif isinstance(job, JobKey):
                resolved_project = getattr(job, "project_id", None) or job.ticket_id
            else:
                resolved_project = "darkfac"

        if task_spec is None:
            ticket_id = (
                job.claim.job_key.ticket_id
                if isinstance(job, StageContext)
                else (job.ticket_id if isinstance(job, JobKey) else "task-default")
            )
            # Default allowed_paths encompasses standard scopes to ensure relevant rules are discovered
            task_spec = TaskSpec(
                task_id=ticket_id,
                objective=f"Execute task {ticket_id}",
                allowed_paths=["core/", "tests/", "backend/", "frontend/", "hub/", "deploy/"],
            )

        task_context = self.memory_service.get_selective_context(
            task_spec=task_spec,
            project_id=resolved_project,
            checkpoint=checkpoint,
            max_summary_tokens=max_summary_tokens,
        )

        memory_ver = self.memory_service.compute_memory_version(project_id=resolved_project)
        active_rules = list(task_context.active_rules)

        return LoadedContext(
            task_context=task_context,
            memory_version=memory_ver,
            project_id=resolved_project,
            active_rules=active_rules,
        )


class ObservationHandler:
    """StageHandler for the autonomous memory observation stage (HF-10-01).

    Observes preceding stage execution outcomes, proposes learning hypotheses,
    generates non-blocking Owner Learning Packs, and prevents recursive loops.
    """

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/StageResult",
        output_schema_ref="schema://contracts/MemoryHypothesis",
        role="memory_agent",
        required_capabilities=["read_file", "observe_events"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        memory_service: PersistentMemoryService | None = None,
        consumer_version: str = "v1",
    ) -> None:
        self.memory_service = memory_service or PersistentMemoryService()
        self.consumer_version = consumer_version
        self._processed_events: set[tuple[str, str]] = set()

    def handle(self, context: StageContext) -> StageResult:
        """Execute memory observation stage within the given context."""
        jk = context.claim.job_key
        canonical_key = jk.canonical_key()

        # 1. Anti-recursion verification:
        # Check if the incoming inputs or event refs indicate origin in LEARNING_STAGES
        is_learning_event = False
        for ref in context.input_refs:
            ref_lower = ref.lower()
            if any(ls in ref_lower for ls in LEARNING_STAGES):
                is_learning_event = True
                break

        if is_learning_event:
            logger.info("Anti-recursion: skipping observation for learning stage input in %s", canonical_key)
            return StageResult(
                outcome="success",
                output_refs=[f"ref://memory/recursion_skipped/{canonical_key}"],
                evidence_refs=[f"ref://evidence/noop/{canonical_key}"],
                operation_refs=[],
                actual_cost=0.0,
            )

        # 2. Idempotency tracking by (event_id, consumer_version)
        event_id = context.input_refs[0] if context.input_refs else f"event://job/{canonical_key}"
        tracking_key = (event_id, self.consumer_version)
        if tracking_key in self._processed_events:
            logger.info("Idempotent replay detected for event %s (consumer=%s)", event_id, self.consumer_version)
            return StageResult(
                outcome="success",
                output_refs=[f"ref://memory/hypothesis/{canonical_key}"],
                evidence_refs=[f"ref://evidence/idempotent/{canonical_key}"],
                operation_refs=[],
                actual_cost=0.0,
            )

        self._processed_events.add(tracking_key)

        # 3. Non-blocking Session Learning Pack generation
        try:
            self.memory_service.generate_owner_learning_pack(
                title=f"Session Pack: {jk.ticket_id} [{jk.stage}]",
                session_id=jk.run_id,
                files_analyzed=[ref for ref in context.input_refs if not ref.startswith("event://")],
                save_to_disk=True,
            )
        except Exception as exc:
            # Enforce invariant: Pack failure must NEVER fail the stage or block production
            logger.warning(
                "Learning pack generation failed for %s (continuing fail-safe): %s",
                canonical_key,
                exc,
            )

        # 4. Success outcome with valid output_refs and evidence_refs
        return StageResult(
            outcome="success",
            output_refs=[f"ref://memory/hypothesis/{canonical_key}"],
            evidence_refs=[f"ref://evidence/memory_observation/{canonical_key}"],
            operation_refs=[],
            actual_cost=0.0,
        )


__all__ = [
    "LoadedContext",
    "ContextLoader",
    "ObservationHandler",
]
