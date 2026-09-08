"""Demand Clarification Grill Engine.

Provides automated Q&A clarification between the user and the Dark Factory
prior to entering the PIV development loop.

Key capabilities:
1. ProjectDocScout: Reads repository documentation (MISSION.md, README.md, docs/)
   to educate the engine on existing architecture and inviolable boundaries.
2. DemandGrillEngine: Formulates 2 to 4 essential, surgical questions with
   pre-filled recommended alternatives and open write-in text ($0.00 cost guaranteed).
3. TicketRefiner: Applies answers into the UserTicket (problem, non-goals,
   acceptance criteria, reachability) and produces a transparent audit trail.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from core.demands.models import (
    GrillAnswersPayload,
    GrillQuestion,
    GrillQuestionOption,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.demands.specifier import DemandSpecifier, _extract_json_object
from core.roadmap.models import utc_now

logger = logging.getLogger(__name__)


class ProjectDocScout:
    """Fast, lightweight document scanner that educates the grill engine on project context."""

    def __init__(self, root_dir: Path | str | None = None) -> None:
        self.root_dir = Path(root_dir) if root_dir else Path.cwd()

    def scout_insights(self, limit: int = 5) -> list[str]:
        """Inspect key documentation files to synthesize foundational project insights."""
        insights: list[str] = []

        # 1. Inspect MISSION.md
        mission_file = self.root_dir / "MISSION.md"
        if mission_file.exists():
            try:
                text = mission_file.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    cleaned = line.strip(" -*#")
                    if cleaned and len(cleaned) > 20 and not cleaned.startswith("http"):
                        insights.append(f"Missão: {cleaned}")
                        break
            except Exception as exc:
                logger.debug(f"Error reading MISSION.md: {exc}")

        # 2. Inspect FACTORY_RULES.md / AGENTS.md
        rules_file = self.root_dir / "FACTORY_RULES.md"
        if not rules_file.exists():
            rules_file = self.root_dir / "AGENTS.md"
        if rules_file.exists():
            try:
                text = rules_file.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    cleaned = line.strip(" -*#")
                    if any(k in cleaned.lower() for k in ("headless", "validação", "reachability", "teste", "custo")):
                        insights.append(f"Regra de Arquitetura: {cleaned}")
                        if len(insights) >= 3:
                            break
            except Exception as exc:
                logger.debug(f"Error reading rules: {exc}")

        # 3. Inspect docs/ directory
        docs_dir = self.root_dir / "docs"
        if docs_dir.exists() and docs_dir.is_dir():
            try:
                for doc in sorted(docs_dir.glob("*.md"))[:3]:
                    name = doc.stem.replace("_", " ").title()
                    insights.append(f"Documento existente: {name} ({doc.name})")
            except Exception as exc:
                logger.debug(f"Error scanning docs: {exc}")

        if not insights:
            insights = [
                "Arquitetura: Separação estrita entre lógica headless e apresentação",
                "Validação: Toda funcionalidade deve possuir contrato determinístico de teste",
                "Operação: Prioridade para execução local com custo zero ($0.00)",
            ]

        return insights[:limit]


class DemandGrillEngine:
    """Generates clarifying questions and refines tickets through targeted user feedback."""

    def __init__(
        self,
        doc_scout: ProjectDocScout | None = None,
        specifier: DemandSpecifier | None = None,
    ) -> None:
        self.doc_scout = doc_scout or ProjectDocScout()
        self.specifier = specifier or DemandSpecifier()

    def start_grill(
        self,
        ticket: UserTicket,
        *,
        force_heuristic: bool = False,
        timeout: float | None = None,
    ) -> GrillSession:
        """Analyze ticket and documentation to formulate 2 to 4 essential clarifying questions."""
        doc_insights = self.doc_scout.scout_insights()

        if not force_heuristic:
            model = self.specifier.get_available_local_model()
            if model:
                try:
                    return self._generate_with_ollama(model, ticket, doc_insights, timeout=timeout)
                except Exception as exc:
                    logger.warning(f"Ollama grill generation failed ({exc}); falling back to heuristic grill.")

        return self._generate_heuristic(ticket, doc_insights)

    def _generate_heuristic(self, ticket: UserTicket, doc_insights: list[str]) -> GrillSession:
        """Deterministic, zero-token question synthesizer adhering to Skill 02 PRD standards."""
        questions: list[GrillQuestion] = []

        # Question 1: Scope & Non-Goals (always critical to avoid scope creep)
        title_lower = ticket.title.lower()
        has_nongoals = bool(ticket.non_goals and len(ticket.non_goals) > 1)
        questions.append(
            GrillQuestion(
                id="q_scope_boundaries",
                question=f"Quais limites explícitos de escopo (Non-Goals) devem ser fixados para '{ticket.title}'?",
                context_reason="Sem non-goals explícitos, o agente autônomo pode inflar o escopo ou modificar módulos não relacionados.",
                category="non_goals",
                options=[
                    GrillQuestionOption(
                        id="opt_scope_1",
                        label="Não criar novos frameworks ou acoplamentos visuais; focar na lógica de negócios headless e testes.",
                        is_recommended=True,
                        description="Mantém a entrega cirúrgica e 100% testável via terminal e API.",
                    ),
                    GrillQuestionOption(
                        id="opt_scope_2",
                        label="Limitar a entrega exclusivamente à camada de backend/serviço, postergando ajustes de UI para outro ciclo.",
                        is_recommended=False,
                        description="Prioriza a estabilização das regras de domínio antes de qualquer tela.",
                    ),
                    GrillQuestionOption(
                        id="opt_scope_3",
                        label="Entregar fluxo completo ponta a ponta (backend + painel web do DarkHub) no mesmo ciclo.",
                        is_recommended=False,
                        description="Escopo mais amplo cobrindo tanto serviço quanto interface de usuário.",
                    ),
                ],
                allow_custom_input=True,
            )
        )

        # Question 2: Driver de Teste e Reachability
        questions.append(
            GrillQuestion(
                id="q_reachability_driver",
                question="Qual deve ser a estratégia primária de validação e driver de teste desta funcionalidade?",
                context_reason="Garante que a funcionalidade possa ser executada por scripts automatizados de forma headless.",
                category="validation",
                options=[
                    GrillQuestionOption(
                        id="opt_reach_1",
                        label="Teste de integração e reachability headless via pytest exercendo CLI e endpoints HTTP.",
                        is_recommended=True,
                        description="Padrão Staff+ da Dark Factory com assertivas determinísticas rápidas.",
                    ),
                    GrillQuestionOption(
                        id="opt_reach_2",
                        label="Validação focada estritamente em chamadas diretas de biblioteca (Domain Service puro).",
                        is_recommended=False,
                        description="Isola a lógica de negócio de qualquer protocolo de transporte.",
                    ),
                    GrillQuestionOption(
                        id="opt_reach_3",
                        label="Validação combinada com suite de fixtures mockadas e prova de idempotência.",
                        is_recommended=False,
                        description="Recomendado quando há dependências de filesystem ou persistência de estado.",
                    ),
                ],
                allow_custom_input=True,
            )
        )

        # Question 3: Resiliência e Fallback de Falha
        questions.append(
            GrillQuestion(
                id="q_failure_resilience",
                question="Qual o comportamento esperado caso dependências externas ou modelos locais estejam offline?",
                context_reason="Define o modo de tolerância a falhas para garantir disponibilidade contínua.",
                category="architecture",
                options=[
                    GrillQuestionOption(
                        id="opt_fail_1",
                        label="Degradar graciosamente para script determinístico local a custo zero ($0.00).",
                        is_recommended=True,
                        description="Garante que nenhuma operação falhe por indisponibilidade de IA ou créditos.",
                    ),
                    GrillQuestionOption(
                        id="opt_fail_2",
                        label="Falhar de forma segura (fail-closed) com erro estruturado e diagnóstico claro.",
                        is_recommended=False,
                        description="Previne execuções parciais se houver risco de inconsistência.",
                    ),
                    GrillQuestionOption(
                        id="opt_fail_3",
                        label="Emitir aviso no structured log e adotar defaults pré-configurados silenciosamente.",
                        is_recommended=False,
                        description="Prioriza continuidade máxima sem interromper o operador.",
                    ),
                ],
                allow_custom_input=True,
            )
        )

        return GrillSession(
            ticket_id=ticket.id,
            project_id=ticket.project_id,
            status="pending",
            doc_insights=doc_insights,
            questions=questions,
            engine_used="heuristic_script",
            created_at=utc_now(),
        )

    def _generate_with_ollama(
        self,
        model: str,
        ticket: UserTicket,
        doc_insights: list[str],
        timeout: float | None = None,
    ) -> GrillSession:
        """Query local Ollama to craft tailored clarifying questions based on doc context."""
        import urllib.request

        effective_timeout = timeout or self.specifier.timeout
        system_prompt = (
            "Você é o Arquiteto Líder da Dark Factory conduzindo uma sessão de Q&A rápida (Grill) "
            "com o originador da demanda para estressar escopo, non-goals e reachability. "
            "Formule de 2 a 3 perguntas cirúrgicas. Cada pergunta DEVE conter de 2 a 3 opções "
            "prováveis (a melhor marcada com 'is_recommended': true). "
            "Responda APENAS em JSON estrito no formato:\n"
            "{\n"
            '  "questions": [\n'
            "    {\n"
            '      "id": "q_1",\n'
            '      "question": "...",\n'
            '      "context_reason": "...",\n'
            '      "category": "scope|non_goals|validation|architecture",\n'
            '      "options": [\n'
            '        {"id": "opt_1", "label": "...", "is_recommended": true, "description": "..."},\n'
            '        {"id": "opt_2", "label": "...", "is_recommended": false, "description": "..."}\n'
            "      ]\n"
            "    }\n"
            "  ]\n"
            "}"
        )

        user_content = (
            f"Demanda do usuário para refinar:\n"
            f"- Título: {ticket.title}\n"
            f"- Problema: {ticket.problem_statement}\n"
            f"- Non-goals atuais: {', '.join(ticket.non_goals)}\n"
            f"- Insights da documentação do projeto: {'; '.join(doc_insights)}\n"
        )

        payload = {
            "model": model,
            "prompt": user_content,
            "system": system_prompt,
            "stream": False,
            "format": "json",
            "keep_alive": self.specifier.keep_alive,
            "options": {"temperature": 0.3},
        }

        req = urllib.request.Request(
            f"{self.specifier.ollama_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("response", "").strip()
            parsed = _extract_json_object(raw)

            questions: list[GrillQuestion] = []
            for item in parsed.get("questions", []):
                options = [
                    GrillQuestionOption(
                        id=opt.get("id", f"opt_{idx}"),
                        label=opt.get("label", ""),
                        is_recommended=bool(opt.get("is_recommended", False)),
                        description=opt.get("description"),
                    )
                    for idx, opt in enumerate(item.get("options", []))
                    if opt.get("label")
                ]
                if options:
                    questions.append(
                        GrillQuestion(
                            id=item.get("id", f"q_{len(questions)+1}"),
                            question=item.get("question", ""),
                            context_reason=item.get("context_reason", ""),
                            category=item.get("category", "scope"),
                            options=options,
                            allow_custom_input=True,
                        )
                    )

            if not questions:
                return self._generate_heuristic(ticket, doc_insights)

            return GrillSession(
                ticket_id=ticket.id,
                project_id=ticket.project_id,
                status="pending",
                doc_insights=doc_insights,
                questions=questions,
                engine_used=f"ollama:{model}",
                created_at=utc_now(),
            )

    def refine_ticket(
        self,
        ticket: UserTicket,
        answers: dict[str, str],
        session: GrillSession | None = None,
    ) -> GrillRefinementResult:
        """Apply clarified answers into ticket attributes and produce an updated UserTicket."""
        changes: list[str] = []
        applied_answers: dict[str, str] = {}

        # If a session is provided, auto-resolve unanswered questions with recommended options
        effective_answers = dict(answers)
        if session:
            for q in session.questions:
                if q.id not in effective_answers or not str(effective_answers[q.id]).strip():
                    rec = next((opt for opt in q.options if opt.is_recommended), None)
                    if rec:
                        effective_answers[q.id] = rec.label

        # 1. Process scope / non-goals answers
        non_goals = list(ticket.non_goals)
        for q_id, answer in effective_answers.items():
            ans_clean = answer.strip()
            if not ans_clean:
                continue
            applied_answers[q_id] = ans_clean

            if "scope" in q_id.lower() or "non_goal" in q_id.lower():
                if ans_clean not in non_goals:
                    non_goals.append(ans_clean)
                    changes.append(f"Adicionado non-goal: {ans_clean}")
            elif "reachability" in q_id.lower() or "validation" in q_id.lower():
                # Append to acceptance criteria
                crit = f"Validação alinhada no Grill: {ans_clean}"
                if crit not in ticket.acceptance_criteria:
                    ticket.acceptance_criteria.append(crit)
                    changes.append(f"Critério de validação refinado: {ans_clean}")
            elif "resilience" in q_id.lower() or "failure" in q_id.lower():
                crit = f"Tolerância a falha acordada: {ans_clean}"
                if crit not in ticket.acceptance_criteria:
                    ticket.acceptance_criteria.append(crit)
                    changes.append(f"Comportamento de resiliência: {ans_clean}")
            else:
                changes.append(f"Esclarecimento registrado ({q_id}): {ans_clean}")

        # Ensure non-goals aren't empty
        if not non_goals:
            non_goals = ["Não modificar arquivos ou componentes fora do escopo desta demanda"]

        # If answers provided new details, enrich the problem statement
        refined_problem = ticket.problem_statement
        if applied_answers:
            refinements_text = "; ".join([f"{k}: {v}" for k, v in applied_answers.items()])
            if "Refinamentos acordados no Grill:" not in refined_problem:
                refined_problem = (
                    f"{refined_problem}\n\n[Refinamentos acordados no Grill]:\n"
                    + "\n".join([f"- {v}" for v in applied_answers.values()])
                ).strip()
                changes.append("Problem statement enriquecido com decisões do Grill")

        refined_ticket = ticket.model_copy(
            update={
                "problem_statement": refined_problem,
                "non_goals": non_goals,
                "acceptance_criteria": list(ticket.acceptance_criteria),
                "updated_at": utc_now(),
            }
        )

        return GrillRefinementResult(
            ticket_id=ticket.id,
            original_title=ticket.title,
            refined_ticket=refined_ticket,
            applied_answers=applied_answers,
            summary_of_changes=changes,
        )
