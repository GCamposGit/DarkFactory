"""Multichannel notification dispatcher with anti-spam cooldown and Telegram integration."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.integrations.telegram import TelegramService, load_telegram_config, redact_secrets
from core.notifications.models import (
    AlertCategory,
    AlertSeverity,
    NotificationChannel,
    NotificationEvent,
)
from core.notifications.store import NotificationStore

logger = logging.getLogger("darkfac.notifications.service")


class NotificationService:
    """Dispatches operational notifications across Telegram and DarkHub."""

    def __init__(
        self,
        store: Optional[NotificationStore] = None,
        telegram_service: Optional[TelegramService] = None,
        cooldown_minutes: int = 30,
    ) -> None:
        self.store = store or NotificationStore()
        self.telegram_service = telegram_service
        self.cooldown_minutes = max(1, cooldown_minutes)
        # In-memory tracking of recent alerts: (provider_id, severity) -> last_sent_datetime
        self._recent_alerts: Dict[Tuple[str, AlertSeverity], datetime] = {}

    def _get_telegram_service(self) -> Optional[TelegramService]:
        """Lazy-initializes or returns active TelegramService."""
        if self.telegram_service is not None:
            return self.telegram_service
        try:
            cfg = load_telegram_config()
            if cfg.bot_token:
                self.telegram_service = TelegramService(cfg)
                return self.telegram_service
        except Exception as exc:
            logger.debug("TelegramService could not be auto-loaded: %s", exc)
        return None

    def should_suppress_alert(self, provider_id: Optional[str], severity: AlertSeverity) -> bool:
        """Checks if an alert should be suppressed due to anti-spam cooldown."""
        if not provider_id:
            return False
        key = (provider_id, severity)
        last_sent = self._recent_alerts.get(key)
        if last_sent is None:
            return False
        elapsed = datetime.now(UTC) - last_sent
        return elapsed < timedelta(minutes=self.cooldown_minutes)

    def notify(
        self,
        category: AlertCategory,
        severity: AlertSeverity,
        title: str,
        message: str,
        provider_id: Optional[str] = None,
        remaining_percent: Optional[float] = None,
        channel: NotificationChannel = NotificationChannel.ALL,
        details: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[NotificationEvent]:
        """Dispatches an alert to configured channels with anti-spam check and secret redaction."""
        if not force and self.should_suppress_alert(provider_id, severity):
            logger.info(
                "Suppressed %s alert for provider %s (within %d min cooldown)",
                severity.value,
                provider_id,
                self.cooldown_minutes,
            )
            return None

        delivered_channels: List[str] = []
        safe_title = redact_secrets(title)
        safe_message = redact_secrets(message)

        # 1. Delivery to DarkHub / Store
        event = NotificationEvent(
            notification_id=f"notif_{uuid.uuid4().hex[:12]}",
            category=category,
            severity=severity,
            title=safe_title,
            message=safe_message,
            provider_id=provider_id,
            remaining_percent=remaining_percent,
            delivered_channels=[],
            acknowledged=False,
            details=details or {},
            timestamp=datetime.now(UTC),
        )

        if channel in {NotificationChannel.DARKHUB, NotificationChannel.ALL}:
            self.store.add_notification(event)
            delivered_channels.append("darkhub")

        # 2. Delivery to Telegram
        if channel in {NotificationChannel.TELEGRAM, NotificationChannel.ALL}:
            tg = self._get_telegram_service()
            if tg:
                icon = "🚨" if severity == AlertSeverity.CRITICAL else ("⚠️" if severity == AlertSeverity.WARNING else "ℹ️")
                tg_text = (
                    f"{icon} <b>[{severity.value.upper()}: {safe_title}]</b>\n\n"
                    f"{safe_message}\n"
                )
                if provider_id:
                    p_label = (details or {}).get("provider_name") or provider_id
                    tg_text += f"• <b>Provedor:</b> <code>{p_label}</code>\n"
                if remaining_percent is not None:
                    tg_text += f"• <b>Cota Restante:</b> <code>{remaining_percent:.1f}%</code>\n"
                tg_text += f"• <b>Horário:</b> <i>{datetime.now(UTC).strftime('%H:%M:%S UTC')}</i>"

                # Send to authorized users/chats
                chats = list(tg.config.authorized_chat_ids) or list(tg.config.authorized_user_ids)
                if chats:
                    for cid in chats:
                        sent = tg.send_message(chat_id=cid, text=tg_text, parse_mode="HTML")
                        if sent and "telegram" not in delivered_channels:
                            delivered_channels.append("telegram")
                else:
                    # Enqueue in outbox if no direct chats configured
                    tg.send_message(chat_id=0, text=tg_text, parse_mode="HTML")
                    delivered_channels.append("telegram_outbox")

        # Record cooldown timestamp if provider is set
        if provider_id:
            self._recent_alerts[(provider_id, severity)] = datetime.now(UTC)

        # Update delivered channels on the stored event if store was used
        if "darkhub" in delivered_channels and delivered_channels != event.delivered_channels:
            event = event.model_copy(update={"delivered_channels": delivered_channels})

        logger.info(
            "Notification %s [%s] dispatched to %s",
            event.notification_id,
            severity.value,
            delivered_channels,
        )
        return event

    def notify_token_quota(
        self,
        provider_id: str,
        remaining_percent: float,
        provider_name: Optional[str] = None,
        severity: Optional[AlertSeverity] = None,
        message: Optional[str] = None,
        force: bool = False,
    ) -> Optional[NotificationEvent]:
        """Convenience method to trigger a token quota warning or critical alert."""
        p_name = provider_name or provider_id
        sev = severity or (AlertSeverity.CRITICAL if remaining_percent <= 10.0 else AlertSeverity.WARNING)
        title = "Limite Crítico de Cota de Tokens" if sev == AlertSeverity.CRITICAL else "Alerta de Consumo de Tokens"
        default_msg = (
            f"A conta do provedor {p_name} atingiu nível {sev.value.upper()} com apenas "
            f"{remaining_percent:.1f}% de capacidade disponível. O roteador acionará failover "
            f"para rotas alternativas caso a cota se esgote."
        )
        return self.notify(
            category=AlertCategory.TOKEN_QUOTA,
            severity=sev,
            title=title,
            message=message or default_msg,
            provider_id=provider_id,
            remaining_percent=remaining_percent,
            details={"provider_name": p_name, "threshold_eval": sev.value},
            force=force,
        )
