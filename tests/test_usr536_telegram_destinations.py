"""USR-536 T1: Telegram destination selection and sanitized HTTP failure categories."""

from __future__ import annotations

import io
import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.integrations.telegram import (
    TelegramConfig,
    TelegramGateway,
    classify_telegram_failure,
    load_telegram_config,
    parse_telegram_chat_ids,
    resolve_telegram_destinations,
)
from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore

TOKEN = "123456789:AAHtestTokenValueShouldNotLeak"
NOTE = "notif_0123456789ab"
TEXT = "UNIQUE_ALERT_TEXT_MARKER"
BODY_MARKER = "RAW_BODY_MARKER_DO_NOT_LOG"
_LOG_RE = re.compile(
    r"telegram_delivery notification_id=notif_[0-9a-f]{8,32} "
    r"role=(owner|ops|all|unknown) http_status=(none|\d{3}) "
    r"category=(missing_destination|invalid_entities|chat_not_found|bot_blocked|permanent|transient)"
)


class _HttpResponse:
    status = 200

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _gateway(
    tmp_path: Path,
    *,
    chats: list[int] | None = None,
    users: list[int] | None = None,
    role: str = "ops",
    token: str | None = TOKEN,
) -> TelegramGateway:
    config = TelegramConfig(
        bot_token=token,
        role=role,
        authorized_chat_ids=chats or [],
        authorized_user_ids=users or [],
    )
    return TelegramGateway(config=config, state_dir=tmp_path / "tg")


def _script_http(monkeypatch: pytest.MonkeyPatch, steps: list[object]) -> list[dict]:
    """Each step is None (HTTP 200), an exception, or (status, description)."""

    calls: list[dict] = []
    pending = list(steps)

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        calls.append(json.loads(req.data.decode("utf-8")))
        step = pending.pop(0)
        if step is None:
            return _HttpResponse()
        if isinstance(step, Exception):
            raise step
        status, description = step
        body = json.dumps(
            {
                "ok": False,
                "error_code": status,
                "description": description,
                "leak": BODY_MARKER,
            }
        ).encode("utf-8")
        raise urllib.error.HTTPError(req.full_url, status, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)
    return calls


def _delivery_logs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "darkfac.integrations.telegram"]


def test_parse_telegram_chat_ids_keeps_negative_ids_and_drops_zero() -> None:
    parsed = parse_telegram_chat_ids("-1001234567890, 42, 0, nope, --5, '-77'")
    assert parsed == [-1001234567890, 42, -77]


def test_load_telegram_config_reads_negative_authorized_chats(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "-1001234567890, 42")
    config = load_telegram_config(role="ops", config_dir=tmp_path / "telegram")
    assert config.authorized_chat_ids == [-1001234567890, 42]

    monkeypatch.delenv("TELEGRAM_AUTHORIZED_CHATS")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHATS", "-88")
    alias = load_telegram_config(role="ops", config_dir=tmp_path / "telegram")
    assert alias.authorized_chat_ids == [-88]


def test_resolve_destinations_prefers_chats_and_never_returns_zero() -> None:
    both = TelegramConfig(bot_token=TOKEN, authorized_chat_ids=[-9, 0], authorized_user_ids=[8])
    assert resolve_telegram_destinations(both) == [-9]
    users_only = TelegramConfig(bot_token=TOKEN, authorized_chat_ids=[0], authorized_user_ids=[0, 8])
    assert resolve_telegram_destinations(users_only) == [8]
    assert resolve_telegram_destinations(TelegramConfig(bot_token=TOKEN)) == []


def test_missing_destination_does_not_call_send_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _script_http(monkeypatch, [])
    gateway = _gateway(tmp_path, role="ops")
    sent: list[object] = []
    gateway.send_message = lambda *args, **kwargs: sent.append((args, kwargs)) or True  # type: ignore[method-assign]
    service = NotificationService(
        store=NotificationStore(store_path=tmp_path / "notifications.jsonl"),
        telegram_service=gateway,
        telegram_role="ops",
    )
    caplog.set_level(logging.DEBUG)
    event = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Disco cheio",
        message=TEXT,
        channel=NotificationChannel.TELEGRAM,
        force=True,
    )
    assert event is not None
    assert sent == []
    assert calls == []
    assert "telegram_outbox" not in event.delivered_channels
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    messages = _delivery_logs(caplog)
    assert messages == [
        f"telegram_delivery notification_id={event.notification_id} role=ops http_status=none category=missing_destination"
    ]
    blob = caplog.text
    assert TOKEN not in blob
    assert TEXT not in blob
    assert BODY_MARKER not in blob
    assert "chat_id" not in blob


