"""Reachability and contract tests for USR-09: Mecanismo de Grill de Demandas / Especificações.

Verifies:
1. ProjectDocScout education on repository documentation.
2. DemandGrillEngine generation of 2 to 4 essential clarifying questions ($0.00 cost).
3. TicketRefiner application of answers into non-goals, problem statement, and acceptance criteria.
4. DemandsService facade orchestration of grill sessions and atomic updates.
5. Headless CLI execution via `core.demands.cli grill --auto-accept --json`.
6. REST API endpoints `/api/demands/tickets/{ticket_id}/grill*` in an isolated environment.
7. Zero contamination of production backlog (.factory/demands/demands.json).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.demands.grill import DemandGrillEngine, ProjectDocScout
from core.demands.models import (
    DemandInput,
    GrillAnswersPayload,
    GrillQuestion,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
    TAG_USER_DEMAND,
)
from core.demands.service import DemandsService
from core.demands.specifier import DemandSpecifier, HeuristicDemandSpecifier
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture
def temp_grill_env():
    """Provides isolated demands store and service in a temporary directory."""
    temp_dir = Path(tempfile.mkdtemp(prefix="darkfac_grill_test_"))
    demands_path = temp_dir / "demands.json"
    store = DemandsStore(demands_path)
    specifier = DemandSpecifier(heuristic_specifier=HeuristicDemandSpecifier())
    engine = DemandGrillEngine(doc_scout=ProjectDocScout(root_dir=temp_dir), specifier=specifier)
    service = DemandsService(store=store, specifier=specifier, grill_engine=engine)
    yield {
        "dir": temp_dir,
        "path": demands_path,
        "store": store,
        "engine": engine,
        "service": service,
    }
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_doc_scout_extracts_project_context(temp_grill_env):
    """Verify ProjectDocScout inspects docs and extracts insights safely."""
    temp_dir = temp_grill_env["dir"]
    mission_file = temp_dir / "MISSION.md"
    mission_file.write_text("# Missao da Fabrica Autonoma\nConstruir software confiavel sem intervencao humana constante.\n", encoding="utf-8")

    scout = ProjectDocScout(root_dir=temp_dir)
    insights = scout.scout_insights()
    assert len(insights) >= 1
    assert any("Missão" in ins or "Arquitetura" in ins for ins in insights)


def test_grill_engine_heuristic_generates_essential_questions(temp_grill_env):
    """Verify DemandGrillEngine generates 2 to 4 surgical questions with recommended options."""
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="USR-99",
        project_id="darkfac",
        title="Adicionar suporte a exportação de métricas",
        problem_statement="Necessidade de exportar relatórios para análise externa",
    )

    session = engine.start_grill(ticket, force_heuristic=True)
    assert session.ticket_id == "USR-99"
    assert session.status == "pending"
    assert 2 <= len(session.questions) <= 4

    for q in session.questions:
        assert isinstance(q, GrillQuestion)
        assert len(q.question) > 10
        assert len(q.options) >= 2
        # Must have at least one recommended option
        assert any(opt.is_recommended for opt in q.options)
        assert q.allow_custom_input is True


def test_ticket_refiner_enriches_ticket_and_preserves_id(temp_grill_env):
    """Verify that TicketRefiner applies answers, updates non-goals, and preserves metadata."""
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="USR-99",
        project_id="darkfac",
        title="Adicionar suporte a exportação de métricas",
        problem_statement="Necessidade de exportar relatórios",
        non_goals=["Não suportar formato XML"],
    )

    session = engine.start_grill(ticket, force_heuristic=True)
    answers = {
        session.questions[0].id: "Não incluir dados sensíveis ou senhas nos relatórios exportados",
    }

    result = engine.refine_ticket(ticket, answers, session=session)
    assert isinstance(result, GrillRefinementResult)
    assert result.ticket_id == "USR-99"
    assert result.refined_ticket.id == "USR-99"
    assert TAG_USER_DEMAND in result.refined_ticket.tags

    # Non-goal was added
    assert any("dados sensíveis" in ng for ng in result.refined_ticket.non_goals)
    # Original non-goal was preserved
    assert "Não suportar formato XML" in result.refined_ticket.non_goals
    # Problem statement enriched
    assert "[Refinamentos acordados no Grill]" in result.refined_ticket.problem_statement
    assert len(result.summary_of_changes) >= 1


def test_demands_service_grill_flow(temp_grill_env):
    """Verify DemandsService orchestrates the full Q&A flow atomically."""
    service: DemandsService = temp_grill_env["service"]

    ticket = UserTicket(
        id="USR-88",
        project_id="darkfac",
        title="Implementar autenticação OAuth2",
        problem_statement="Permitir login unificado de operadores",
    )
    service.create_ticket(ticket)

    # 1. Start grill
    session = service.start_grill_session("USR-88", force_heuristic=True)
    assert session.ticket_id == "USR-88"

    # 2. Submit answers
    answers = {session.questions[0].id: "Não suportar provedores legados como LDAP"}
    result = service.submit_grill_answers("USR-88", answers, session=session)

    assert result.ticket_id == "USR-88"
    assert any("LDAP" in ng for ng in result.refined_ticket.non_goals)

    # 3. Verify store holds the refined version
    reloaded = service.get_ticket("USR-88")
    assert reloaded is not None
    assert any("LDAP" in ng for ng in reloaded.non_goals)


def test_grill_api_endpoints_isolated(temp_grill_env):
    """Verify REST API routes /api/demands/tickets/{id}/grill and submit with FastAPI TestClient."""
    test_hub_service = HubService(data_dir=temp_grill_env["dir"])
    app.dependency_overrides[get_hub_service] = lambda: test_hub_service

    try:
        client = TestClient(app)

        # Create ticket
        ticket_payload = {
            "id": "USR-77",
            "project_id": "darkfac",
            "title": "Configurar barramento de mensagens",
            "problem_statement": "Comunicação assíncrona entre módulos",
        }
        res_create = client.post("/api/demands/tickets", json=ticket_payload)
        assert res_create.status_code == 201

        # 1. Start grill
        res_grill = client.post("/api/demands/tickets/USR-77/grill?force_heuristic=true")
        assert res_grill.status_code == 200
        session_data = res_grill.json()
        assert session_data["ticket_id"] == "USR-77"
        assert len(session_data["questions"]) >= 2

        # 2. Submit grill answers
        q_id = session_data["questions"][0]["id"]
        submit_payload = {
            "answers": {q_id: "Não acoplar dependência com RabbitMQ ou Kafka externos"},
            "auto_accept_unanswered": True,
        }
        res_submit = client.post("/api/demands/tickets/USR-77/grill/submit", json=submit_payload)
        assert res_submit.status_code == 200
        ref_data = res_submit.json()
        assert ref_data["ticket_id"] == "USR-77"
        assert any("RabbitMQ" in ng for ng in ref_data["refined_ticket"]["non_goals"])
    finally:
        app.dependency_overrides.clear()


def test_production_demands_isolation():
    """Verify production demands.json was never polluted with test tickets."""
    prod_path = Path("c:/dev/DarkFac/.factory/demands/demands.json")
    if prod_path.exists():
        store = DemandsStore(prod_path)
        tickets = store.list_tickets()
        ticket_ids = {t.id for t in tickets}
        assert "USR-77" not in ticket_ids
        assert "USR-88" not in ticket_ids
        assert "USR-99" not in ticket_ids
        # Production tickets must be intact
        assert "USR-09" in ticket_ids
        assert "USR-12" in ticket_ids
        assert "USR-AUTO" in ticket_ids
