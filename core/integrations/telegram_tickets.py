"""Telegram ``/tickets``: open demands as HTML messages under the 4096-character cap.

Reads the demands queue, drops completed and cancelled rows, and splits the list
on line boundaries. Length is measured in UTF-16 code units, which is what the
Bot API counts toward the 4096 limit (a supplementary character is one Python
code point and two UTF-16 units).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("darkfac.integrations.telegram_tickets")

# Exclusive upper bound: every outbound part must stay strictly under this.
TELEGRAM_MESSAGE_LIMIT = 4096

_CLOSED_STATUSES = frozenset({"completed", "cancelled", "canceled"})
_ID_NUMBER = re.compile(r"^(.*?)(\d+)$")
_EMPTY_MESSAGE = "ℹ️ Nenhum ticket aberto no momento."


def telegram_units(text: str) -> int:
    """Return the Telegram length of ``text`` (UTF-16 code units)."""

    return len(text.encode("utf-16-le")) // 2


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _field(row: object, name: str, default: object = "") -> object:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def status_value(status: object) -> str:
    """Normalize an enum, a string, or a nested ``{"status": ...}`` value."""

    if isinstance(status, dict):
        status = status.get("status", "")
    raw = getattr(status, "value", status)
    return str(raw or "").strip().lower()


def is_open_status(status: object) -> bool:
    """True when the row is not completed and not cancelled."""

    value = status_value(status)
    if not value:
        return True
    return value not in _CLOSED_STATUSES


def _sort_key(row: object) -> tuple[str, int, str]:
    ticket_id = _clean(_field(row, "id"))
    match = _ID_NUMBER.match(ticket_id)
    if match:
        return (match.group(1), int(match.group(2)), ticket_id)
    return (ticket_id, 0, ticket_id)


def describe_ticket(row: object) -> str:
    """Title plus problem statement, collapsed to a single line."""

    title = _clean(_field(row, "title"))
    problem = _clean(
        _field(row, "problem_statement") or _field(row, "problem") or _field(row, "description")
    )
    if title and problem and problem.casefold() != title.casefold():
        if problem.casefold().startswith(title.casefold()):
            return problem
        return f"{title}: {problem}"
    return title or problem or "(sem descricao)"


def _render_line(row: object, budget: int) -> str:
    """One HTML line. User text is escaped. The line is cut to ``budget`` units."""

    ticket_id = _clean(_field(row, "id")) or "?"
    description = describe_ticket(row)
    prefix = f"• <code>{html.escape(ticket_id, quote=False)}</code> — "
    full = prefix + html.escape(description, quote=False)
    if telegram_units(full) <= budget:
        return full

    ellipsis = "..."
    best = ""
    lo = 0
    hi = len(description)
    while lo <= hi:
        mid = (lo + hi) // 2
        snippet = description[:mid].rstrip()
        line = prefix + html.escape(snippet, quote=False) + ellipsis
        if telegram_units(line) <= budget:
            best = line
            lo = mid + 1
        else:
            hi = mid - 1
    if best:
        return best
    if telegram_units(prefix) <= budget:
        return prefix.rstrip()
    return prefix[:budget]


def split_ticket_lines(
    lines: Sequence[str],
    *,
    header: str,
    continuation: str,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """Pack lines under ``limit`` UTF-16 units. Each chunk keeps its own header."""

    if limit < 1:
        raise ValueError("limit must be positive")
    chunks: list[str] = []
    active_header = header
    current = header
    for line in lines:
        if not line:
            continue
        addition = line if current == active_header else f"\n{line}"
        if telegram_units(current) + telegram_units(addition) < limit:
            current += addition
            continue
        if current != active_header:
            chunks.append(current)
        active_header = continuation
        current = continuation + line
        if telegram_units(current) >= limit:
            logger.warning("Ticket line still exceeds the Telegram limit after fitting; trimming")
            current = current[: limit - 1]
    if current and current not in (header, continuation):
        chunks.append(current)
    return chunks


def format_open_tickets_messages(
    tickets: Sequence[object],
    *,
    limit: int = TELEGRAM_MESSAGE_LIMIT,
) -> list[str]:
    """HTML messages listing open tickets. Closed rows are omitted."""

    open_rows = [row for row in tickets if is_open_status(_field(row, "status"))]
    open_rows.sort(key=_sort_key)
    if not open_rows:
        return [_EMPTY_MESSAGE]

    header = f"📋 <b>Tickets abertos</b> ({len(open_rows)})\n"
    continuation = "📋 <b>Tickets abertos</b> (continua)\n"
    overhead = max(telegram_units(header), telegram_units(continuation))
    line_budget = limit - overhead - 1
    if line_budget < 1:
        raise ValueError("limit is smaller than the tickets header")
    lines = [_render_line(row, line_budget) for row in open_rows]
    parts = split_ticket_lines(lines, header=header, continuation=continuation, limit=limit)
    return parts or [_EMPTY_MESSAGE]


def load_open_tickets(path: Path | str | None = None) -> list[Any]:
    """Load the demands queue and return rows that are still open.

    ``DARKFAC_DEMANDS_PATH`` wins, matching ``DemandsStore``. Otherwise the queue
    is ``<project>/.factory/demands/demands.json``, independent of the process cwd.
    """

    import os

    from core.demands.store import DemandsStore

    if path is not None:
        store = DemandsStore(path)
    elif os.environ.get("DARKFAC_DEMANDS_PATH"):
        store = DemandsStore()
    else:
        from core.paths import project_root

        store = DemandsStore(project_root() / ".factory" / "demands" / "demands.json")
    return [ticket for ticket in store.list_tickets() if is_open_status(ticket.status)]
