"""Notifications for terminal ticket budgets (one emission per durable hold)."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def notify_ticket_hold(ticket: str, cause: str, detail: str, log_path: str) -> None:
    """Use the existing Hub/Telegram channel without creating another ticket."""
    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    try:
        NotificationService().notify(
            category=AlertCategory.SYSTEM_HEALTH, severity=AlertSeverity.CRITICAL,
            title=f"Ticket {ticket} em hold",
            message=f"Causa: {cause}\nDiagnostico: {detail}\nLog: {log_path}",
            channel=NotificationChannel.ALL,
            details={"ticket": ticket, "cause": cause, "detail": detail, "log": log_path},
        )
    except Exception:
        logger.exception("Notification failed for durable ticket hold %s", ticket)
