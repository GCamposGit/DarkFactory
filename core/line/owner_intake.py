"""Bridge from owner demands (Telegram, DarkHub) into the autonomous production line.

Before this module, a Telegram `/demand` or a DarkHub ticket only created a
`demands.json` ticket (and, for the Hub's own `/demands/intake`, a row in a
*local* SQLite control store that no worker reads). The line's workers claim
jobs from the shared Postgres control store, so nothing entered the line.

`submit_ticket_to_line` pushes a `UserTicket` through the same public intake the
canary and dogfood use (`AutonomousIntakeService.accept` on the Postgres store the
workers read), creating a run whose first job is the grill stage. It is:

- idempotent per attempt (`channel=owner`, `external_id=ticket:<id>`, then `ticket:<id>:a<n>`):
  pushing a ticket whose run is still in flight returns that run instead of a second one, a
  delivered ticket is reported as delivered, and a run that ended without delivering is retried
  as a new attempt (cap `DARKFAC_LINE_MAX_TICKET_ATTEMPTS`, default 5);
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
from datetime import UTC, datetime, timedelta
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
MAX_ATTEMPTS_ENV = "DARKFAC_LINE_MAX_TICKET_ATTEMPTS"
DEFAULT_MAX_TICKET_ATTEMPTS = 5
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
    attempt: int = 1
    state: str = ""  # "submitted" | "in_flight" | "delivered" ("" when refused)
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


def open_line_store(fallback: ControlStore | None = None, *, url: str | None = None) -> ControlStore:
    """The control store the line's workers read.

    Postgres when a line database URL is configured (raising `StoreUnavailableError`
    if it cannot be reached, never the silent in-memory mock); otherwise `fallback`
    (e.g. the Hub's local SQLite store) or the canary's `default_control_store()`.
    """
    url = url or line_database_url()
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


def ticket_external_id(ticket_id: str, attempt: int = 1) -> str:
    """`ticket:<id>` for attempt 1 (the historical id, unchanged), `ticket:<id>:a<n>` after."""
    if attempt <= 1:
        return f"ticket:{ticket_id}"
    return f"ticket:{ticket_id}:a{attempt}"


def max_ticket_attempts_from_env() -> int:
    """`DARKFAC_LINE_MAX_TICKET_ATTEMPTS`; unset/invalid/<1 falls back to 5."""
    raw = os.environ.get(MAX_ATTEMPTS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_TICKET_ATTEMPTS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %d", MAX_ATTEMPTS_ENV, raw, DEFAULT_MAX_TICKET_ATTEMPTS)
        return DEFAULT_MAX_TICKET_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAX_TICKET_ATTEMPTS


def command_for_ticket(ticket: UserTicket, attempt: int = 1) -> IntakeCommand:
    """Intake command for a demands.json ticket, keyed by the ticket id (idempotent per attempt).

    Attempt 1 is byte-identical to the pre-retry command (`ticket:<id>`, no `attempt` key), so
    a run already accepted before retries existed is replayed, never conflicted.
    """
    payload: dict[str, Any] = {
        "title": ticket.title,
        "problem": ticket.problem_statement or ticket.title,
        "journey": list(ticket.core_journey) or [ticket.title],
        "non_goals": list(ticket.non_goals),
        "criteria": list(ticket.acceptance_criteria),
        "suggested_files": list(ticket.suggested_files),
        "reachability_contract": ticket.reachability_contract,
        "ticket_id": ticket.id,
    }
    if attempt > 1:
        payload["attempt"] = attempt
    return IntakeCommand(
        project_id=ticket.project_id,
        channel=OWNER_CHANNEL,
        external_id=ticket_external_id(ticket.id, attempt),
        mode="autonomous",
        policy_ref=OWNER_POLICY_REF,
        payload=payload,
    )


def _attempt_number(base_external_id: str, external_id: str) -> int:
    if external_id == base_external_id:
        return 1
    suffix = external_id[len(base_external_id) :]
    if suffix.startswith(":a") and suffix[2:].isdigit():
        return int(suffix[2:])
    return 0


def _ticket_attempts(store: Any, ticket_id: str) -> list[tuple[int, str | None]]:
    """`[(attempt, run_id)]` already accepted for this ticket, ascending. Empty if unknown/none."""
    finder = getattr(store, "find_intake_runs", None)
    base = ticket_external_id(ticket_id)
    if finder is None:
        return []
    attempts = [
        (_attempt_number(base, external_id), run_id) for external_id, run_id in finder(OWNER_CHANNEL, base)
    ]
    return sorted((a for a in attempts if a[0] >= 1), key=lambda item: item[0])


def previous_attempt_run_ids(store: Any, payload: dict[str, Any] | None) -> list[str]:
    """Run ids of EARLIER attempts of the ticket behind a retry run, newest first.

    `payload` is the run's intake payload. Empty (fail-safe, never raises) unless it is a retry
    attempt (`attempt` > 1) of a ticket, and only attempts whose own payload is identical to this
    one apart from the `attempt` counter are returned: if the ticket was edited between attempts,
    what the owner decided before may no longer apply.
    """
    try:
        data = payload or {}
        ticket_id = str(data.get("ticket_id") or "")
        attempt = data.get("attempt")
        if not ticket_id or not isinstance(attempt, int) or attempt <= 1:
            return []
        getter = getattr(store, "get_run_payload", None)
        if getter is None:
            return []
        comparable = {k: v for k, v in data.items() if k != "attempt"}
        run_ids: list[str] = []
        for earlier, run_id in reversed(_ticket_attempts(store, ticket_id)):
            if earlier >= attempt or not run_id:
                continue
            earlier_payload = getter(run_id)
            if isinstance(earlier_payload, dict) and {
                k: v for k, v in earlier_payload.items() if k != "attempt"
            } == comparable:
                run_ids.append(run_id)
        return run_ids
    except Exception as exc:
        logger.warning("Could not list previous attempts of the ticket: %s", exc)
        return []


RunState = str  # "in_flight" | "succeeded" | "failed"


def _expired_no_route_wait(
    status: dict[str, Any], *, now: datetime | None = None, wall_clock_hours: float | None = None,
) -> bool:
    """A lone no-route wait beyond the run budget cannot make useful progress."""
    from core.workflow.control_store import RUN_OPEN_JOB_STATUSES

    open_jobs = [job for job in status.get("jobs", []) if job.get("status") in RUN_OPEN_JOB_STATUSES]
    if len(open_jobs) != 1:
        return False
    job = open_jobs[0]
    if job.get("status") != "waiting_human" or job.get("cause_code") != "no_route_available":
        return False
    raw_created = status.get("created_at")
    if not raw_created:
        return False
    try:
        created = raw_created if isinstance(raw_created, datetime) else datetime.fromisoformat(str(raw_created))
        if wall_clock_hours is None:
            from core.line.routing import load_routing_config

            wall_clock_hours = load_routing_config().run_caps.wall_clock_hours
        if wall_clock_hours <= 0:
            return False
        started = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
        current = now or datetime.now(UTC)
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
        return current - started > timedelta(hours=wall_clock_hours)
    except (TypeError, ValueError, OSError) as exc:
        logger.warning("Could not determine whether no-route wait expired: %s", exc)
        return False


def run_state(
    store: Any, run_id: str | None, *, now: datetime | None = None, wall_clock_hours: float | None = None,
) -> RunState:
    """Coarse state of a line run from its jobs.

    `succeeded`: every required line stage has a succeeded job; `in_flight`: anything is still
    pending/running/waiting (or the state cannot be read: never duplicate a run we cannot see
    the end of); `failed`: nothing is in flight and the run did not deliver.

    `retry`/`replan` rows are history, not work: the successor is written as a new row and the
    old one keeps that status forever, so they never make a run look alive.
    """
    getter = getattr(store, "get_run_status", None)
    if not run_id or getter is None:
        return "in_flight"
    try:
        status = getter(run_id)
    except Exception as exc:  # unreadable: assume alive rather than start a duplicate
        logger.warning("Could not read status of run %s: %s", run_id, exc)
        return "in_flight"
    jobs = (status or {}).get("jobs") or []
    if not jobs:
        return "in_flight"
    from core.line.bindings import LINE_STAGES

    required = [stage for stage in LINE_STAGES if stage != "retrospective"]
    succeeded = {job.get("stage") for job in jobs if job.get("status") == "succeeded"}
    if all(stage in succeeded for stage in required):
        return "succeeded"
    if _expired_no_route_wait(status, now=now, wall_clock_hours=wall_clock_hours):
        logger.info("Run %s exhausted its clock while waiting for a route; a new attempt may start", run_id)
        return "failed"
    from core.workflow.control_store import RUN_OPEN_JOB_STATUSES

    if any(job.get("status") in RUN_OPEN_JOB_STATUSES for job in jobs):
        return "in_flight"
    return "failed"


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

    Retries mirror the canary: the first send uses `ticket:<id>`; while a run is in flight it is
    replayed (never duplicated); a succeeded run is reported as delivered; when the latest run
    ended without delivering, a new send starts `ticket:<id>:a<n>` (capped by
    `DARKFAC_LINE_MAX_TICKET_ATTEMPTS`, default 5).
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

    def refused(message: str, *, attempt: int = 1, replayed: bool = False) -> LineSubmission:
        return LineSubmission(
            ok=False, ticket_id=ticket.id, project_id=ticket.project_id, attempt=attempt, replayed=replayed,
            message=message,
        )

    attempt = 1
    replay = False
    try:
        line_store = store if store is not None else open_line_store()
        service = AutonomousIntakeService(store=line_store, demands_store=None)  # None: never re-project a dem-* ticket
        attempts = _ticket_attempts(line_store, ticket.id)
        if attempts:
            latest_attempt, latest_run = attempts[-1]
            state = run_state(line_store, latest_run)
            if state == "succeeded":
                return LineSubmission(
                    ok=True, ticket_id=ticket.id, project_id=ticket.project_id, run_id=latest_run,
                    attempt=latest_attempt, replayed=True, state="delivered",
                    message=f"Ticket {ticket.id} ja foi entregue (tentativa {latest_attempt}, run {latest_run}).",
                )
            if state == "in_flight":
                attempt, replay = latest_attempt, True
            else:
                limit = max_ticket_attempts_from_env()
                if latest_attempt >= limit:
                    return refused(
                        f"Ticket {ticket.id} ja usou as {limit} tentativas permitidas "
                        f"(ultima: tentativa {latest_attempt}, run {latest_run}, terminou sem entregar).",
                        attempt=latest_attempt,
                    )
                attempt = latest_attempt + 1
        command = command_for_ticket(ticket, attempt)
        receipt = service.accept(command, now or datetime.now(UTC))
    except IdempotencyConflict:
        return refused(
            f"Ticket {ticket.id} ja foi enviado a linha com outro conteudo; "
            "crie um novo ticket para a versao editada.",
            attempt=attempt,
            replayed=True,
        )
    except Exception as exc:  # store down, schema, etc: fail closed, tell the caller
        logger.error("Line intake for ticket %s failed: %s", ticket.id, exc)
        return refused(f"Falha ao enfileirar {ticket.id} na linha: {exc}")

    if not replay and ticket.status in _PROMOTABLE_STATUSES:
        try:  # best-effort visibility in the Hub board; the line itself never depends on it
            demands_store.update_status(ticket.id, DeliveryStatus.IMPLEMENTING)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not mark %s implementing after line submit: %s", ticket.id, exc)

    logger.info("Ticket %s -> line run %s (attempt %d, replayed=%s)", ticket.id, receipt.run_id, attempt, replay)
    return LineSubmission(
        ok=True,
        ticket_id=ticket.id,
        project_id=ticket.project_id,
        run_id=receipt.run_id,
        demand_id=receipt.demand_id,
        replayed=replay,
        attempt=attempt,
        state="in_flight" if replay else "submitted",
        message=(
            f"Ticket {ticket.id} ja estava na linha (tentativa {attempt}, run {receipt.run_id})."
            if replay
            else (
                f"Ticket {ticket.id} enviado a linha (tentativa {attempt}, run {receipt.run_id}); "
                "o Grill comeca em seguida."
            )
        ),
    )

