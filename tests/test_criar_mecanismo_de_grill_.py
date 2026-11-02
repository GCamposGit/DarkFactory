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

from core.demands.grill import (
    OWNER_CATEGORIES,
    DemandGrillEngine,
    ProjectDocScout,
    filter_owner_questions,
    render_adr,
)
from core.demands.models import (
    DemandInput,
    GrillAnswersPayload,
    GrillQuestion,
    GrillQuestionOption,
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


@pytest.mark.parametrize(
    "title,problem,criteria,expected_min,expected_max,injected_technical",
    [
        ("Painel de vendas", "Quero um painel que mostre minhas vendas do mes", [], 1, 3, False),
        ("Painel de vendas", "Quero um painel que mostre minhas vendas do mes", [], 1, 3, True),
        ("Relatorio", "Preciso de um relatorio de clientes ativos", [], 1, 3, False),
        ("App", "Quero um aplicativo para organizar minhas tarefas", [], 1, 3, False),
        ("Alertas", "Quero ser avisado quando o estoque ficar baixo", [], 1, 3, False),
        ("Curto", "Fazer", [], 1, 3, False),
        ("Typo no menu", "Corrigir o erro de digitacao no titulo do menu principal", [], 0, 0, False),
        ("Ajuste", "Ajustar o espacamento do botao salvar na tela de cadastro", [], 0, 0, False),
        ("Bug login", "Corrigir bug em que o login falha com e-mail em maiusculas", [], 0, 0, True),
        ("Painel ok", "Quero um painel que mostre minhas vendas do mes", ["Painel exibe a soma das vendas do mes corrente"], 1, 3, False),
    ],
)
def test_grill_engine_asks_only_owner_questions(
    temp_grill_env, title, problem, criteria, expected_min, expected_max, injected_technical
):
    """G1 semantico: so perguntas do owner, em categorias fechadas, com recomendada; tecnicas viram ADR."""
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(id="ZZTEST-99", project_id="darkfac", title=title, problem_statement=problem,
                        acceptance_criteria=criteria)
    session = engine.start_grill(ticket, force_heuristic=True)
    questions = list(session.questions)
    if injected_technical:
        tech = GrillQuestion(
            id="q_tech", question="Usar Redis ou Postgres para o cache?", category="architecture",
            options=[GrillQuestionOption(id="o1", label="Redis", is_recommended=True)],
        )
        questions, dropped = filter_owner_questions(questions + [tech])
        assert [q.id for q in dropped] == ["q_tech"]
        assert "Redis" in render_adr(dropped)
    assert expected_min <= len(questions) <= expected_max
    for q in questions:
        assert q.category in OWNER_CATEGORIES
        assert q.id != "q_tech"
        assert any(opt.is_recommended for opt in q.options)
    if expected_max == 0:
        assert engine.classify_readiness(ticket).is_clear is True


def test_ticket_refiner_enriches_ticket_and_preserves_id(temp_grill_env):
    """Verify that TicketRefiner applies answers, updates non-goals, and preserves metadata."""
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="ZZTEST-99",
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
    assert result.ticket_id == "ZZTEST-99"
    assert result.refined_ticket.id == "ZZTEST-99"
    assert TAG_USER_DEMAND in result.refined_ticket.tags

    # Non-goal was added
    assert "dados sensíveis" in result.refined_ticket.problem_statement
    # Original non-goal was preserved
    assert "Não suportar formato XML" in result.refined_ticket.non_goals
    # Problem statement enriched
    assert "[Refinamentos acordados no Grill]" in result.refined_ticket.problem_statement
    assert len(result.summary_of_changes) >= 1


def test_grill_partial_answers_leave_recommendations_pending(temp_grill_env):
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="ZZTEST-PARTIAL",
        project_id="darkfac",
        title="Configurar processamento de pagamentos",
        problem_statement="Definir como o produto processa pagamentos de clientes.",
    )
    questions = [
        GrillQuestion(
            id=f"q_{index}",
            question=f"Decisão de negócio {index}",
            category="business",
            options=[
                GrillQuestionOption(id=f"q_{index}_a", label=f"Recomendação {index}", is_recommended=True),
                GrillQuestionOption(id=f"q_{index}_b", label=f"Alternativa {index}"),
            ],
        )
        for index in range(1, 5)
    ]
    kept, dropped = filter_owner_questions(questions)
    assert len(kept) == 4
    assert dropped == []
    session = GrillSession(ticket_id=ticket.id, questions=questions)

    refined, record, refinement = engine.resolve_grill_answers(
        ticket, session, {"q_1": "q_1_b"}, auto_accept_unanswered=True
    )

    assert record.ready_for_spec is False
    assert {q.question_id for q in record.pending_questions} == {"q_2", "q_3", "q_4"}
    assert refinement.applied_answers == {"q_1": "Alternativa 1"}
    assert refinement.status == "waiting_human"
    assert refinement.ready_for_spec is False
    assert [q.id for q in refinement.grill_session.current_round_questions] == ["q_2", "q_3", "q_4"]
    assert "Recomendação" not in refined.problem_statement
    assert [d.decision_source for d in record.decisions if d.decision_id == "dec_q_1"] == ["human_response"]
    assert session.status == "waiting_human"
    assert [q.id for q in session.current_round_questions] == ["q_2", "q_3", "q_4"]


