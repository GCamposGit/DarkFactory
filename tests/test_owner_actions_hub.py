"""DarkHub integration of the owner action backlog: queue, endpoints, UI contract (USR-190)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.owner_actions.models import OwnerActionDraft
from core.owner_actions.store import OwnerActionStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.models import PriorityInterventionKind
from hub.backend.service import HubService

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "hub" / "frontend"


def _draft(**kw: Any) -> OwnerActionDraft:
    base: dict[str, Any] = {
        "title": "Acao do owner de teste",
        "priority": "medium",
        "why": "Motivo",
        "blocks": ["USR-T1"],
        "steps": [{"text": "Passo um", "command": "echo 1"}, {"text": "Passo dois"}],
        "verify": "Verifique",
    }
    base.update(kw)
    return OwnerActionDraft.model_validate(base)


@pytest.fixture
def service(tmp_path: Path) -> HubService:
    svc = HubService(data_dir=tmp_path / "data")
    demands = DemandsStore(tmp_path / "demands.json")
    demands.save_ticket(UserTicket(id="USR-T1", title="Ticket bloqueado pela decisao", problem_statement="Contexto"))
    svc.demands_store = demands
    svc.demands_service.store = demands
    return svc


@pytest.fixture
def seeded(service: HubService) -> dict[str, str]:
    store = service.owner_action_store
    low = store.add(_draft(title="Baixa prioridade", priority="low"))
    critical = store.add(_draft(title="Critica primeiro", priority="critical"))
    dependent = store.add(_draft(title="Depende da critica", priority="critical", depends_on=[critical.id]))
    decision = store.add(
        _draft(
            kind="decision", title="Decidir a retencao", priority="high", steps=[],
            options=[{"id": "A", "label": "14 dias", "detail": "recomendado"}, {"id": "B", "label": "7 dias"}],
        )
    )
    return {"low": low.id, "critical": critical.id, "dependent": dependent.id, "decision": decision.id}


@pytest.fixture
def client(service: HubService) -> Iterator[TestClient]:
    app.dependency_overrides[get_hub_service] = lambda: service
    with TestClient(app) as test_client:
        test_client.headers.update({"X-Hub-Session": service.session_token})
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------- aggregation


def test_queue_includes_owner_actions_sorted_critical_first(service: HubService, seeded: dict[str, str]) -> None:
    report = service.get_priority_interventions()
    owner_items = [i for i in report.items if i.kind == PriorityInterventionKind.OWNER_ACTION]
    assert report.owner_action_count == len(owner_items) == 4
    assert report.total_count >= 4
    assert [i.urgency for i in owner_items] == ["critical", "critical", "high", "low"]
    # Within the same priority, an actionable item precedes one blocked by an unresolved dependency.
    assert [i.action_target_id for i in owner_items][:2] == [seeded["critical"], seeded["dependent"]]


def test_item_carries_full_metadata(service: HubService, seeded: dict[str, str]) -> None:
    report = service.get_priority_interventions()
    by_target = {i.action_target_id: i for i in report.items if i.kind == PriorityInterventionKind.OWNER_ACTION}
    critical = by_target[seeded["critical"]]
    assert critical.id == f"owner_action:{seeded['critical']}"
    assert critical.urgency == "critical" and critical.description == "Motivo"
    assert critical.metadata["steps"][0] == {"text": "Passo um", "command": "echo 1"}
    assert critical.metadata["verify"] == "Verifique"
    assert critical.metadata["blocks"] == ["USR-T1"]
    assert critical.metadata["unblocks"] == [seeded["dependent"]]
    assert by_target[seeded["dependent"]].metadata["blocked_by"] == [seeded["critical"]]
    decision = by_target[seeded["decision"]]
    assert decision.metadata["action_kind"] == "decision"
    assert [o["id"] for o in decision.metadata["options"]] == ["A", "B"]


def test_missing_or_corrupted_backlog_never_breaks_the_queue(service: HubService, tmp_path: Path) -> None:
    report = service.get_priority_interventions()
    assert report.owner_action_count == 0 and report.owner_action_warnings == []
    path = service.owner_action_store.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupted", encoding="utf-8")
    report = service.get_priority_interventions()
    assert report.owner_action_count == 0
    assert report.owner_action_warnings and "corrompido" in report.owner_action_warnings[0]


def test_done_items_leave_the_queue(service: HubService, seeded: dict[str, str]) -> None:
    service.owner_action_store.resolve(seeded["low"])
    ids = [i.action_target_id for i in service.get_priority_interventions().items]
    assert seeded["low"] not in ids


# ---------------------------------------------------------------- endpoints


def test_get_owner_actions_endpoint(client: TestClient, seeded: dict[str, str]) -> None:
    data = client.get("/api/owner-actions").json()
    assert data["open_count"] == 4 and data["done_count"] == 0
    assert data["items"][0]["action"]["priority"] == "critical"
    blocked = next(i for i in data["items"] if i["action"]["id"] == seeded["dependent"])
    assert blocked["blocked_by"] == [seeded["critical"]]
    client.post(f"/api/owner-actions/{seeded['low']}/done", json={})
    assert client.get("/api/owner-actions").json()["done_count"] == 1
    assert client.get("/api/owner-actions?include_done=true").json()["items"][-1]["action"]["status"] == "done"


def test_priority_endpoint_exposes_owner_action_count(client: TestClient, seeded: dict[str, str]) -> None:
    data = client.get("/api/interventions/priority").json()
    assert data["owner_action_count"] == 4
    assert any(i["kind"] == "owner_action" for i in data["items"])


def test_done_endpoint_sets_resolved_at_and_persists_to_overlay(
    client: TestClient, service: HubService, seeded: dict[str, str]
) -> None:
    response = client.post(f"/api/owner-actions/{seeded['critical']}/done", json={"note": "feito"})
    assert response.status_code == 200
    body = response.json()
    assert body["item"]["action"]["status"] == "done" and body["item"]["action"]["resolved_at"]
    # The dependent is no longer blocked.
    after = {i["action"]["id"]: i for i in client.get("/api/owner-actions").json()["items"]}
    assert after[seeded["dependent"]]["blocked_by"] == []
    overlay = json.loads((service.data_dir / "owner_actions" / "resolutions.json").read_text(encoding="utf-8"))
    assert seeded["critical"] in overlay["resolutions"]
    # definitions (shipped with the image) are untouched
    raw = json.loads(service.owner_action_store.path.read_text(encoding="utf-8"))["actions"]
    assert {r["id"]: r["status"] for r in raw}[seeded["critical"]] == "open"
    # redeploy: a brand new service on the same volume still sees it closed
    fresh = HubService(data_dir=service.data_dir)
    assert fresh.owner_action_store.get(seeded["critical"]).status.value == "done"


def test_answer_endpoint_records_answer_and_annotates_blocked_ticket(
    client: TestClient, service: HubService, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/api/owner-actions/{seeded['decision']}/answer", json={"option_id": "B", "note": "sete dias bastam"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["item"]["action"]["answer"]["option_id"] == "B"
    assert body["annotated_tickets"] == ["USR-T1"]
    ticket = service.demands_service.get_ticket("USR-T1")
    assert f"owner-decision:{seeded['decision']}=B" in ticket.tags
    assert "sete dias bastam" in ticket.problem_statement
    # Answering again with the same option is idempotent; another option is refused.
    assert client.post(f"/api/owner-actions/{seeded['decision']}/answer", json={"option_id": "B"}).status_code == 200
    assert client.post(f"/api/owner-actions/{seeded['decision']}/answer", json={"option_id": "A"}).status_code == 422


def test_resolution_endpoint_errors(client: TestClient, seeded: dict[str, str]) -> None:
    assert client.post("/api/owner-actions/OA-404/done", json={}).status_code == 404
    assert client.post(f"/api/owner-actions/{seeded['decision']}/done", json={}).status_code == 422
    assert client.post(f"/api/owner-actions/{seeded['critical']}/answer", json={"option_id": "A"}).status_code == 422
    assert client.post(f"/api/owner-actions/{seeded['decision']}/answer", json={"option_id": "Z"}).status_code == 422
    assert client.post(f"/api/owner-actions/{seeded['decision']}/answer", json={}).status_code == 422


def test_resolution_requires_the_owner_session(service: HubService, seeded: dict[str, str]) -> None:
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        with TestClient(app) as anonymous:
            assert anonymous.post(f"/api/owner-actions/{seeded['low']}/done", json={}).status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_hub_in_production_layout_reads_the_baked_in_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No data_dir: definitions come from DARKFAC_OWNER_ACTIONS_PATH (else the repo's .factory)."""
    definitions = tmp_path / "owner_actions.json"
    OwnerActionStore(definitions).add(_draft(title="Item embutido na imagem"))
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(definitions))
    svc = HubService()
    assert svc.owner_action_store.path == definitions
    assert [i.action_target_id for i in svc.get_priority_interventions().items if i.kind == PriorityInterventionKind.OWNER_ACTION] == ["OA-001"]


# ------------------------------------------------------------------- the UI


def test_ui_assets_wire_owner_actions() -> None:
    js = (FRONTEND / "interventions.js").read_text(encoding="utf-8")
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    for needle in (
        "owner_action",
        "renderOwnerActionCard",
        "copyOwnerActionCommand",
        "markOwnerActionDone",
        "answerOwnerDecision",
        "setOwnerActionPriorityFilter",
        "updateOwnerActionsSidebarBadge",
        "/api/owner-actions/",
        "blocked_by",
    ):
        assert needle in js, needle
    assert 'id="owner-actions-sidebar-count"' in html
    assert 'id="owner-actions-section"' in html
    assert "owner-actions" in html


def test_ui_escapes_untrusted_text_and_has_no_inline_command_injection() -> None:
    js = (FRONTEND / "interventions.js").read_text(encoding="utf-8")
    card = js.split("function renderOwnerActionCard", 1)[1].split("\nfunction ", 1)[0]
    assert "escapeInterventionsHtml(step.text)" in card
    assert "escapeInterventionsHtml(step.command)" in card
    # commands are copied by index from state, never interpolated into an onclick handler
    assert "copyOwnerActionCommand(" in card and "step.command)}'" not in card
