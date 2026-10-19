"""Alerta do loop de sincronizacao de cotas decidido no Hub (USR-235). Relogio injetado, sem rede."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest
from fastapi.testclient import TestClient

from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService
from hub.backend.usage_sync_watch import (
    CAUSE_KEY_MISSING,
    CAUSE_STALE,
    CAUSE_UNAUTHORIZED,
    UsageSyncWatch,
)

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
SECRET = "chave-de-teste-nao-real"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: int) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def sent() -> List[str]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(tmp_path: Path, clock: Clock, sent: List[str], monkeypatch: pytest.MonkeyPatch) -> HubService:
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", SECRET)
    svc = HubService(data_dir=tmp_path / "data")
    svc.usage_sync_watch = UsageSyncWatch(
        tmp_path / "state.json", clock=clock, notifier=lambda text: sent.append(text) or True
    )
    return svc


@pytest.fixture
def client(service: HubService):
    app.dependency_overrides[get_hub_service] = lambda: service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


PAYLOAD = {"client_node_id": "node-test", "accounts": [], "credits": []}


def _sync(client: TestClient, key: str | None):
    headers = {"X-DarkFac-Telemetry-Key": key} if key else {}
    return client.post("/api/usage/sync", json=PAYLOAD, headers=headers)


def _usage_items(service: HubService) -> list:
    report = service.get_priority_interventions()
    return [i for i in report.items if i.metadata.get("created_by") == "usage_sync_watch"]


def test_no_history_no_alert(service: HubService, sent: List[str]) -> None:
    assert _usage_items(service) == []
    assert sent == []


def test_missing_key_alerts(client: TestClient, service: HubService, sent: List[str], monkeypatch) -> None:
    monkeypatch.delenv("DARKFAC_TELEMETRY_KEY")
    assert _sync(client, "qualquer").status_code == 503
    items = _usage_items(service)
    assert len(items) == 1
    assert items[0].urgency == "critical"
    assert items[0].metadata["cause"] == CAUSE_KEY_MISSING
    assert len(sent) == 1


def test_401_alerts_without_leaking_key(client: TestClient, service: HubService, sent: List[str]) -> None:
    assert _sync(client, "errada-xyz").status_code == 401
    items = _usage_items(service)
    assert [i.metadata["cause"] for i in items] == [CAUSE_UNAUTHORIZED]
    blob = repr(items) + "".join(sent) + service.usage_sync_watch.state_path.read_text(encoding="utf-8")
    assert SECRET not in blob and "errada-xyz" not in blob


def test_stale_after_five_minutes(client: TestClient, service: HubService, clock: Clock, sent: List[str]) -> None:
    assert _sync(client, SECRET).status_code == 200
    clock.advance(minutes=5)
    assert _usage_items(service) == []
    clock.advance(seconds=1)
    items = _usage_items(service)
    assert [i.metadata["cause"] for i in items] == [CAUSE_STALE]
    assert items[0].urgency == "critical"
    assert len(sent) == 1


def test_dedup_notifies_once_per_cause(client: TestClient, service: HubService, clock: Clock, sent: List[str]) -> None:
    _sync(client, "errada")
    for _ in range(3):
        _sync(client, "errada")
        assert len(_usage_items(service)) == 1
    clock.advance(hours=1)
    assert len(_usage_items(service)) == 1
    assert len(sent) == 1


def test_auto_close_and_rearm(client: TestClient, service: HubService, clock: Clock, sent: List[str]) -> None:
    _sync(client, "errada")
    assert len(_usage_items(service)) == 1
    clock.advance(minutes=1)
    assert _sync(client, SECRET).status_code == 200
    assert _usage_items(service) == []
    # a new failure after recovery notifies again
    clock.advance(minutes=10)
    assert len(_usage_items(service)) == 1
    assert len(sent) == 2


def test_notifier_failure_does_not_break_queue(tmp_path: Path, clock: Clock) -> None:
    def boom(text: str) -> bool:
        raise RuntimeError("sem rede")

    watch = UsageSyncWatch(tmp_path / "s.json", clock=clock, notifier=boom)
    watch.record_refusal(CAUSE_UNAUTHORIZED)
    assert watch.evaluate() is not None