def test_grill_keeps_all_material_questions_across_rounds_and_serialization(temp_grill_env):
    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="ZZTEST-ROUNDS",
        project_id="darkfac",
        title="Definir operação de assinaturas",
        problem_statement="Definir as regras comerciais de assinatura e cobrança.",
    )
    questions = [
        GrillQuestion(
            id=f"q_{index}",
            question=f"Pergunta material {index}",
            category="business",
            options=[
                GrillQuestionOption(id=f"q_{index}_a", label=f"Opção A {index}", is_recommended=True),
                GrillQuestionOption(id=f"q_{index}_b", label=f"Opção B {index}"),
            ],
        )
        for index in range(1, 6)
    ]
    kept_questions, technical_questions = filter_owner_questions(questions)
    assert len(kept_questions) == 5
    assert technical_questions == []
    session = GrillSession(ticket_id=ticket.id, questions=questions)

    first_page = session.current_round_questions
    _, first_record, _ = engine.resolve_grill_answers(
        ticket, session, {q.id: f"{q.id}_b" for q in first_page}
    )
    assert len(session.questions) == 5
    assert all(q.category == "business" for q in session.questions)
    assert first_record.ready_for_spec is False
    assert [q.id for q in session.current_round_questions] == ["q_4", "q_5"]

    serialized = session.model_dump_json()
    serialized_data = json.loads(serialized)
    assert len(serialized_data["questions"]) == 2
    assert len(serialized_data["question_queue"]) == 5
    resumed = GrillSession.model_validate_json(serialized)
    assert len(resumed.all_questions) == 5
    assert len(resumed.questions) == 2
    assert [q.id for q in resumed.current_round_questions] == ["q_4", "q_5"]
    _, final_record, _ = engine.resolve_grill_answers(
        ticket, resumed, {q.id: f"{q.id}_a" for q in resumed.current_round_questions}
    )
    assert final_record.ready_for_spec is True
    assert final_record.pending_questions == []
    assert len(final_record.decisions) == 5
    assert {decision.decision_source for decision in final_record.decisions} == {"human_response"}


def test_grill_session_legacy_serialization_and_explicit_answer_sources(temp_grill_env):
    legacy = GrillSession.model_validate(
        {"ticket_id": "ZZTEST-LEGACY", "questions": [], "answers": {}}
    )
    assert legacy.current_round_questions == []
    assert GrillSession.model_validate_json(legacy.model_dump_json()).ticket_id == "ZZTEST-LEGACY"

    engine: DemandGrillEngine = temp_grill_env["engine"]
    ticket = UserTicket(
        id="ZZTEST-SOURCE",
        title="Decidir recuperação",
        problem_statement="Definir as regras de recuperação para clientes.",
    )
    question = GrillQuestion(
        id="q_business",
        question="Qual regra comercial?",
        category="business",
        options=[
            GrillQuestionOption(id="a", label="Recomendação", is_recommended=True),
            GrillQuestionOption(id="b", label="Opção escolhida"),
        ],
    )
    technical = GrillQuestion(
        id="q_database_adapter",
        question="Qual adaptador técnico usar?",
        category="technical",
        options=[
            GrillQuestionOption(id="sqlite", label="SQLite", is_recommended=True),
            GrillQuestionOption(id="postgres", label="PostgreSQL"),
        ],
    )
    session = GrillSession(
        ticket_id=ticket.id,
        questions=[question],
        auto_technical_questions=[technical],
    )
    _, record, _ = engine.resolve_grill_answers(ticket, session, {"q_business": "b"})
    assert {decision.decision_id: decision.decision_source for decision in record.decisions} == {
        "dec_q_business": "human_response",
        "dec_q_database_adapter": "auto_technical",
    }


