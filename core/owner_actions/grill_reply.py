"""Durable record of a free-text Grill answer sent through Telegram (USR-353).

A Grill question reaches the owner on both Telegram bots, and the owner often answers with free text
(a reply to the message, or a voice note) instead of pressing a button. Those answers used to vanish: the
owner bot is alerts-only and ignored them, and the ops bot only recorded buttons and ``/grill``.

``record_grill_reply`` turns the reply into a DONE decision in the Hub's dynamic owner-action store, with
the free text as the answer note. That makes it visible in ``GET /api/owner-actions/answers`` (so the
Notebook sync persists it in the repository) and lets ``annotate_blocked_tickets`` attach it to the ticket.
Idempotent per ``(target, update_id)``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.owner_actions.models import (
    ActionKind,
    ActionStatus,
    DecisionAnswer,
    DecisionOption,
    OwnerAction,
    redact,
)
from core.owner_actions.propagate import annotate_blocked_tickets
from core.owner_actions.store import OwnerActionStore

GRILL_REPLY_OPTION = "owner-reply"
GRILL_ID_PREFIX = "OA-GRILL-"
TICKET_RE = re.compile(r"\b((?:USR|DF)-\d+)\b", re.IGNORECASE)
RUN_RE = re.compile(r"\b(run-[0-9a-f]{6,})\b", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def find_grill_target(*texts: str) -> str | None:
    """Ticket id (USR-/DF-) or line run id mentioned in the first text that has one."""

    for text in texts:
        match = TICKET_RE.search(text or "") or RUN_RE.search(text or "")
        if match:
            value = match.group(1)
            return value.upper() if TICKET_RE.fullmatch(value) else value.lower()
    return None


def is_grill_context(reply_text: str) -> bool:
    lower = (reply_text or "").lower()
    return any(key in lower for key in ("grill", "intervenção prioritária", "intervencao prioritaria", "desambiguação"))


def grill_reply_id(target: str, update_id: int | str) -> str:
    slug = _UNSAFE.sub("-", f"{target}-{update_id}").strip("-.") or "grill"
    return f"{GRILL_ID_PREFIX}{slug}"[:80]


def record_grill_reply(
    store: OwnerActionStore,
    demands_store: Any,
    *,
    target: str,
    text: str,
    update_id: int | str,
    user_id: int = 0,
    channel: str = "telegram",
) -> tuple[OwnerAction, list[str]]:
    """Persist the reply as a DONE decision; returns it with the ticket ids it was annotated on."""

    if store.dynamic_path is None:
        raise ValueError("dynamic owner action storage is not configured")
    clean = redact((text or "").strip())
    if not clean:
        raise ValueError("empty grill reply")
    is_ticket = bool(TICKET_RE.fullmatch(target))
    action = OwnerAction(
        id=grill_reply_id(target, update_id),
        kind=ActionKind.DECISION,
        title=f"Resposta de Grill via {channel} para {target}",
        why=f"O owner respondeu em texto livre ao Grill de {target} pelo {channel} (usuario {user_id}).",
        status=ActionStatus.DONE,
        blocks=[target] if is_ticket else [],
        options=[
            DecisionOption(id=GRILL_REPLY_OPTION, label="Resposta livre do owner"),
            DecisionOption(id="no-reply", label="Sem resposta"),
        ],
        answer=DecisionAnswer(option_id=GRILL_REPLY_OPTION, note=clean, answered_by=f"{channel}:{user_id}"),
        created_by=f"{channel}-grill-reply",
    )
    saved, _created = OwnerActionStore(Path(store.dynamic_path), remote_ref=None).upsert(action)
    annotated = annotate_blocked_tickets(saved, demands_store) if demands_store is not None else []
    return saved, annotated
