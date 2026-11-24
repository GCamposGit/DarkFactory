"""USR-536 T3: sanitized Telegram diagnosis and one-shot replay."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.integrations.telegram import OUTBOX_PENDING, OUTBOX_UNCERTAIN, TelegramConfig
from core.notifications.models import AlertCategory, AlertSeverity, NotificationEvent
from core.notifications.store import NotificationStore
from core.notifications.telegram_delivery import dump_public, main, run_delivery
from scripts import telegram_delivery_check as cli

TOKEN = "123456789:AAHtestTokenValueShouldNotLeak"
NOTE = "notif_3e46d90ee946"
CHAT = -100155009988
OTHER_CHAT = -100155009977
TITLE = "SECRET_TITLE_MARKER"
MESSAGE = "SECRET_BODY_MARKER"
BODY = "RAW_BODY_MARKER_DO_NOT_PRINT"


class _HttpResponse:
    status = 200

    def __init__(self, message_id: int = 77) -> None:
        self._message_id = message_id

    def read(self) -> bytes:
        body = {"ok": True, "result": {"message_id": self._message_id}}
        return json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sendMessage")

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", boom)


def _script_http(monkeypatch: pytest.MonkeyPatch, steps: list[object]) -> list[dict]:
    calls: list[dict] = []
    pending = list(steps)

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        calls.append(json.loads(req.data.decode("utf-8")))
        step = pending.pop(0) if pending else None
        if step is None or isinstance(step, int):
            return _HttpResponse(77 if step is None else step)
        status, description = step
        body = json.dumps(
            {"ok": False, "error_code": status, "description": description, "leak": BODY}
        ).encode("utf-8")
        raise urllib.error.HTTPError(req.full_url, status, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)
    return calls


def _config(
    *,
    token: str | None = TOKEN,
    chats: list[int] | None = None,
    users: list[int] | None = None,
) -> TelegramConfig:
    return TelegramConfig(
        bot_token=token,
        role="owner",
        authorized_chat_ids=chats if chats is not None else [CHAT, OTHER_CHAT],
        authorized_user_ids=users or [],
    )


def _store(tmp_path: Path, *, message: str = MESSAGE, details: dict | None = None) -> NotificationStore:
    store = NotificationStore(store_path=tmp_path / "notifications.jsonl")
    store.add_notification(
        NotificationEvent(
            notification_id=NOTE,
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.CRITICAL,
            title=TITLE,
            message=message,
            details=details or {"chat_id": CHAT, "ticket": "USR-536"},
        )
    )
    return store


def _assert_private_hidden(text: str) -> None:
    assert TOKEN not in text
    assert TITLE not in text
    assert MESSAGE not in text
    assert BODY not in text
    assert str(CHAT) not in text
    assert str(OTHER_CHAT) not in text
    assert "chat_id" not in text
    assert "bot_token" not in text


def _run(
    tmp_path: Path,
    store: NotificationStore,
    config: TelegramConfig,
    *,
    replay: bool,
    notification_id: str = NOTE,
) -> tuple[int, dict]:
    return run_delivery(
        notification_id,
        replay=replay,
        store=store,
        config=config,
        state_dir=tmp_path / "tg",
    )


def test_invalid_id_refuses_without_loading_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> TelegramConfig:
        raise AssertionError("config loaded")

    monkeypatch.setattr("core.notifications.telegram_delivery.load_owner_config", boom)
    monkeypatch.setattr("core.notifications.telegram_delivery.open_store", boom)
    code, payload = run_delivery(f"nope-{TOKEN}")
    rendered = dump_public(payload)
    assert code == 2
    assert payload["status"] == "refused"
    assert payload["reason"] == "invalid_notification_id"
    assert TOKEN not in rendered
    assert payload["notification_id"] == "notif_unknown"


def test_missing_notification_does_not_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_http(monkeypatch)
    store = NotificationStore(store_path=tmp_path / "notifications.jsonl")
    code, payload = _run(tmp_path, store, _config(), replay=True, notification_id="notif_0123456789ab")
    rendered = dump_public(payload)
    assert code == 2
    assert payload["status"] == "not_found"
    assert payload["reason"] == "notification_not_found"
    assert payload["event_found"] is False
    _assert_private_hidden(rendered)


def test_missing_token_refuses_without_revealing_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_http(monkeypatch)
    store = _store(tmp_path, message=f"{MESSAGE} {TOKEN}")
    code, payload = _run(tmp_path, store, _config(token=None), replay=True)
    rendered = dump_public(payload)
    assert code == 2
    assert payload == {
        "notification_id": NOTE,
        "role": "owner",
        "status": "refused",
        "reason": "missing_token",
        "event_found": True,
        "token_present": False,
        "destination_present": True,
    }
    _assert_private_hidden(rendered)
    assert not (tmp_path / "tg" / "outbox_owner.json").exists()


def test_missing_destination_refuses_without_revealing_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _forbid_http(monkeypatch)
    store = _store(tmp_path)
    code, payload = _run(tmp_path, store, _config(chats=[0], users=[0]), replay=False)
    rendered = dump_public(payload)
    assert code == 2
    assert payload["status"] == "refused"
    assert payload["reason"] == "missing_destination"
    assert payload["token_present"] is True
    assert payload["destination_present"] is False
    _assert_private_hidden(rendered)


def test_diagnose_is_ready_and_does_not_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_http(monkeypatch)
    code, payload = _run(tmp_path, _store(tmp_path), _config(), replay=False)
    rendered = dump_public(payload)
    assert code == 0
    assert payload["status"] == "ready"
    assert payload["role"] == "owner"
    assert payload["token_present"] is True
    assert payload["destination_present"] is True
    assert payload["event_found"] is True
    assert payload["outbox_status"] == "absent"
    _assert_private_hidden(rendered)


def test_replay_delivers_once_and_second_run_is_already_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _script_http(monkeypatch, [41])
    store = _store(tmp_path)
    code, payload = _run(tmp_path, store, _config(), replay=True)
    rendered = dump_public(payload)
    assert code == 0
    assert payload["notification_id"] == NOTE
    assert payload["role"] == "owner"
    assert payload["status"] == "delivered"
    assert payload["message_id"] == 41
    assert payload["timestamp"]
    assert set(payload) == {"notification_id", "role", "status", "message_id", "timestamp"}
    assert calls[0]["chat_id"] == CHAT
    assert MESSAGE in calls[0]["text"]
    assert len(calls) == 1
    _assert_private_hidden(rendered)

    code_again, again = _run(tmp_path, store, _config(), replay=True)
    assert code_again == 0
    assert again["status"] == "already_delivered"
    assert again["message_id"] == 41
    assert again["timestamp"] == payload["timestamp"]
    assert len(calls) == 1
    _assert_private_hidden(dump_public(again))


def test_transient_failure_keeps_the_pending_row_and_other_alerts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda _seconds: None)
    calls = _script_http(monkeypatch, [(503, "Service Unavailable")] * 3)
    state = tmp_path / "tg"
    state.mkdir()
    other = "notif_abcdefabcdef"
    (state / "outbox_owner.json").write_text(
        json.dumps(
            [
                {
                    "notification_id": other,
                    "chat_id": OTHER_CHAT,
                    "role": "owner",
                    "text": "OTHER_ALERT",
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "delivered": False,
                    "failure_reason": "category=transient",
                    "status": OUTBOX_PENDING,
                }
            ]
        ),
        encoding="utf-8",
    )
    code, payload = _run(tmp_path, _store(tmp_path), _config(chats=[CHAT]), replay=True)
    rendered = dump_public(payload)
    assert code == 1
    assert payload["status"] == "failed"
    assert payload["reason"] == "transient"
    assert len(calls) == 3
    stored = json.loads((state / "outbox_owner.json").read_text(encoding="utf-8"))
    by_id = {item["notification_id"]: item for item in stored}
    assert set(by_id) == {NOTE, other}
    assert by_id[NOTE]["status"] == OUTBOX_PENDING
    assert by_id[other]["text"] == "OTHER_ALERT"
    assert by_id[other]["status"] == OUTBOX_PENDING
    _assert_private_hidden(rendered)
    assert BODY not in (state / "outbox_owner.json").read_text(encoding="utf-8")


def test_uncertain_row_is_not_sent_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_http(monkeypatch)
    state = tmp_path / "tg"
    state.mkdir()
    (state / "outbox_owner.json").write_text(
        json.dumps(
            [
                {
                    "notification_id": NOTE,
                    "chat_id": CHAT,
                    "role": "owner",
                    "text": MESSAGE,
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "delivered": False,
                    "failure_reason": "category=uncertain",
                    "status": OUTBOX_UNCERTAIN,
                }
            ]
        ),
        encoding="utf-8",
    )
    code, payload = _run(tmp_path, _store(tmp_path), _config(chats=[CHAT]), replay=True)
    assert code == 1
    assert payload["status"] == "uncertain"
    assert payload["reason"] == "uncertain"
    _assert_private_hidden(dump_public(payload))


def test_permanent_http_error_does_not_echo_the_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script_http(monkeypatch, [(400, f"chat not found {BODY}")])
    code, payload = _run(tmp_path, _store(tmp_path), _config(chats=[CHAT]), replay=True)
    rendered = dump_public(payload)
    assert code == 1
    assert payload["status"] == "failed"
    assert payload["reason"] == "permanent"
    assert len(calls) == 1
    _assert_private_hidden(rendered)


def test_cli_replay_prints_only_the_public_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _script_http(monkeypatch, [88])
    store = _store(tmp_path)
    config = _config(chats=[CHAT])
    monkeypatch.setattr("core.notifications.telegram_delivery.load_owner_config", lambda: config)
    monkeypatch.setattr("core.notifications.telegram_delivery.open_store", lambda: store)
    monkeypatch.setattr("core.notifications.telegram_delivery.gateway_state_dir", lambda: tmp_path / "tg")
    code = cli.main(["--notification-id", NOTE, "--replay"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["status"] == "delivered"
    assert payload["message_id"] == 88
    assert payload["role"] == "owner"
    assert cli.main is main
    combined = captured.out + captured.err
    _assert_private_hidden(combined)
    assert len(calls) == 1

    code_again = cli.main(["--notification-id", NOTE, "--replay"])
    second = json.loads(capsys.readouterr().out)
    assert code_again == 0
    assert second["status"] == "already_delivered"
    assert second["message_id"] == 88
    assert len(calls) == 1


def test_dump_public_strips_private_keys() -> None:
    rendered = dump_public(
        {
            "notification_id": NOTE,
            "role": "owner",
            "status": "delivered",
            "message_id": 5,
            "timestamp": "2025-01-01T00:00:00+00:00",
            "title": TITLE,
            "message": MESSAGE,
            "chat_id": CHAT,
            "bot_token": TOKEN,
            "text": BODY,
        }
    )
    parsed = json.loads(rendered)
    assert set(parsed) == {"notification_id", "role", "status", "message_id", "timestamp"}
    _assert_private_hidden(rendered)
