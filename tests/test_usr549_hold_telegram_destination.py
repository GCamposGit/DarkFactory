"""USR-549: hold alerts resolve the owner destination and surface missing setup."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from core.integrations.telegram import load_telegram_config
from core.line import human
from core.line.human import HumanRequest
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
    monkeypatch.delenv("TELEGRAM_AUTHORIZED_CHATS", raising=False)
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
