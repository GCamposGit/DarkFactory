"""Sanitized owner-bot delivery check for one notification (USR-536).

The CLI prints a whitelist: notification id, bot role, status, and, after a
confirmed send, message id and timestamp. Token, chat id, title and message
never enter that document.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from core.integrations.telegram import (
    OUTBOX_DELIVERED,
    OUTBOX_PENDING,
    OUTBOX_PERMANENT,
    OUTBOX_SENDING,
    OUTBOX_UNCERTAIN,
    TelegramConfig,
    TelegramGateway,
    claim_is_abandoned,
    load_telegram_config,
    redact_secrets,
    resolve_telegram_destinations,
)
from core.notifications.models import NotificationEvent
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore
from core.paths import state_root

logger = logging.getLogger("darkfac.notifications.telegram_delivery")

_NOTIFICATION_ID_RE = re.compile(r"notif_[0-9a-f]{8,32}\Z")
_ROLES = frozenset({"owner", "ops", "all"})
_CATEGORIES = frozenset({
    "missing_destination",
    "missing_token",
    "invalid_entities",
    "chat_not_found",
    "bot_blocked",
    "permanent",
    "transient",
    "uncertain",
    "notification_not_found",
    "invalid_notification_id",
    "config_unreadable",
    "sending",
})
_RECEIPT_STATUSES = frozenset({"delivered", "already_delivered"})
_DIAGNOSE_STATUSES = frozenset({"ready", "refused", "not_found"})
OWNER_ROLE = "owner"


class DeliveryView(BaseModel):
    """Internal result. ``public_payload`` chooses the fields that can be printed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notification_id: str
    role: str = OWNER_ROLE
    status: str
    message_id: int | None = None
    timestamp: str | None = None
    reason: str | None = None
    token_present: bool = False
    destination_present: bool = False
    event_found: bool = False
    outbox_status: str | None = None


def load_owner_config() -> TelegramConfig:
    """Owner bot as ``load_telegram_config`` resolves it. Failures hide the cause text."""

    try:
        return load_telegram_config(role=OWNER_ROLE)
    except Exception as exc:
        logger.warning("telegram owner config failed: %s", type(exc).__name__)
        return TelegramConfig(bot_token=None, role=OWNER_ROLE)


def open_store() -> NotificationStore:
    return NotificationStore()


def gateway_state_dir():
    return state_root() / "telegram"


def find_notification(store: NotificationStore, notification_id: str) -> NotificationEvent | None:
    """Last valid row for ``notification_id``. Parse failures are skipped, not logged."""

    path = store.store_path
    if not path.is_file():
        return None
    found: NotificationEvent | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if notification_id not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("notification_id") != notification_id:
            continue
        try:
            found = NotificationEvent.model_validate(data)
        except ValidationError:
            continue
    return found


def effective_destination(config: TelegramConfig) -> int | None:
    """First configured chat, otherwise the first user id. Never zero."""

    found = resolve_telegram_destinations(config)
    return found[0] if found else None


def _role(config: TelegramConfig) -> str:
    return config.role if config.role in _ROLES else OWNER_ROLE


def _token_present(config: TelegramConfig) -> bool:
    return bool((config.bot_token or "").strip())


def _reason(value: str | None, default: str) -> str:
    if value in _CATEGORIES:
        return str(value)
    if isinstance(value, str) and value.startswith("category="):
        category = value.split("=", 1)[1].strip()
        if category in _CATEGORIES:
            return category
    return default


def _view(
    notification_id: str,
    *,
    status: str,
    role: str = OWNER_ROLE,
    reason: str | None = None,
    message_id: int | None = None,
    timestamp: str | None = None,
    token_present: bool = False,
    destination_present: bool = False,
    event_found: bool = False,
    outbox_status: str | None = None,
) -> DeliveryView:
    return DeliveryView(
        notification_id=notification_id if _NOTIFICATION_ID_RE.fullmatch(notification_id or "") else "notif_unknown",
        role=role if role in _ROLES else "unknown",
        status=status,
        message_id=message_id,
        timestamp=timestamp,
        reason=reason,
        token_present=token_present,
        destination_present=destination_present,
        event_found=event_found,
        outbox_status=outbox_status,
    )


