"""USR-536 T2: idempotent Telegram outbox and confirmed delivery receipts."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.integrations.telegram import (
    OUTBOX_DELIVERED,
    OUTBOX_PENDING,
    OUTBOX_SENDING,
    OUTBOX_UNCERTAIN,
    TelegramConfig,
    TelegramGateway,
    delivery_fingerprint,
    outbox_item_key,
)
from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore

TOKEN = "123456789:AAHtestTokenValueShouldNotLeak"
NOTE = "notif_0123456789ab"
OTHER = "notif_abcdefabcdef"
TEXT = "UNIQUE_ALERT_TEXT_MARKER"


class _HttpResponse:
    status = 200

    def __init__(self, message_id: int = 41) -> None:
        self._message_id = message_id

    def read(self) -> bytes:
        body = {"ok": True, "result": {"message_id": self._message_id}}
        return json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_HttpResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _gateway(
    tmp_path: Path,
    *,
    chats: list[int] | None = None,
    role: str = "ops",
    token: str | None = TOKEN,
) -> TelegramGateway:
    config = TelegramConfig(
        bot_token=token,
        role=role,
        authorized_chat_ids=chats if chats is not None else [-5],
        authorized_user_ids=[],
    )
    return TelegramGateway(config=config, state_dir=tmp_path / "tg")


def _script_http(monkeypatch: pytest.MonkeyPatch, steps: list[object]) -> list[dict]:
    calls: list[dict] = []
    pending = list(steps)
    lock = threading.Lock()

    def fake(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        payload = json.loads(req.data.decode("utf-8"))
        with lock:
            calls.append(payload)
            step = pending.pop(0) if pending else None
        if step is None or isinstance(step, int):
            return _HttpResponse(41 if step is None else step)
        if isinstance(step, Exception):
            raise step
        status, description = step
        body = json.dumps({"ok": False, "error_code": status, "description": description}).encode("utf-8")
        raise urllib.error.HTTPError(req.full_url, status, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", fake)
    return calls


def _items(gateway: TelegramGateway) -> list[dict]:
    return json.loads(gateway.outbox_file.read_text(encoding="utf-8"))


def _service(tmp_path: Path, gateway: TelegramGateway, role: str = "ops") -> NotificationService:
    return NotificationService(
        store=NotificationStore(store_path=tmp_path / "notifications.jsonl"),
        telegram_service=gateway,
        telegram_role=role,
    )


def _notify(service: NotificationService, *, channel: NotificationChannel = NotificationChannel.ALL):
    return service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Fila parada",
        message=TEXT,
        channel=channel,
        force=True,
    )


def test_service_propagates_notification_id_and_records_telegram_only_after_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [77])
    gateway = _gateway(tmp_path, chats=[-100155], role="owner")
    service = _service(tmp_path, gateway, role="owner")
    event = _notify(service)
    assert event is not None
    assert calls[0]["chat_id"] == -100155
    saved = service.store.get_notification(event.notification_id)
    assert saved is not None
    assert event.delivered_channels == ["darkhub", "telegram"]
    assert saved.delivered_channels == ["darkhub", "telegram"]
    assert "telegram_outbox" not in event.delivered_channels
    stored = _items(gateway)
    assert stored[0]["notification_id"] == event.notification_id
    assert stored[0]["status"] == OUTBOX_DELIVERED
    assert stored[0]["message_id"] == 77
    assert gateway.get_status()["pending_outbox_notifications"] == 0


def test_service_failure_records_outbox_channel_without_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda _seconds: None)
    calls = _script_http(monkeypatch, [(503, "Service Unavailable")] * 3)
    gateway = _gateway(tmp_path, chats=[-5])
    service = _service(tmp_path, gateway)
    event = _notify(service)
    assert event is not None
    assert len(calls) == 3
    assert event.delivered_channels == ["darkhub", "telegram_outbox"]
    assert "telegram" not in event.delivered_channels
    stored = _items(gateway)
    assert len(stored) == 1
    assert stored[0]["notification_id"] == event.notification_id
    assert stored[0]["status"] == OUTBOX_PENDING
    assert stored[0]["key"] == outbox_item_key(event.notification_id, "ops", -5)


def test_missing_bot_records_telegram_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.notifications.service.load_telegram_config",
        lambda **_kwargs: TelegramConfig(bot_token=None),
    )
    service = NotificationService(
        store=NotificationStore(store_path=tmp_path / "notifications.jsonl"),
        telegram_service=None,
    )
    event = _notify(service)
    assert event is not None
    assert event.delivered_channels == ["darkhub", "telegram_outbox"]
    assert "telegram" not in event.delivered_channels


def test_telegram_only_failure_does_not_claim_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda _seconds: None)
    _script_http(monkeypatch, [(503, "Service Unavailable")] * 3)
    gateway = _gateway(tmp_path)
    service = _service(tmp_path, gateway)
    event = _notify(service, channel=NotificationChannel.TELEGRAM)
    assert event is not None
    assert event.delivered_channels == ["telegram_outbox"]


def test_repeated_enqueue_collapses_by_notification_role_and_destination(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, token="")
    gateway._enqueue_outbox(-5, "one", None, "category=transient", notification_id=NOTE)
    created = _items(gateway)[0]["created_at"]
    gateway._enqueue_outbox(-5, "two", None, "category=transient", notification_id=NOTE)
    gateway.config.role = "owner"
    gateway._enqueue_outbox(-5, "owner-copy", None, "category=transient", notification_id=NOTE)
    gateway._enqueue_outbox(-9, "other-chat", None, "category=transient", notification_id=NOTE)
    stored = _items(gateway)
    assert len(stored) == 3
    ops = next(item for item in stored if item["key"] == outbox_item_key(NOTE, "ops", -5))
    assert ops["text"] == "two"
    assert ops["created_at"] == created
    assert ops["status"] == OUTBOX_PENDING
    keys = {item["key"] for item in stored}
    assert outbox_item_key(NOTE, "owner", -5) in keys
    assert outbox_item_key(NOTE, "owner", -9) in keys
    assert gateway.get_status()["pending_outbox_notifications"] == 3


def test_failed_replace_keeps_the_previous_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _gateway(tmp_path, token="")
    gateway._enqueue_outbox(-5, "first", None, "category=transient", notification_id=NOTE)

    def boom(self: Path, _target: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    gateway._enqueue_outbox(-6, "second", None, "category=transient", notification_id=OTHER)
    stored = _items(gateway)
    assert [item["notification_id"] for item in stored] == [NOTE]
    assert TOKEN not in gateway.outbox_file.read_text(encoding="utf-8")


def test_success_stores_receipt_and_blocks_another_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [88, 89])
    gateway = _gateway(tmp_path)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is True
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is True
    assert len(calls) == 1
    stored = _items(gateway)
    assert stored[0]["status"] == OUTBOX_DELIVERED
    assert stored[0]["message_id"] == 88
    assert stored[0]["delivered_at"]
    assert stored[0]["fingerprint"] == delivery_fingerprint(TEXT)
    assert stored[0]["fingerprint"] != TEXT
    assert TOKEN not in stored[0]["fingerprint"]
    assert TOKEN not in gateway.outbox_file.read_text(encoding="utf-8")
    assert gateway.get_status()["pending_outbox_notifications"] == 0


def test_ambiguous_timeout_stays_uncertain_and_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [TimeoutError("The read operation timed out"), None])
    gateway = _gateway(tmp_path)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert gateway.replay_outbox() == 0
    assert len(calls) == 1
    stored = _items(gateway)
    assert stored[0]["status"] == OUTBOX_UNCERTAIN
    assert stored[0]["failure_reason"] == "category=uncertain"
    assert stored[0]["message_id"] is None
    assert gateway.get_status()["pending_outbox_notifications"] == 0
    assert TOKEN not in gateway.outbox_file.read_text(encoding="utf-8")


def test_transient_stays_pending_until_replay_delivers_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.integrations.telegram._retry_pause", lambda _seconds: None)
    calls = _script_http(monkeypatch, [(503, "Service Unavailable")] * 3 + [64])
    gateway = _gateway(tmp_path)
    assert gateway.send_message(-5, TEXT, notification_id=NOTE) is False
    assert gateway.get_status()["pending_outbox_notifications"] == 1
    assert _items(gateway)[0]["status"] == OUTBOX_PENDING
    restarted = _gateway(tmp_path)
    assert restarted.get_status()["pending_outbox_notifications"] == 1
    assert restarted.replay_outbox() == 1
    assert len(calls) == 4
    assert restarted.replay_outbox() == 0
    assert len(calls) == 4
    stored = _items(restarted)
    assert stored[0]["status"] == OUTBOX_DELIVERED
    assert stored[0]["message_id"] == 64
    assert stored[0]["fingerprint"] == delivery_fingerprint(TEXT)
    again = _gateway(tmp_path)
    assert again.replay_outbox() == 0
    assert _items(again)[0]["status"] == OUTBOX_DELIVERED


def test_concurrent_replay_claims_each_pending_item_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [])
    seen = threading.Lock()
    originals = calls

    def slow(req: urllib.request.Request, timeout: float = 10.0) -> _HttpResponse:
        payload = json.loads(req.data.decode("utf-8"))
        with seen:
            originals.append(payload)
        threading.Event().wait(0.02)
        return _HttpResponse(100 + len(originals))

    monkeypatch.setattr("core.integrations.telegram.urllib.request.urlopen", slow)
    gateway = _gateway(tmp_path, token="")
    expected: list[str] = []
    for index in range(4):
        note = f"notif_{index:012x}"
        expected.append(f"body-{index}")
        gateway._enqueue_outbox(-5, expected[-1], None, "category=transient", notification_id=note)
    gateway.config.bot_token = TOKEN
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            gateway.replay_outbox()
        except BaseException as exc:  # noqa: BLE001 - surface worker failures in the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(call["text"] for call in calls) == sorted(expected)
    assert len(calls) == 4
    stored = _items(gateway)
    assert len(stored) == 4
    assert {item["status"] for item in stored} == {OUTBOX_DELIVERED}
    assert gateway.get_status()["pending_outbox_notifications"] == 0


def test_restart_reclaims_a_dead_claim_and_keeps_a_live_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(monkeypatch, [51])
    gateway = _gateway(tmp_path, token="")
    gateway._enqueue_outbox(-5, TEXT, None, "category=transient", notification_id=NOTE)
    gateway._enqueue_outbox(-5, "held", None, "category=transient", notification_id=OTHER)
    raw = _items(gateway)
    raw[1]["status"] = OUTBOX_SENDING
    raw[1]["claim_token"] = "held-token"
    raw[1]["claim_pid"] = __import__("os").getpid()
    raw[1]["claimed_at"] = datetime.now(UTC).isoformat()
    gateway.outbox_file.write_text(json.dumps(raw), encoding="utf-8")

    revived = _gateway(tmp_path)
    assert revived.get_status()["pending_outbox_notifications"] == 1
    assert revived.replay_outbox() == 1
    assert len(calls) == 1
    stored = _items(revived)
    pending = next(item for item in stored if item["notification_id"] == NOTE)
    held = next(item for item in stored if item["notification_id"] == OTHER)
    assert pending["status"] == OUTBOX_DELIVERED
    assert pending["message_id"] == 51
    assert held["status"] == OUTBOX_SENDING

    raw = _items(revived)
    for item in raw:
        if item["notification_id"] == OTHER:
            item["claim_pid"] = 0
    revived.outbox_file.write_text(json.dumps(raw), encoding="utf-8")
    resumed = _gateway(tmp_path)
    assert resumed.replay_outbox() == 1
    finished = next(item for item in _items(resumed) if item["notification_id"] == OTHER)
    assert finished["status"] == OUTBOX_DELIVERED
    assert len(calls) == 2
