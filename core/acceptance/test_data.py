"""Deterministic test data generators and fixtures for HF-15 (Scenarios G1-G8).

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 267 & lines 316-333)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenarios G1-G8)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from core.acceptance.models import HF15Scenario, ScenarioDataFixture


def generate_g1_fixture() -> ScenarioDataFixture:
    """G1: Demanda ambígua recebe opções; demanda clara passa direto; resposta retoma jobs."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G1,
        title="G1: Demanda Ambígua vs Clara (Grill de Especificação)",
        description="Verifica que demanda vaga abre opções no Grill e demanda bem especificada avança sem perguntas redundantes.",
        payload={
            "ambiguous_demand": {
                "text": "Criar sistema de notificações para a fábrica",
                "expected_grill_questions": ["channels", "retention_policy", "auth_level"],
                "answers": {
                    "channels": "telegram_and_hub",
                    "retention_policy": "14_days",
                    "auth_level": "owner_only",
                },
            },
            "clear_demand": {
                "text": "Adicionar endpoint GET /healthz retornando HTTP 200 com JSON {'status': 'ok'}",
                "expected_skip_grill": True,
            },
        },
        expected_outcome="Demanda ambígua pausa em WAITING_HUMAN e retoma pós-resposta; demanda clara vai direto para planejamento.",
        tags=["grill", "intake", "clarification"],
    )


def generate_g2_fixture() -> ScenarioDataFixture:
    """G2: Chave ausente com alternativa equivalente vs chave insubstituível."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G2,
        title="G2: Resolução de Chaves de API e Portão Fail-Closed",
        description="Substituição automática para chaves com fallback aprovado; bloqueio fail-closed para chaves críticas.",
        payload={
            "replaceable_key": {
                "name": "DEEPSEEK_API_KEY",
                "approved_fallback": "qwen-fast",
                "fallback_type": "local_ollama",
                "expected_resolution": "auto_fallback",
            },
            "irreplaceable_key": {
                "name": "TELEGRAM_BOT_TOKEN",
                "approved_fallback": None,
                "expected_resolution": "manual_dependency_required",
                "invalid_attempt": "invalid_format_token",
                "valid_attempt": "8366386707:AAFTGyHUi38E6kgVKaJbIThww-G_F9OgCJg",
            },
        },
        expected_outcome="Chave substituível não interrompe pipeline; chave crítica bloqueia gate se inválida e libera apenas com chave válida.",
        tags=["security", "keys", "fail-closed"],
    )


def generate_g3_fixture() -> ScenarioDataFixture:
    """G3: Unitários verdes com worker real bloqueado por firewall/scopes."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G3,
        title="G3: Bloqueio de Prontidão por Ambiente Operacional Inválido",
        description="Testes unitários sintéticos não superam falha de conectividade ou credenciais reais no worker de destino.",
        payload={
            "unit_tests": {"status": "passed", "tests_passed": 42, "tests_failed": 0},
            "worker_preflight": {
                "target_host": "darkfac-vps-primary",
                "network_probe": {"port_5678_open": False, "error": "connection_refused"},
                "oauth_scope": {"scope_granted": "read_only", "scope_required": "read_write"},
            },
            "remediation": {
                "network_probe": {"port_5678_open": True, "error": None},
                "oauth_scope": {"scope_granted": "read_write", "scope_required": "read_write"},
            },
        },
        expected_outcome="Prontidão bloqueada com código INVALID_ENVIRONMENT_PROOF; liberada somente após evidência real corrigida.",
        tags=["environment", "worker", "readiness_gate"],
    )


def generate_g4_fixture() -> ScenarioDataFixture:
    """G4: 4 desenvolvimentos e 5 testes independentes usam 9 slots; concorrência sem starvation."""
    projects = [f"proj-{i:02d}" for i in range(1, 11)]
    dev_jobs = [
        {"job_id": f"dev-job-{i}", "project_id": projects[i - 1], "type": "development", "duration_est_ms": 150}
        for i in range(1, 5)
    ]
    test_jobs = [
        {"job_id": f"test-job-{i}", "project_id": projects[i + 3], "type": "testing", "duration_est_ms": 100}
        for i in range(1, 6)
    ]
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G4,
        title="G4: Concorrência 4 Dev + 5 Testes em 9 Slots e Fila Justa",
        description="Alocação paralela em 9 slots; quando restrito a 2 slots, prova justiça de fila sem fome (starvation).",
        payload={
            "available_slots": 9,
            "dev_jobs": dev_jobs,
            "test_jobs": test_jobs,
            "portfolio_projects": projects,
            "constrained_slots": 2,
        },
        expected_outcome="Todos os 9 jobs executam em paralelo com 9 slots; com 2 slots, todos completam em ordem de chegada justa.",
        tags=["concurrency", "scheduler", "slots", "justice"],
    )


