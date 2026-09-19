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
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
from core.workflow.contracts import (
    EnvironmentKind,
    EnvironmentManifest,
    EnvironmentTool,
    GrillAlternative,
    GrillDecision,
    GrillFact,
    GrillRecord,
    HandoffOrigin,
    PlannerTier,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
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

    def build_handoff(
        self,
        ticket: UserTicket,
        *,
        baseline_sha: str = "83e5298eb231599076811802dceac8575c7f6feb",
        plan_version: str = "1.0",
        planner_id: str = "hf08-planner",
        planner_tier: PlannerTier | str = PlannerTier.ECONOMY,
        approval_reference: str = "continuous-autonomy-planning",
        parent_id: str = "HF-08",
        environment_ref: str | None = None,
    ) -> WorkflowHandoff:
        """Convenience method delegating to build_handoff_from_ticket."""
        return build_handoff_from_ticket(
            ticket,
            baseline_sha=baseline_sha,
            plan_version=plan_version,
            planner_id=planner_id,
            planner_tier=planner_tier,
            approval_reference=approval_reference,
            parent_id=parent_id,
            environment_ref=environment_ref,
        )

    def extract_dag(
        self,
        demand: DemandInput | UserTicket | dict[str, Any],
        *,
        bindings_registry: dict[str, Any] | None = None,
        baseline_sha: str = "83e5298eb231599076811802dceac8575c7f6feb",
        demand_version: int = 1,
        parent_id: str = "HF-08",
    ) -> IntermediateDAG:
        """Convenience method delegating to extract_intermediate_dag."""
        return extract_intermediate_dag(
            demand,
            bindings_registry=bindings_registry,
            baseline_sha=baseline_sha,
            demand_version=demand_version,
            parent_id=parent_id,
        )


# ==============================================================================
# Intermediate Planning DAG & Handoff Generation (HF-08-04)
# ==============================================================================


class PlannedLeaf(BaseModel):
    """An atomic execution leaf within the intermediate planning DAG."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    parent_id: str = Field(default="HF-08")
    project_id: str = Field(default="darkfac")
    title: str = Field(..., min_length=1)
    state: str = Field(
        default="ready_for_handoff",
        description="Leaf planning state: ready_for_handoff, design_specified, waiting_dependency, needs_architecture_binding",
    )
    dependencies: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    reachability_contract: str = Field(default="")
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    architecture_binding: str | None = Field(default=None)
    handoff: WorkflowHandoff | None = Field(default=None)
    target_role: str = Field(default="economy")


class IntermediateDAG(BaseModel):
    """Intermediate Directed Acyclic Graph connecting decomposed demand units."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default="darkfac")
    root_ticket_id: str = Field(..., min_length=1)
    demand_version: int = Field(default=1)
    leaves: dict[str, PlannedLeaf] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def ready_leaves(self) -> list[PlannedLeaf]:
        """Return leaves ready to be handed off without unmet dependencies."""
        return [
            leaf
            for leaf in self.leaves.values()
            if leaf.state in ("ready_for_handoff", "design_specified")
            and not self._has_unmet_dependencies(leaf)
        ]

    def blocked_leaves(self) -> list[PlannedLeaf]:
        """Return leaves waiting on explicit dependencies."""
        return [
            leaf
            for leaf in self.leaves.values()
            if leaf.state == "waiting_dependency" or self._has_unmet_dependencies(leaf)
        ]

    def architecture_gap_leaves(self) -> list[PlannedLeaf]:
        """Return leaves that require architectural binding resolution."""
        return [
            leaf
            for leaf in self.leaves.values()
            if leaf.state == "needs_architecture_binding"
        ]

    def _has_unmet_dependencies(self, leaf: PlannedLeaf) -> bool:
        for dep in leaf.dependencies:
            if dep in self.leaves:
                dep_leaf = self.leaves[dep]
                if dep_leaf.state != "ready_for_handoff" and dep_leaf.handoff is None:
                    return True
            else:
                return True
        return False

    def is_complete(self) -> bool:
        """Check if all leaves have a justified, valid terminal state."""
        valid_states = {
            "ready_for_handoff",
            "design_specified",
            "waiting_dependency",
            "needs_architecture_binding",
        }
        return bool(self.leaves) and all(
            leaf.state in valid_states for leaf in self.leaves.values()
        )


def build_handoff_from_ticket(
    ticket: UserTicket,
    *,
    baseline_sha: str = "83e5298eb231599076811802dceac8575c7f6feb",
    plan_version: str = "1.0",
    planner_id: str = "hf08-planner",
    planner_tier: PlannerTier | str = PlannerTier.ECONOMY,
    approval_reference: str = "continuous-autonomy-planning",
    parent_id: str = "HF-08",
    environment_ref: str | None = None,
) -> WorkflowHandoff:
    """Transform a UserTicket into a fully validated WorkflowHandoff instance."""
    clean_id = re.sub(r"[^A-Za-z0-9._:/-]", "-", ticket.id)
    if not clean_id or not clean_id[0].isalnum():
        clean_id = f"TICK-{clean_id.lstrip('._:/-') or '1'}"

    env_ref = environment_ref or f"env-{clean_id.lower()}"
    tier = PlannerTier(planner_tier) if isinstance(planner_tier, str) else planner_tier

    criteria = [c.strip() for c in ticket.acceptance_criteria if c.strip()]
    if not criteria:
        criteria = [f"A entrega descrita em '{ticket.title}' passa em todos os testes determinísticos."]

    non_goals = [g.strip() for g in ticket.non_goals if g.strip()]
    if not non_goals:
        non_goals = ["Não modificar arquivos ou contratos fora dos allowed_paths."]

    validate_cmds = [ticket.reachability_contract.strip()] if ticket.reachability_contract.strip() else ["python core/harness/runner.py --quick"]

    allowed_paths = [p.strip() for p in ticket.suggested_files if p.strip()]
    if not allowed_paths:
        slug = re.sub(r"[^a-z0-9]+", "_", ticket.title.lower()).strip("_")[:30] or "task"
        allowed_paths = [f"core/{slug}/service.py", f"tests/test_{slug}.py"]

    grill = GrillRecord(
        demand_id=clean_id,
        demand_version=1,
        intent_summary=ticket.problem_statement or ticket.title,
        known_facts=[
            GrillFact(
                fact_id=f"fact-{clean_id}-1",
                statement=f"Demanda do projeto {ticket.project_id}: {ticket.title}",
                source="demands_specifier",
            )
        ],
        decisions=[
            GrillDecision(
                decision_id=f"dec-{clean_id}-1",
                question=f"Qual é o contrato de validação determinística para {clean_id}?",
                alternatives=[
                    GrillAlternative(
                        alternative_id="alt-headless",
                        label="Validação Headless",
                        consequence="Garante teste automatizado e determinístico.",
                    ),
                    GrillAlternative(
                        alternative_id="alt-interactive",
                        label="Validação Interativa",
                        consequence="Requer ambiente com intervenção humana.",
                    ),
                ],
                selected_alternative_id="alt-headless",
                response=validate_cmds[0],
                decision_source="owner-demand",
            )
        ],
        assumptions=["Ambiente de execução local configurado conforme CONTRACTS.md."],
        pending_questions=[],
        example_criteria=criteria,
        ready_for_spec=True,
        readiness_justification="Demanda especificada e pronta para decomposição em handoff.",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    environment = EnvironmentManifest(
        environment_ref=env_ref,
        ticket_id=clean_id,
        kind=EnvironmentKind.TARGET_ENVIRONMENT,
        tools=[EnvironmentTool(name="python", version="3.12", source="runtime")],
        system="Windows 11 with WSL2",
        architecture="x86_64",
        services=["local workflow runner"],
        accounts=["local-operator"],
        network_policy="Only declared endpoints; no implicit egress.",
        worker_identity=SanitizedIdentity(subject="worker-local", role=tier.value, host="local"),
        installation=["Use the repository environment."],
        probes=validate_cmds,
        rollback=["Discard the candidate worktree."],
        cleanup=["Remove temporary test fixtures."],
        target_differences=["None for local target environment."],
        observed_at=datetime.now(UTC),
    )

    return WorkflowHandoff(
        ticket_id=clean_id,
        parent_id=parent_id,
        objective=ticket.problem_statement or ticket.title,
        origin=HandoffOrigin.USER_DEMAND,
        plan_version=plan_version,
        planner_id=planner_id,
        planner_tier=tier,
        approval_reference=approval_reference,
        baseline_sha=baseline_sha,
        grill=grill,
        environment=environment,
        state=WorkflowState.READY_FOR_HANDOFF,
        allowed_paths=allowed_paths,
        read_only_paths=["docs/"],
        non_goals=non_goals,
        acceptance_criteria=criteria,
        validate_commands=validate_cmds,
        trigger_events=["demand.specified"],
        successor_event=f"workflow.{clean_id.lower()}.validated",
        conflict_keys=[f"ticket:{clean_id}"],
        resource_requirements=["local Python 3.12"],
        retry_policy="Retry up to 2 times on transient failures.",
        resume_strategy="Re-read handoff and reconcile evidence by ID.",
        rollback_plan="Revert modified files from allowed_paths.",
    )


def extract_intermediate_dag(
    demand: DemandInput | UserTicket | dict[str, Any],
    *,
    bindings_registry: dict[str, Any] | None = None,
    baseline_sha: str = "83e5298eb231599076811802dceac8575c7f6feb",
    demand_version: int = 1,
    parent_id: str = "HF-08",
) -> IntermediateDAG:
    """Decompose a demand or ticket into an IntermediateDAG of planned leaves."""
    if isinstance(demand, dict):
        t_id = demand.get("id") or demand.get("ticket_id") or "USR-DRAFT"
        ticket = UserTicket(
            id=t_id,
            project_id=demand.get("project_id", "darkfac"),
            title=demand.get("title", f"Demand {t_id}"),
            problem_statement=demand.get("problem_statement", ""),
            core_journey=demand.get("core_journey", []),
            non_goals=demand.get("non_goals", []),
            reachability_contract=demand.get("reachability_contract", ""),
            acceptance_criteria=demand.get("acceptance_criteria", []),
            suggested_files=demand.get("suggested_files", []),
            dependencies=demand.get("dependencies", []),
        )
        raw_subtasks = demand.get("subtasks", [])
        architecture_binding = demand.get("architecture_binding") or demand.get("binding")
        requires_binding = bool(demand.get("requires_binding", False)) or ("architecture" in demand.get("tags", []))
    elif isinstance(demand, DemandInput):
        guidance = HeuristicDemandSpecifier().analyze(demand)
        ticket = guidance.suggested_ticket or UserTicket(
            id="USR-DRAFT",
            project_id=demand.project_id,
            title=demand.title,
        )
        raw_subtasks = []
        architecture_binding = None
        requires_binding = "architecture" in demand.extra_tags
    elif isinstance(demand, UserTicket):
        ticket = demand
        raw_subtasks = []
        architecture_binding = None
        requires_binding = "architecture" in ticket.tags
    else:
        raise TypeError(f"Unsupported demand input type: {type(demand).__name__}")

    dag = IntermediateDAG(
        project_id=ticket.project_id,
        root_ticket_id=ticket.id,
        demand_version=demand_version,
    )

    if raw_subtasks:
        for sub in raw_subtasks:
            sub_id = sub.get("id") or f"{ticket.id}-{len(dag.leaves)+1}"
            sub_deps = sub.get("dependencies", [])
            sub_binding = sub.get("architecture_binding") or sub.get("binding")
            sub_req_binding = bool(sub.get("requires_binding", False)) or (sub_binding is not None)

            if sub_req_binding:
                if not sub_binding or (bindings_registry is not None and sub_binding not in bindings_registry):
                    state = "needs_architecture_binding"
                    target_role = "high_architecture"
                else:
                    state = "waiting_dependency" if sub_deps else "ready_for_handoff"
                    target_role = "economy"
            elif sub_deps:
                state = "waiting_dependency"
                target_role = "economy"
            else:
                state = "ready_for_handoff"
                target_role = "economy"

            leaf_ticket = UserTicket(
                id=sub_id,
                project_id=ticket.project_id,
                title=sub.get("title", f"Subtask {sub_id}"),
                problem_statement=sub.get("problem_statement", ticket.problem_statement),
                acceptance_criteria=sub.get("acceptance_criteria", ticket.acceptance_criteria),
                reachability_contract=sub.get("reachability_contract", ticket.reachability_contract),
                suggested_files=sub.get("suggested_files", ticket.suggested_files),
                non_goals=sub.get("non_goals", ticket.non_goals),
                dependencies=sub_deps,
            )

            handoff = None
            if state == "ready_for_handoff":
                handoff = build_handoff_from_ticket(
                    leaf_ticket,
                    baseline_sha=baseline_sha,
                    parent_id=ticket.id,
                    planner_tier=PlannerTier.ECONOMY,
                )

            dag.leaves[sub_id] = PlannedLeaf(
                ticket_id=sub_id,
                parent_id=ticket.id,
                project_id=ticket.project_id,
                title=leaf_ticket.title,
                state=state,
                dependencies=sub_deps,
                allowed_paths=leaf_ticket.suggested_files,
                reachability_contract=leaf_ticket.reachability_contract,
                acceptance_criteria=leaf_ticket.acceptance_criteria,
                non_goals=leaf_ticket.non_goals,
                architecture_binding=sub_binding,
                handoff=handoff,
                target_role=target_role,
            )
    else:
        if requires_binding or architecture_binding is not None:
            if not architecture_binding or (bindings_registry is not None and architecture_binding not in bindings_registry):
                state = "needs_architecture_binding"
                target_role = "high_architecture"
            else:
                state = "waiting_dependency" if ticket.dependencies else "ready_for_handoff"
                target_role = "economy"
        elif ticket.dependencies:
            state = "waiting_dependency"
            target_role = "economy"
        else:
            state = "ready_for_handoff"
            target_role = "economy"

        handoff = None
        if state == "ready_for_handoff":
            handoff = build_handoff_from_ticket(
                ticket,
                baseline_sha=baseline_sha,
                parent_id=parent_id,
                planner_tier=PlannerTier.ECONOMY,
            )

        dag.leaves[ticket.id] = PlannedLeaf(
            ticket_id=ticket.id,
            parent_id=parent_id,
            project_id=ticket.project_id,
            title=ticket.title,
            state=state,
            dependencies=ticket.dependencies,
            allowed_paths=ticket.suggested_files,
            reachability_contract=ticket.reachability_contract,
            acceptance_criteria=ticket.acceptance_criteria,
            non_goals=ticket.non_goals,
            architecture_binding=architecture_binding,
            handoff=handoff,
            target_role=target_role,
        )

    return dag
