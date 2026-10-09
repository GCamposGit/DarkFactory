"""Attach an owner's decision to the tickets it blocks so the production line can resume (USR-190).

``UserTicket`` has no comment field, so the answer is recorded the way the rest of the backlog
records machine-readable state: a ``owner-decision:<OA-id>=<option>`` tag (queryable) plus a short
marked paragraph appended to ``problem_statement`` (read by the planning stage). The operation is
idempotent: answering again replaces the previous tag and paragraph instead of stacking them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.owner_actions.models import ActionKind, OwnerAction, iso_z, redact
from core.roadmap.models import DeliveryStatus, utc_now

logger = logging.getLogger(__name__)

TAG_PREFIX = "owner-decision:"


def decision_tag(action: OwnerAction) -> str:
    option = action.answer.option_id if action.answer else ""
    return f"{TAG_PREFIX}{action.id}={option}"


def _marker(action_id: str) -> str:
    return f"[Decisao do owner {action_id}]"


def decision_paragraph(action: OwnerAction) -> str:
    answer = action.answer
    if answer is None:
        return ""
    label = next((o.label for o in action.options if o.id == answer.option_id), answer.option_id)
    when = iso_z(answer.answered_at)
    note = f" Nota do owner: {answer.note}" if answer.note else ""
    return redact(
        f"{_marker(action.id)} {action.title} -> opcao {answer.option_id} ({label}), em {when}.{note}"
    )


def annotate_blocked_tickets(action: OwnerAction, store: Any) -> list[str]:
    """Annotate every non-completed ticket in ``action.blocks``; returns the ids that were updated.

    ``store`` is a ``core.demands.store.DemandsStore`` (anything with ``get_ticket``/``save_ticket``).
    Failures per ticket are logged and skipped: recording the answer must never fail because a
    referenced ticket is missing or locked.
    """

    if action.kind is not ActionKind.DECISION or action.answer is None:
        return []
    updated: list[str] = []
    tag = decision_tag(action)
    paragraph = decision_paragraph(action)
    own_marker = re.compile(rf"\n*{re.escape(_marker(action.id))}[^\n]*")
    for ticket_id in action.blocks:
        try:
            ticket = store.get_ticket(ticket_id)
            if ticket is None or ticket.status == DeliveryStatus.COMPLETED:
                continue
            tags = [t for t in ticket.tags if not t.startswith(f"{TAG_PREFIX}{action.id}=")]
            tags.append(tag)
            statement = own_marker.sub("", ticket.problem_statement or "").rstrip()
            statement = f"{statement}\n\n{paragraph}".strip()
            ticket = ticket.model_copy(update={"tags": tags, "problem_statement": statement, "updated_at": utc_now()})
            store.save_ticket(ticket)
            updated.append(ticket_id)
        except Exception as exc:  # noqa: BLE001 - best effort per ticket
            logger.warning("owner_actions: nao foi possivel anexar a decisao %s ao ticket %s: %s", action.id, ticket_id, exc)
    return updated


__all__ = ["TAG_PREFIX", "annotate_blocked_tickets", "decision_paragraph", "decision_tag"]
