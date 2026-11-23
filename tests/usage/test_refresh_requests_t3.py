from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import threading

from core.usage.refresh_requests import RefreshRequestStore, RefreshRequestUpdate, RefreshStatus


def test_provider_refresh_is_reused_across_store_instances_and_waits_for_a_new_sample(tmp_path: Path) -> None:
    path = tmp_path / "refresh_requests.json"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    store_a = RefreshRequestStore(path, clock=lambda: now)
    store_b = RefreshRequestStore(path, clock=lambda: now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = list(pool.map(lambda store: store.create("node-a", "openai"), (store_a, store_b)))
    assert requests[0].id == requests[1].id
    assert requests[0].provider_id == "openai"

    store_a.complete_from_snapshots("node-a", [
        {"provider_id": "openai", "checked_at": "2026-11-21T11:59:00+00:00"},
    ])
    assert store_b.get(requests[0].id).status == RefreshStatus.PENDING
    store_b.complete_from_snapshots("node-a", [
        {"provider_id": "anthropic", "checked_at": "2026-11-21T12:01:00+00:00"},
    ])
    assert store_b.get(requests[0].id).status == RefreshStatus.PENDING
    store_b.complete_from_snapshots("node-a", [
        {"provider_id": "openai", "checked_at": "2026-11-21T12:01:00+00:00"},
    ])
    assert store_a.get(requests[0].id).status == RefreshStatus.DONE


def test_successful_usage_publication_acknowledges_only_post_request_samples(tmp_path: Path) -> None:
    from core.usage.sync_task import _settle_published_refresh_requests

    usage_dir = tmp_path / ".factory" / "usage"
    provider_dir = usage_dir / "providers"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    store = RefreshRequestStore(usage_dir / "refresh_requests.json", clock=lambda: now)
    request = store.create("node-a", "google")

    _settle_published_refresh_requests(provider_dir, "node-a", [
        {"provider_id": "google", "checked_at": "2026-11-21T12:01:00+00:00"},
    ])
    assert store.get(request.id).status == RefreshStatus.DONE


def test_refresh_failure_is_durable_and_terminal(tmp_path: Path) -> None:
    path = tmp_path / "refresh_requests.json"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    store = RefreshRequestStore(path, clock=lambda: now)
    request = store.create("node-a", "google")
    store.update(request.id, RefreshRequestUpdate(status=RefreshStatus.DONE, checked_at=now.isoformat()))

    failed = store.fail_provider_request("google", "probe_unavailable")
    assert failed is not None
    assert failed.status == RefreshStatus.FAILED
    assert failed.failure_reason == "probe_unavailable"
    assert store.update(request.id, RefreshRequestUpdate(status=RefreshStatus.DONE)) == failed


def test_persistent_unknown_after_published_sample_creates_one_darkfac_repair(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "refresh_requests.json"
    now = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
    store = RefreshRequestStore(path, clock=lambda: now)
    request = store.create("node-a", "openai")
    store.update(request.id, RefreshRequestUpdate(status=RefreshStatus.DONE, checked_at=now.isoformat()))
    provider_dir = tmp_path / "providers"
    provider_dir.mkdir()
    (provider_dir / "openai.json").write_text('{"source_node":"node-a"}', encoding="utf-8")

    registered = {}

    register_lock = threading.Lock()

    def fake_get(ticket_id: str):
        with register_lock:
            return registered.get(ticket_id)

    def fake_register(ticket, *, source=None):
        with register_lock:
            return registered.setdefault(ticket.id, ticket)

    monkeypatch.setattr("core.demands.queue_store.get_ticket", fake_get)
    monkeypatch.setattr("core.demands.queue_store.register_ticket", fake_register)
    from core.line.routing import _record_unknown_quota

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: _record_unknown_quota("openai", "probe_unavailable", provider_dir), range(2)))

    assert len(registered) == 1
    repair = next(iter(registered.values()))
    assert repair.project_id == "darkfac"
    assert repair.id == "INFRA-QUOTA-openai-probe-unavailable"
    assert store.get(request.id).status == RefreshStatus.FAILED