def test_send_message_rejects_chat_zero_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _script_http(monkeypatch, [None])
    gateway = _gateway(tmp_path, chats=[-100], token="")
    caplog.set_level(logging.DEBUG)
    delivered = gateway.send_message(0, TEXT, notification_id=NOTE)
    assert delivered is False
    assert calls == []
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    assert _delivery_logs(caplog) == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=none category=missing_destination"
    ]


def test_missing_token_still_queues_outbox_for_a_real_chat(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, users=[1001], token="")
    assert gateway.send_message(1001, "Critical alert: build failed") is False
    assert gateway.get_status()["pending_outbox_notifications"] == 1


@pytest.mark.parametrize(
    ("status", "description", "category"),
    [
        (400, "Bad Request: chat not found", "chat_not_found"),
        (400, "Bad Request: user not found", "chat_not_found"),
        (400, "Bad Request: PEER_ID_INVALID", "chat_not_found"),
        (403, "Forbidden: bot was blocked by the user", "bot_blocked"),
        (403, "Forbidden: bot was kicked from the supergroup chat", "bot_blocked"),
        (400, "Bad Request: message is too long", "permanent"),
        (401, "Unauthorized", "permanent"),
        (403, "Forbidden: bot can't initiate conversation with a user", "permanent"),
    ],
)
def test_permanent_telegram_errors_are_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    description: str,
    category: str,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(monkeypatch, [(status, description), None, None, None])
    gateway = _gateway(tmp_path, chats=[-1009876543210], role="owner")
    caplog.set_level(logging.DEBUG)
    delivered = gateway.send_message(
        -1009876543210,
        TEXT,
        buttons=[[{"text": "Abrir", "url": "https://darkhub.example/alert"}]],
        notification_id=NOTE,
    )
    assert delivered is False
    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["chat_id"] == -1009876543210
    assert pauses == []
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    assert _delivery_logs(caplog) == [
        f"telegram_delivery notification_id={NOTE} role=owner http_status={status} category={category}"
    ]
    blob = caplog.text
    assert TOKEN not in blob
    assert TEXT not in blob
    assert BODY_MARKER not in blob
    assert description not in blob
    assert "chat_id" not in blob
    assert "-1009876543210" not in blob


def test_invalid_entities_retries_once_without_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(
        monkeypatch,
        [(400, "Bad Request: can't parse entities: Unsupported start tag \"b\" at byte offset 3"), None],
    )
    gateway = _gateway(tmp_path, chats=[-5])
    buttons = [[{"text": "Abrir", "url": "https://darkhub.example/alert"}]]
    caplog.set_level(logging.DEBUG)
    delivered = gateway.send_message(-5, TEXT, buttons=buttons, notification_id=NOTE)
    assert delivered is True
    assert len(calls) == 2
    assert calls[0]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]
    assert calls[1]["reply_markup"] == {"inline_keyboard": buttons}
    assert calls[1]["text"] == TEXT
    assert pauses == []
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    assert _delivery_logs(caplog) == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=400 category=invalid_entities"
    ]
    assert _LOG_RE.fullmatch(_delivery_logs(caplog)[0])


def test_invalid_entities_plain_permanent_does_not_enter_the_retry_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(
        monkeypatch,
        [
            (400, "Bad Request: can't parse entities"),
            (400, "Bad Request: chat not found"),
            None,
        ],
    )
    gateway = _gateway(tmp_path, chats=[-5])
    caplog.set_level(logging.DEBUG)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert len(calls) == 2
    assert pauses == []
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    assert _delivery_logs(caplog) == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=400 category=invalid_entities",
        f"telegram_delivery notification_id={NOTE} role=ops http_status=400 category=chat_not_found",
    ]


