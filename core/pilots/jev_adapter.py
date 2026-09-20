"""TypeSafe / Jev shadow adapter for task pre-classification pilot (HF-23-02).

Operates strictly in shadow mode:
- Direct API client with SecretReference (never exposes raw keys).
- Offline / local tasks fail-closed and are NEVER transmitted over the wire.
- Narrow questions structure for immediate stage, complexity, and owner/local nouls.
- Zero production mutation: does not alter dispatch, router authority, or execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from core.benchmarks.models import BenchmarkDomain
from core.benchmarks.router import TaskBenchmarkRouter
from core.pilots.contracts import PilotObservation
from core.workflow.contracts import SecretReference


logger = logging.getLogger("darkfac.pilots.jev")

DEFAULT_JEV_MODEL = "typesafe/jev-1.13"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
QUESTIONS_VERSION = "jev-stage-complexity-v1"

JEV_QUESTIONS = {
    "stage": {
        "type": "choice",
        "instructions": "Qual é a etapa principal solicitada agora pela tarefa? Julgue a ação imediata, não etapas futuras mencionadas no texto.",
        "criteria": {
            "planning": "Esclarecer requisitos, desenhar solução, arquitetura ou plano antes de implementação.",
            "coding": "Implementar ou corrigir código conforme contrato já definido.",
            "review": "Revisar independentemente um patch ou solução já produzida.",
            "testing": "Executar verificadores determinísticos ou coletar resultados de teste.",
            "research": "Pesquisar fontes externas ou documentação e sintetizar achados sem implementar.",
            "unknown": "Nenhuma etapa acima está suficientemente especificada.",
        },
    },
    "complexity": {
        "type": "choice",
        "instructions": "Qual a complexidade técnica e o risco da etapa principal, considerando escopo e impacto descritos?",
        "criteria": {
            "low": "Mudança ou execução localizada, contrato claro, impacto pequeno e verificadores disponíveis.",
            "medium": "Mais de um componente ou casos de borda relevantes, mas interfaces e impacto controláveis.",
            "high": "Contratos transversais, concorrência, arquitetura ou impacto operacional elevado.",
            "critical": "Incidente de segurança, isolamento de dados ou produção com alcance desconhecido e alto impacto.",
        },
    },
    "needs_owner_decision": {
        "type": "noul",
        "instructions": "Há alguma decisão material de produto, permissão ou regra de negócio ainda não especificada que somente o owner pode fornecer antes de especificar ou implementar? Não conte escolhas técnicas resolvíveis pelo planejador.",
    },
    "must_stay_local": {
        "type": "noul",
        "instructions": "O texto exige explicitamente que dados ou execução permaneçam locais/offline, sem enviar conteúdo a um provedor de nuvem?",
    },
}

_OFFLINE_PATTERNS = [
    re.compile(r"\b(offline|local[_-]?only|somente local|apenas local|não enviar|nao enviar|sem nuvem|sem cloud)\b", re.IGNORECASE),
    re.compile(r"\b(dados confidenciais|confidencial|n[aã]o pode sair da m[aá]quina|dados de cliente)\b", re.IGNORECASE),
]


class JevShadowAdapter:
    """Headless client for Jev in shadow mode."""

    def __init__(
        self,
        secret_ref: SecretReference | None = None,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_JEV_MODEL,
        transport: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
        router: TaskBenchmarkRouter | None = None,
    ) -> None:
        self.secret_ref = secret_ref or SecretReference(
            ref_id="typesafe-shadow-ref",
            provider="typesafe",
            locator="env:TYPESAFE_API_KEY",
            variable_name="TYPESAFE_API_KEY",
        )
        self.endpoint = endpoint
        self.model = model
        self.questions_version = QUESTIONS_VERSION
        self._transport = transport
        self._router = router or TaskBenchmarkRouter()

    def __repr__(self) -> str:
        return f"JevShadowAdapter(model={self.model!r}, endpoint={self.endpoint!r}, secret_ref={self.secret_ref.ref_id!r})"

    def _resolve_api_key(self) -> str | None:
        """Resolves the secret key from environment or reference without exposing it."""
        var_name = self.secret_ref.variable_name or "TYPESAFE_API_KEY"
        key = os.environ.get(var_name)
        if not key and var_name == "TYPESAFE_API_KEY":
            # Fallback to OPENROUTER_API_KEY if using OpenRouter endpoint
            key = os.environ.get("OPENROUTER_API_KEY")
        return key

    def is_offline_or_confidential(
        self, task_text: str, stratum: str = "", offline_flag: bool = False
    ) -> tuple[bool, str]:
        """Checks if a task must be protected from external cloud transmission."""
        if offline_flag:
            return True, "explicit_offline_flag"
        if stratum.lower() == "offline":
            return True, "offline_stratum"
        for pattern in _OFFLINE_PATTERNS:
            if pattern.search(task_text):
                return True, "confidential_offline_restriction"
        return False, ""

    def predict_baseline(self, task_text: str) -> dict[str, Any]:
        """Baseline stage prediction using existing TaskBenchmarkRouter regex classifier."""
        clean = task_text.strip().lower()

        # Check explicit stage keywords for research / review / testing heuristics
        if re.search(r"\b(pesquis[a-z]*|research[a-z]*|papers?|arxiv)\b", clean) and not re.search(r"\b(implementar|codificar|corrigir)\b", clean):
            return {
                "choice": "research",
                "confidence": 0.85,
                "heuristic": "keyword_research",
            }
        if re.search(r"\b(revis[aã]o|revisar|review|adversarial)\b", clean) and "implementar" not in clean:
            return {
                "choice": "review",
                "confidence": 0.85,
                "heuristic": "keyword_review",
            }
        if (
            re.search(r"\b(testes? determin[ií]sticos?|pytest|runner\.py)\b", clean)
            and "implementar" not in clean
            and "corrigir" not in clean
            and "pesquis" not in clean
        ):
            return {
                "choice": "testing",
                "confidence": 0.85,
                "heuristic": "keyword_testing",
            }

        domain, conf, _ = self._router.classify_intent(task_text)

        # Map BenchmarkDomain to workflow stage
        if domain == BenchmarkDomain.DEEP_RESEARCH:
            choice = "research"
        elif domain in (BenchmarkDomain.FORMAL_REASONING, BenchmarkDomain.LEGAL_CONTRACT):
            choice = "planning"
        else:
            choice = "coding"

        return {
            "choice": choice,
            "confidence": conf,
            "domain": domain.value,
        }

    def _default_http_transport(self, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        """Performs HTTPS POST request to the decisions endpoint."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "DarkFactory-JevPilot/1.0",
        }
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def observe_task(
        self,
        case_id: str,
        task_text: str,
        stratum: str = "feature",
        offline: bool = False,
        ground_truth: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> PilotObservation:
        """Observes a task in shadow mode.

        Computes baseline and candidate predictions.
        Guarantees that offline tasks are never transmitted over the wire.
        Guarantees that raw task text, prompts, and secrets are never persisted in the observation.
        """
        demand_hash = hashlib.sha256(task_text.encode("utf-8")).hexdigest()
        baseline_pred = self.predict_baseline(task_text)

        # 1. Fail-closed offline protection
        is_offline, reason = self.is_offline_or_confidential(task_text, stratum, offline)
        if is_offline:
            return PilotObservation(
                case_id=case_id,
                demand_hash=demand_hash,
                stratum=stratum,
                eligible=False,
                exclusion_reason=f"offline_task_preserved:{reason}",
                baseline_prediction=baseline_pred,
                candidate_prediction=None,
                effective_version=self.model,
                ground_truth=ground_truth,
                provenance={**(provenance or {}), "shadow_mode": True, "offline_guard": True},
            )

        # 2. Check API key resolution
        api_key = self._resolve_api_key()
        if not api_key:
            return PilotObservation(
                case_id=case_id,
                demand_hash=demand_hash,
                stratum=stratum,
                eligible=False,
                exclusion_reason="missing_api_credentials",
                baseline_prediction=baseline_pred,
                candidate_prediction=None,
                effective_version=self.model,
                ground_truth=ground_truth,
                provenance={**(provenance or {}), "shadow_mode": True},
            )

        # 3. Call candidate in shadow mode
        payload = {
            "model": self.model,
            "state": {
                "task": task_text,
                "environment": "Dark Factory Python; preserve human Grill, qualified roles, budget and deterministic validation.",
            },
            "questions": JEV_QUESTIONS,
        }

        started = time.perf_counter()
        transport_fn = self._transport or self._default_http_transport
        cand_prediction: dict[str, Any] | None = None
        cost_usd: float | None = None
        error_msg: str | None = None
        effective_version = self.model

        try:
            resp_data = transport_fn(payload, api_key)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            effective_version = resp_data.get("model", self.model)

            answers = resp_data.get("answers", {})
            stage_ans = answers.get("stage", {})
            complexity_ans = answers.get("complexity", {})
            owner_ans = answers.get("needs_owner_decision", {})
            local_ans = answers.get("must_stay_local", {})

            cand_prediction = {
                "choice": stage_ans.get("choice", "unknown"),
                "stage": stage_ans,
                "complexity": complexity_ans,
                "needs_owner_decision_p": owner_ans.get("noul"),
                "must_stay_local_p": local_ans.get("noul"),
            }

            # Estimate cost if token usage is returned
            usage = resp_data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if prompt_tokens or completion_tokens:
                # Approximate $0.05 / 1M tokens for Jev decision queries
                cost_usd = round(((prompt_tokens + completion_tokens) / 1_000_000.0) * 0.05, 7)

        except urllib.error.HTTPError as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            error_msg = f"http_{exc.code}"
        except TimeoutError:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            error_msg = "timeout"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            # Sanitize exception message to avoid credential leaks
            clean_err = re.sub(r"sk-[A-Za-z0-9]{15,}", "[REDACTED]", str(exc))
            error_msg = f"transport_error:{clean_err[:80]}"

        return PilotObservation(
            case_id=case_id,
            demand_hash=demand_hash,
            stratum=stratum,
            eligible=True,
            candidate_prediction=cand_prediction,
            baseline_prediction=baseline_pred,
            effective_version=effective_version,
            latency_ms=elapsed_ms,
            cost_usd=cost_usd,
            error=error_msg,
            ground_truth=ground_truth,
            provenance={**(provenance or {}), "shadow_mode": True},
        )
