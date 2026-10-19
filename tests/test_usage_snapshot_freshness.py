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
        "checked_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
        "windows": [{"quota_id": "w", "used_percent": 40.0}],
    }
    if node:
        payload["source_node"] = node
    monitor.save_snapshot("anthropic", payload)
    return SyncedSnapshotAdapter(SPEC, tmp_path)


@pytest.mark.parametrize("age,stale", [(0, False), (599, False), (600, False), (601, True), (86400, True)])
def test_stale_boundary_is_strictly_above_600_seconds(tmp_path: Path, age: int, stale: bool) -> None:
    account = _adapter(tmp_path, age).inspect(now=NOW)
    assert account.age_seconds == age
    assert account.stale is stale
    assert account.source_node == "predator-neo-16"
    assert account.checked_at == (NOW - timedelta(seconds=age)).isoformat()
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
