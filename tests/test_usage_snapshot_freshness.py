"""USR-233: synced snapshots expose checked_at, origin node, age and the stale flag (> 600 s)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.usage.adapters import ProviderSpec, SyncedSnapshotAdapter
from core.usage.models import AccountConnectionStatus, ProviderFamily
from core.usage.monitor import AccountUsageMonitor

NOW = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
SPEC = ProviderSpec("anthropic", "Anthropic", ProviderFamily.FRONTIER, "https://example.test", ())


def _adapter(tmp_path: Path, age_seconds: int, node: str | None = "predator-neo-16") -> SyncedSnapshotAdapter:
    monitor = AccountUsageMonitor(tmp_path, adapters=[], synced_only=True)
    payload = {
        "provider_id": "anthropic",
        "checked_at": NOW.isoformat(),
        "received_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "windows": [{"quota_id": "w", "used_percent": 40.0}],
    }
    if node:
        payload["source_node"] = node
    monitor.save_snapshot("anthropic", payload)
    return SyncedSnapshotAdapter(SPEC, tmp_path)


@pytest.mark.parametrize("age,stale", [(0, False), (1199, False), (1200, False), (1201, True), (86400, True)])
def test_stale_boundary_is_strictly_above_1200_seconds(tmp_path: Path, age: int, stale: bool) -> None:
    adapter = _adapter(tmp_path, age)
    account = adapter.inspect(now=NOW)
    import json
    account.received_at = json.loads((tmp_path / "anthropic.json").read_text())["received_at"]
    account.with_freshness(NOW)
    assert account.age_seconds == age
    assert account.stale is stale
    assert account.source_node == "predator-neo-16"
    assert account.checked_at == NOW.isoformat()
    # Stale data keeps its status and percentages: never zeroed nor disconnected.
    assert account.status == AccountConnectionStatus.CONNECTED
    assert account.windows[0].used_percent == 40.0


def test_missing_snapshot_has_no_age_and_is_not_stale(tmp_path: Path) -> None:
    account = SyncedSnapshotAdapter(SPEC, tmp_path).inspect(now=NOW)
    assert account.status == AccountConnectionStatus.UNKNOWN
    assert account.age_seconds is None and account.stale is False and account.source_node is None


def test_sync_records_origin_node(tmp_path: Path) -> None:
    from hub.backend.models import UsageSyncPayload
    from hub.backend.service import HubService

    service = HubService(data_dir=tmp_path / "data", usage_dir=tmp_path / "usage")
    service.sync_usage_data(
        UsageSyncPayload(
            client_node_id="predator-neo-16",
            accounts=[{"provider_id": "anthropic", "windows": [{"quota_id": "w", "used_percent": 10.0}]}],
        )
    )
    snapshot = (tmp_path / "usage" / "providers" / "anthropic.json").read_text(encoding="utf-8")
    assert '"source_node": "predator-neo-16"' in snapshot
    import json
    assert json.loads(snapshot)["received_at"]


def test_api_uses_server_arrival_and_recalculates_age_on_cached_report(tmp_path, monkeypatch) -> None:
    import json
    from fastapi.testclient import TestClient
    from hub.backend.main import app
    from hub.backend.api import get_hub_service
    from hub.backend.service import HubService
    from hub.backend.models import UsageSyncPayload

    volume = tmp_path / "synced"
    monkeypatch.setenv("DARKHUB_USAGE_SNAPSHOT_DIR", str(volume))
    service = HubService(data_dir=tmp_path / "hub")
    client_arrival = "2000-01-01T00:00:00Z"
    service.sync_usage_data(UsageSyncPayload(client_node_id="host", accounts=[{
        "provider_id": "anthropic", "checked_at": datetime.now(timezone.utc).isoformat(), "received_at": client_arrival,
        "windows": [{"quota_id": "w", "label": "w", "used_percent": 40}],
    }]))
    path = volume / "providers" / "anthropic.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["received_at"] != client_arrival
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        with TestClient(app) as client:
            first = next(a for a in client.get("/api/usage/accounts").json()["accounts"] if a["provider_id"] == "anthropic")
            assert first["stale"] is False and first["age_seconds"] < 60
            payload["received_at"] = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
            path.write_text(json.dumps(payload), encoding="utf-8")
            after = next(a for a in client.get("/api/usage/accounts").json()["accounts"] if a["provider_id"] == "anthropic")
            assert after["stale"] is True and after["age_seconds"] >= 1260
            assert after["windows"][0]["used_percent"] == 40
            service.sync_usage_data(UsageSyncPayload(client_node_id="host", accounts=[{
                **payload, "checked_at": client_arrival,
            }]))
            replay = next(a for a in client.get("/api/usage/accounts").json()["accounts"] if a["provider_id"] == "anthropic")
            assert replay["age_seconds"] < 60
            assert replay["measurement_age_seconds"] > 1200 and replay["stale"] is True
    finally:
        app.dependency_overrides.clear()
