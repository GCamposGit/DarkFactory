"""Tests for Dual-Bot Telegram Architecture (Owner Bot vs Ops Bot).

Validates:
1. Role-specific configuration loading (owner vs ops vs all).
2. Dedicated state files to prevent offset collision across bots.
3. Command gating: /grill and /approve allowed on Owner Bot, redirected on Ops Bot.
4. Role-specific start/help menus.
5. Callback query role gating (owner bot = alerts only, ops bot = everything else).
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


def test_telegram_environment_precedes_local_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "7")
    config_dir = tmp_path / ".factory" / "telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "ops_config.json").write_text(
        json.dumps({"bot_token": "local-token", "authorized_user_ids": [8], "authorized_chat_ids": [9]}),
        encoding="utf-8",
    )

    config = load_telegram_config(role="ops")
    assert config.bot_token == "env-token"
    assert config.authorized_user_ids == [7]
    assert config.authorized_chat_ids == [9]
    monkeypatch.delenv("TELEGRAM_OPS_BOT_TOKEN")
    assert load_telegram_config(role="ops").bot_token == "local-token"


def test_telegram_default_state_dir_uses_project_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    gateway = TelegramGateway(config=TelegramConfig(role="ops"))
    assert gateway.state_dir == tmp_path / ".factory" / "telegram"
    assert gateway.state_dir.is_absolute()


def test_role_config_without_token_falls_back_to_local_legacy_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    for key in ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    config_dir = tmp_path / ".factory" / "telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "ops_config.json").write_text('{"bot_token": null}', encoding="utf-8")
    (config_dir / "config.json").write_text('{"bot_token": "legacy-local"}', encoding="utf-8")
    assert load_telegram_config(role="ops").bot_token == "legacy-local"


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


def _msg(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 10},
            "chat": {"id": 10},
            "date": 1700000000,
            "text": text,
        },
    }


def test_owner_bot_is_alerts_only_and_points_commands_to_the_ops_bot(tmp_path: Path) -> None:
    """Owner bot (@darkfac_bot) handles nothing: commands get a pointer to @darkfac_ops_bot."""
    grill_mock = MagicMock(return_value={"resumed": True})
    approval_mock = MagicMock(return_value={"receipt_id": "RCPT-999"})
    demand_mock = MagicMock(return_value={"ticket_id": "DF-OWNER-1"})
    line_grill_mock = MagicMock(return_value={"resumed": True})
    line_mock = MagicMock(return_value={"message": "x"})
    cfg_owner = TelegramConfig(bot_token="tok_owner", role="owner", authorized_user_ids=[10])
    gw_owner = TelegramGateway(
        config=cfg_owner,
        state_dir=tmp_path / "owner",
        grill_handler=grill_mock,
        approval_handler=approval_mock,
        demand_handler=demand_mock,
        line_grill_handler=line_grill_mock,
        line_handler=line_mock,
    )

    for i, text in enumerate(
        ["/grill TICKET-1 Option B", "/approve darkfac sha256abcd", "/demand Urgent security fix", "/linha USR-62", "/help"], start=1
    ):
        res = gw_owner.process_update(_msg(i, text))
        assert "@darkfac_ops_bot" in res.response_text
        assert "apenas alertas" in res.response_text
    # Non-commands (including plain text) are ignored silently.
    assert not gw_owner.process_update(_msg(20, "oi tudo bem?")).response_text

    # Grill/accept/release buttons are never handled on the owner bot.
    for n, data in enumerate(["cb:grill:run-1#q1:0", "cb:grill:TICKET-1:OptionA", "cb:accept:run-1", "cb:release:darkfac:abc:approve"]):
        res_cb = gw_owner.process_update(
            {"update_id": 30 + n, "callback_query": {"id": f"cbo{n}", "from": {"id": 10}, "data": data}}
        )
        assert "@darkfac_ops_bot" in res_cb.response_text
    for mock in (grill_mock, approval_mock, demand_mock, line_grill_mock, line_mock):
        mock.assert_not_called()


def test_ops_bot_executes_grill_approve_demand_and_callbacks(tmp_path: Path) -> None:
    """Ops bot (@darkfac_ops_bot) owns grill answers, demands, /linha, approvals and every button."""
    grill_mock = MagicMock(return_value={"resumed": True, "ticket_id": "TICKET-1"})
    approval_mock = MagicMock(return_value={"receipt_id": "RCPT-999"})
    demand_mock = MagicMock(return_value={"ticket_id": "DF-OPS-1"})
    line_grill_mock = MagicMock(return_value={"resumed": True})
    accept_mock = MagicMock(return_value={"resumed": True, "sha": "a" * 40})
    cfg_ops = TelegramConfig(bot_token="tok_ops", role="ops", authorized_user_ids=[10])
    gw_ops = TelegramGateway(
        config=cfg_ops,
        state_dir=tmp_path / "ops",
        grill_handler=grill_mock,
        approval_handler=approval_mock,
        demand_handler=demand_mock,
        line_grill_handler=line_grill_mock,
        commercial_acceptance_handler=accept_mock,
    )

    res_grill = gw_ops.process_update(_msg(10, "/grill TICKET-1 Option B"))
    assert res_grill.resumed is True
    grill_mock.assert_called_once_with("TICKET-1", "Option B", 10)

    res_appr = gw_ops.process_update(_msg(11, "/approve darkfac sha256abcd"))
    assert res_appr.resumed is True and "RCPT-999" in res_appr.response_text
    approval_mock.assert_called_once_with("darkfac", "sha256abcd", 10)

    res_demand = gw_ops.process_update(_msg(12, "/demand Urgent security fix"))
    assert "DF-OPS-1" in res_demand.response_text
    demand_mock.assert_called_once()

    # Inline buttons: the line grill answer (the button of a worker-sent grill message) is recorded.
    res_cb = gw_ops.process_update(
        {"update_id": 13, "callback_query": {"id": "cb-line", "from": {"id": 10}, "data": "cb:grill:run-abc#q1:0"}}
    )
    assert res_cb.resumed is True and "Resposta registrada" in res_cb.response_text
    line_grill_mock.assert_called_once_with("run-abc", "q1", "0", 10)
    assert "@darkfac_bot" not in res_cb.response_text

    res_accept = gw_ops.process_update(
        {"update_id": 14, "callback_query": {"id": "cb-acc", "from": {"id": 10}, "data": "cb:accept:run-abc"}}
    )
    assert res_accept.resumed is True
    accept_mock.assert_called_once_with("run-abc", 10)


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
