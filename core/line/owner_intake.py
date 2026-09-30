"""Bridge from owner demands (Telegram, DarkHub) into the autonomous production line.

Before this module, a Telegram `/demand` or a DarkHub ticket only created a
`demands.json` ticket (and, for the Hub's own `/demands/intake`, a row in a
*local* SQLite control store that no worker reads). The line's workers claim
jobs from the shared Postgres control store, so nothing entered the line.

`submit_ticket_to_line` pushes a `UserTicket` through the same public intake the
canary and dogfood use (`AutonomousIntakeService.accept` on the Postgres store the
workers read), creating a run whose first job is the grill stage. It is:

- idempotent per ticket (`channel=owner`, `external_id=ticket:<id>`): pushing the
  same ticket twice returns the same run instead of a second one;
- fail-closed on the store: when a database URL is configured but Postgres cannot
  be reached, `open_line_store` raises instead of silently accepting the demand
  into `PostgresControlStore`'s in-memory mock (which no worker would ever see);
- scoped: only projects in `DARKFAC_LINE_INTAKE_PROJECTS` (default `darkfac`).

Environment
-----------
- `DARKHUB_LINE_DATABASE_URL` / `DARKFAC_HF02_DATABASE_URL` /
  `DARKHUB_CONTROL_DATABASE_URL` (first one set wins): Postgres URL of the line's
  control store. It must allow INSERT (intake writes runs/jobs), so a read-only
  role is not enough for this bridge.
- `DARKFAC_LINE_AUTOSUBMIT`: `true`/`false`. Whether a Telegram `/demand` also
  enqueues a line run. Unset means "on iff a line database URL is configured".
- `DARKFAC_LINE_INTAKE_PROJECTS`: comma-separated project ids (default `darkfac`).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus
from core.workflow.control_contracts import IdempotencyConflict, IntakeCommand, StoreUnavailableError
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

OWNER_CHANNEL = "owner"
OWNER_POLICY_REF = "darkfac://line/owner/v1"
DATABASE_URL_ENVS: tuple[str, ...] = (
    "DARKHUB_LINE_DATABASE_URL",
    "DARKFAC_HF02_DATABASE_URL",
    "DARKHUB_CONTROL_DATABASE_URL",
)
AUTOSUBMIT_ENV = "DARKFAC_LINE_AUTOSUBMIT"
PROJECTS_ENV = "DARKFAC_LINE_INTAKE_PROJECTS"
DEFAULT_PROJECTS = "darkfac"
# Statuses from which pushing a ticket into the line moves it to `implementing`.
_PROMOTABLE_STATUSES = frozenset({DeliveryStatus.DISCOVERED, DeliveryStatus.ACCEPTED, DeliveryStatus.PLANNED})


class LineSubmission(BaseModel):
    """Outcome of pushing one ticket into the line (never raises for expected refusals)."""

    ok: bool
    ticket_id: str
    project_id: str = ""
    run_id: str | None = None
    demand_id: str | None = None
    replayed: bool = False
    message: str = ""


def line_database_url() -> str | None:
    for name in DATABASE_URL_ENVS:
        value = os.environ.get(name, "").strip()
        if value and not value.startswith("mock"):
            return value
    return None


def autosubmit_enabled() -> bool:
    """Should a Telegram `/demand` also enqueue a line run? Explicit env wins, else URL presence."""
    raw = os.environ.get(AUTOSUBMIT_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return line_database_url() is not None


def allowed_projects() -> frozenset[str]:
    raw = os.environ.get(PROJECTS_ENV, "").strip() or DEFAULT_PROJECTS
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def open_line_store(fallback: ControlStore | None = None) -> ControlStore:
    """The control store the line's workers read.

    Postgres when a line database URL is configured (raising `StoreUnavailableError`
    if it cannot be reached, never the silent in-memory mock); otherwise `fallback`
    (e.g. the Hub's local SQLite store) or the canary's `default_control_store()`.
    """
    url = line_database_url()
    if url:
        from core.orchestrator.adapters.control_postgres import PostgresControlStore
        from core.workflow.control_contracts import RuntimeOwner

        store = PostgresControlStore(
            database_url=url,
            runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
            lease_duration_sec=300,
        )
        if getattr(store, "mock_mode", False):
            raise StoreUnavailableError(
                "Line Postgres control store is unreachable (would have used an in-memory mock "
                "no worker reads); demand NOT submitted"
            )
        return store
    if fallback is not None:
        return fallback
    from core.line.store_selection import default_control_store

    return default_control_store()


def command_for_ticket(ticket: UserTicket) -> IntakeCommand:
    """Intake command for a demands.json ticket, keyed by the ticket id (idempotent)."""
    return IntakeCommand(
        project_id=ticket.project_id,
        channel=OWNER_CHANNEL,
        external_id=f"ticket:{ticket.id}",
        mode="autonomous",
        policy_ref=OWNER_POLICY_REF,
        payload={
            "title": ticket.title,
            "problem": ticket.problem_statement or ticket.title,
            "journey": list(ticket.core_journey) or [ticket.title],
            "non_goals": list(ticket.non_goals),
            "criteria": list(ticket.acceptance_criteria),
            "suggested_files": list(ticket.suggested_files),
            "reachability_contract": ticket.reachability_contract,
            "ticket_id": ticket.id,
        },
    )


def submit_ticket_to_line(
    ticket_id: str,
    *,
    demands_store: DemandsStore,
    store: ControlStore | None = None,
    now: datetime | None = None,
) -> LineSubmission:
    """Push an existing demands.json ticket into the production line.

    Returns `ok=False` with a human-readable `message` (never raises) when the ticket
    is unknown, its project is not enabled for the line, the ticket was already
    submitted with different content, or the control store is unavailable.
    """
    ticket = demands_store.get_ticket(ticket_id)
    if ticket is None:
        return LineSubmission(ok=False, ticket_id=ticket_id, message=f"Ticket '{ticket_id}' nao encontrado.")
    if ticket.project_id not in allowed_projects():
        return LineSubmission(
            ok=False,
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            message=(
                f"Projeto '{ticket.project_id}' nao esta habilitado para a linha "
                f"({PROJECTS_ENV}={','.join(sorted(allowed_projects()))})."
            ),
        )
    if ticket.status in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED):
        return LineSubmission(
            ok=False,
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            message=f"Ticket {ticket.id} esta '{ticket.status.value}'; nada a enviar para a linha.",
        )

    try:
        line_store = store if store is not None else open_line_store()
        service = AutonomousIntakeService(store=line_store, demands_store=None)  # None: never re-project a dem-* ticket
        command = command_for_ticket(ticket)
        previous_run = _existing_run_id(line_store, command)
        receipt = service.accept(command, now or datetime.now(UTC))
    except IdempotencyConflict:
        return LineSubmission(
            ok=False,
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            replayed=True,
            message=(
                f"Ticket {ticket.id} ja foi enviado a linha com outro conteudo; "
                "crie um novo ticket para a versao editada."
            ),
        )
    except Exception as exc:  # store down, schema, etc: fail closed, tell the caller
        logger.error("Line intake for ticket %s failed: %s", ticket.id, exc)
        return LineSubmission(
            ok=False,
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            message=f"Falha ao enfileirar {ticket.id} na linha: {exc}",
        )

    replayed = previous_run is not None
    if not replayed and ticket.status in _PROMOTABLE_STATUSES:
        try:  # best-effort visibility in the Hub board; the line itself never depends on it
            demands_store.update_status(ticket.id, DeliveryStatus.IMPLEMENTING)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not mark %s implementing after line submit: %s", ticket.id, exc)

    logger.info("Ticket %s -> line run %s (replayed=%s)", ticket.id, receipt.run_id, replayed)
    return LineSubmission(
        ok=True,
        ticket_id=ticket.id,
        project_id=ticket.project_id,
        run_id=receipt.run_id,
        demand_id=receipt.demand_id,
        replayed=replayed,
        message=(
            f"Ticket {ticket.id} ja estava na linha (run {receipt.run_id})."
            if replayed
            else f"Ticket {ticket.id} enviado a linha (run {receipt.run_id}); o Grill comeca em seguida."
        ),
    )


def _existing_run_id(store: Any, command: IntakeCommand) -> str | None:
    """`run_id` already recorded for this command's (channel, external_id), if any.

    Best-effort: SQLite and Postgres stores expose different internals, so an unknown
    store simply reports "not previously submitted" (the accept itself stays idempotent).
    """
    connect = getattr(store, "_connect", None)
    backend = getattr(store, "_backend", None)
    if connect is None and backend is not None:
        connect = getattr(backend, "_connect", None)
    try:
        if connect is not None:
            conn = connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT run_id FROM intake_commands WHERE channel = ? AND external_id = ?",
                    (command.channel, command.external_id),
                )
                row = cur.fetchone()
                return row[0] if row else None
            finally:
                conn.close()
        psycopg = getattr(store, "_psycopg", None)
        raw_url = getattr(store, "raw_url", None)
        if psycopg is not None and raw_url:
            with psycopg.connect(raw_url) as pg_conn:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "SELECT run_id FROM intake_commands WHERE channel = %s AND external_id = %s",
                        (command.channel, command.external_id),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
    except Exception as exc:
        logger.debug("Could not look up prior intake for %s: %s", command.external_id, exc)
    return None
