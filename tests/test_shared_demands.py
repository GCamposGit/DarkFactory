"""Persistent demand ledger shared by DarkHub and the cloud line."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from core.demands.models import UserTicket
from core.demands.store import DemandsStore


ROOT = Path(__file__).resolve().parents[1]


def _ticket(ticket_id: str, title: str) -> dict:
    return json.loads(UserTicket(id=ticket_id, project_id="darkfac", title=title).model_dump_json())


def test_persistent_ledger_merges_new_image_seed_without_losing_live_updates(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    seed_path.parent.mkdir()
    original = _ticket("USR-01", "original")
    original["updated_at"] = "2026-01-01T00:00:00Z"
    seed_path.write_text(json.dumps([original]), encoding="utf-8")
    store = DemandsStore(live_path, seed_path=seed_path)
    assert [ticket.id for ticket in store.list_tickets()] == ["USR-01"]

    live = _ticket("USR-01", "changed in Hub")
    live["updated_at"] = "2026-03-01T00:00:00Z"
    live_path.write_text(json.dumps([live]), encoding="utf-8")
    new = _ticket("USR-02", "new in image")
    new["updated_at"] = "2026-04-01T00:00:00Z"
    seed_path.write_text(json.dumps([original, new]), encoding="utf-8")

    restarted = DemandsStore(live_path, seed_path=seed_path)
    assert {t.id: t.title for t in restarted.list_tickets()} == {
        "USR-01": "changed in Hub", "USR-02": "new in image",
    }


def test_invalid_live_ledger_fails_closed_during_seed_merge(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    live_path.parent.mkdir()
    seed_path.parent.mkdir()
    live_path.write_text("{broken", encoding="utf-8")
    seed_path.write_text(json.dumps([_ticket("USR-01", "seed")]), encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read demand ledger"):
        DemandsStore(live_path, seed_path=seed_path)
    assert live_path.read_text(encoding="utf-8") == "{broken"


def test_runtime_corruption_cannot_look_like_an_empty_backlog(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    seed_path.parent.mkdir()
    seed_path.write_text(json.dumps([_ticket("USR-01", "seed")]), encoding="utf-8")
    store = DemandsStore(live_path, seed_path=seed_path)
    live_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read demand ledger"):
        store.list_tickets()


def test_newer_image_seed_updates_an_older_live_row(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    live_path.parent.mkdir()
    seed_path.parent.mkdir()
    live = _ticket("USR-01", "older live row")
    live["updated_at"] = "2026-01-01T00:00:00Z"
    newer = _ticket("USR-01", "newer committed row")
    newer["updated_at"] = "2026-02-01T00:00:00Z"
    live_path.write_text(json.dumps([live]), encoding="utf-8")
    seed_path.write_text(json.dumps([newer]), encoding="utf-8")
    assert DemandsStore(live_path, seed_path=seed_path).get_ticket("USR-01").title == "newer committed row"


def test_explicit_test_ledger_ignores_production_seed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(tmp_path / "production.json"))
    monkeypatch.setenv("DARKFAC_DEMANDS_SEED_PATH", str(tmp_path / "missing-seed.json"))
    assert DemandsStore(tmp_path / "isolated.json").list_tickets() == []


def test_independent_store_instances_do_not_lose_concurrent_tickets(tmp_path: Path) -> None:
    path = tmp_path / "demands.json"
    stores = [DemandsStore(path), DemandsStore(path)]

    def save(index: int) -> None:
        stores[index % 2].save_ticket(UserTicket(
            id=f"USR-{index:02d}", project_id="darkfac", title=f"ticket {index}",
        ))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(1, 41)))
    assert len(DemandsStore(path).list_tickets()) == 40


def test_cloud_and_hub_share_one_named_demand_volume() -> None:
    cloud = yaml.safe_load((ROOT / "deploy/dokploy/docker-compose.cloud.yml").read_text(encoding="utf-8"))
    hub = yaml.safe_load((ROOT / "deploy/dokploy/docker-compose.hub.yml").read_text(encoding="utf-8"))
    for compose in (cloud, hub):
        assert compose["volumes"]["darkfac-demands"]["name"] == "darkfac-demands-v1"
    for name in ("darkfac-coordinator", "darkfac-worker", "darkfac-backup-cron", "darkfac-canary"):
        service = cloud["services"][name]
        assert "darkfac-demands:/app/.factory/demands" in service["volumes"]
        assert "DARKFAC_DEMANDS_SEED_PATH=/app/.factory_seed/demands/demands.json" in service["environment"]
    service = hub["services"]["darkhub"]
    assert "darkfac-demands:/app/.factory/demands" in service["volumes"]
    assert "DARKFAC_DEMANDS_PATH=/app/.factory/demands/demands.json" in service["environment"]
