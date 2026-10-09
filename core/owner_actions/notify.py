"""Telegram notice for new critical/high owner actions (USR-190).

Same delivery mechanism as ``HubService.notify_pending_grill``: the owner and ops bots are tried in
turn through ``core.integrations.telegram`` and the message carries an inline button linking to the
DarkHub. Delivery is best effort: this module never raises, so a missing token or a Telegram outage
cannot fail the command that created the item.
"""

from __future__ import annotations

import html
import logging
import os
from typing import Any, Callable, Optional

from core.owner_actions.models import NOTIFY_PRIORITIES, OwnerAction, redact

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = "https://darkhub.ggcampos.com"
Sender = Callable[[str, list[list[dict[str, str]]]], bool]


def hub_link(base_url: Optional[str] = None) -> str:
    base = (base_url or os.getenv("DARKHUB_BASE_URL") or DEFAULT_HUB_URL).rstrip("/")
    return f"{base}/#owner-actions"


def should_notify(action: OwnerAction) -> bool:
    return action.priority in NOTIFY_PRIORITIES


def build_message(action: OwnerAction, link: str) -> str:
    why = redact(action.why).strip()
    if len(why) > 260:
        why = why[:257].rstrip() + "..."
    blocks = ", ".join(action.blocks) if action.blocks else "nenhum ticket"
    kind = "Decisao" if action.kind.value == "decision" else "Acao"
    lines = [
        f"<b>DarkHub: {kind} do owner ({html.escape(action.priority.value)})</b>",
        "",
        f"<b>{html.escape(action.id)}</b> - {html.escape(redact(action.title))}",
        f"<b>Bloqueia:</b> {html.escape(blocks)}",
    ]
    if why:
        lines.append(f"<b>Por que:</b> {html.escape(why)}")
    lines += ["", f"Passo a passo completo no DarkHub: {html.escape(link)}"]
    return "\n".join(lines)


def _telegram_sender() -> Sender:
    def _send(text: str, buttons: list[list[dict[str, str]]]) -> bool:
        from core.integrations.telegram import TelegramGateway, load_telegram_config

        sent = False
        for role in ("owner", "ops"):
            try:
                cfg = load_telegram_config(role=role)
                if not cfg.bot_token:
                    continue
                gateway = TelegramGateway(config=cfg)
                targets = list(cfg.authorized_chat_ids) or list(cfg.authorized_user_ids)
                for chat_id in targets:
                    if gateway.send_message(chat_id=chat_id, text=text, buttons=buttons):
                        sent = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("owner_actions: telegram role %s falhou: %s", role, exc.__class__.__name__)
        return sent

    return _send


def notify_owner_action(
    action: OwnerAction,
    *,
    hub_base_url: Optional[str] = None,
    sender: Optional[Sender] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Notify the owner about ``action``. Returns ``{"attempted": bool, "sent": bool}``; never raises."""

    if not force and not should_notify(action):
        return {"attempted": False, "sent": False, "reason": "priority-below-threshold"}
    link = hub_link(hub_base_url)
    text = build_message(action, link)
    buttons = [[{"text": "Abrir no DarkHub", "url": link}]]
    try:
        sent = bool((sender or _telegram_sender())(text, buttons))
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner_actions: notificacao falhou: %s", exc.__class__.__name__)
        sent = False
    return {"attempted": True, "sent": sent, "link": link}


__all__ = ["DEFAULT_HUB_URL", "build_message", "hub_link", "notify_owner_action", "should_notify"]
