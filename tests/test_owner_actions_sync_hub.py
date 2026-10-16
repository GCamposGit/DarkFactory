from __future__ import annotations

from pathlib import Path

import pytest

from core.line.human import HumanRequest
from core.owner_actions.models import ActionStatus, OwnerActionDraft
from core.owner_actions.mirror import mirrored_id
from core.owner_actions.store import OwnerActionStore, OwnerActionInvalid


def request() -> HumanRequest:
    return HumanRequest(kind="infra", run_id="sync-test", blocking_stage="development", guide_md="1. Verifique o ambiente.")


def test_dynamic_requests_survive_reload_without_changing_curated_definitions(tmp_path: Path) -> None:
    definitions = tmp_path / "tracked.json"
    dynamic = tmp_path / "volume" / "dynamic.json"
    store = OwnerActionStore(definitions, dynamic_path=dynamic, remote_ref=None)
    store.add(OwnerActionDraft(title="Curated action", why="Required", steps=[{"text": "Do it"}], verify="Check"))
    original = definitions.read_bytes()
    action = store.sync_human_request(request(), closed=False)
    store.sync_human_request(request(), closed=False)
    reloaded = OwnerActionStore(definitions, dynamic_path=dynamic, remote_ref=None)
    assert reloaded.get(action.id).status is ActionStatus.OPEN
    assert len(reloaded.load().actions) == 2
    assert definitions.read_bytes() == original
    reloaded.sync_human_request(request(), closed=True)
    reloaded.sync_human_request(request(), closed=False)
    assert reloaded.get(action.id).status is ActionStatus.DONE
    assert definitions.read_bytes() == original


def test_close_before_open_is_terminal_and_machine_cannot_touch_curated_item(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "tracked.json", dynamic_path=tmp_path / "volume" / "dynamic.json", remote_ref=None)
    store.sync_human_request(request(), closed=True)
    store.sync_human_request(request(), closed=False)
    assert store.get(mirrored_id("sync-test", "development")).status is ActionStatus.DONE
    other = request().model_copy(update={"run_id": "curated"})
    store.add(OwnerActionDraft(id=mirrored_id("curated", "development"), title="Curated action", why="Required", steps=[{"text": "Do it"}], verify="Check"))
    with pytest.raises(OwnerActionInvalid):
        store.sync_human_request(other, closed=True)


def test_sync_endpoint_authentication_persistence_and_forbidden_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hub.backend.api import router, get_hub_service

    store = OwnerActionStore(tmp_path / "tracked.json", dynamic_path=tmp_path / "volume" / "dynamic.json", remote_ref=None)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hub_service] = lambda: SimpleNamespace(owner_action_store=store)
    payload = {"request": request().model_dump(mode="json"), "closed": False}
    with TestClient(app) as client:
        monkeypatch.delenv("DARKFAC_TELEMETRY_KEY", raising=False)
        assert client.post("/api/owner-actions/sync", json=payload).status_code == 503
        monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "  test-machine-key ")
        assert client.post("/api/owner-actions/sync", json=payload).status_code == 401
        headers = {"X-DarkFac-Telemetry-Key": "test-machine-key"}
        response = client.post("/api/owner-actions/sync", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"id": mirrored_id("sync-test", "development"), "status": "open"}
        assert client.post("/api/owner-actions/sync", json={**payload, "id": "OA-001"}, headers=headers).status_code == 422
        assert client.post("/api/owner-actions/sync", json={**payload, "closed": True}, headers=headers).status_code == 200
        assert client.post("/api/owner-actions/sync", json=payload, headers=headers).json()["status"] == "done"
    assert not (tmp_path / "tracked.json").exists()


@pytest.mark.parametrize("first,second", [
    (("a-b", "c"), ("a", "b-c")),
    (("unsafe/x", "stage"), ("unsafe?x", "stage")),
    (("x" * 100 + "a", "stage"), ("x" * 100 + "b", "stage")),
])
def test_dynamic_identity_collision_cannot_close_another_request(tmp_path: Path, first: tuple[str, str], second: tuple[str, str]) -> None:
    store = OwnerActionStore(tmp_path / "tracked.json", dynamic_path=tmp_path / "dynamic.json", remote_ref=None)
    first_request = request().model_copy(update={"run_id": first[0], "blocking_stage": first[1]})
    second_request = request().model_copy(update={"run_id": second[0], "blocking_stage": second[1]})
    action = store.sync_human_request(first_request, closed=False)
    assert action.id == mirrored_id(*second)
    original = store.dynamic_path.read_bytes()
    with pytest.raises(OwnerActionInvalid, match="identity"):
        store.sync_human_request(second_request, closed=True)
    assert store.dynamic_path.read_bytes() == original
    assert store.get(action.id).status is ActionStatus.OPEN
    store.sync_human_request(first_request, closed=True)
    assert store.get(action.id).status is ActionStatus.DONE


def test_dynamic_legacy_without_identity_fails_closed(tmp_path: Path) -> None:
    from core.owner_actions.mirror import human_request_to_action
    dynamic_path = tmp_path / "dynamic.json"
    OwnerActionStore(dynamic_path, remote_ref=None).upsert(human_request_to_action(request()))
    store = OwnerActionStore(tmp_path / "tracked.json", dynamic_path=dynamic_path, remote_ref=None)
    original = dynamic_path.read_bytes()
    with pytest.raises(OwnerActionInvalid, match="identity"):
        store.sync_human_request(request(), closed=True)
    assert dynamic_path.read_bytes() == original


def test_worker_transport_reaches_separate_hub_volume_and_survives_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import urlsplit
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hub.backend.api import router, get_hub_service
    from hub.backend.service import HubService
    from core.owner_actions import sync

    worker_root, hub_root = tmp_path / "worker", tmp_path / "hub"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(worker_root))
    monkeypatch.setenv("DARKHUB_CLOUD_URL", "https://test-hub.invalid")
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "  integration-machine-key  ")
    service = HubService(data_dir=hub_root)
    tracked = service.owner_action_store.path
    original = tracked.read_bytes() if tracked.exists() else None
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hub_service] = lambda: service
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
    with TestClient(app) as client:
        def forward(req, **kwargs):
            result = client.post(urlsplit(req.full_url).path, content=req.data, headers=dict(req.header_items()))
            assert result.status_code == 200
            return Response()
        monkeypatch.setattr(sync.urllib.request, "urlopen", forward)
        event = request().model_copy(update={"run_id": "cross-host-contract"})
        action_id = mirrored_id(event.run_id, event.blocking_stage)
        sync.publish_request(event)
        assert sync.outbox_path().is_relative_to(worker_root)
        assert service.owner_action_store.dynamic_path.is_relative_to(hub_root)
        assert service.owner_action_store.get(action_id).status is ActionStatus.OPEN
        service = HubService(data_dir=hub_root)
        assert service.owner_action_store.get(action_id).status is ActionStatus.OPEN
        sync.publish_closed(event.run_id, event.blocking_stage)
        sync.publish_request(event)
        service = HubService(data_dir=hub_root)
        assert service.owner_action_store.get(action_id).status is ActionStatus.DONE
    assert (tracked.read_bytes() if tracked.exists() else None) == original
