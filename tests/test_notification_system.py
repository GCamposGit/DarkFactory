"""Comprehensive test suite for Dark Factory Notification and Quota Alert System.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 5 & 10)
- HYBRID_AUTONOMY_REQUIREMENTS (Scenarios G1, G6, G8)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.integrations.telegram import TelegramConfig, TelegramService, TelegramUpdate
from core.notifications.cli import main as cli_main
from core.notifications.models import (
    AlertCategory,
    AlertSeverity,
    NotificationChannel,
    NotificationEvent,
    TokenAlertThresholds,
)
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore
from core.notifications.token_watcher import TokenQuotaWatcher
from core.usage.models import (
    AccountConnectionStatus,
    AccountUsageReport,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)
from hub.backend.api import get_hub_service, router
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture
def temp_notification_store(tmp_path: Path) -> NotificationStore:
    """Provides isolated NotificationStore."""
    store_file = tmp_path / "notifications" / "notifications.jsonl"
    return NotificationStore(store_path=store_file, retention_days=30)


@pytest.fixture
def temp_notification_service(temp_notification_store: NotificationStore) -> NotificationService:
    """Provides isolated NotificationService with dummy telegram client."""
    dummy_tg = MagicMock(spec=TelegramService)
    dummy_tg.config = TelegramConfig(bot_token="test:token", authorized_user_ids=[12345678])
    dummy_tg.send_message.return_value = True
    return NotificationService(
        store=temp_notification_store,
        telegram_service=dummy_tg,
        cooldown_minutes=30,
    )


def test_notification_models() -> None:
    """Tests that notification models are properly validated and frozen."""
    event = NotificationEvent(
        notification_id="notif-test-01",
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.CRITICAL,
        title="Alerta Crítico de Quota",
        message="Limite atingido para OpenAI.",
        provider_id="openai",
        remaining_percent=9.5,
    )
    assert event.notification_id == "notif-test-01"
    assert event.severity == AlertSeverity.CRITICAL
    assert event.acknowledged is False
    with pytest.raises(Exception):
        event.severity = AlertSeverity.WARNING  # Frozen model test


def test_notification_store_crud(temp_notification_store: NotificationStore) -> None:
    """Tests basic CRUD and filtering in NotificationStore."""
    store = temp_notification_store
    assert store.list_notifications() == []

    evt1 = NotificationEvent(
        notification_id="notif_1",
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.WARNING,
        title="Warning 1",
        message="Quota at 22%",
        provider_id="deepseek",
        remaining_percent=22.0,
    )
    evt2 = NotificationEvent(
        notification_id="notif_2",
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.CRITICAL,
        title="Critical 2",
        message="Quota at 5%",
        provider_id="anthropic",
        remaining_percent=5.0,
    )
    store.add_notification(evt1)
    store.add_notification(evt2)

    all_items = store.list_notifications()
    assert len(all_items) == 2
    assert all_items[0].notification_id == "notif_2"  # Newest first

    crit_items = store.list_notifications(severity=AlertSeverity.CRITICAL)
    assert len(crit_items) == 1
    assert crit_items[0].notification_id == "notif_2"

    # Acknowledge
    assert store.mark_acknowledged("notif_1") is True
    unread = store.list_notifications(unread_only=True)
    assert len(unread) == 1
    assert unread[0].notification_id == "notif_2"

    assert store.get_notification("notif_1").acknowledged is True


def test_notification_store_retention_pruning(tmp_path: Path) -> None:
    """Tests automated pruning of events older than 30 days."""
    store_file = tmp_path / "notifications" / "retention_test.jsonl"
    store = NotificationStore(store_path=store_file, retention_days=30)

    now = datetime.now(UTC)
    old_time = now - timedelta(days=35)
    fresh_time = now - timedelta(days=5)

    old_event = NotificationEvent(
        notification_id="notif_old",
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.INFO,
        title="Old Event",
        message="Old info",
        timestamp=old_time,
    )
    fresh_event = NotificationEvent(
        notification_id="notif_fresh",
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.CRITICAL,
        title="Fresh Event",
        message="Critical fresh alert",
        timestamp=fresh_time,
    )

    store.add_notification(old_event)
    store.add_notification(fresh_event)

    assert len(store.list_notifications()) == 2
    pruned = store.prune_expired()
    assert pruned == 1

    remaining = store.list_notifications()
    assert len(remaining) == 1
    assert remaining[0].notification_id == "notif_fresh"


def test_notification_service_dispatch_and_secret_redaction(
    temp_notification_service: NotificationService,
) -> None:
    """Tests multichannel delivery, secret redaction, and Telegram message formatting."""
    svc = temp_notification_service
    evt = svc.notify_token_quota(
        provider_id="openai",
        remaining_percent=8.5,
        provider_name="OpenAI Primary",
        message="Token sk-proj-1234567890abcdef12345 is near limit.",
    )

    assert evt is not None
    assert evt.severity == AlertSeverity.CRITICAL
    assert "sk-" not in evt.message
    assert "[REDACTED_SECRET]" in evt.message

    # Verify store
    stored = svc.store.get_notification(evt.notification_id)
    assert stored is not None
    assert stored.remaining_percent == 8.5

    # Verify telegram call
    svc.telegram_service.send_message.assert_called_once()
    call_args = svc.telegram_service.send_message.call_args[1]
    assert call_args["chat_id"] == 12345678
    assert "🚨" in call_args["text"]
    assert "OpenAI Primary" in call_args["text"]
    assert "8.5%" in call_args["text"]


def test_notification_service_anti_spam_cooldown(
    temp_notification_service: NotificationService,
) -> None:
    """Verifies that alerts for the same provider within cooldown are suppressed."""
    svc = temp_notification_service

    # First alert passes
    evt1 = svc.notify_token_quota(provider_id="anthropic", remaining_percent=22.0)
    assert evt1 is not None

    # Immediate second alert for same provider and severity is suppressed
    evt2 = svc.notify_token_quota(provider_id="anthropic", remaining_percent=21.0)
    assert evt2 is None

    # Alert for different provider passes
    evt3 = svc.notify_token_quota(provider_id="deepseek", remaining_percent=20.0)
    assert evt3 is not None

    # Alert escalating to CRITICAL passes despite recent WARNING
    evt4 = svc.notify_token_quota(provider_id="anthropic", remaining_percent=7.0)
    assert evt4 is not None
    assert evt4.severity == AlertSeverity.CRITICAL

    # Force=True bypasses cooldown
    evt5 = svc.notify(
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.CRITICAL,
        title="Critical Resend",
        message="Forced bypass",
        provider_id="anthropic",
        force=True,
    )
    assert evt5 is not None


def test_token_quota_watcher_evaluations(temp_notification_service: NotificationService) -> None:
    """Tests TokenQuotaWatcher inspecting provider accounts across healthy, warning, and critical."""
    watcher = TokenQuotaWatcher(
        notification_service=temp_notification_service,
        thresholds=TokenAlertThresholds(
            warning_threshold_percent=25.0,
            critical_threshold_percent=10.0,
        ),
    )

    # 1. Critical Account (8% remaining)
    crit_account = ProviderAccountUsage(
        provider_id="openai",
        provider_name="OpenAI Tier 5",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="openai_adapter",
        quota_supported=True,
        windows=[QuotaWindow(quota_id="q1", label="hourly", used_percent=92.0, remaining_percent=8.0)],
        message="Operational",
    )
    alert_crit = watcher.evaluate_account(crit_account)
    assert alert_crit is not None
    assert alert_crit.severity == AlertSeverity.CRITICAL
    assert alert_crit.remaining_percent == 8.0

    # 2. Warning Account (22% remaining)
    warn_account = ProviderAccountUsage(
        provider_id="anthropic",
        provider_name="Anthropic Build",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="anthropic_adapter",
        quota_supported=True,
        windows=[QuotaWindow(quota_id="q2", label="hourly", used_percent=78.0, remaining_percent=22.0)],
        message="Operational",
    )
    alert_warn = watcher.evaluate_account(warn_account)
    assert alert_warn is not None
    assert alert_warn.severity == AlertSeverity.WARNING
    assert alert_warn.remaining_percent == 22.0

    # 3. Healthy Account (65% remaining)
    healthy_account = ProviderAccountUsage(
        provider_id="deepseek",
        provider_name="DeepSeek API",
        family=ProviderFamily.CHINESE,
        status=AccountConnectionStatus.CONNECTED,
        adapter="deepseek_adapter",
        quota_supported=True,
        windows=[QuotaWindow(quota_id="q3", label="hourly", used_percent=35.0, remaining_percent=65.0)],
        message="Operational",
    )
    assert watcher.evaluate_account(healthy_account) is None

    # 4. Local Ollama Account ($0 cost, no quota limits)
    local_account = ProviderAccountUsage(
        provider_id="ollama",
        provider_name="Ollama Local",
        family=ProviderFamily.LOCAL,
        status=AccountConnectionStatus.CONNECTED,
        adapter="ollama_adapter",
        message="Local Host Active",
    )
    assert watcher.evaluate_account(local_account) is None


def test_telegram_alerts_command(tmp_path: Path) -> None:
    """Tests that Telegram command /alerts returns active alerts to authorized owner."""
    store_file = tmp_path / "telegram_notif.jsonl"
    store = NotificationStore(store_path=store_file)
    store.add_notification(
        NotificationEvent(
            notification_id="notif_tg_test",
            category=AlertCategory.TOKEN_QUOTA,
            severity=AlertSeverity.CRITICAL,
            title="OpenAI Quota Low",
            message="Restam apenas 5% de tokens na janela horária.",
            provider_id="openai",
            remaining_percent=5.0,
        )
    )

    tg_cfg = TelegramConfig(bot_token="test:tok", authorized_user_ids=[8939220558])
    service = TelegramService(config=tg_cfg, state_dir=tmp_path / "tg_state")

    with patch("core.notifications.store.NotificationStore", return_value=store):
        update = TelegramUpdate.model_validate({
            "update_id": 7771,
            "message": {
                "message_id": 101,
                "from": {"id": 8939220558, "first_name": "Owner"},
                "chat": {"id": 8939220558, "type": "private"},
                "date": 1700000000,
                "text": "/alerts",
            },
        })
        res = service.process_update(update.model_dump())

    assert res.authorized is True
    assert res.action.value == "alerts"
    assert "🚨" in res.response_text
    assert "OpenAI Quota Low" in res.response_text


def test_darkhub_notifications_api_endpoints(tmp_path: Path) -> None:
    """Tests DarkHub REST endpoints for notifications."""
    store_file = tmp_path / "hub_notif.jsonl"
    store = NotificationStore(store_path=store_file)
    evt = NotificationEvent(
        notification_id="notif_api_01",
        category=AlertCategory.TOKEN_QUOTA,
        severity=AlertSeverity.WARNING,
        title="DeepSeek Quota Warning",
        message="Consumo elevado de tokens.",
        provider_id="deepseek",
        remaining_percent=18.0,
    )
    store.add_notification(evt)

    client = TestClient(app)

    with patch("core.notifications.store.NotificationStore", return_value=store):
        # 1. GET /api/notifications
        resp = client.get("/api/notifications")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["notifications"][0]["notification_id"] == "notif_api_01"

        # 2. POST /api/notifications/{id}/acknowledge
        hub_token = get_hub_service().session_token
        ack_resp = client.post(
            "/api/notifications/notif_api_01/acknowledge",
            headers={"X-Hub-Session": hub_token},
        )
        assert ack_resp.status_code == 200
        assert ack_resp.json()["acknowledged"] is True

        # Verify acknowledged in store
        assert store.get_notification("notif_api_01").acknowledged is True


def test_notifications_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Tests CLI invocation for notifications."""
    store_file = tmp_path / "cli_store.jsonl"
    with patch.dict("os.environ", {"DARKFAC_NOTIFICATIONS_PATH": str(store_file)}):
        # 1. Test alert dispatch
        ret = cli_main(["test-alert", "--json"])
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["title"] == "Alerta de Teste Operacional"

        # 2. List notifications
        ret_list = cli_main(["list", "--json"])
        assert ret_list == 0
        captured_list = capsys.readouterr()
        list_data = json.loads(captured_list.out)
        assert len(list_data) == 1
        assert list_data[0]["title"] == "Alerta de Teste Operacional"
