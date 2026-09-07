"""Local-first demand specifier and AI/Script guidance engine.

Guarantees 100% operation with zero cloud credits ($0.00):
1. Local Ollama (qwen-fast, qwen-deep, etc.) when running.
2. Deterministic Heuristic script fallback when offline or Ollama is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from typing import Any

from core.demands.models import (
    DemandInput,
    DemandOrigin,
    DemandSpecificationGuidance,
    UserTicket,
    TAG_USER_DEMAND,
)
from core.roadmap.models import (
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapItemType,
    utc_now,
)

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_TIMEOUT = float(os.environ.get("DEMANDS_OLLAMA_TIMEOUT", "90.0"))
DEFAULT_OLLAMA_KEEP_ALIVE = os.environ.get("DEMANDS_OLLAMA_KEEP_ALIVE", "30m")
PREFERRED_LOCAL_MODELS = (
    "qwen-fast:latest",
    "qwen-code-fast:latest",
    "qwen-deep:latest",
    "qwen-code-deep:latest",
    "gpt-oss-clean:latest",
)


class HeuristicDemandSpecifier:
    """Deterministic, pure-script demand analyzer and specifier.

    Zero dependencies, zero network requests, and zero tokens.
    Evaluates inputs against the standards of PRD & Architecture (Skill 02).
    """

    def analyze(self, demand: DemandInput, ticket_id: str = "USR-DRAFT") -> DemandSpecificationGuidance:
        missing: list[str] = []
        suggestions: list[str] = []
        score = 100

        # Title evaluation
        title = demand.title.strip()
        if len(title) < 8:
            score -= 20
            missing.append("Título muito curto ou vago. Descreva claramente a ação desejada.")
        elif not any(title.lower().startswith(v) for v in ("criar", "adicionar", "implementar", "corrigir", "refatorar", "permitir", "melhorar", "integrar")):
            score -= 5
            suggestions.append("Inicie o título com um verbo de ação (ex: 'Criar', 'Permitir', 'Implementar').")

        # Problem evaluation
        problem = demand.problem_statement.strip()
        if not problem:
            score -= 25
            missing.append("Problema real não detalhado. Explique qual dor ou limitação motivou a demanda.")
            problem = f"O projeto necessita da seguinte melhoria: {title}"
        elif len(problem) < 20:
            score -= 10
            suggestions.append("Aprofunde a descrição do problema para evitar ambiguidades durante o desenvolvimento.")

        # Core journey
        journey_text = demand.core_journey.strip()
        journey_steps: list[str] = []
        if journey_text:
            journey_steps = [s.strip("- *1234567890.").strip() for s in journey_text.splitlines() if s.strip()]
        if not journey_steps:
            score -= 15
            missing.append("Jornada principal observável não definida.")
            journey_steps = [
                f"1. O usuário solicita a operação relacionada a: {title}",
                "2. O sistema executa a regra de negócio headless e emite o resultado esperado",
                "3. A interface do DarkHub reflete o estado atualizado com sucesso",
            ]
            suggestions.append("Defina os passos que o usuário realiza do início ao fim para comprovar a conclusão.")

        # Non-goals (inviolable scope guardrail)
        non_goals = [g.strip() for g in demand.non_goals if g.strip()]
        if not non_goals:
            score -= 20
            missing.append("Nenhum Non-Goal definido. Sem limites explícitos, o escopo pode inflar desnecessariamente.")
            non_goals = [
                "Não adicionar dependências externas pesadas sem necessidade estrita",
                "Não modificar modelos de dados ou arquivos fora do escopo desta demanda",
                "Não acoplar lógica de negócios diretamente à apresentação gráfica",
            ]
            suggestions.append("Adicione pelo menos 1 a 2 Non-Goals para blindar o escopo.")

        # Reachability contract (CLI / HTTP / library command)
        reachability = demand.reachability_contract.strip()
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:30] or "demand"
        if not reachability:
            score -= 10
            missing.append("Contrato de reachability ausente. Como a lógica será testada sem GUI?")
            reachability = f"python -m pytest tests/test_{slug}.py -v"
            suggestions.append(f"Recomendado teste headless automatizado: '{reachability}'.")

        # Acceptance criteria
        criteria = [c.strip() for c in demand.acceptance_criteria if c.strip()]
        if not criteria:
            score -= 10
            missing.append("Critérios de aceitação objetivos ausentes.")
            criteria = [
                f"A funcionalidade descrita em '{title}' opera sem erros sintáticos ou de tipos.",
                f"Validação headless executada com sucesso via `{reachability}`.",
                "O status e histórico do ticket são atualizados no backlog da Dark Factory.",
            ]
        else:
            # Ensure reachability command is referenced
            if not any("python" in c or "pytest" in c for c in criteria):
                criteria.append(f"Teste headless de reachability: `{reachability}`")

        # Tags: enforce user-demand tag
        tags = [TAG_USER_DEMAND]
        for t in demand.extra_tags:
            t_clean = t.strip()
            if t_clean and t_clean not in tags:
                tags.append(t_clean)
        if demand.item_type.value not in tags:
            tags.append(demand.item_type.value)

        # Suggested files
        files = list(demand.suggested_files)
        if not files:
            files = [f"core/{slug}/service.py", f"tests/test_{slug}.py"]

        score = max(0, min(100, score))
        is_ready = score >= 70 and len(missing) <= 1

        suggested_ticket = UserTicket(
            id=ticket_id,
            project_id=demand.project_id,
            title=title,
            origin=DemandOrigin.USER,
            status=DeliveryStatus.PLANNED,
            item_type=demand.item_type,
            lifecycle_stage=LifecycleStage.EXECUTION,
            horizon=demand.horizon,
            tags=tags,
            problem_statement=problem,
            core_journey=journey_steps,
            non_goals=non_goals,
            reachability_contract=reachability,
            acceptance_criteria=criteria,
            suggested_files=files,
            estimated_complexity="medium" if len(files) <= 3 else "high",
            dependencies=[],
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        return DemandSpecificationGuidance(
            is_ready=is_ready,
            readiness_score=score,
            missing_elements=missing,
            suggestions=suggestions,
            suggested_ticket=suggested_ticket,
            engine_used="heuristic_script",
            cost_usd=0.0,
        )


def _extract_json_object(text: str) -> dict[str, Any]:
    """Resilient JSON extractor stripping markdown fences and thought tags."""
    cleaned = re.sub(r"<(think|thought)>.*?</\1>", "", text, flags=re.DOTALL).strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(1)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Model response does not contain a JSON object")
    return json.loads(cleaned[start : end + 1])


class DemandSpecifier:
    """Orchestrates local Ollama AI guidance with deterministic script fallback."""

    def __init__(
        self,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        heuristic_specifier: HeuristicDemandSpecifier | None = None,
        timeout: float = DEFAULT_OLLAMA_TIMEOUT,
        keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE,
    ) -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.heuristic = heuristic_specifier or HeuristicDemandSpecifier()
        self.timeout = timeout
        self.keep_alive = keep_alive

    def get_available_local_model(self) -> str | None:
        """Query local Ollama to find installed and ready models."""
        try:
            req = urllib.request.Request(f"{self.ollama_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                for preferred in PREFERRED_LOCAL_MODELS:
                    if preferred in models:
                        return preferred
                return models[0] if models else None
        except Exception:
            return None

    def guide_demand(
        self,
        demand: DemandInput,
        ticket_id: str = "USR-DRAFT",
        *,
        force_heuristic: bool = False,
        timeout: float | None = None,
    ) -> DemandSpecificationGuidance:
        """Provide guidance and structured ticket proposal at zero cost."""
        if force_heuristic:
            return self.heuristic.analyze(demand, ticket_id=ticket_id)

        model = self.get_available_local_model()
        if not model:
            logger.info("Ollama unavailable or no local models installed; using heuristic specifier.")
            return self.heuristic.analyze(demand, ticket_id=ticket_id)

        effective_timeout = timeout if timeout is not None else self.timeout

        try:
            return self._call_ollama(model, demand, ticket_id, timeout=effective_timeout)
        except Exception as exc:
            logger.warning(f"Ollama call failed ({exc}); falling back to heuristic specifier.")
            guidance = self.heuristic.analyze(demand, ticket_id=ticket_id)
            is_timeout = (
                isinstance(exc, TimeoutError)
                or "timed out" in str(exc).lower()
                or (isinstance(exc, urllib.error.URLError) and "timed out" in str(exc.reason).lower())
            )
            if is_timeout:
                msg = f"Aviso: Modelo local {model} não respondeu a tempo (timeout={effective_timeout:.0f}s); gerado via script heurístico ($0)."
            elif "connection refused" in str(exc).lower() or "winerror 10061" in str(exc).lower():
                msg = f"Aviso: Ollama local inacessível; gerado via script heurístico ($0)."
            else:
                msg = f"Aviso: Modelo local {model} indisponível ({exc}); gerado via script heurístico ($0)."
            guidance.suggestions.insert(0, msg)
            return guidance

    def _call_ollama(
        self,
        model: str,
        demand: DemandInput,
        ticket_id: str,
        timeout: float | None = None,
    ) -> DemandSpecificationGuidance:
        effective_timeout = timeout if timeout is not None else self.timeout
        system_prompt = (
            "Você é o Arquiteto de Software da Dark Factory. Sua missão é refinar uma demanda do usuário "
            "em um ticket cirúrgico (one-pass-ready) com tag obrigatória 'user-demand'. "
            "Responda APENAS em JSON estrito com o formato:\n"
            "{\n"
            '  "readiness_score": 85,\n'
            '  "missing_elements": ["..."],\n'
            '  "suggestions": ["..."],\n'
            '  "title": "...",\n'
            '  "problem_statement": "...",\n'
            '  "core_journey": ["passo 1", "passo 2"],\n'
            '  "non_goals": ["não fazer X", "não alterar Y"],\n'
            '  "reachability_contract": "python -m pytest tests/test_... -v",\n'
            '  "acceptance_criteria": ["critério 1", "critério 2"],\n'
            '  "suggested_files": ["core/...", "tests/..."],\n'
            '  "estimated_complexity": "low|medium|high"\n'
            "}"
        )

        user_content = (
            f"Demanda do usuário:\n"
            f"- Título: {demand.title}\n"
            f"- Problema: {demand.problem_statement}\n"
            f"- Jornada: {demand.core_journey}\n"
            f"- Non-goals: {', '.join(demand.non_goals)}\n"
            f"- Critérios: {', '.join(demand.acceptance_criteria)}\n"
            f"- Horizonte: {demand.horizon.value}\n"
            f"- Tipo: {demand.item_type.value}\n"
        )

        payload = {
            "model": model,
            "prompt": user_content,
            "system": system_prompt,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.2},
        }

        req = urllib.request.Request(
            f"{self.ollama_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw_response = data.get("response", "").strip()

            parsed = _extract_json_object(raw_response)
            score = int(parsed.get("readiness_score", 80))
            score = max(0, min(100, score))

            tags = [TAG_USER_DEMAND]
            for t in demand.extra_tags:
                if t and t not in tags:
                    tags.append(t)
            if demand.item_type.value not in tags:
                tags.append(demand.item_type.value)

            suggested_ticket = UserTicket(
                id=ticket_id,
                project_id=demand.project_id,
                title=parsed.get("title", demand.title),
                origin=DemandOrigin.USER,
                status=DeliveryStatus.PLANNED,
                item_type=demand.item_type,
                lifecycle_stage=LifecycleStage.EXECUTION,
                horizon=demand.horizon,
                tags=tags,
                problem_statement=parsed.get("problem_statement", demand.problem_statement),
                core_journey=parsed.get("core_journey", [demand.core_journey] if demand.core_journey else []),
                non_goals=parsed.get("non_goals", demand.non_goals),
                reachability_contract=parsed.get("reachability_contract", demand.reachability_contract),
                acceptance_criteria=parsed.get("acceptance_criteria", demand.acceptance_criteria),
                suggested_files=parsed.get("suggested_files", demand.suggested_files),
                estimated_complexity=parsed.get("estimated_complexity", "medium"),
                dependencies=[],
                created_at=utc_now(),
                updated_at=utc_now(),
            )

            return DemandSpecificationGuidance(
                is_ready=score >= 70,
                readiness_score=score,
                missing_elements=parsed.get("missing_elements", []),
                suggestions=parsed.get("suggestions", []),
                suggested_ticket=suggested_ticket,
                engine_used=f"ollama:{model}",
                cost_usd=0.0,
            )