def test_invalid_entities_plain_transient_is_queued_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(
        monkeypatch,
        [
            (400, "Bad Request: can't find end of the entity starting at byte offset 1"),
            (503, "Service Unavailable"),
            None,
        ],
    )
    gateway = _gateway(tmp_path, chats=[-5])
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert len(calls) == 2
    assert pauses == []
    assert gateway.get_status()["pending_outbox_notifications"] == 1
    stored = json.loads(gateway.outbox_file.read_text(encoding="utf-8"))
    assert stored[0]["notification_id"] == NOTE
    assert stored[0]["failure_reason"] == "category=transient"
    assert TOKEN not in gateway.outbox_file.read_text(encoding="utf-8")
    assert BODY_MARKER not in stored[0]["failure_reason"]


def test_transient_errors_retry_three_times_then_keep_one_outbox_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(monkeypatch, [(503, "Service Unavailable")] * 3)
    gateway = _gateway(tmp_path, chats=[-5], role="ops")
    caplog.set_level(logging.DEBUG)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert len(calls) == 3
    assert pauses == [0.8, 1.6]
    assert gateway.get_status()["pending_outbox_notifications"] == 1
    messages = _delivery_logs(caplog)
    assert messages == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=503 category=transient"
    ] * 3
    assert all(_LOG_RE.fullmatch(message) for message in messages)
    assert TOKEN not in caplog.text
    assert TEXT not in caplog.text
    assert BODY_MARKER not in caplog.text


def test_transient_success_on_second_attempt_does_not_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pauses: list[float] = []
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda seconds: pauses.append(seconds))
    calls = _script_http(monkeypatch, [(429, "Too Many Requests"), None])
    gateway = _gateway(tmp_path, chats=[-5])
    assert gateway.send_message(-5, "ok") is True
    assert len(calls) == 2
    assert pauses == [0.8]
    assert gateway.get_status()["pending_outbox_notifications"] == 0


def test_transport_error_is_transient_and_does_not_log_the_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen_urls: list[str] = []

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        seen_urls.append(req.full_url)
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda _seconds: None)
    gateway = _gateway(tmp_path, chats=[-5])
    caplog.set_level(logging.DEBUG)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert len(seen_urls) == 3
    assert all(TOKEN in url for url in seen_urls)
    assert gateway.get_status()["pending_outbox_notifications"] == 1
    messages = _delivery_logs(caplog)
    assert messages == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=none category=transient"
    ] * 3
    assert TOKEN not in caplog.text
    assert TEXT not in caplog.text


def test_non_json_400_is_permanent_and_body_is_not_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            None,
            io.BytesIO(b"not-json LEAK_PLAIN"),
        )

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)
    gateway = _gateway(tmp_path, chats=[-5])
    caplog.set_level(logging.DEBUG)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert _delivery_logs(caplog) == [
        f"telegram_delivery notification_id={NOTE} role=ops http_status=400 category=permanent"
    ]
    assert "LEAK_PLAIN" not in caplog.text
    assert TOKEN not in caplog.text


def test_send_message_boolean_interface_accepts_positional_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [None])
    gateway = _gateway(tmp_path, chats=[-5])
    assert gateway.send_message(-5, "ola") is True
    assert calls[0]["chat_id"] == -5
    assert calls[0]["parse_mode"] == "HTML"


def test_operational_alert_posts_to_the_configured_negative_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "-100155")
    config = load_telegram_config(role="owner", config_dir=tmp_path / "telegram")
    gateway = TelegramGateway(config=config, state_dir=tmp_path / "state")
    calls = _script_http(monkeypatch, [None])
    service = NotificationService(
        store=NotificationStore(store_path=tmp_path / "notifications.jsonl"),
        telegram_service=gateway,
        telegram_role="owner",
    )
    event = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Fila parada",
        message=TEXT,
        channel=NotificationChannel.ALL,
        force=True,
    )
    assert event is not None
    assert len(calls) == 1
    assert calls[0]["chat_id"] == -100155
    assert calls[0]["parse_mode"] == "HTML"
    assert "telegram" in event.delivered_channels
    assert gateway.get_status()["pending_outbox_notifications"] == 0


def test_classify_telegram_failure_hides_the_body() -> None:
    body = json.dumps(
        {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: can't parse entities: " + BODY_MARKER,
        }
    )
    assert classify_telegram_failure(400, body) == "invalid_entities"
    assert BODY_MARKER not in classify_telegram_failure(400, body)
    assert classify_telegram_failure(400, "not-json " + BODY_MARKER) == "permanent"
    assert classify_telegram_failure(500, body) == "transient"
    assert classify_telegram_failure(None, body) == "transient"
