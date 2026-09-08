"""Comprehensive automated tests for the User Demands module and Backlog integration.

Covers:
- Pydantic v2 models and tag enforcements (user-demand distinction).
- Zero-credit guidance ($0.00) via heuristic script and local model fallback.
- Durable atomic persistence in DemandsStore.
- Headless business logic in DemandsService.
- Integration into the Operational Roadmap (UserDemandsRoadmapSource).
- DarkHub REST API endpoints (/api/demands/*).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core.demands.models import (
    DemandInput,
    DemandOrigin,
    DemandSpecificationGuidance,
    UserTicket,
    TAG_USER_DEMAND,
    TAG_CODE_REVIEW,
    TAG_AGENT_FEATURE,
)
from core.demands.specifier import DemandSpecifier, HeuristicDemandSpecifier
from core.demands.store import DemandsStore
from core.demands.service import DemandsService
from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType
from core.roadmap.sources import UserDemandsRoadmapSource
from hub.backend.main import app


@pytest.fixture
def temp_demands_env():
    """Provides an isolated DemandsStore and DemandsService in a temporary directory."""
    temp_dir = Path(tempfile.mkdtemp(prefix="darkfac_demands_test_"))
    demands_path = temp_dir / "demands.json"
    store = DemandsStore(demands_path)
    specifier = DemandSpecifier(heuristic_specifier=HeuristicDemandSpecifier())
    service = DemandsService(store=store, specifier=specifier)
    yield {
        "dir": temp_dir,
        "path": demands_path,
        "store": store,
        "specifier": specifier,
        "service": service,
    }
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_user_demand_tag_enforcement():
    """Verify that UserTicket always enforces the user-demand tag and preserves classification."""
    # Even if tags are initially empty, user-demand must be injected
    ticket = UserTicket(
        id="USR-01",
        project_id="darkfac",
        title="Implementar exportação CSV",
        origin=DemandOrigin.USER,
        tags=["feature", "dashboard"],
    )
    assert TAG_USER_DEMAND in ticket.tags
    assert ticket.tags[0] == TAG_USER_DEMAND
    assert TAG_CODE_REVIEW not in ticket.tags
    assert TAG_AGENT_FEATURE not in ticket.tags
    assert ticket.origin == DemandOrigin.USER


def test_heuristic_specifier_offline_zero_cost():
    """Verify heuristic specifier runs deterministically with 0 tokens and $0 cost."""
    specifier = HeuristicDemandSpecifier()
    inp = DemandInput(
        project_id="darkfac",
        title="Adicionar suporte a exportação de métricas",
        problem_statement="Os clientes não conseguem consolidar métricas fora do painel",
        core_journey="1. Usuário acessa aba de métricas\n2. Clica em exportar\n3. Recebe JSON/CSV",
        non_goals=["Não suportar formatos legados como XML"],
        acceptance_criteria=["Endpoint /api/export retorna 200"],
    )

    guidance = specifier.analyze(inp, ticket_id="USR-01")
    assert isinstance(guidance, DemandSpecificationGuidance)
    assert guidance.cost_usd == 0.0
    assert guidance.engine_used == "heuristic_script"
    assert guidance.readiness_score >= 70
    assert guidance.is_ready is True
    assert guidance.suggested_ticket is not None
    assert guidance.suggested_ticket.id == "USR-01"
    assert TAG_USER_DEMAND in guidance.suggested_ticket.tags
    assert guidance.suggested_ticket.reachability_contract != ""


def test_heuristic_specifier_detects_missing_elements():
    """Verify that vague inputs are penalized and actionable feedback is returned."""
    specifier = HeuristicDemandSpecifier()
    vague_input = DemandInput(
        title="fix",
        problem_statement="",
        core_journey="",
    )

    guidance = specifier.analyze(vague_input, ticket_id="USR-01")
    assert guidance.is_ready is False
    assert guidance.readiness_score < 60
    assert any("título" in m.lower() for m in guidance.missing_elements)
    assert any("problema" in m.lower() for m in guidance.missing_elements)
    assert any("non-goal" in m.lower() for m in guidance.missing_elements)


def test_demands_store_persistence(temp_demands_env):
    """Verify atomic storage, ID generation, retrieval, and status updates."""
    store: DemandsStore = temp_demands_env["store"]

    assert store.next_ticket_id() == "USR-01"

    t1 = UserTicket(
        id="USR-01",
        project_id="darkfac",
        title="Demanda de teste 1",
        problem_statement="Problema 1",
        acceptance_criteria=["Critério 1"],
    )
    store.save_ticket(t1)

    assert store.next_ticket_id() == "USR-02"
    all_tickets = store.list_tickets()
    assert len(all_tickets) == 1
    assert all_tickets[0].id == "USR-01"
    assert all_tickets[0].title == "Demanda de teste 1"

    # Status update
    updated = store.update_status("USR-01", DeliveryStatus.IMPLEMENTING)
    assert updated.status == DeliveryStatus.IMPLEMENTING
    reloaded = store.get_ticket("USR-01")
    assert reloaded is not None
    assert reloaded.status == DeliveryStatus.IMPLEMENTING


def test_demands_service_headless(temp_demands_env):
    """Verify high-level DemandsService operations."""
    service: DemandsService = temp_demands_env["service"]

    inp = DemandInput(
        project_id="darkfac",
        title="Criar endpoint de status operacional",
        problem_statement="Monitoramento externo necessita checar saúde dos módulos",
    )
    guidance = service.guide_demand(inp, force_heuristic=True)
    assert guidance.suggested_ticket is not None

    created = service.create_ticket(guidance.suggested_ticket)
    assert created.id == "USR-01"
    assert TAG_USER_DEMAND in created.tags

    listed = service.list_tickets(project_id="darkfac")
    assert len(listed) == 1
    assert listed[0].id == "USR-01"

    updated = service.update_ticket_status("USR-01", DeliveryStatus.VALIDATING)
    assert updated.status == DeliveryStatus.VALIDATING


def test_roadmap_integration_with_user_demands(temp_demands_env):
    """Verify that UserDemandsRoadmapSource compiles user tickets into the roadmap snapshot."""
    store: DemandsStore = temp_demands_env["store"]
    path: Path = temp_demands_env["path"]

    # Add a user demand ticket
    store.save_ticket(
        UserTicket(
            id="USR-01",
            project_id="darkfac",
            title="Funcionalidade solicitada pelo usuário",
            problem_statement="Permitir download de logs",
            core_journey=["Acessar", "Baixar"],
            non_goals=["Não incluir credenciais"],
            status=DeliveryStatus.PLANNED,
            horizon=PlanningHorizon.NOW,
            acceptance_criteria=["Logs exportados em UTF-8"],
            tags=["user-demand", "logs"],
        )
    )

    source = UserDemandsRoadmapSource(path)
    result = source.read("darkfac")
    assert result.state.status == "available"
    assert len(result.records) == 1
    candidate = result.records[0]
    assert candidate.id == "USR-01"
    assert "user-demand" in candidate.tags
    assert candidate.source_id == "user-demands"

    # Compile with RoadmapCompiler
    compiler = RoadmapCompiler([source])
    snapshot = compiler.compile("darkfac")
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.id == "USR-01"
    assert "user-demand" in item.tags
    assert any(ref.produced_by == "user" for ref in item.source_refs)


from hub.backend.api import get_hub_service
from hub.backend.service import HubService


def test_hub_api_demands_endpoints(temp_demands_env):
    """Verify REST API routes /api/demands/* via FastAPI TestClient in isolated environment."""
    test_hub_service = HubService(data_dir=temp_demands_env["dir"])
    app.dependency_overrides[get_hub_service] = lambda: test_hub_service

    try:
        client = TestClient(app)

        # 0. Next ID endpoint
        next_resp = client.get("/api/demands/next-id?project_id=darkfac")
        assert next_resp.status_code == 200
        assert next_resp.json()["next_id"] == "USR-01"

        # 1. Guide demand endpoint
        guide_payload = {
            "project_id": "darkfac",
            "title": "Implementar webhook de notificações",
            "problem_statement": "Usuário precisa receber alertas no Discord",
            "core_journey": "Evento ocorre -> webhook é disparado",
            "non_goals": ["Não suportar SMS neste ciclo"],
            "acceptance_criteria": ["Webhook entrega payload JSON válido"],
            "horizon": "now",
            "item_type": "feature",
            "extra_tags": ["discord", "alerts"],
            "suggested_files": ["core/webhook/service.py"],
            "reachability_contract": "python -m pytest tests/test_webhook.py -v",
        }
        resp = client.post("/api/demands/guide?force_heuristic=true", json=guide_payload)
        assert resp.status_code == 200
        guidance = resp.json()
        assert guidance["cost_usd"] == 0.0
        assert guidance["suggested_ticket"] is not None
        assert TAG_USER_DEMAND in guidance["suggested_ticket"]["tags"]

        suggested = guidance["suggested_ticket"]

        # 2. Create demand ticket endpoint
        create_resp = client.post("/api/demands/tickets", json=suggested)
        assert create_resp.status_code == 201
        created_ticket = create_resp.json()
        ticket_id = created_ticket["id"]
        assert TAG_USER_DEMAND in created_ticket["tags"]

        # 3. List demand tickets endpoint
        list_resp = client.get("/api/demands/tickets?project_id=darkfac")
        assert list_resp.status_code == 200
        tickets = list_resp.json()
        assert any(t["id"] == ticket_id for t in tickets)

        # 4. Get demand ticket endpoint
        get_resp = client.get(f"/api/demands/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == ticket_id

        # 5. Update ticket status endpoint
        patch_resp = client.patch(f"/api/demands/tickets/{ticket_id}/status?status=implementing")
        assert patch_resp.status_code == 200
        assert patch_resp.json()["status"] == "implementing"
    finally:
        app.dependency_overrides.clear()