def generate_g5_fixture() -> ScenarioDataFixture:
    """G5: Evento duplicado, evento perdido e reinício recuperam despacho sem efeitos colaterais redundantes."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G5,
        title="G5: Idempotência de Eventos, Recuperação e Despacho Durável",
        description="Deduplicação de update_id/callback_id e recuperação de outbox pendente após crash/restart.",
        payload={
            "duplicate_events": [
                {"update_id": 9001, "sender_id": 8939220558, "text": "/demand Nova feature X"},
                {"update_id": 9001, "sender_id": 8939220558, "text": "/demand Nova feature X"},
            ],
            "lost_outbox_event": {
                "event_id": "outbox_evt_3821",
                "event_type": "telegram_notification",
                "recipient_chat_id": 8939220558,
                "message": "Run DF-15 iniciado com sucesso",
                "delivered": False,
            },
        },
        expected_outcome="Segundo evento é ignorado como duplicata; evento pendente no outbox é reconciliado e entregue.",
        tags=["idempotency", "deduplication", "outbox", "restart"],
    )


def generate_g6_fixture() -> ScenarioDataFixture:
    """G6: Assinatura sem cota usa fallback; tarefa urgente usa Pareto; sem espera por crédito."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G6,
        title="G6: Roteamento de Fronteira de Pareto e Falha de Cota",
        description="Transição automática para rotas qualificadas ao esgotar cota; priorização de Pareto para tarefas urgentes.",
        payload={
            "exhausted_account": "openai-paid",
            "available_credits": 0.00,
            "urgent_task": {
                "task_id": "task_urgent_critical_patch",
                "complexity": "high",
                "budget_ceiling_usd": 0.50,
                "preferred_pareto_model": "deepseek-v4.1-flash",
            },
        },
        expected_outcome="Nenhum job trava aguardando recarga de crédito; roteador seleciona automaticamente modelo ótimo da Pareto.",
        tags=["router", "pareto", "quota", "budget"],
    )


def generate_g7_fixture() -> ScenarioDataFixture:
    """G7: Pesquisa com links canônicos, memória resistente a restart e Learning Pack do owner."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G7,
        title="G7: Proveniência de Pesquisa, Memória Durável e Learning Pack",
        description="Links canônicos preservados, decisões vinculadas a evidências e Learning Pack gerado com tópicos opcionais.",
        payload={
            "research_evidence": {
                "source_id": "arxiv:2408.01234",
                "canonical_url": "https://arxiv.org/abs/2408.01234",
                "title": "Durable Workflow Orchestration in Agentic Frameworks",
                "extracted_insights": [
                    "Transactional outbox avoids split-brain state in distributed LLM loops"
                ],
            },
            "learning_pack": {
                "topic": "Arquitetura Híbrida da Dark Factory",
                "feynman_pitch": "Uma fábrica autônoma que orquestra código sem intervenção humana a menos de 1 centavo por ticket.",
                "architectural_defense": "Garante reprodutibilidade por snapshots imutáveis e checkpoints de recuperação atômicos.",
                "optional_topics": [
                    "Como escalamos de 2 para 9 slots de concorrência sem lockup",
                    "Tradeoffs de SQLite local vs PostgreSQL remoto na latência de despacho",
                ],
            },
        },
        expected_outcome="Memória sobrevive a reinício do nó; Learning Pack disponível no Hub sem bloquear próximo ticket.",
        tags=["research", "memory", "learning_pack", "provenance"],
    )


def generate_g8_fixture() -> ScenarioDataFixture:
    """G8: Projeto pagante exige aceite prévio; prova operacional; rollback isolado."""
    return ScenarioDataFixture(
        scenario_id=HF15Scenario.G8,
        title="G8: Aceite Formal de Cliente, Prova Operacional e Rollback Isolado",
        description="Bloqueio estrito de deploy em produção sem ClientAcceptanceReceipt; falha de release dispara rollback sem afetar outros projetos.",
        payload={
            "paid_project": {
                "project_id": "client-corp-payments",
                "tier": "commercial_paid",
                "artifact_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "production_ready": False,
            },
            "independent_project": {
                "project_id": "internal-free-tools",
                "tier": "internal_free",
                "status": "production_deployed",
            },
            "failure_injection": {
                "stage": "production_smoke",
                "error_type": "db_migration_timeout",
            },
        },
        expected_outcome="Tentativa de deploy sem aceite é rejeitada; falha injetada aciona rollback para o digest anterior; projeto independente segue ativo.",
        tags=["release", "acceptance_receipt", "rollback", "isolation"],
    )


def get_all_hf15_fixtures() -> List[ScenarioDataFixture]:
    """Returns all eight deterministic scenario fixtures for HF-15."""
    return [
        generate_g1_fixture(),
        generate_g2_fixture(),
        generate_g3_fixture(),
        generate_g4_fixture(),
        generate_g5_fixture(),
        generate_g6_fixture(),
        generate_g7_fixture(),
        generate_g8_fixture(),
    ]


def seed_test_data(target_dir: Path) -> Dict[str, Path]:
    """Generates and writes all test fixtures into target directory as JSON files."""
    target_dir.mkdir(parents=True, exist_ok=True)
    seeded: Dict[str, Path] = {}
    fixtures = get_all_hf15_fixtures()

    for f in fixtures:
        path = target_dir / f"scenario_{f.scenario_id.value.lower()}.json"
        path.write_text(json.dumps(f.model_dump(mode="json"), indent=2), encoding="utf-8")
        seeded[f.scenario_id.value] = path

    # Also seed a master index of 10 portfolio projects
    portfolio_path = target_dir / "portfolio_projects.json"
    portfolio_data = {
        "projects": [
            {
                "project_id": f"proj-{i:02d}",
                "name": f"Portfolio Project {i:02d}",
                "tier": "commercial_paid" if i % 2 == 0 else "internal_free",
                "status": "active",
            }
            for i in range(1, 11)
        ]
    }
    portfolio_path.write_text(json.dumps(portfolio_data, indent=2), encoding="utf-8")
    seeded["portfolio_projects"] = portfolio_path

    return seeded
