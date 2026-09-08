"""
Task-Adaptive Benchmark Router & Multi-Domain Model Selector.
Analyzes development requirements and prompts to route to the optimal external benchmark,
retrieves the domain-specific Pareto efficiency frontier, and selects Top-3 speculative models.
"""

import re
import logging
from typing import Dict, List, Any, Optional, Tuple

from core.benchmarks.models import (
    BenchmarkDomain,
    BenchmarkDomainMetadata,
    BenchmarkRoutingDecision,
    ModelBenchmarkEntry,
    SpeculativeCandidate,
    ModelTier,
)
from core.benchmarks.frontier import (
    DOMAIN_METADATA,
    compute_pareto_frontier,
    compute_domain_pareto_frontiers,
    get_domain_top3_candidates,
    is_production_interactive_model,
)
from core.benchmarks.fetcher import ensure_daily_benchmark
from core.benchmarks.racing import EmpiricalBenchmarkLedger

logger = logging.getLogger("darkfac.benchmarks.router")

# Regex patterns for high-speed (<1ms) multi-domain intent classification
DOMAIN_INTENT_PATTERNS = {
    BenchmarkDomain.LEGAL_CONTRACT: [
        re.compile(r"\b(contrat[oa]s?|cl[aá]usul[as]?|compliance|jur[ií]dic[oa]s?|termos?(?: de uso)?|pol[ií]ticas?(?: de privacidade)?|nda|vesting|societ[aá]ri[oa]s?|due diligence|patentes?|propriedade intelectual|advocacia|processo judicial|lit[ií]gios?|tribunais|tribunal|liability|indemnification|governan[cç]a corporativa)\b", re.IGNORECASE),
        re.compile(r"\b(legal|contracts?|clauses?|regulatory|statutes?|statutory|attorneys?|arbitration|intellectual property|agreements?|indemnif(?:y|ication)|liabilit(?:y|ies))\b", re.IGNORECASE),
    ],
    BenchmarkDomain.DEEP_RESEARCH: [
        re.compile(r"\b(pesquis[ar]|arxiv|papers?|surveys?|estado da arte|sota|literatura|acad[eê]mic[oa]s?|deep research|web research|investiga[cç][aã]o|investiga[cç][oõ]es|scout|artigo cient[ií]fico|busca na web|rastrear|procurar fontes)\b", re.IGNORECASE),
        re.compile(r"\b(research(?:ed|ing|er)?|investigat(?:e|es|ed|ing|ion)|scrap(?:e|es|ed|ing)|brows(?:e|es|ed|ing|er)|citations?|literature review|find sources|fact check|hallucination)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.BUSINESS_AUTOMATION: [
        re.compile(r"\b(automa[cç][aã]o|automa[cç][oõ]es|automatiz[ar]|workflows?|planilhas?|excel|csv|erp|crm|zapier|webhook|integra[cç][aã]o de sistemas|faturamento|financeir[oa]|processo operacional|nota fiscal|email marketing|rpa|osworld|desktop automation)\b", re.IGNORECASE),
        re.compile(r"\b(automat(?:e|es|ed|ing|ion)|workflows?|spreadsheets?|invoic(?:e|es|ing)|quickbooks|salesforce|servicenow|gui automation|browser automation|tool calling|desktop windows?|osworld)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.FORMAL_REASONING: [
        re.compile(r"\b(matem[aá]tic[oa]s?|prova formal|provas formais|teoremas?|c[aá]lculo|dedu[cç][aã]o|dedu[cç][oõ]es|l[oó]gica formal|estat[ií]stica avan[cç]ada|geometria|olympiad|aime|math|equa[cç][aã]o|equa[cç][oõ]es|derivadas?|integra(?:l|is)|probabilidade)\b", re.IGNORECASE),
        re.compile(r"\b(proofs?|theorems?|deduct(?:ion|ive)|formal logic|discrete math|algebra|calculus|mathematical olympiad|symbolic logic|integrals?)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.MULTIMODAL_AUDIO: [
        re.compile(r"\b([aá]udios?|transcri[cç][aã]o|transcri[cç][oõ]es|whisper|voz|vozes|grava[cç][aã]o|grava[cç][oõ]es|podcasts?|reuni[aã]o|reuni[oõ]es|diariza[cç][aã]o|speakers?|microfones?|canal duplo|stereo|fon[eê]m[as]?)\b", re.IGNORECASE),
        re.compile(r"\b(audio|transcrib(?:e|ed|ing|er)|transcriptions?|speech|voices?|diarization|podcasts?|sound|recording|dual-channel)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.IMAGE_GENERATION: [
        re.compile(r"\b(imagens?|foto|fotos|ilustra[cç][aã]o|ilustra[cç][oõ]es|desenho|desenhos|banner|banners|arte|mockup|mockups|dall-?e|midjourney|flux|sdxl|recraft|stable diffusion|text-?to-?image|t2i)\b", re.IGNORECASE),
        re.compile(r"\b(image(?:s)?|photo(?:s)?|picture(?:s)?|illustrat(?:e|ion|ions)|render(?:s|ing)?|banner(?:s)?|mockup(?:s)?|text-?to-?image|t2i|flux|dall-?e|recraft|midjourney|sdxl)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.VIDEO_GENERATION: [
        re.compile(r"\b(v[ií]deos?|anima[cç][aã]o|anima[cç][oõ]es|clipes?|filme|filmes|cinema|motion|text-?to-?video|t2v|sora|runway|gen-?3|kling|hailuo|luma ray)\b", re.IGNORECASE),
        re.compile(r"\b(video(?:s)?|clip(?:s)?|animation(?:s)?|cinematic|text-?to-?video|t2v|sora|kling|hailuo|luma ray|runway|motion graphics)\b", re.IGNORECASE),
    ],
    BenchmarkDomain.CODING: [
        re.compile(r"\b(c[oó]digos?|fun[cç][aã]o|fun[cç][oõ]es|classes?|bugs?|refatora[cç][aã]o|pytest|testes?|git|commits?|pull requests?|apis?|backend|frontend|react|python|fastapi|typescript|banco de dados|sql|ast|linters?|compila[cç][aã]o)\b", re.IGNORECASE),
        re.compile(r"\b(code|coding|software|refactor(?:ing)?|functions?|classes?|unittest(?:s)?|debugging|debug|repo|repositor(?:y|ies)|pull_requests?|endpoints?|compilers?)\b", re.IGNORECASE),
    ],
}


class TaskBenchmarkRouter:
    """
    Analyzes developer requirements, selects the most relevant benchmark domain,
    retrieves the domain-specific Pareto frontier, and recommends the optimal model and Top-3.
    """

    def __init__(self, empirical_ledger: Optional[EmpiricalBenchmarkLedger] = None):
        self.empirical_ledger = empirical_ledger or EmpiricalBenchmarkLedger()

    def classify_intent(self, requirement: str) -> Tuple[BenchmarkDomain, float, str]:
        """
        Classifies the requirement text into one of the canonical BenchmarkDomains in <1ms.
        Returns (domain, confidence, explanation).
        """
        req_clean = requirement.strip()
        if not req_clean:
            return (
                BenchmarkDomain.CODING,
                0.70,
                "Defaulting to Coding domain for empty requirement."
            )

        match_counts: Dict[BenchmarkDomain, int] = {d: 0 for d in BenchmarkDomain}

        for domain, patterns in DOMAIN_INTENT_PATTERNS.items():
            for pattern in patterns:
                matches = pattern.findall(req_clean)
                match_counts[domain] += len(matches)

        # Find domain with highest matches
        best_domain = max(match_counts.keys(), key=lambda d: match_counts[d])
        best_count = match_counts[best_domain]

        if best_count == 0:
            # Fallback to Coding as core engineering baseline
            return (
                BenchmarkDomain.CODING,
                0.75,
                "General engineering context with no specific specialized domain terms detected; defaulting to SWE-bench Verified coding baseline."
            )

        # Compute confidence score
        total_matches = sum(match_counts.values())
        dominance_ratio = best_count / max(total_matches, 1)
        confidence = round(min(0.98, 0.70 + (0.28 * dominance_ratio)), 2)

        meta = DOMAIN_METADATA.get(best_domain.value)
        disp_name = meta.display_name if meta else best_domain.value
        explanation = (
            f"Detected strong semantic alignment with '{disp_name}' "
            f"({best_count} domain-specific signals found; {dominance_ratio*100:.0f}% intent dominance)."
        )

        return best_domain, confidence, explanation

    def route_task(
        self,
        requirement: str,
        complexity: str = "high",
        offline: bool = False,
        domain_override: Optional[str] = None,
    ) -> BenchmarkRoutingDecision:
        """
        Routes the task requirement to the appropriate benchmark domain and optimal model.
        """
        complexity = complexity.lower()
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())

        # Step 1: Determine Domain
        if domain_override and domain_override in [d.value for d in BenchmarkDomain]:
            detected_domain = BenchmarkDomain(domain_override)
            confidence = 1.0
            explanation = f"Manual override specified for domain '{detected_domain.value}'."
        else:
            detected_domain, confidence, explanation = self.classify_intent(requirement)

        domain_key = detected_domain.value
        meta = DOMAIN_METADATA.get(domain_key)
        canonical_bench = meta.canonical_benchmark if meta else "SWE-bench Verified"

        # Step 2: Handle Offline Mode
        if offline:
            if detected_domain == BenchmarkDomain.IMAGE_GENERATION:
                local_models = [
                    m for m in all_models
                    if (m.tier == ModelTier.LOCAL_ZERO_COST or m.provider in ["ollama", "local"])
                    and ("image_gen" in m.domain_scores or m.metadata.get("modality") == "image")
                ]
            elif detected_domain == BenchmarkDomain.VIDEO_GENERATION:
                local_models = [
                    m for m in all_models
                    if (m.tier == ModelTier.LOCAL_ZERO_COST or m.provider in ["ollama", "local"])
                    and ("video_gen" in m.domain_scores or m.metadata.get("modality") == "video")
                ]
            else:
                local_models = [
                    m for m in all_models
                    if (m.tier == ModelTier.LOCAL_ZERO_COST or m.provider in ["ollama", "local"])
                    and m.metadata.get("modality", "text") == "text"
                ]

            if not local_models:
                local_models = [m for m in all_models if m.tier == ModelTier.LOCAL_ZERO_COST or m.provider == "ollama"]
            if not local_models:
                local_models = all_models[:3]

            best_local = max(local_models, key=lambda m: (m.get_domain_score(domain_key) or 0.0))
            if detected_domain in [BenchmarkDomain.FORMAL_REASONING, BenchmarkDomain.LEGAL_CONTRACT] or (complexity in ["high", "critical"] and detected_domain == BenchmarkDomain.CODING):
                deep_opt = next((m for m in local_models if "deep" in m.model_id), None)
                if deep_opt:
                    best_local = deep_opt

            top3 = get_domain_top3_candidates(all_models, domain=domain_key, complexity=complexity, offline=True)
            return BenchmarkRoutingDecision(
                requirement=requirement,
                detected_domain=domain_key,
                confidence=confidence,
                intent_explanation=f"{explanation} (Routing to Local Ollama Cluster for $0 zero-cost offline execution).",
                canonical_benchmark=canonical_bench,
                optimal_model_id=best_local.model_id,
                optimal_model_name=best_local.name,
                domain_score=best_local.get_domain_score(domain_key),
                cost_per_task=0.0,
                speculative_candidates=[c.to_dict() for c in top3],
                local_empirical_stats=self.empirical_ledger.stats.get(best_local.model_id, None) and self.empirical_ledger.stats[best_local.model_id].to_dict(),
            )

        # Step 3: Compute/Retrieve Domain Pareto Frontier
        prod = [m for m in all_models if is_production_interactive_model(m, domain=domain_key)]
        if not prod:
            prod = [m for m in all_models if domain_key in m.domain_scores]
        if not prod:
            prod = [m for m in all_models if m.tier != ModelTier.LOCAL_ZERO_COST]
        if not prod:
            prod = all_models

        domain_frontier = compute_pareto_frontier(prod, metric=domain_key)
        if not domain_frontier:
            domain_frontier = prod

        # Select optimal model for domain & complexity
        if complexity == "critical":
            best_model = max(domain_frontier, key=lambda m: (m.get_domain_score(domain_key) or 0.0))
        elif complexity == "high":
            # Best quality-weighted efficiency
            candidates = [m for m in domain_frontier if (m.get_domain_score(domain_key) or 0.0) >= 85.0]
            if not candidates:
                candidates = domain_frontier
            best_model = max(candidates, key=lambda m: (((m.get_domain_score(domain_key) or 0.0) ** 2) / max(m.cost_per_task, 0.001)))
        elif complexity == "medium":
            candidates = [m for m in domain_frontier if (m.get_domain_score(domain_key) or 0.0) >= 80.0]
            if not candidates:
                candidates = domain_frontier
            best_model = max(candidates, key=lambda m: (m.get_domain_score(domain_key) or 0.0) / max(m.cost_per_task, 0.001))
        else:
            best_model = min(domain_frontier, key=lambda m: m.cost_per_task)

        # Step 4: Top 3 Speculative Candidates for Domain
        top3 = get_domain_top3_candidates(all_models, domain=domain_key, complexity=complexity, offline=False)

        # Step 5: Local Empirical Track Record
        emp_stats = self.empirical_ledger.stats.get(best_model.model_id)

        return BenchmarkRoutingDecision(
            requirement=requirement,
            detected_domain=domain_key,
            confidence=confidence,
            intent_explanation=explanation,
            canonical_benchmark=canonical_bench,
            optimal_model_id=best_model.model_id,
            optimal_model_name=best_model.name,
            domain_score=best_model.get_domain_score(domain_key),
            cost_per_task=best_model.cost_per_task,
            speculative_candidates=[c.to_dict() for c in top3],
            local_empirical_stats=emp_stats.to_dict() if emp_stats else None,
        )


_ROUTER_INSTANCE: Optional[TaskBenchmarkRouter] = None

def get_benchmark_router() -> TaskBenchmarkRouter:
    """Singleton getter for TaskBenchmarkRouter."""
    global _ROUTER_INSTANCE
    if _ROUTER_INSTANCE is None:
        _ROUTER_INSTANCE = TaskBenchmarkRouter()
    return _ROUTER_INSTANCE
