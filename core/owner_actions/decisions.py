"""Safe, bounded rendering of answered owner decisions for line prompts."""

from __future__ import annotations

from core.owner_actions.models import OwnerAction, redact
from core.owner_actions.store import OwnerActionStore

DEFAULT_MAX_ITEMS = 8
DEFAULT_MAX_CHARS = 4_000


def render_answered_decisions(
    *,
    store: OwnerActionStore | None = None,
    project_id: str | None = None,
    ticket_id: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Render decisions scoped to a project or blocking ticket, with redaction and hard bounds."""
    if max_items <= 0 or max_chars <= 0:
        return ""
    action_store = store or OwnerActionStore()
    relevant: list[OwnerAction] = []
    for action in action_store.answered_decisions():
        matches_project = bool(project_id and action.project_id == project_id)
        matches_ticket = bool(ticket_id and ticket_id in action.blocks)
        if matches_project or matches_ticket:
            relevant.append(action)
    if not relevant:
        return ""

    lines = ["Decisoes ja respondidas pelo Owner (nao reabra estas escolhas):"]
    for action in relevant[:max_items]:
        option = next((item for item in action.options if action.answer and item.id == action.answer.option_id), None)
        selection = option.label if option else (action.answer.option_id if action.answer else "")
        fields = [action.id, action.title, f"Resposta: {selection}"]
        if action.answer and action.answer.note.strip():
            fields.append(f"Detalhe: {action.answer.note.strip()}")
        line = " | ".join(redact(str(value)) for value in fields)
        remaining = max_chars - sum(len(value) for value in lines) - len(lines)
        if remaining <= 0:
            break
        lines.append(line[:remaining])
    rendered = "\n".join(lines)
    return rendered[:max_chars]


__all__ = ["render_answered_decisions"]
