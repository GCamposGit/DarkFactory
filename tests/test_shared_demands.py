"""Persistent demand ledger shared by DarkHub and the cloud line."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from core.demands.models import GrillRefinementResult, UserTicket
from core.demands.service import DemandsService
from core.demands.store import DemandsStore
from core.line.dogfood import pick_candidate
from core.roadmap.models import DeliveryStatus, utc_now
from hub.backend.service import HubService


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
    live["created_at"] = original["created_at"]
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


def test_new_image_seed_id_collision_keeps_the_live_ledger_intact(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    live_path.parent.mkdir()
    seed_path.parent.mkdir()
    live = _ticket("USR-01", "created in Hub")
    committed = _ticket("USR-01", "different ticket created in Git")
    live["created_at"] = "2026-01-01T00:00:00Z"
    committed["created_at"] = "2026-02-01T00:00:00Z"
    live_path.write_text(json.dumps([live]), encoding="utf-8")
    seed_path.write_text(json.dumps([committed]), encoding="utf-8")

    with pytest.raises(ValueError, match="Demand ID collision"):
        DemandsStore(live_path, seed_path=seed_path)
    assert json.loads(live_path.read_text(encoding="utf-8")) == [live]


def test_runtime_corruption_cannot_look_like_an_empty_backlog(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    seed_path.parent.mkdir()
    seed_path.write_text(json.dumps([_ticket("USR-01", "seed")]), encoding="utf-8")
    store = DemandsStore(live_path, seed_path=seed_path)
    live_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read demand ledger"):
        store.list_tickets()


def test_missing_or_invalid_live_rows_fail_closed(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    seed_path.parent.mkdir()
    seed_path.write_text(json.dumps([_ticket("USR-01", "seed")]), encoding="utf-8")
    store = DemandsStore(live_path, seed_path=seed_path)
    live_path.write_text(json.dumps([_ticket("USR-01", "valid"), {"id": "USR-02"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid demand row"):
        store.list_tickets()
    live_path.unlink()
    with pytest.raises(ValueError, match="Demand ledger is missing"):
        store.list_tickets()


def test_newer_image_seed_never_regresses_live_status_or_evidence(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    live_path.parent.mkdir()
    seed_path.parent.mkdir()
    live = _ticket("USR-01", "completed live row")
    live["updated_at"] = "2026-01-01T00:00:00Z"
    live["status"] = "completed"
    live["delivery_evidence"] = "commit abc123"
    newer = _ticket("USR-01", "newer committed row")
    newer["created_at"] = live["created_at"]
    newer["updated_at"] = "2026-02-01T00:00:00Z"
    live_path.write_text(json.dumps([live]), encoding="utf-8")
    seed_path.write_text(json.dumps([newer]), encoding="utf-8")
    actual = DemandsStore(live_path, seed_path=seed_path).get_ticket("USR-01")
    assert actual.title == "completed live row"
    assert actual.status == DeliveryStatus.COMPLETED
    assert actual.delivery_evidence == "commit abc123"


def test_seed_advances_committed_delivery_without_replacing_live_fields(tmp_path: Path) -> None:
    live_path = tmp_path / "live" / "demands.json"
    seed_path = tmp_path / "seed" / "demands.json"
    live_path.parent.mkdir()
    seed_path.parent.mkdir()
    live = _ticket("USR-01", "edited in Hub")
    live["updated_at"] = "2026-03-01T00:00:00Z"
    committed = _ticket("USR-01", "old committed title")
    committed["created_at"] = live["created_at"]
    committed["updated_at"] = "2026-02-01T00:00:00Z"
    committed["status"] = "completed"
    committed["delivery_evidence"] = "commit abc123"
    live_path.write_text(json.dumps([live]), encoding="utf-8")
    seed_path.write_text(json.dumps([committed]), encoding="utf-8")

    actual = DemandsStore(live_path, seed_path=seed_path).get_ticket("USR-01")
    assert actual.title == "edited in Hub"
    assert actual.status == DeliveryStatus.COMPLETED
    assert actual.delivery_evidence == "commit abc123"


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


def test_hub_and_worker_observe_the_same_ticket_and_status(tmp_path: Path) -> None:
    path = tmp_path / "demands.json"
    hub = DemandsStore(path)
    worker = DemandsStore(path)
    hub.save_ticket(UserTicket(
        id="USR-01", project_id="darkfac", title="shared", tags=["line-ok"],
    ))
    assert worker.get_ticket("USR-01").title == "shared"
    assert pick_candidate(tickets=worker.list_tickets()).id == "USR-01"
    worker.update_status("USR-01", DeliveryStatus.IMPLEMENTING)
    assert hub.get_ticket("USR-01").status == DeliveryStatus.IMPLEMENTING


def test_stale_refinement_cannot_revert_worker_status(tmp_path: Path) -> None:
    path = tmp_path / "demands.json"
    hub = DemandsStore(path)
    worker = DemandsStore(path)
    hub.save_ticket(UserTicket(id="USR-01", project_id="darkfac", title="initial"))
    stale = hub.get_ticket("USR-01")
    worker.update_status("USR-01", DeliveryStatus.IMPLEMENTING)
    with pytest.raises(ValueError, match="Stale demand ticket"):
        hub.save_ticket(
            stale.model_copy(update={"title": "refined"}),
            expected_updated_at=stale.updated_at,
        )
    current = hub.get_ticket("USR-01")
    assert current.title == "initial"
    assert current.status == DeliveryStatus.IMPLEMENTING


def test_equal_timestamp_cannot_replace_a_different_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "demands.json"
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(path))
    monkeypatch.setenv("DARKFAC_DEMANDS_SHARED_VOLUME", "darkfac-demands-v1")
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    store = DemandsStore(path)
    original = UserTicket(id="USR-01", project_id="darkfac", title="original")
    store.save_ticket(original)
    with pytest.raises(ValueError, match="Stale demand ticket"):
        store.save_ticket(original.model_copy(update={"title": "different"}))
    assert store.get_ticket("USR-01").title == "original"


def test_status_timestamp_advances_past_future_clock(tmp_path: Path) -> None:
    from datetime import timedelta

    store = DemandsStore(tmp_path / "demands.json")
    future = utc_now() + timedelta(days=1)
    store.save_ticket(UserTicket(
        id="USR-01", project_id="darkfac", title="future clock", updated_at=future,
    ))
    updated = store.update_status("USR-01", DeliveryStatus.IMPLEMENTING)
    assert updated.updated_at > future
    assert store.get_ticket("USR-01").status == DeliveryStatus.IMPLEMENTING


def test_grill_refinement_retries_against_latest_worker_status(tmp_path: Path) -> None:
    path = tmp_path / "demands.json"
    hub = DemandsStore(path)
    worker = DemandsStore(path)
    hub.save_ticket(UserTicket(id="USR-01", project_id="darkfac", title="initial"))
    service = DemandsService(store=hub)
    calls = 0

    def refine(ticket: UserTicket, answers: dict[str, str], *, session: object) -> GrillRefinementResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            worker.update_status(ticket.id, DeliveryStatus.IMPLEMENTING)
        refined = ticket.model_copy(update={"title": "refined", "updated_at": utc_now()})
        return GrillRefinementResult(
            ticket_id=ticket.id, original_title=ticket.title, refined_ticket=refined,
            applied_answers=answers, summary_of_changes=["title"],
        )

    service.grill_engine.refine_ticket = refine  # type: ignore[method-assign]
    result = service.submit_grill_answers("USR-01", {"title": "refined"})
    assert calls == 2
    assert result.refined_ticket.status == DeliveryStatus.IMPLEMENTING
    assert hub.get_ticket("USR-01").title == "refined"
    assert hub.get_ticket("USR-01").status == DeliveryStatus.IMPLEMENTING


def test_missing_shared_mount_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(tmp_path / "demands.json"))
    monkeypatch.setenv("DARKFAC_DEMANDS_SHARED_VOLUME", "darkfac-demands-v1")
    with pytest.raises(RuntimeError, match="not mounted"):
        DemandsStore()


def test_cloud_cannot_consume_seed_until_shared_migration_marker_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps([_ticket("USR-01", "seed")]), encoding="utf-8")
    monkeypatch.setenv("DARKFAC_DEMANDS_MIGRATION_GATE", "required")
    monkeypatch.setenv("DARKFAC_DEMANDS_SHARED_VOLUME", "darkfac-demands-v1")
    monkeypatch.setattr(Path, "is_mount", lambda self: True)
    path = tmp_path / "volume" / "demands.json"
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(path))
    store = DemandsStore(path, seed_path=seed_path)
    with pytest.raises(RuntimeError, match="migration is pending"):
        store.list_tickets()
    with pytest.raises(RuntimeError, match="migration is pending"):
        store.next_ticket_id()
    marker = path.parent / ".shared_demands_migration.json"
    import hashlib

    versions = {"USR-01": json.loads(path.read_text(encoding="utf-8"))[0]["updated_at"]}
    digest = hashlib.sha256(json.dumps(versions, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    marker.write_text(json.dumps({
        "version": 1, "state": "complete", "versions": versions,
        "ticket_count": 1, "manifest_sha256": digest,
    }), encoding="utf-8")
    assert store.get_ticket("USR-01").title == "seed"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="migration is pending"):
        store.list_tickets()


def test_live_board_labels_local_ledger_without_claiming_a_shared_mount(tmp_path: Path) -> None:
    service = HubService(data_dir=tmp_path)
    service.demands_store.save_ticket(UserTicket(
        id="USR-01", project_id="darkfac", title="visible",
    ))
    snapshot = service.get_line_live()
    assert snapshot.demands_source == "local-file"
    assert snapshot.demands_latest_at is not None


def test_cloud_and_hub_share_one_named_demand_volume() -> None:
    cloud = yaml.safe_load((ROOT / "deploy/dokploy/docker-compose.cloud.yml").read_text(encoding="utf-8"))
    hub = yaml.safe_load((ROOT / "deploy/dokploy/docker-compose.hub.yml").read_text(encoding="utf-8"))
    for compose in (cloud, hub):
        assert compose["volumes"]["darkfac-demands"]["name"] == "darkfac-demands-v1"
    for name in ("darkfac-coordinator", "darkfac-worker", "darkfac-backup-cron", "darkfac-canary"):
        service = cloud["services"][name]
        assert "darkfac-demands:/app/.factory/demands" in service["volumes"]
        assert "DARKFAC_DEMANDS_SEED_PATH=/app/.factory_seed/demands/demands.json" in service["environment"]
        assert "DARKFAC_DEMANDS_SHARED_VOLUME=darkfac-demands-v1" in service["environment"]
        assert "DARKFAC_DEMANDS_MIGRATION_GATE=required" in service["environment"]
    service = hub["services"]["darkhub"]
    assert "darkfac-demands:/app/.factory/demands" in service["volumes"]
    assert "DARKFAC_DEMANDS_PATH=/app/.factory/demands/demands.json" in service["environment"]
    assert "DARKFAC_DEMANDS_SHARED_VOLUME=darkfac-demands-v1" in service["environment"]
