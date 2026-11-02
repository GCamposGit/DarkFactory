"""Development queue in the control store (USR-343 S4a).

The control store (Postgres when ``DARKFAC_HF02_DATABASE_URL`` is set, else the local SQLite
``control.db``) is the source of truth for tickets registered by ``run_ticket.py --create --queue-only``
and the Telegram intake. No ``ticket/queue-*`` worktree or PR is involved. Readers merge these rows with
``.factory/demands/demands.json`` (fallback and history).
"""

from __future__ import annotations

import functools
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.demands.models import UserTicket

logger = logging.getLogger("darkfac.demands.queue_store")

_TERMINAL = frozenset({"completed", "cancelled", "canceled"})


@functools.lru_cache(maxsize=4)
def _store(database_url: str, db_path: str):  # noqa: ANN202 - ControlStore, imported lazily
    from core.line.owner_intake import open_line_store
    from core.line.store_selection import default_control_store

    if database_url:
        return open_line_store(url=database_url)
    return default_control_store(sqlite_db_path=db_path or None)


def control_store(*, database_url: str | None = None, fallback: Any | None = None):  # noqa: ANN201
    """Resolve the canonical demand queue store used by both Hub and line readers.

    ``database_url=None`` follows the line's configured environment precedence;
    an explicit empty URL selects the persistent local store. An injected fallback
    is honored only when no URL is configured.
    """
    from core.paths import state_root
    from core.line.owner_intake import line_database_url

    if database_url is None:
        database_url = line_database_url() or ""
    if database_url:
        return _store(database_url, str(state_root() / "control.db"))
    if fallback is not None:
        return fallback
    return _store("", str(state_root() / "control.db"))


def register_ticket(ticket: UserTicket, *, source: str | None = None, store: Any | None = None) -> UserTicket:
    """Upsert ``ticket`` in the control store (``source`` = intake channel, informational). Raises when the store is unavailable."""
    if source:
        logger.debug("Registering %s in the control store (source=%s)", ticket.id, source)
    payload = ticket.model_dump(mode="json")
    (store or control_store()).upsert_ticket(
        ticket.project_id, ticket.id, str(payload.get("status") or "planned"), payload, datetime.now(UTC)
    )
    return ticket


def list_queue_tickets(*, store: Any | None = None) -> list[UserTicket]:
    """Tickets held by the control store; missing local SQLite means an empty queue."""
    from core.paths import state_root
    from core.line.owner_intake import line_database_url
    from core.workflow.control_contracts import StoreUnavailableError

    if store is None and not line_database_url() and not (state_root() / "control.db").exists():
        return []
    try:
        rows = (store or control_store()).list_tickets()
    except StoreUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize backend failures without hiding them
        raise StoreUnavailableError("Demand control store is unavailable") from exc
    tickets: list[UserTicket] = []
    for raw in rows:
        try:
            tickets.append(UserTicket.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping corrupted control-store ticket row: %s", exc)
    return tickets


def get_ticket(ticket_id: str, *, store: Any | None = None) -> UserTicket | None:
    """Return the control-store ``UserTicket`` for ``ticket_id``.

    ``None`` when the id is absent or the store does not exist yet. Does not
    create the store and does not raise for those two cases.
    """
    if not ticket_id:
        return None
    for ticket in list_queue_tickets(store=store):
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
