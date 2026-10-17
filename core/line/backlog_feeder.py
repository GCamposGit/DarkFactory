"""Continuous backlog feeder for Dark Factory across all registered and eligible projects (USR-283).

Reconciles open tickets from DemandsStore across all projects registered in ProjectRegistry
with active runs in ControlStore, without depending on the default 'darkfac'-only allowlist.

Enforces:
1. Multi-project eligibility: any registered project is eligible, unregistered projects are rejected.
2. Gate G1: checks material ambiguity (problem statement, non-goals, measurable criteria).
3. Dependency resolution: all dependencies must be DeliveryStatus.COMPLETED.
4. Retry caps: respects DARKFAC_LINE_MAX_TICKET_ATTEMPTS.
5. V0 re-evaluation: tickets postponed solely by V0 ('congelado-v0') are re-evaluated as eligible,
   while intentional business/planning postponements remain blocked with clear explanations.
6. Deterministic ordering: sorted by (created_at, project_id, id).
7. Fail-closed on store unavailability: explicit StoreUnavailableError propagation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from core.demands.grill import GrillService
from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line import owner_intake
from core.line.owner_intake import (
    LineSubmission,
    _ticket_attempts,
    allowed_projects,
    max_ticket_attempts_from_env,
    open_line_store,
    run_state,
    submit_ticket_to_line,
)
from core.projects.registry import ProjectRegistry
from core.roadmap.models import DeliveryStatus, PlanningHorizon
from core.workflow.control_contracts import StoreUnavailableError
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

INTENTIONAL_POSTPONE_TAGS = frozenset({"postponed-business", "deferred", "hold", "intentional_postponement"})
V0_FREEZE_TAG = "congelado-v0"


class TicketEligibility(BaseModel):
    """Detailed evaluation of a ticket's eligibility for line admission."""

    ticket_id: str
    project_id: str
    eligible: bool
    reason: str | None = None
    message: str = ""
    attempt: int = 1
    run_id: str | None = None
    ticket: UserTicket | None = None


class BacklogFeederReport(BaseModel):
    """Summary of a reconciliation and feeding cycle."""

    evaluated_count: int = 0
    eligible_count: int = 0
    blocked_count: int = 0
    submitted_count: int = 0
    replayed_count: int = 0
    candidates: list[TicketEligibility] = Field(default_factory=list)
    submissions: list[LineSubmission] = Field(default_factory=list)
    store_error: str | None = None


