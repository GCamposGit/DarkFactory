"""Atomic transactional demand intake service (HF-08-01).

Normative implementation of:
- docs/handoffs/continuous-autonomy/HF-08-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- ADR-HF-001

Invariants:
- Demands are committed atomically with run, initial grill job, and outbox event.
- Crash resistance: An accepted demand never exists without its initial committed job.
- Replay with identical payload digest returns exact same IntakeReceipt IDs.
- Replay with conflicting payload raises IdempotencyConflict.
- Documentary mode produces null run_id and initial_job_id.
- Grill is scheduled as a subsequent job rather than blocking intake execution.
- DemandsStore JSON projection occurs post-commit; projection failure does not duplicate demand.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.workflow.control_contracts import (
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
)
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)


class AutonomousIntakeService:
    """Transactional intake service providing atomic intake commitment and documentary handling."""

    def __init__(
        self,
        store: ControlStore,
        demands_store: DemandsStore | None = None,
    ) -> None:
        self.store = store
        self.demands_store = demands_store

    def accept(
        self,
        command: IntakeCommand,
        now: datetime | None = None,
    ) -> IntakeReceipt:
        """Accept an intake command transactionally and trigger post-commit projection."""
        effective_now = now or datetime.now(UTC)

        # 1. Atomic commit in ControlStore (run + demand + initial grill job + outbox)
        # Idempotency and conflicting payload verification are handled atomically by ControlStore
        receipt = self.store.accept(command, effective_now)

        # 2. Post-commit JSON projection (resilient, non-duplicating)
        if self.demands_store is not None:
            self._project_to_demands_store(command, receipt)

        return receipt

    def _project_to_demands_store(
        self,
        command: IntakeCommand,
        receipt: IntakeReceipt,
    ) -> None:
        """Project committed demand to the local JSON DemandsStore without duplicating on failure."""
        if self.demands_store is None:
            return

        try:
            payload = command.payload
            journey_raw = payload.get("journey")
            core_journey = [journey_raw] if isinstance(journey_raw, str) else list(journey_raw or [])

            reachability = payload.get("reachability_contract")
            ticket = UserTicket(
                id=receipt.demand_id,
                project_id=command.project_id,
                title=payload.get("title", f"Demand {receipt.demand_id}"),
                problem_statement=payload.get("problem", ""),
                core_journey=core_journey,
                non_goals=list(payload.get("non_goals", [])),
                acceptance_criteria=list(payload.get("criteria", [])),
                suggested_files=list(payload.get("suggested_files", [])),
                reachability_contract=reachability or "",
            )
            self.demands_store.save_ticket(ticket)
        except Exception as exc:
            # Post-commit projection failure must NOT rollback database commit
            # and must NOT create duplicate tickets on retry
            logger.warning(
                f"Post-commit JSON projection for demand '{receipt.demand_id}' failed: {exc}"
            )
