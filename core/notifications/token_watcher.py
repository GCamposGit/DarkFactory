"""Proactive watcher inspecting account token quotas and dispatching operational alerts."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.notifications.models import (
    AlertSeverity,
    NotificationEvent,
    TokenAlertThresholds,
)
from core.notifications.service import NotificationService
from core.router.token_budget import _quota_headroom
from core.usage.models import AccountConnectionStatus, ProviderAccountUsage, ProviderFamily
from core.usage.monitor import AccountUsageMonitor

logger = logging.getLogger("darkfac.notifications.token_watcher")


def load_token_thresholds() -> TokenAlertThresholds:
    """Loads alert thresholds from environment or default parameters."""
    try:
        warn = float(os.environ.get("DARKFAC_TOKEN_WARNING_THRESHOLD_PERCENT", "25.0"))
    except (ValueError, TypeError):
        warn = 25.0

    try:
        crit = float(os.environ.get("DARKFAC_TOKEN_CRITICAL_THRESHOLD_PERCENT", "10.0"))
    except (ValueError, TypeError):
        crit = 10.0

    try:
        cooldown = int(os.environ.get("DARKFAC_NOTIFICATIONS_COOLDOWN_MINUTES", "30"))
    except (ValueError, TypeError):
        cooldown = 30

    return TokenAlertThresholds(
        warning_threshold_percent=warn,
        critical_threshold_percent=crit,
        cooldown_minutes=cooldown,
    )


class TokenQuotaWatcher:
    """Monitors active provider accounts and triggers notifications when tokens near critical limits."""

    def __init__(
        self,
        notification_service: Optional[NotificationService] = None,
        usage_monitor: Optional[AccountUsageMonitor] = None,
        thresholds: Optional[TokenAlertThresholds] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        self.notification_service = notification_service or NotificationService()
        self.thresholds = thresholds or load_token_thresholds()
        self.usage_monitor = usage_monitor
        self.state_file = state_file or (self.notification_service.store.store_path.parent / "quota_states.json")
        self._states: Dict[str, str] = self._load_states()

    def _load_states(self) -> Dict[str, str]:
        """Loads persistent provider alert states."""
        if self.state_file and self.state_file.exists():
            try:
                import json
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("Could not load quota states: %s", exc)
        return {}

    def _save_states(self) -> None:
        """Persists provider alert states."""
        if not self.state_file:
            return
        try:
            import json
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(self._states, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not persist quota states: %s", exc)

    def _get_usage_monitor(self) -> AccountUsageMonitor:
        """Returns or creates default AccountUsageMonitor pointing to .factory/usage."""
        if self.usage_monitor is not None:
            return self.usage_monitor
        snapshot_dir = Path.cwd() / ".factory" / "usage"
        self.usage_monitor = AccountUsageMonitor(snapshot_dir=snapshot_dir)
        return self.usage_monitor

    def evaluate_account(
        self,
        account: ProviderAccountUsage,
        force: bool = False,
    ) -> Optional[NotificationEvent]:
        """Evaluates a single account's quota headroom and fires alert if near critical limits."""
        # Local models (Ollama/Qwen) do not have quota limits ($0.00 cost)
        if account.family == ProviderFamily.LOCAL:
            return None

        # Check account connection status
        if account.status not in {AccountConnectionStatus.CONNECTED, AccountConnectionStatus.LIMITED}:
            return None

        # Determine remaining percentage
        remaining = _quota_headroom(account)
        if remaining is None:
            # If account is marked as limited without explicit headroom, treat as warning
            if account.status == AccountConnectionStatus.LIMITED:
                return self.notification_service.notify_token_quota(
                    provider_id=account.provider_id,
                    provider_name=account.provider_name,
                    remaining_percent=15.0,
                    severity=AlertSeverity.WARNING,
                    message=f"Conta {account.provider_name} em estado LIMITED pelo provedor.",
                    force=force,
                )
            return None

        prev_state = self._states.get(account.provider_id, "healthy")
        new_state = prev_state
        event: Optional[NotificationEvent] = None

        # Compare against critical and warning thresholds
        if remaining <= self.thresholds.critical_threshold_percent:
            new_state = "critical"
            # If edge_triggered: only notify when crossing into critical
            if not self.thresholds.edge_triggered or prev_state != "critical" or force:
                event = self.notification_service.notify_token_quota(
                    provider_id=account.provider_id,
                    provider_name=account.provider_name,
                    remaining_percent=remaining,
                    severity=AlertSeverity.CRITICAL,
                    force=force or self.thresholds.edge_triggered,
                )
        elif remaining <= self.thresholds.warning_threshold_percent:
            new_state = "warning"
            # If edge_triggered: only notify when crossing into warning from healthy
            if not self.thresholds.edge_triggered or prev_state not in {"warning", "critical"} or force:
                event = self.notification_service.notify_token_quota(
                    provider_id=account.provider_id,
                    provider_name=account.provider_name,
                    remaining_percent=remaining,
                    severity=AlertSeverity.WARNING,
                    force=force or self.thresholds.edge_triggered,
                )
        else:
            # Headroom > warning threshold: re-arms the thresholds
            new_state = "healthy"

        if new_state != prev_state:
            self._states[account.provider_id] = new_state
            self._save_states()

        return event

    def check_all_quotas(self, force: bool = False) -> List[NotificationEvent]:
        """Inspects all configured providers and returns all emitted alerts."""
        monitor = self._get_usage_monitor()
        report = monitor.inspect(force=force)
        emitted: List[NotificationEvent] = []

        for account in report.accounts:
            event = self.evaluate_account(account, force=force)
            if event is not None:
                emitted.append(event)

        logger.info(
            "TokenQuotaWatcher evaluated %d accounts; emitted %d alert(s)",
            len(report.accounts),
            len(emitted),
        )
        return emitted
