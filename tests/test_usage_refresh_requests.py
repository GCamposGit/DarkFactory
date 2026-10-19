"""USR-234: Hub refresh button creates a persistent request that the metering host closes (no network)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.usage.refresh_requests import RefreshRequestStore
from hub.backend.api import get_hub_service, router
from hub.backend.service import HubService
from scripts.sync_usage_to_cloud import sync_with_refresh_requests

KEY = "test-telemetry-key"
NOW = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", KEY)
    store = RefreshRequestStore(tmp_path / "usage" / "refresh_requests.json", clock=lambda: NOW)
    service = SimpleNamespace(refresh_request_store=store, _probes_locally=False)
    service.request_account_refresh = lambda node=None: HubService.request_account_refresh(service, node)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hub_service] = lambda: service
    return TestClient(app)


def _hdr() -> dict[str, str]:
    return {"X-DarkFac-Telemetry-Key": KEY}


def _host(client: TestClient, collect: Any, send: Any) -> Any:
    def close(rid: str, status: str, **kw: Any) -> bool:
        body = {"status": status, **kw}
        return client.post(
            f"/api/usage/accounts/refresh-requests/{rid}/update", json=body, headers=_hdr()
        ).status_code == 200

    def fetch() -> list[dict[str, Any]]:
        return client.get("/api/usage/accounts/refresh-requests/pending", headers=_hdr()).json()

    return lambda: sync_with_refresh_requests("http://hub", KEY, "n", collect, send, fetch, close)


def test_post_creates_persistent_pending_request_without_probing(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()
    assert created["status"] == "pending"
    assert created["requested_at"] == NOW.isoformat()
    assert created["failure_reason"] is None
    polled = client.get(f"/api/usage/accounts/refresh/{created['id']}").json()
    assert polled["id"] == created["id"] and polled["status"] == "pending"
    assert client.get("/api/usage/accounts/refresh/nope").status_code == 404


def test_machine_endpoints_require_key(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()
    assert client.get("/api/usage/accounts/refresh-requests/pending").status_code == 401
    assert (
        client.get("/api/usage/accounts/refresh-requests/pending", headers={"X-DarkFac-Telemetry-Key": "bad"}).status_code
        == 401
    )
    url = f"/api/usage/accounts/refresh-requests/{created['id']}/update"
    assert client.post(url, json={"status": "done"}).status_code == 401
    assert [r["id"] for r in client.get("/api/usage/accounts/refresh-requests/pending", headers=_hdr()).json()] == [
        created["id"]
    ]


def test_missing_server_key_disables_machine_endpoints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_TELEMETRY_KEY")
    assert client.get("/api/usage/accounts/refresh-requests/pending", headers=_hdr()).status_code == 503


def test_host_loop_closes_request_done_with_checked_at(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()
    stamp = "2030-01-02T03:05:00+00:00"
    seen: list[str] = []

    def send(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(client.get(f"/api/usage/accounts/refresh/{created['id']}").json()["status"])
        return {"accounts_updated": 1}

    result = _host(client, lambda: {"timestamp": stamp}, send)()
    assert result == {"accounts_updated": 1}
    assert seen == ["in_progress"]
    final = client.get(f"/api/usage/accounts/refresh/{created['id']}").json()
    assert final["status"] == "done" and final["checked_at"] == stamp
    assert client.get("/api/usage/accounts/refresh-requests/pending", headers=_hdr()).json() == []


def test_host_failure_closes_request_failed_with_reason_and_redacts_key(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()

    def send(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"HTTP 502 using {KEY}")

    with pytest.raises(RuntimeError):
        _host(client, lambda: {"timestamp": "x"}, send)()
    final = client.get(f"/api/usage/accounts/refresh/{created['id']}").json()
    assert final["status"] == "failed"
    assert "HTTP 502" in final["failure_reason"] and KEY not in final["failure_reason"]


def test_without_pending_requests_loop_just_syncs(client: TestClient) -> None:
    assert _host(client, lambda: {"timestamp": "x"}, lambda p: {"ok": True})() == {"ok": True}


def test_offline_host_keeps_request_pending_and_closed_requests_are_terminal(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()
    assert client.get(f"/api/usage/accounts/refresh/{created['id']}").json()["status"] == "pending"
    url = f"/api/usage/accounts/refresh-requests/{created['id']}/update"
    client.post(url, json={"status": "failed", "failure_reason": "boom"}, headers=_hdr())
    again = client.post(url, json={"status": "done"}, headers=_hdr()).json()
    assert again["status"] == "failed" and again["failure_reason"] == "boom"
