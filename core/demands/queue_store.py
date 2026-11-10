"""Development queue in the control store (USR-343 S4a).

The control store (Postgres when a line database URL is configured -- see
``core.line.owner_intake.line_database_url`` --, else the local SQLite ``control.db``) is the
source of truth for tickets registered by ``run_ticket.py --create --queue-only`` and the
Telegram intake. No ``ticket/queue-*`` worktree or PR is involved. Readers merge these rows with
``.factory/demands/demands.json`` (fallback and history). A *configured* store that fails to
answer raises ``StoreUnavailableError`` (USR-449): a Postgres outage must never look like "no
tickets queued" to a caller merging these rows into a 404.
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime
from pathlib import Path

from core.demands.models import UserTicket

logger = logging.getLogger("darkfac.demands.queue_store")

_TERMINAL = frozenset({"completed", "cancelled", "canceled"})


@functools.lru_cache(maxsize=4)
def _store(database_url: str, db_path: str):  # noqa: ANN202 - ControlStore, imported lazily
    from core.line.store_selection import default_control_store

    return default_control_store(sqlite_db_path=db_path or None, database_url=database_url or None)


def control_store():  # noqa: ANN201
    """The same Postgres the production line's workers claim from, read-only here.

    USR-449: resolves through ``core.line.owner_intake.line_database_url`` (checks
    ``DARKHUB_LINE_DATABASE_URL``, ``DARKFAC_HF02_DATABASE_URL``, ``DARKHUB_CONTROL_DATABASE_URL``,
    first one set wins) instead of only ``DARKFAC_HF02_DATABASE_URL``. The Hub's deployment sets
    one of the ``DARKHUB_*`` names, never ``DARKFAC_HF02_DATABASE_URL``; reading only the latter
    silently pointed the Hub's demand lookups at an empty local SQLite file instead of the
    coordinator/worker's Postgres.
    """
    from core.line.owner_intake import line_database_url
    from core.paths import state_root

    return _store(line_database_url() or "", str(state_root() / "control.db"))


def register_ticket(ticket: UserTicket, *, source: str | None = None) -> UserTicket:
    """Upsert ``ticket`` in the control store (``source`` = intake channel, informational). Raises when the store is unavailable."""
    if source:
        logger.debug("Registering %s in the control store (source=%s)", ticket.id, source)
    payload = ticket.model_dump(mode="json")
    control_store().upsert_ticket(
        ticket.project_id, ticket.id, str(payload.get("status") or "planned"), payload, datetime.now(UTC)
    )
    return ticket


def list_queue_tickets() -> list[UserTicket]:
    """Tickets held by the control store; ``[]`` only when it is not configured and has never existed.

    A configured store (a line database URL set, or an existing local ``control.db``) that fails
    to answer raises ``StoreUnavailableError`` instead of silently returning ``[]`` -- that would
    read as "nothing queued" to ``merge_tickets`` and could serve a 404 for a ticket the store
    actually holds.
    """
    from core.line.owner_intake import line_database_url
    from core.paths import state_root
    from core.workflow.control_contracts import StoreUnavailableError

    if not line_database_url() and not (state_root() / "control.db").exists():
        return []
    try:
        rows = control_store().list_tickets()
    except StoreUnavailableError:
        logger.error("Control store unavailable for the ticket queue", exc_info=True)
        raise
    except Exception as exc:  # noqa: BLE001 - normalized into the structured contract below
        logger.error("Control store read failed for the ticket queue: %s", exc)
        raise StoreUnavailableError(f"Control store read failed for the ticket queue: {exc}") from exc
    tickets: list[UserTicket] = []
    for raw in rows:
        try:
            tickets.append(UserTicket.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping corrupted control-store ticket row: %s", exc)
    return tickets


def get_ticket(ticket_id: str) -> UserTicket | None:
    """Return the control-store ``UserTicket`` for ``ticket_id``.

    ``None`` when the id is absent or the store does not exist yet. Does not
    create the store and does not raise for those two cases. Raises
    ``StoreUnavailableError`` when the store is configured but fails to answer (USR-449).
    """
    if not ticket_id:
        return None
    for ticket in list_queue_tickets():
        if ticket.id == ticket_id:
            return ticket
    return None


def is_terminal_ticket(ticket: UserTicket) -> bool:
    """True when the ledger row is closed (``completed`` / ``cancelled``)."""
    return str(getattr(ticket.status, "value", ticket.status)) in _TERMINAL


def is_default_ledger(path: Path) -> bool:
    from core.paths import project_root

    try:
        return path.resolve() == (project_root() / ".factory" / "demands" / "demands.json").resolve()
    except OSError:
        return False


def merge_tickets(file_rows: list[UserTicket], control_rows: list[UserTicket]) -> list[UserTicket]:
    """Control store first; a terminal status in the ledger wins (delivery closed it there)."""
    merged = {t.id: t for t in file_rows}
    for ticket in control_rows:
        current = merged.get(ticket.id)
        if current is not None and is_terminal_ticket(current):
            continue
        merged[ticket.id] = ticket
    return sorted(merged.values(), key=lambda t: t.id)
