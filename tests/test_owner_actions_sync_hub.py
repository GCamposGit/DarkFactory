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
        monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-machine-key")
        assert client.post("/api/owner-actions/sync", json=payload).status_code == 401
        headers = {"X-DarkFac-Telemetry-Key": "test-machine-key"}
        response = client.post("/api/owner-actions/sync", json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"id": mirrored_id("sync-test", "development"), "status": "open"}
        assert client.post("/api/owner-actions/sync", json={**payload, "id": "OA-001"}, headers=headers).status_code == 422
        assert client.post("/api/owner-actions/sync", json={**payload, "closed": True}, headers=headers).status_code == 200
        assert client.post("/api/owner-actions/sync", json=payload, headers=headers).json()["status"] == "done"
    assert not (tmp_path / "tracked.json").exists()
