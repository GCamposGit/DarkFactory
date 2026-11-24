"""USR-549: hold alerts resolve the owner destination and surface missing setup."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from core.integrations.telegram import load_telegram_config, parse_telegram_chat_ids
from core.line import human
from core.line.failure_alerts import notify_ticket_hold
from core.line.human import HumanRequest
from core.notifications.store import NotificationStore
from core.owner_actions.store import OwnerActionStore

TOKEN = "123456789:AAHtestTokenValueShouldNotLeak"


class _HttpResponse:
    status = 200

    def read(self) -> bytes:
        return b'{"ok":true,"result":{"message_id":93}}'

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _request() -> HumanRequest:
    return HumanRequest(
        kind="infra",
        run_id="run-usr-549",
        blocking_stage="build",
        guide_md="A linha aguarda a resolucao do bloqueio.\n\n1. Verifique o alerta no Hub.",
    )


def test_owner_chat_id_is_used_as_owner_destination_and_precedes_shared_chats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")

    owner = load_telegram_config(role="owner", config_dir=tmp_path / "telegram")
    ops = load_telegram_config(role="ops", config_dir=tmp_path / "telegram")

    assert owner.authorized_chat_ids == [-1001234567890]
    assert ops.authorized_chat_ids == [42]


def test_owner_destination_does_not_fall_back_to_shared_ops_chats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")

    owner = load_telegram_config(role="owner", config_dir=tmp_path / "telegram")
    hold_owner = load_telegram_config(
        role="owner", config_dir=tmp_path / "telegram", owner_destination_only=True
    )

    assert owner.authorized_chat_ids == [42]
    assert hold_owner.authorized_chat_ids == []
    assert hold_owner.authorized_user_ids == []


def test_owner_chat_id_parser_accepts_negative_and_rejects_zero() -> None:
    assert parse_telegram_chat_ids("-100155009988,0,not-a-chat") == [-100155009988]


def test_hold_owner_destination_does_not_fall_back_to_local_shared_chat_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_AUTHORIZED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    config_dir = tmp_path / "telegram"
    config_dir.mkdir()
    (config_dir / "owner_config.json").write_text(
        '{"authorized_chat_ids": [42]}', encoding="utf-8"
    )

    hold_owner = load_telegram_config(
        role="owner", config_dir=config_dir, owner_destination_only=True
    )

    assert hold_owner.authorized_chat_ids == []


def test_hold_request_sends_to_configured_owner_and_is_reported_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "-100155009988")
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(tmp_path / "owner_actions.json"))
    monkeypatch.setattr(human, "save_request", lambda *_args: tmp_path / "HUMAN_REQUEST.json")
    calls: list[dict[str, object]] = []

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        calls.append(json.loads(req.data.decode("utf-8")))
        return _HttpResponse()

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)

    human.request_human_help(object(), _request())  # type: ignore[arg-type]

    assert len(calls) == 1
    assert calls[0]["chat_id"] == -100155009988
    assert "A linha aguarda a resolucao do bloqueio" in str(calls[0]["text"])
    assert OwnerActionStore(tmp_path / "owner_actions.json").list_actions()
    rows = (tmp_path / "state" / "notifications" / "notifications.jsonl").read_text(encoding="utf-8")
    assert '"delivered_channels": ["darkhub", "telegram"]' in rows
    assert TOKEN not in rows


def test_missing_owner_destination_creates_hub_action_with_setting_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    actions_path = tmp_path / "owner_actions.json"
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(actions_path))
    monkeypatch.setattr(human, "save_request", lambda *_args: tmp_path / "HUMAN_REQUEST.json")

    human.request_human_help(object(), _request())  # type: ignore[arg-type]

    actions = OwnerActionStore(actions_path).list_actions()
    setup = next(action for action in actions if action.id == "OA-TELEGRAM-OWNER-DESTINATION")
    assert setup.priority.value == "high"
    assert "TELEGRAM_OWNER_CHAT_ID" in setup.steps[0].text
    assert "TELEGRAM_OWNER_BOT_TOKEN" not in setup.model_dump_json()


def test_default_hold_sender_selects_owner_notification_role(monkeypatch: pytest.MonkeyPatch) -> None:
    roles: list[str] = []

    class _Event:
        delivered_channels = ["telegram"]

    class _NotificationService:
        def __init__(self, *, telegram_role: str, owner_destination_only: bool) -> None:
            roles.append(telegram_role)
            assert owner_destination_only is True

        def notify(self, **_kwargs: object) -> _Event:
            return _Event()

    monkeypatch.setattr("core.notifications.service.NotificationService", _NotificationService)

    sender = human._default_sender()

    assert sender is not None
    assert sender("hold alert") is True
    assert roles == ["owner"]


def test_ticket_hold_sends_to_dedicated_owner_chat_through_notification_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    chat_id = "-100155009988"
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", chat_id)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "1234")
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("core.integrations.telegram._read_env_fallback", lambda *_args: {})
    calls: list[dict[str, object]] = []

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        calls.append(json.loads(req.data.decode("utf-8")))
        return _HttpResponse()

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)

    notify_ticket_hold("USR-549-positive", "candidate_failed", "hold detail", "hold.log")

    events = NotificationStore().list_notifications()
    assert len(calls) == 1
    assert calls[0]["chat_id"] == int(chat_id)
    assert len(events) == 1
    assert events[0].delivered_channels == ["darkhub", "telegram"]
    assert chat_id not in events[0].model_dump_json()
    assert chat_id not in caplog.text


def test_ticket_hold_without_owner_destination_persists_visible_remediation_without_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shared_chat = "42"
    shared_user = "1234"
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", shared_chat)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", shared_user)
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("core.integrations.telegram._read_env_fallback", lambda *_args: {})
    calls: list[urllib.request.Request] = []

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        calls.append(req)
        return _HttpResponse()

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)

    notify_ticket_hold("USR-549-missing", "candidate_failed", "hold detail", "hold.log")

    rows_path = tmp_path / "state" / "notifications" / "notifications.jsonl"
    event = NotificationStore().list_notifications()[0]
    assert len(calls) == 0
    assert event.delivered_channels == ["darkhub"]
    assert "missing_destination" in caplog.text
    assert "TELEGRAM_OWNER_CHAT_ID" in event.message
    # The full JSON also contains a timestamp, whose fractional seconds can
    # coincidentally contain short IDs such as "42". Check all persisted alert
    # fields while excluding only that unrelated timestamp.
    persisted_fields = event.model_dump_json(exclude={"timestamp"})
    assert shared_chat not in persisted_fields
    assert shared_user not in persisted_fields
    assert TOKEN not in rows_path.read_text(encoding="utf-8")
    assert not list((tmp_path / "state").rglob("*outbox*.json"))
