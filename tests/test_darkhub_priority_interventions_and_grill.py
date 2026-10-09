"""Test suite for DarkHub Priority Interventions and Interactive Grill (USR-58).

Validates:
1. Pydantic models for PriorityInterventionsReport and PriorityInterventionItem.
2. HubService aggregation of Grills, Dokploy G8 deploy gates, and WAITING_HUMAN tasks.
3. Automated Telegram notification dispatch for pending Grills with outbox resilience.
4. REST API endpoints (GET /api/interventions/priority and POST /api/interventions/notify-grill/{id}).
5. Interactive Grill lifecycle: session initiation, answer submission, and ticket refinement.
6. Frontend assets presence (Hero Strip, Grill Modal, and interventions.js in index.html).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.demands.models import DemandOrigin, LifecycleStage, PlanningHorizon, RoadmapItemType, UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus
from hub.backend.main import app
from hub.backend.models import (
    PriorityInterventionItem,
    PriorityInterventionKind,
    PriorityInterventionsReport,
)
from hub.backend.api import get_hub_service
from hub.backend.service import HubService


@pytest.fixture
def temp_demands_store(tmp_path: Path) -> DemandsStore:
    """Fixture creating an isolated DemandsStore with test tickets."""
    store_file = tmp_path / "demands.json"
    store = DemandsStore(store_file)

    t1 = UserTicket(
        id="USR-TEST-01",
        project_id="darkfac",
        title="Demanda Normal sem Grill",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        tags=["user-demand", "core"],
        problem_statement="Problema simples",
    )
    t2 = UserTicket(
        id="USR-TEST-02",
        project_id="darkfac",
        title="Demanda com Grill Pendente",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        tags=["user-demand", "grill-pending"],
        problem_statement="Necessita desambiguação de escopo com o owner",
    )
    t3 = UserTicket(
        id="USR-TEST-03",
        project_id="darkfac",
        title="Demanda Bloqueada em WAITING_HUMAN",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        tags=["user-demand", "grill", "waiting-human"],
        problem_statement="Owner precisa decidir entre alternativa A e B",
    )
    store.save_ticket(t1)
    store.save_ticket(t2)
    store.save_ticket(t3)
    return store


@pytest.fixture
def mock_hub_service(temp_demands_store: DemandsStore, tmp_path: Path) -> HubService:
    """Fixture providing a HubService wired with isolated stores."""
    service = HubService(data_dir=tmp_path / "data")
    service.demands_store = temp_demands_store
    service.demands_service.store = temp_demands_store
    return service


@pytest.fixture
def client(mock_hub_service: HubService) -> TestClient:
    """FastAPI TestClient with overridden get_hub_service dependency."""
    app.dependency_overrides[get_hub_service] = lambda: mock_hub_service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ==============================================================================
# 1. Pydantic Models & Contracts Tests
# ==============================================================================


def test_priority_interventions_models():
    """Ensure data contracts serialize and validate correctly."""
    item = PriorityInterventionItem(
        id="grill:USR-10",
        kind=PriorityInterventionKind.GRILL,
        title="Grill Pendente: Teste",
        description="Detalhes do grill",
        project_id="darkfac",
        urgency="high",
        action_type="modal_grill",
        action_target_id="USR-10",
        metadata={"tags": ["user-demand"]},
    )
    assert item.kind == PriorityInterventionKind.GRILL
    assert item.action_type == "modal_grill"

    report = PriorityInterventionsReport(
        total_count=1,
        grill_count=1,
        deploy_count=0,
        waiting_human_count=0,
        items=[item],
    )
    assert report.total_count == 1
    dumped = report.model_dump(mode="json")
    assert dumped["total_count"] == 1
    assert dumped["items"][0]["kind"] == "grill"


# ==============================================================================
# 2. HubService Aggregation Tests
# ==============================================================================


def test_hub_service_aggregates_grills(mock_hub_service: HubService):
    """Ensure get_priority_interventions gathers pending grills correctly."""
    report = mock_hub_service.get_priority_interventions()
    assert report.total_count >= 2
    assert report.grill_count >= 2

    # Verify USR-TEST-02 and USR-TEST-03 are collected
    target_ids = [it.action_target_id for it in report.items]
    assert "USR-TEST-02" in target_ids
    assert "USR-TEST-03" in target_ids


def test_hub_service_aggregates_waiting_human_tasks(mock_hub_service: HubService):
    """Ensure WAITING_HUMAN tasks from task dashboard queue are included."""
    fake_task = {
        "id": "job_blocked_99",
        "ticket_id": "USR-99",
        "title": "Build step failed",
        "status": "WAITING_HUMAN",
        "project_id": "darkfac",
        "cause_code": "ambiguous_contract",
    }
    with patch.object(mock_hub_service, "get_task_dashboard") as mock_dash:
        mock_dash.return_value = MagicMock(queue=[fake_task])
        report = mock_hub_service.get_priority_interventions()
        waiting_items = [it for it in report.items if it.kind == PriorityInterventionKind.WAITING_HUMAN]
        assert len(waiting_items) >= 1
        assert any(it.action_target_id == "USR-99" for it in waiting_items)


def test_hub_service_aggregates_g8_deploy_gates(mock_hub_service: HubService):
    """Ensure projects requiring owner signoff are aggregated into G8 deploy gates."""
    from core.enterprise.policy import EnterprisePolicyGuard
    fake_cfg = MagicMock(
        project_id="paid-client-app",
        enabled=True,
        require_owner_signoff=True,
        data_residency="standard",
    )

    def fake_load(self):
        self._configs = {"paid-client-app": fake_cfg}

    with patch.object(EnterprisePolicyGuard, "_load", fake_load):
        report = mock_hub_service.get_priority_interventions()
        deploy_items = [it for it in report.items if it.kind == PriorityInterventionKind.DEPLOY_G8]
        assert len(deploy_items) >= 1
        assert deploy_items[0].action_target_id == "paid-client-app"
        assert deploy_items[0].urgency == "critical"


# ==============================================================================
# 3. Telegram Notification Tests
# ==============================================================================


def test_notify_pending_grill_success(mock_hub_service: HubService, monkeypatch: pytest.MonkeyPatch):
    """Ensure active Telegram notification is dispatched with formatted text and link."""
    # USR-184: the token no longer comes from a tracked .factory/telegram/config.json.
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "unit-test-fake-token")
    with patch("core.integrations.telegram.TelegramGateway.send_message", return_value=True) as mock_send:
        res = mock_hub_service.notify_pending_grill("USR-TEST-02", hub_base_url="https://darkhub.test")
        assert res["ok"] is True
        assert res["sent"] is True
        assert "https://darkhub.test/#grill=USR-TEST-02" in res["action_link"]

        # Check call arguments
        mock_send.assert_called()
        args, kwargs = mock_send.call_args
        sent_text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
        assert "USR-TEST-02" in sent_text
        assert "Grill Pendente" in sent_text


def test_notify_pending_grill_nonexistent_ticket(mock_hub_service: HubService):
    """Ensure notifying a nonexistent ticket fails closed gracefully."""
    res = mock_hub_service.notify_pending_grill("USR-UNKNOWN-404")
    assert res["ok"] is False
    assert res["sent"] is False
    assert "not found" in res["error"]


def test_auto_notification_on_ticket_creation(mock_hub_service: HubService):
    """Ensure create_demand_ticket automatically notifies when ticket has grill-pending tag."""
    new_ticket = UserTicket(
        id="USR-AUTO-GRILL",
        project_id="darkfac",
        title="Nova Demanda Grill",
        tags=["user-demand", "grill-pending"],
        problem_statement="Precisa de alinhamento",
    )
    with patch.object(mock_hub_service, "notify_pending_grill") as mock_notify:
        mock_hub_service.create_demand_ticket(new_ticket)
        mock_notify.assert_called_once_with("USR-AUTO-GRILL")


# ==============================================================================
# 4. REST API Endpoint Tests
# ==============================================================================


def test_api_get_priority_interventions(client: TestClient):
    """Test GET /api/interventions/priority returns valid PriorityInterventionsReport."""
    resp = client.get("/api/interventions/priority")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_count" in data
    assert "items" in data
    assert data["total_count"] >= 2
    assert any(it["action_target_id"] == "USR-TEST-02" for it in data["items"])


def test_api_notify_pending_grill(client: TestClient):
    """Test POST /api/interventions/notify-grill/{ticket_id}."""
    with patch("core.integrations.telegram.TelegramGateway.send_message", return_value=True):
        resp = client.post("/api/interventions/notify-grill/USR-TEST-02")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["ticket_id"] == "USR-TEST-02"


# ==============================================================================
# 5. Interactive Grill Lifecycle & Refinement
# ==============================================================================


def test_api_grill_lifecycle_resolves_pending_intervention(client: TestClient, mock_hub_service: HubService):
    """Test end-to-end: start grill, answer questions, submit, and observe resolution."""
    # 1. Start Grill
    start_resp = client.post("/api/demands/tickets/USR-TEST-02/grill?force_heuristic=true")
    assert start_resp.status_code == 200
    session = start_resp.json()
    assert session["ticket_id"] == "USR-TEST-02"
    assert len(session["questions"]) >= 1

    # 2. Pick answers
    answers = {}
    for q in session["questions"]:
        opt = q["options"][0]["label"]
        answers[q["id"]] = opt

    # 3. Submit Answers
    submit_resp = client.post(
        "/api/demands/tickets/USR-TEST-02/grill/submit",
        json={"answers": answers, "auto_accept_unanswered": True},
    )
    assert submit_resp.status_code == 200
    refinement = submit_resp.json()
    assert refinement["ticket_id"] == "USR-TEST-02"
    assert len(refinement["applied_answers"]) >= 1

    # 4. Verify ticket was updated in store
    updated = mock_hub_service.get_demand_ticket("USR-TEST-02")
    assert updated is not None
    # Tags should no longer have grill-pending
    assert "grill-pending" not in updated.tags


# ==============================================================================
# 6. Frontend Artifacts Presence Tests
# ==============================================================================


def test_frontend_index_html_has_hero_strip_and_modal():
    """Verify hub/frontend/index.html includes required DOM containers and scripts."""
    html_path = Path("hub/frontend/index.html")
    assert html_path.exists(), "index.html missing"
    html = html_path.read_text(encoding="utf-8")

    assert 'id="interventions-hero-strip"' in html
    assert 'id="grill-modal"' in html
    assert 'id="grill-modal-content"' in html
    assert 'id="grill-modal-title"' in html
    assert 'interventions.js' in html


def test_frontend_interventions_js_structure():
    """Verify hub/frontend/interventions.js implements required methods."""
    js_path = Path("hub/frontend/interventions.js")
    assert js_path.exists(), "interventions.js missing"
    js = js_path.read_text(encoding="utf-8")

    assert "loadPriorityInterventions" in js
    assert "renderInterventionsHeroStrip" in js
    assert "openGrillModal" in js
    assert "closeGrillModal" in js
    assert "submitGrillModalAnswers" in js
    assert "autoSelectRecommendedGrillOptions" in js
