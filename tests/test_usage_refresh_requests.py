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


@pytest.mark.parametrize("checked,status", [("2030-01-02T03:05:00+00:00", "done"), ("2030-01-02T03:03:00+00:00", "failed"), (None, "failed")])
def test_publication_settles_refresh_without_claiming_old_measurements_are_new(tmp_path, checked, status):
    store = RefreshRequestStore(tmp_path / "requests.json", clock=lambda: NOW)
    request = store.create("host")
    other = store.create("other-host")
    store.complete_from_snapshots("host", [{"provider_id": "anthropic", "checked_at": checked}])
    result = store.get(request.id)
    assert result.status.value == status
    assert result.checked_at == (checked if status == "done" else None)
    assert store.get(other.id).status.value == "pending"


def test_offline_host_keeps_request_pending_and_closed_requests_are_terminal(client: TestClient) -> None:
    created = client.post("/api/usage/accounts/refresh").json()
    assert client.get(f"/api/usage/accounts/refresh/{created['id']}").json()["status"] == "pending"
    url = f"/api/usage/accounts/refresh-requests/{created['id']}/update"
    client.post(url, json={"status": "failed", "failure_reason": "boom"}, headers=_hdr())
    again = client.post(url, json={"status": "done"}, headers=_hdr()).json()
    assert again["status"] == "failed" and again["failure_reason"] == "boom"
