"""Tests for Dual-Bot Telegram Architecture (Owner Bot vs Ops Bot).

Validates:
1. Role-specific configuration loading (owner vs ops vs all).
2. Dedicated state files to prevent offset collision across bots.
3. Command gating: /grill and /approve allowed on Owner Bot, redirected on Ops Bot.
4. Role-specific start/help menus.
5. Callback query role gating.
6. Notification service routing to Owner Bot for critical alerts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from core.integrations.telegram import (
    TelegramActionType,
    TelegramConfig,
    TelegramGateway,
    load_telegram_config,
)
from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore


def test_load_telegram_config_role_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load_telegram_config resolves owner and ops tokens from env independently."""
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "1111:owner_token")
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "2222:ops_token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "999")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "888")

    cfg_owner = load_telegram_config(role="owner")
    assert cfg_owner.role == "owner"
    assert cfg_owner.bot_token == "1111:owner_token"
    assert cfg_owner.authorized_user_ids == [999]

    cfg_ops = load_telegram_config(role="ops")
    assert cfg_ops.role == "ops"
    assert cfg_ops.bot_token == "2222:ops_token"
    assert cfg_ops.authorized_user_ids == [999]


def test_dual_bot_state_file_isolation(tmp_path: Path) -> None:
    """Owner and Ops bots persist to independent state files to avoid offset collisions."""
    cfg_owner = TelegramConfig(bot_token="tok1", role="owner", authorized_user_ids=[10])
    gw_owner = TelegramGateway(config=cfg_owner, state_dir=tmp_path)
    assert gw_owner.state_file.name == "gateway_state_owner.json"
    assert gw_owner.outbox_file.name == "outbox_owner.json"

    cfg_ops = TelegramConfig(bot_token="tok2", role="ops", authorized_user_ids=[10])
    gw_ops = TelegramGateway(config=cfg_ops, state_dir=tmp_path)
    assert gw_ops.state_file.name == "gateway_state_ops.json"
    assert gw_ops.outbox_file.name == "outbox_ops.json"

    # Updates on one do not touch the other
    gw_owner.process_update({
        "update_id": 100,
        "message": {
            "message_id": 1,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/status",
        },
    })
    assert gw_owner.last_offset == 101
    assert (tmp_path / "gateway_state_owner.json").exists()
    assert not (tmp_path / "gateway_state_ops.json").exists()

    gw_ops.process_update({
        "update_id": 50,
        "message": {
            "message_id": 1,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/status",
        },
    })
    assert gw_ops.last_offset == 51
    assert (tmp_path / "gateway_state_ops.json").exists()

    # Verify reload
    reloaded_owner = TelegramGateway(config=cfg_owner, state_dir=tmp_path)
    reloaded_ops = TelegramGateway(config=cfg_ops, state_dir=tmp_path)
    assert reloaded_owner.last_offset == 101
    assert reloaded_ops.last_offset == 51


def test_command_gating_governance_restricted_on_ops_bot(tmp_path: Path) -> None:
    """Ops bot rejects /grill and /approve, directing owner to @darkfac_bot."""
    cfg_ops = TelegramConfig(bot_token="tok_ops", role="ops", authorized_user_ids=[10])
    gw_ops = TelegramGateway(config=cfg_ops, state_dir=tmp_path / "ops")

    # /grill on Ops Bot -> Redirected
    res_grill = gw_ops.process_update({
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/grill TICKET-1 Option A",
        },
    })
    assert "@darkfac_bot" in res_grill.response_text
    assert "Canal Restrito" in res_grill.response_text

    # /approve on Ops Bot -> Redirected
    res_appr = gw_ops.process_update({
        "update_id": 2,
        "message": {
            "message_id": 2,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/approve darkfac abc123def456",
        },
    })
    assert "@darkfac_bot" in res_appr.response_text
    assert "Canal Restrito" in res_appr.response_text

    # Callback grill on Ops Bot -> Redirected
    res_cb = gw_ops.process_update({
        "update_id": 3,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 10},
            "data": "cb:grill:TICKET-1:OptionA",
        },
    })
    assert "@darkfac_bot" in res_cb.response_text


def test_owner_bot_executes_governance_and_accepts_priority_demands(tmp_path: Path) -> None:
    """Owner bot handles /grill, /approve, and accepts priority demands."""
    grill_mock = MagicMock(return_value={"resumed": True, "ticket_id": "TICKET-1"})
    approval_mock = MagicMock(return_value={"receipt_id": "RCPT-999"})
    demand_mock = MagicMock(return_value={"ticket_id": "DF-OWNER-1"})

    cfg_owner = TelegramConfig(bot_token="tok_owner", role="owner", authorized_user_ids=[10])
    gw_owner = TelegramGateway(
        config=cfg_owner,
        state_dir=tmp_path / "owner",
        grill_handler=grill_mock,
        approval_handler=approval_mock,
        demand_handler=demand_mock,
    )

    # 1. /grill succeeds
    res_grill = gw_owner.process_update({
        "update_id": 10,
        "message": {
            "message_id": 1,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/grill TICKET-1 Option B",
        },
    })
    assert res_grill.resumed is True
    grill_mock.assert_called_once_with("TICKET-1", "Option B", 10)

    # 2. /approve succeeds
    res_appr = gw_owner.process_update({
        "update_id": 11,
        "message": {
            "message_id": 2,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/approve darkfac sha256abcd",
        },
    })
    assert res_appr.resumed is True
    assert "RCPT-999" in res_appr.response_text
    approval_mock.assert_called_once_with("darkfac", "sha256abcd", 10)

    # 3. /demand from Owner succeeds with Owner tag
    res_demand = gw_owner.process_update({
        "update_id": 12,
        "message": {
            "message_id": 3,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": "/demand Urgent security fix",
        },
    })
    assert "Owner Demand" in res_demand.response_text
    assert "DF-OWNER-1" in res_demand.response_text


def test_notification_service_defaults_to_owner_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """NotificationService routes critical alerts to the Owner Bot by default."""
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "1111:owner_token")
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "2222:ops_token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "999")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "999")

    store = NotificationStore(store_path=tmp_path / "notifs.jsonl")
    notif_svc = NotificationService(store=store, cooldown_minutes=1)
    tg_svc = notif_svc._get_telegram_service()
    assert tg_svc is not None
    assert tg_svc.config.role == "owner"
    assert tg_svc.config.bot_token == "1111:owner_token"