def _alert_text(store: NotificationStore, event: NotificationEvent) -> str:
    service = NotificationService(store=store, telegram_role=OWNER_ROLE)
    return service._telegram_text(
        severity=event.severity,
        safe_title=redact_secrets(event.title),
        safe_message=redact_secrets(event.message),
        provider_id=event.provider_id,
        remaining_percent=event.remaining_percent,
        details=event.details,
    )


def _peek(gateway: TelegramGateway, notification_id: str, chat_id: int) -> dict[str, Any] | None:
    return gateway._lookup_outbox(notification_id, gateway.config.role, chat_id)


def _confirmed(item: dict[str, Any] | None) -> tuple[int, str] | None:
    if item is None or item.get("status") != OUTBOX_DELIVERED:
        return None
    message_id = item.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    stamp = item.get("delivered_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    return message_id, stamp


def _hold_status(item: dict[str, Any] | None) -> str | None:
    """Statuses that must not call ``sendMessage`` again."""

    if item is None:
        return None
    status = item.get("status")
    if status == OUTBOX_UNCERTAIN:
        return OUTBOX_UNCERTAIN
    if status == OUTBOX_PERMANENT:
        return OUTBOX_PERMANENT
    if status == OUTBOX_SENDING and not claim_is_abandoned(item):
        return OUTBOX_SENDING
    return None


def _failure_view(
    notification_id: str,
    role: str,
    item: dict[str, Any] | None,
    *,
    token_present: bool,
    destination_present: bool,
) -> DeliveryView:
    if item is None:
        return _view(
            notification_id,
            status="failed",
            role=role,
            reason="permanent",
            token_present=token_present,
            destination_present=destination_present,
            event_found=True,
            outbox_status="absent",
        )
    status = str(item.get("status") or "")
    if status == OUTBOX_UNCERTAIN:
        return _view(
            notification_id,
            status="uncertain",
            role=role,
            reason="uncertain",
            token_present=token_present,
            destination_present=destination_present,
            event_found=True,
            outbox_status=OUTBOX_UNCERTAIN,
        )
    reason = _reason(item.get("failure_reason") if isinstance(item.get("failure_reason"), str) else None, "permanent")
    if status == OUTBOX_PENDING:
        reason = _reason(item.get("failure_reason") if isinstance(item.get("failure_reason"), str) else None, "transient")
    return _view(
        notification_id,
        status="failed",
        role=role,
        reason=reason,
        token_present=token_present,
        destination_present=destination_present,
        event_found=True,
        outbox_status=status or None,
    )


def _send_or_hold(
    gateway: TelegramGateway,
    store: NotificationStore,
    event: NotificationEvent,
    chat_id: int,
    *,
    token_present: bool,
    destination_present: bool,
) -> DeliveryView:
    role = _role(gateway.config)
    item = _peek(gateway, event.notification_id, chat_id)
    confirmed = _confirmed(item)
    if confirmed is not None:
        message_id, stamp = confirmed
        return _view(
            event.notification_id,
            status="already_delivered",
            role=role,
            message_id=message_id,
            timestamp=stamp,
            token_present=token_present,
            destination_present=destination_present,
            event_found=True,
            outbox_status=OUTBOX_DELIVERED,
        )
    held = _hold_status(item)
    if held is not None:
        return _view(
            event.notification_id,
            status="uncertain" if held == OUTBOX_UNCERTAIN else "failed",
            role=role,
            reason=held,
            token_present=token_present,
            destination_present=destination_present,
            event_found=True,
            outbox_status=held,
        )
    sent = gateway.send_message(
        chat_id,
        _alert_text(store, event),
        parse_mode="HTML",
        notification_id=event.notification_id,
    )
    current = _peek(gateway, event.notification_id, chat_id)
    confirmed = _confirmed(current)
    if sent and confirmed is not None:
        message_id, stamp = confirmed
        return _view(
            event.notification_id,
            status="delivered",
            role=role,
            message_id=message_id,
            timestamp=stamp,
            token_present=token_present,
            destination_present=destination_present,
            event_found=True,
            outbox_status=OUTBOX_DELIVERED,
        )
    return _failure_view(
        event.notification_id,
        role,
        current,
        token_present=token_present,
        destination_present=destination_present,
    )


def evaluate(
    notification_id: str,
    *,
    replay: bool,
    store: NotificationStore,
    config: TelegramConfig,
    state_dir,
) -> DeliveryView:
    """Locate the event, resolve the owner bot, and send only on replay when it is safe."""

    if _NOTIFICATION_ID_RE.fullmatch(notification_id or "") is None:
        return _view(notification_id or "notif_unknown", status="refused", reason="invalid_notification_id")
    event = find_notification(store, notification_id)
    token_ok = _token_present(config)
    chat_id = effective_destination(config)
    destination_ok = chat_id is not None
    role = _role(config)
    if event is None:
        return _view(
            notification_id,
            status="not_found",
            role=role,
            reason="notification_not_found",
            token_present=token_ok,
            destination_present=destination_ok,
        )
    if not token_ok or not destination_ok:
        reason = "missing_token" if not token_ok else "missing_destination"
        return _view(
            notification_id,
            status="refused",
            role=role,
            reason=reason,
            token_present=token_ok,
            destination_present=destination_ok,
            event_found=True,
        )
    gateway = TelegramGateway(config=config, state_dir=state_dir)
    if not replay:
        item = _peek(gateway, notification_id, chat_id)
        confirmed = _confirmed(item)
        if confirmed is not None:
            message_id, stamp = confirmed
            return _view(
                notification_id,
                status="already_delivered",
                role=role,
                message_id=message_id,
                timestamp=stamp,
                token_present=True,
                destination_present=True,
                event_found=True,
                outbox_status=OUTBOX_DELIVERED,
            )
        return _view(
            notification_id,
            status="ready",
            role=role,
            token_present=True,
            destination_present=True,
            event_found=True,
            outbox_status=str(item.get("status")) if item and item.get("status") else "absent",
        )
    return _send_or_hold(
        gateway,
        store,
        event,
        chat_id,
        token_present=True,
        destination_present=True,
    )


def public_payload(view: DeliveryView) -> dict[str, Any]:
    """Fields safe to print. Anything else on the view stays internal."""

    if view.status in _RECEIPT_STATUSES:
        return {
            "notification_id": view.notification_id,
            "role": view.role,
            "status": view.status,
            "message_id": view.message_id,
            "timestamp": view.timestamp,
        }
    payload: dict[str, Any] = {
        "notification_id": view.notification_id,
        "role": view.role,
        "status": view.status,
    }
    if view.reason:
        payload["reason"] = view.reason
    if view.status in _DIAGNOSE_STATUSES or view.status == "refused":
        payload["event_found"] = view.event_found
        payload["token_present"] = view.token_present
        payload["destination_present"] = view.destination_present
        if view.outbox_status:
            payload["outbox_status"] = view.outbox_status
    return payload


def exit_code(status: str) -> int:
    if status in {"ready", "delivered", "already_delivered"}:
        return 0
    if status in {"refused", "not_found"}:
        return 2
    return 1


def run_delivery(
    notification_id: str,
    *,
    replay: bool = False,
    store: NotificationStore | None = None,
    config: TelegramConfig | None = None,
    state_dir=None,
) -> tuple[int, dict[str, Any]]:
    if _NOTIFICATION_ID_RE.fullmatch(notification_id or "") is None:
        view = _view(notification_id or "notif_unknown", status="refused", reason="invalid_notification_id")
        return exit_code(view.status), public_payload(view)
    view = evaluate(
        notification_id,
        replay=replay,
        store=store if store is not None else open_store(),
        config=config if config is not None else load_owner_config(),
        state_dir=state_dir if state_dir is not None else gateway_state_dir(),
    )
    return exit_code(view.status), public_payload(view)


def dump_public(payload: dict[str, Any]) -> str:
    allowed = {
        "notification_id",
        "role",
        "status",
        "message_id",
        "timestamp",
        "reason",
        "event_found",
        "token_present",
        "destination_present",
        "outbox_status",
    }
    safe = {key: payload[key] for key in sorted(allowed) if key in payload}
    return json.dumps(safe, ensure_ascii=True, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnostico sanitizado de um alerta Telegram.")
    parser.add_argument("--notification-id", required=True)
    parser.add_argument("--replay", action="store_true", help="Entrega o alerta pendente uma vez.")
    args = parser.parse_args(argv)
    code, payload = run_delivery(args.notification_id, replay=args.replay)
    sys.stdout.write(dump_public(payload) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