class BacklogFeeder:
    """Continuous feeder that reads open demands, checks eligibility, and admits them to the line."""

    def __init__(
        self,
        demands_store: DemandsStore,
        control_store: ControlStore | None = None,
        project_registry: ProjectRegistry | None = None,
        grill_service: GrillService | None = None,
        allow_registered_projects: bool = True,
    ) -> None:
        self.demands_store = demands_store
        self._control_store = control_store
        self.project_registry = project_registry if project_registry is not None else ProjectRegistry()
        self.grill_service = grill_service if grill_service is not None else GrillService()
        self.allow_registered_projects = allow_registered_projects

    def get_control_store(self) -> ControlStore:
        if self._control_store is not None:
            return self._control_store
        return open_line_store()

    def evaluate_ticket(
        self,
        ticket: UserTicket,
        *,
        store: ControlStore | None = None,
        now: datetime | None = None,
    ) -> TicketEligibility:
        """Evaluate whether a single ticket is eligible to enter the line."""
        ctrl_store = store if store is not None else self.get_control_store()
        current_now = now or datetime.now(UTC)

        # 1. Terminal status check
        if ticket.status in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED):
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="terminal_status",
                message=f"Ticket {ticket.id} está concluído ou cancelado ({ticket.status.value}).",
                ticket=ticket,
            )

        # 2. Project registration check
        is_allowed = ticket.project_id in allowed_projects()
        if not is_allowed and self.allow_registered_projects:
            is_allowed = self.project_registry.get_project(ticket.project_id) is not None

        if not is_allowed:
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="unregistered_project",
                message=f"Projeto '{ticket.project_id}' não está registrado ou habilitado para a linha.",
                ticket=ticket,
            )

        # 3. WAITING_HUMAN status check
        if ticket.status == DeliveryStatus.WAITING_HUMAN:
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="waiting_human",
                message=f"Ticket {ticket.id} está pausado aguardando decisão humana (WAITING_HUMAN).",
                ticket=ticket,
            )

        # 4. Intentional postponement check (differentiating from V0 freeze)
        tags = set(ticket.tags or [])
        has_v0_freeze = V0_FREEZE_TAG in tags
        has_intentional_tag = any(t in INTENTIONAL_POSTPONE_TAGS for t in tags)

        if has_intentional_tag:
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="intentional_postponement",
                message="Ticket possui adiamento intencional de negócio/planejamento.",
                ticket=ticket,
            )

        # Horizon check: only block on LATER/NEXT if it is NOT solely due to the historic V0 freeze
        if ticket.horizon in (PlanningHorizon.LATER, PlanningHorizon.NEXT) and not has_v0_freeze:
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="intentional_postponement",
                message=f"Ticket agendado para horizonte futuro ({ticket.horizon.value}).",
                ticket=ticket,
            )

        # 5. Dependency check
        if ticket.dependencies:
            unmet: list[str] = []
            for dep_id in ticket.dependencies:
                dep_ticket = self.demands_store.get_ticket(dep_id)
                if dep_ticket is None:
                    unmet.append(f"{dep_id} (não encontrado)")
                elif dep_ticket.status != DeliveryStatus.COMPLETED:
                    unmet.append(f"{dep_id} ({dep_ticket.status.value})")
            if unmet:
                return TicketEligibility(
                    ticket_id=ticket.id,
                    project_id=ticket.project_id,
                    eligible=False,
                    reason="unmet_dependencies",
                    message=f"Dependências não satisfeitas: {', '.join(unmet)}.",
                    ticket=ticket,
                )

        # 6. Gate G1 ambiguity check
        is_clear, ambiguity_reasons = self.grill_service.evaluate_ambiguity(ticket)
        if not is_clear:
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="g1_ambiguity",
                message=f"Bloqueado no Gate G1 por ambiguidade: {'; '.join(ambiguity_reasons)}.",
                ticket=ticket,
            )

        # 7. Existing runs & retry limit check on ControlStore
        try:
            attempts = _ticket_attempts(ctrl_store, ticket.id)
            if attempts:
                latest_attempt, latest_run = attempts[-1]
                state = run_state(ctrl_store, latest_run, now=current_now)
                if state == "succeeded":
                    return TicketEligibility(
                        ticket_id=ticket.id,
                        project_id=ticket.project_id,
                        eligible=False,
                        reason="already_delivered",
                        message=f"Ticket {ticket.id} já foi entregue com sucesso (run {latest_run}).",
                        attempt=latest_attempt,
                        run_id=latest_run,
                        ticket=ticket,
                    )
                if state == "in_flight":
                    return TicketEligibility(
                        ticket_id=ticket.id,
                        project_id=ticket.project_id,
                        eligible=False,
                        reason="in_flight",
                        message=f"Ticket {ticket.id} já possui um run em andamento na linha ({latest_run}).",
                        attempt=latest_attempt,
                        run_id=latest_run,
                        ticket=ticket,
                    )
                limit = max_ticket_attempts_from_env()
                if latest_attempt >= limit:
                    return TicketEligibility(
                        ticket_id=ticket.id,
                        project_id=ticket.project_id,
                        eligible=False,
                        reason="max_retries_exhausted",
                        message=f"Ticket {ticket.id} esgotou o limite de {limit} tentativas (último: run {latest_run}).",
                        attempt=latest_attempt,
                        run_id=latest_run,
                        ticket=ticket,
                    )
                # Failed previous attempt but retry allowed
                return TicketEligibility(
                    ticket_id=ticket.id,
                    project_id=ticket.project_id,
                    eligible=True,
                    reason=None,
                    message=f"Apto para nova tentativa ({latest_attempt + 1}).",
                    attempt=latest_attempt + 1,
                    ticket=ticket,
                )
        except StoreUnavailableError:
            raise
        except Exception as exc:
            logger.error("Erro ao consultar histórico de runs para %s: %s", ticket.id, exc)
            return TicketEligibility(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                eligible=False,
                reason="store_error",
                message=f"Falha ao consultar store: {exc}",
                ticket=ticket,
            )

        return TicketEligibility(
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            eligible=True,
            reason=None,
            message="Ticket apto para admissão na linha de produção.",
            attempt=1,
            ticket=ticket,
        )

    def list_candidates(
        self,
        *,
        now: datetime | None = None,
    ) -> list[TicketEligibility]:
        """List and evaluate all open tickets sorted by (created_at, project_id, id)."""
        ctrl_store = self.get_control_store()
        all_tickets = self.demands_store.list_tickets()

        open_tickets = [
            t for t in all_tickets if t.status not in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED)
        ]

        # Stable sort: oldest creation date first, tie-break by project_id and ticket_id
        open_tickets.sort(key=lambda t: (t.created_at, t.project_id, t.id))

        results: list[TicketEligibility] = []
        for ticket in open_tickets:
            eligibility = self.evaluate_ticket(ticket, store=ctrl_store, now=now)
            results.append(eligibility)

        return results

    def list_eligible_tickets(
        self,
        *,
        now: datetime | None = None,
    ) -> list[UserTicket]:
        """Return the sorted list of tickets currently eligible for admission."""
        candidates = self.list_candidates(now=now)
        return [c.ticket for c in candidates if c.eligible and c.ticket is not None]

    def feed_next(
        self,
        *,
        now: datetime | None = None,
    ) -> LineSubmission | None:
        """Admit the single oldest eligible ticket into the production line."""
        ctrl_store = self.get_control_store()
        eligible_tickets = self.list_eligible_tickets(now=now)
        if not eligible_tickets:
            return None

        target = eligible_tickets[0]
        submission = submit_ticket_to_line(
            target.id,
            demands_store=self.demands_store,
            store=ctrl_store,
            now=now,
            allow_registered_projects=self.allow_registered_projects,
            project_registry=self.project_registry,
        )
        return submission

    def reconcile_and_feed(
        self,
        limit: int | None = None,
        *,
        now: datetime | None = None,
    ) -> BacklogFeederReport:
        """Scan backlog, reconcile tickets with active runs, and feed up to `limit` eligible tickets."""
        ctrl_store = self.get_control_store()
        candidates = self.list_candidates(now=now)

        report = BacklogFeederReport(
            evaluated_count=len(candidates),
            eligible_count=sum(1 for c in candidates if c.eligible),
            blocked_count=sum(1 for c in candidates if not c.eligible),
            candidates=candidates,
        )

        eligible = [c for c in candidates if c.eligible and c.ticket is not None]
        to_submit = eligible[:limit] if limit is not None else eligible

        for candidate in to_submit:
            ticket = candidate.ticket
            assert ticket is not None
            sub = submit_ticket_to_line(
                ticket.id,
                demands_store=self.demands_store,
                store=ctrl_store,
                now=now,
                allow_registered_projects=self.allow_registered_projects,
                project_registry=self.project_registry,
            )
            report.submissions.append(sub)
            if sub.ok:
                if sub.replayed:
                    report.replayed_count += 1
                else:
                    report.submitted_count += 1

        return report
