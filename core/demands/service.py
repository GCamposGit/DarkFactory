"""High-level domain service for User Demands."""

from __future__ import annotations

import logging
from pathlib import Path

from core.demands.grill import DemandGrillEngine
from core.demands.integrated_service import IntegratedIntakeService
from core.demands.models import (
    DemandInput,
    DemandSpecificationGuidance,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.demands.specifier import DemandSpecifier
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus

logger = logging.getLogger(__name__)


class DemandsService:
    """Facade providing headless business logic for user demands and backlog insertion."""

    def __init__(
        self,
        store: DemandsStore | None = None,
        specifier: DemandSpecifier | None = None,
        grill_engine: DemandGrillEngine | None = None,
        intake_service: IntegratedIntakeService | None = None,
    ) -> None:
        self.store = store or DemandsStore()
        self.specifier = specifier or DemandSpecifier()
        self.grill_engine = grill_engine or DemandGrillEngine(specifier=self.specifier)
        self.intake = intake_service or IntegratedIntakeService(
            store=self.store,
            grill_engine=self.grill_engine,
            specifier=self.specifier,
        )

    def guide_demand(
        self,
        demand: DemandInput,
        *,
        force_heuristic: bool = False,
        timeout: float | None = None,
    ) -> DemandSpecificationGuidance:
        """Run zero-credit specification assistance (Ollama or Heuristic Script)."""
        ticket_id = self.store.next_ticket_id(demand.project_id)
        return self.specifier.guide_demand(
            demand,
            ticket_id=ticket_id,
            force_heuristic=force_heuristic,
            timeout=timeout,
        )

    def get_next_ticket_id(self, project_id: str = "darkfac") -> str:
        """Return the next sequential user demand ticket ID."""
        return self.store.next_ticket_id(project_id)

    def create_ticket(self, ticket: UserTicket) -> UserTicket:
        """Persist a fully specified user ticket into the durable backlog."""
        existing = self.store.get_ticket(ticket.id)
        if ticket.id in ("USR-AUTO", "AUTO", "USR-DRAFT", ""):
            if existing is not None and existing.title != ticket.title:
                new_id = self.store.next_ticket_id(ticket.project_id)
                logger.info(f"Auto-assigning next ticket ID {new_id} to avoid collision with {ticket.id}")
                ticket = ticket.model_copy(update={"id": new_id})
            elif existing is None and ticket.id in ("AUTO", "USR-DRAFT", ""):
                new_id = self.store.next_ticket_id(ticket.project_id)
                ticket = ticket.model_copy(update={"id": new_id})

        logger.info(f"Inserting new user demand ticket into backlog: {ticket.id} - {ticket.title}")
        return self.store.save_ticket(ticket)

    def create_ticket_from_input(
        self,
        demand: DemandInput,
        *,
        force_heuristic: bool = False,
        timeout: float | None = None,
    ) -> UserTicket:
        """Helper to guide and immediately create a ticket from raw user input."""
        guidance = self.guide_demand(demand, force_heuristic=force_heuristic, timeout=timeout)
        if not guidance.suggested_ticket:
            raise ValueError("Failed to formulate a valid ticket from input")
        return self.create_ticket(guidance.suggested_ticket)

    def list_tickets(
        self,
        project_id: str | None = None,
        status: DeliveryStatus | None = None,
    ) -> list[UserTicket]:
        return self.store.list_tickets(project_id=project_id, status=status)

    def get_ticket(self, ticket_id: str) -> UserTicket | None:
        return self.store.get_ticket(ticket_id)

    def update_ticket_status(
        self,
        ticket_id: str,
        status: DeliveryStatus,
        notes: str | None = None,
    ) -> UserTicket:
        logger.info(f"Updating user demand ticket {ticket_id} status to {status.value}")
        return self.store.update_status(ticket_id, status, notes=notes)

    def start_grill_session(
        self,
        ticket_id: str,
        *,
        force_heuristic: bool = False,
        timeout: float | None = None,
    ) -> GrillSession:
        """Start a clarifying Q&A grill session for a demand ticket."""
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket '{ticket_id}' not found")
        return self.grill_engine.start_grill(
            ticket, force_heuristic=force_heuristic, timeout=timeout
        )

    def submit_grill_answers(
        self,
        ticket_id: str,
        answers: dict[str, str],
        session: GrillSession | None = None,
    ) -> GrillRefinementResult:
        """Apply answers from a grill session and update the persisted ticket."""
        ticket = self.get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket '{ticket_id}' not found")
        result = self.grill_engine.refine_ticket(ticket, answers, session=session)
        self.store.save_ticket(result.refined_ticket)
        logger.info(f"Refined ticket {ticket_id} with grill answers: {len(result.summary_of_changes)} changes")
        return result

    # --- Integrated Intake & Hybrid Autonomy (HF-08) ---

    def receive_integrated_demand(self, demand: DemandInput, **kwargs: Any) -> dict[str, Any]:
        """Orchestrate demand ingestion, clarity check (Scenario G1), and run registration."""
        return self.intake.receive_demand(demand, **kwargs)

    def submit_integrated_grill_answers(
        self,
        ticket_id: str,
        answers: dict[str, str],
        session: GrillSession,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Apply answers, finalize GrillRecord, and resume run."""
        return self.intake.submit_grill_answers(ticket_id, answers, session, **kwargs)

    def plan_and_resolve_dependencies(self, ticket_id: str, grill_record: Any, **kwargs: Any) -> dict[str, Any]:
        """Resolve dependencies (Scenario G2), generate manifest, and compile WorkflowHandoff."""
        return self.intake.plan_and_resolve_dependencies(ticket_id, grill_record, **kwargs)

    def bootstrap_project(self, project_name: str, target_dir: Path | str, **kwargs: Any) -> dict[str, Any]:
        """Bootstrap greenfield or brownfield project using Skill 07 and register runtime."""
        return self.intake.bootstrap_new_project(project_name, target_dir, **kwargs)

    def receive_audio_demand(self, audio_path: Path | str, project_id: str, title: str, **kwargs: Any) -> dict[str, Any]:
        """Ingest demand via audio file transcription (Skill 09)."""
        return self.intake.receive_audio_demand(audio_path, project_id, title, **kwargs)


def build_default_demands_service(repository_root: Path | str | None = None) -> DemandsService:
    root = Path(repository_root) if repository_root else Path.cwd()
    store_path = root / ".factory" / "demands" / "demands.json"
    return DemandsService(store=DemandsStore(store_path))