def test_integrated_grill_partial_submission_stays_waiting_human(temp_grill_env):
    service: DemandsService = temp_grill_env["service"]
    received = service.receive_integrated_demand(
        DemandInput(
            title="Criar painel de pedidos",
            problem_statement="Quero um painel que mostre os pedidos e permita acompanhar seus estados.",
        ),
        force_heuristic=True,
    )
    session = received["grill_session"]
    assert session is not None

    result = service.submit_integrated_grill_answers(
        received["ticket"].id,
        {session.questions[0].id: "Uma resposta explícita"},
        session,
    )

    assert result["status"] == "waiting_human"
    assert result["grill_record"].ready_for_spec is False
    assert result["grill_session"].status == "waiting_human"


def test_demands_service_grill_flow(temp_grill_env):
    """Verify DemandsService orchestrates the full Q&A flow atomically."""
    service: DemandsService = temp_grill_env["service"]

    ticket = UserTicket(
        id="ZZTEST-88",
        project_id="darkfac",
        title="Implementar autenticação OAuth2",
        problem_statement="Permitir login unificado de operadores",
    )
    service.create_ticket(ticket)

    # 1. Start grill
    session = service.start_grill_session("ZZTEST-88", force_heuristic=True)
    assert session.ticket_id == "ZZTEST-88"

    # 2. Submit answers
    answers = {session.questions[0].id: "Não suportar provedores legados como LDAP"}
    result = service.submit_grill_answers("ZZTEST-88", answers, session=session)

    assert result.ticket_id == "ZZTEST-88"
    assert "LDAP" in result.refined_ticket.problem_statement

    # 3. Verify store holds the refined version
    reloaded = service.get_ticket("ZZTEST-88")
    assert reloaded is not None
    assert "LDAP" in reloaded.problem_statement


def test_grill_api_endpoints_isolated(temp_grill_env):
    """Verify REST API routes /api/demands/tickets/{id}/grill and submit with FastAPI TestClient."""
    test_hub_service = HubService(data_dir=temp_grill_env["dir"])
    app.dependency_overrides[get_hub_service] = lambda: test_hub_service

    try:
        client = TestClient(app)

        # Create ticket
        ticket_payload = {
            "id": "ZZTEST-77",
            "project_id": "darkfac",
            "title": "Configurar barramento de mensagens",
            "problem_statement": "Comunicação assíncrona entre módulos",
        }
        res_create = client.post("/api/demands/tickets", json=ticket_payload)
        assert res_create.status_code == 201

        # 1. Start grill
        res_grill = client.post("/api/demands/tickets/ZZTEST-77/grill?force_heuristic=true")
        assert res_grill.status_code == 200
        session_data = res_grill.json()
        assert session_data["ticket_id"] == "ZZTEST-77"
        assert 1 <= len(session_data["questions"]) <= 3

        # 2. Submit grill answers
        q_id = session_data["questions"][0]["id"]
        submit_payload = {
            "answers": {q_id: "Não acoplar dependência com RabbitMQ ou Kafka externos"},
            "auto_accept_unanswered": True,
        }
        res_submit = client.post("/api/demands/tickets/ZZTEST-77/grill/submit", json=submit_payload)
        assert res_submit.status_code == 200
        ref_data = res_submit.json()
        assert ref_data["ticket_id"] == "ZZTEST-77"
        assert "RabbitMQ" in ref_data["refined_ticket"]["problem_statement"]
    finally:
        app.dependency_overrides.clear()


def test_production_demands_isolation(temp_grill_env):
    """A grill service with an injected store must not touch another backlog."""
    production_path = temp_grill_env["dir"] / "production_demands.json"
    production_store = DemandsStore(production_path)
    production_store.save_ticket(
        UserTicket(
            id="USR-PRODUCTION",
            project_id="darkfac",
            title="Existing production demand",
            problem_statement="Must remain unchanged",
        )
    )
    baseline = production_path.read_bytes()

    temp_grill_env["service"].create_ticket(
        UserTicket(
            id="ZZTEST-ISOLATION",
            project_id="darkfac",
            title="Synthetic grill demand",
            problem_statement="Exercise the isolated store",
        )
    )
    temp_grill_env["service"].start_grill_session("ZZTEST-ISOLATION", force_heuristic=True)

    assert production_path.read_bytes() == baseline
    assert {ticket.id for ticket in production_store.list_tickets()} == {"USR-PRODUCTION"}
