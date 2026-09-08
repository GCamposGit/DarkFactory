"""
Pareto Efficiency Frontier Engine for LLM Models.
Determines non-dominated models across capability, task cost, latency, and throughput.
"""

import math
from typing import List, Dict, Tuple, Optional
from core.benchmarks.models import (
    ModelBenchmarkEntry,
    ParetoFrontierSummary,
    SpeculativeCandidate,
    BenchmarkDomain,
    BenchmarkDomainMetadata,
    ModelTier,
    TaskComplexity,
)

DEFAULT_STANDARD_INPUT_TOKENS = 25000
DEFAULT_STANDARD_OUTPUT_TOKENS = 2500


def calculate_task_cost(
    input_cost_per_m: float,
    output_cost_per_m: float,
    input_tokens: int = DEFAULT_STANDARD_INPUT_TOKENS,
    output_tokens: int = DEFAULT_STANDARD_OUTPUT_TOKENS,
) -> float:
    """Calculates estimated cost for a standard engineering task in USD."""
    prompt_cost = (input_tokens / 1_000_000.0) * input_cost_per_m
    comp_cost = (output_tokens / 1_000_000.0) * output_cost_per_m
    return round(prompt_cost + comp_cost, 6)


def calculate_efficiency_score(capability_score: float, cost_per_task: float) -> float:
    """
    Computes capability-to-cost efficiency index.
    Higher score indicates superior return per dollar spent.
    """
    # Guard against zero-cost (local models) or free tier
    effective_cost = max(cost_per_task, 0.001)
    return round(capability_score / effective_cost, 2)


DOMAIN_METADATA: Dict[str, BenchmarkDomainMetadata] = {
    "coding": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.CODING,
        display_name="Software Engineering & Coding",
        canonical_benchmark="SWE-bench Verified & Coding Agent Index",
        evaluates="Issue resolution, diff generation, AST modification, unit test passing in real repositories.",
        source_authority="Princeton / OpenAI / Artificial Analysis",
    ),
    "deep_research": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.DEEP_RESEARCH,
        display_name="Deep Web Research & Agentic Investigation",
        canonical_benchmark="GAIA Benchmark & BrowseBench",
        evaluates="Multi-hop reasoning, live web discovery, multimodal search, hallucination resistance.",
        source_authority="Meta AI / Hugging Face / AutoGPT",
    ),
    "legal_contract": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.LEGAL_CONTRACT,
        display_name="Contract Review, Legal & Compliance",
        canonical_benchmark="LegalBench (Stanford 162-Task Composite) & CUAD",
        evaluates="Contract clause extraction, issue-spotting, statutory interpretation, regulatory risk.",
        source_authority="Stanford Center for Legal Informatics / CUAD",
    ),
    "business_automation": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.BUSINESS_AUTOMATION,
        display_name="Business Workflow Automation & Tool-Use",
        canonical_benchmark="OSWorld & ToolBench",
        evaluates="Computer-use desktop automation, multi-step API tool calling, spreadsheet & ERP workflows.",
        source_authority="Carnegie Mellon University / ServiceNow",
    ),
    "formal_reasoning": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.FORMAL_REASONING,
        display_name="Formal Logic, Science & Mathematics",
        canonical_benchmark="AIME 2024/2025 & MATH-500",
        evaluates="Hard deductive logic, mathematical proofs, competition problem solving.",
        source_authority="Mathematical Association of America / UC Berkeley",
    ),
    "multimodal_audio": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.MULTIMODAL_AUDIO,
        display_name="Edge Audio, Voice & Multimodal Intelligence",
        canonical_benchmark="AudioBench & FLEURS Word Error Rate (WER)",
        evaluates="Dual-channel audio transcription, speaker diarization, voice activity detection, multimodal reasoning.",
        source_authority="OpenAI / Google / Meta Audio Research",
    ),
    "image_gen": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.IMAGE_GENERATION,
        display_name="Text-to-Image Generation & Visual Synthesis",
        canonical_benchmark="Artificial Analysis Text-to-Image ELO & GenEval",
        evaluates="Visual fidelity, prompt adherence, typography rendering, spatial coherence, style versatility.",
        source_authority="Artificial Analysis / ImageArena / GenEval Benchmark Consortium",
    ),
    "video_gen": BenchmarkDomainMetadata(
        domain=BenchmarkDomain.VIDEO_GENERATION,
        display_name="Text-to-Video Synthesis & Cinematic Generation",
        canonical_benchmark="VBench 2.0 & VideoArena ELO",
        evaluates="Temporal consistency, dynamic motion fidelity, cinematic framing, visual artifacts, prompt adherence.",
        source_authority="VBench Consortium / VideoArena / Shanghai AI Lab",
    ),
}


def get_model_metric_score(model: ModelBenchmarkEntry, metric: str) -> float:
    """Extracts score from attribute or domain_scores dictionary."""
    if hasattr(model, metric):
        val = getattr(model, metric)
        if isinstance(val, (int, float)):
            return float(val)
    return float(model.get_domain_score(metric))


def compute_pareto_frontier(
    models: List[ModelBenchmarkEntry],
    metric: str = "coding_score"
) -> List[ModelBenchmarkEntry]:
    """
    Calculates the 2D Pareto Efficiency Frontier for (cost_per_task, capability_score).
    A model A is Pareto-optimal if NO other model B has:
    - capability_score >= A's capability_score AND
    - cost_per_task <= A's cost_per_task
    with at least one strict inequality.
    """
    if not models:
        return []

    # Sort primarily by cost ascending, then capability descending
    sorted_models = sorted(
        models,
        key=lambda m: (m.cost_per_task, -get_model_metric_score(m, metric))
    )

    frontier: List[ModelBenchmarkEntry] = []
    max_capability_seen = -1.0

    for model in sorted_models:
        score = get_model_metric_score(model, metric)
        # If this model achieves a higher capability than any cheaper model seen so far,
        # it is Pareto-optimal (strictly expands the capability envelope).
        if score > max_capability_seen:
            frontier.append(model)
            max_capability_seen = score

    return frontier


def is_production_interactive_model(m: ModelBenchmarkEntry, domain: Optional[str] = None) -> bool:
    """Filters out promotional free tiers, batch queues, data-sharing contributor tiers, and non-target modality models."""
    mid = m.model_id.lower()
    name = m.name.lower()
    dom = domain.lower() if domain else None

    # 1. Reject promotional free endpoints and asynchronous batch queues
    if mid.endswith(":free") or mid.endswith(":batch"):
        return False
    # 2. Reject data-sharing / contributor tiers (e.g. Meta Contributor) to protect proprietary code
    if "contributor" in mid or "contributor" in name or "data-sharing" in mid:
        return False

    # 3. Local models are evaluated separately under Local-First ($0)
    if m.tier == ModelTier.LOCAL_ZERO_COST or m.provider in ["ollama", "local"]:
        return False

    # 4. Modality / domain filtering
    modality = m.metadata.get("modality", "text") if hasattr(m, "metadata") and m.metadata else "text"

    if dom in ["image_gen", "image_generation"]:
        return modality == "image" or "image_gen" in m.domain_scores

    if dom in ["video_gen", "video_generation"]:
        return modality == "video" or "video_gen" in m.domain_scores

    # Text / General domains (coding, deep_research, legal_contract, business_automation, formal_reasoning, multimodal_audio)
    # Reject dedicated media-only models
    if modality in ["image", "video"]:
        return False
    if any(k in mid for k in ["lyria", "whisper", "tts", "dall-e", "midjourney", "flux", "sora", "kling", "hailuo", "runway", "luma", "recraft", "ltx-video", "cogvideox"]):
        return False

    return True


def compute_frontier_proximity_indices(
    models: List[ModelBenchmarkEntry],
    metric: str = "coding_score"
) -> List[ModelBenchmarkEntry]:
    """
    Computes the Frontier Proximity Index (FPI), epsilon-gap (cost and capability),
    and opportunity score for all models relative to the Pareto frontier.
    
    FPI in [0.0, 1.0]:
      - 1.0 = Exactly on the Pareto frontier
      - >= 0.95 = Near-Pareto challenger (within 5% of optimal trade-off curve)
      - < 0.85 = Dominated / sub-optimal
      
    Epsilon-gap:
      - epsilon_gap_capability: Points needed to reach the frontier at current cost
      - epsilon_gap_cost: USD cost reduction needed to reach frontier at current capability
    """
    if not models:
        return models

    eval_models = [m for m in models if is_production_interactive_model(m, domain=metric)]
    if not eval_models:
        eval_models = [m for m in models if metric in m.domain_scores]
    if not eval_models:
        eval_models = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]
    if not eval_models:
        eval_models = models

    frontier = compute_pareto_frontier(eval_models, metric=metric)
    if not frontier:
        return models

    frontier_ids = {m.model_id for m in frontier}

    costs = [max(m.cost_per_task, 0.0001) for m in eval_models]
    scores = [get_model_metric_score(m, metric) for m in eval_models]

    min_log_c = math.log(min(costs))
    max_log_c = math.log(max(costs))
    log_c_range = max_log_c - min_log_c if max_log_c > min_log_c else 1.0

    min_s = min(scores)
    max_s = max(scores)
    s_range = max_s - min_s if max_s > min_s else 1.0

    frontier_pts = []
    for f in frontier:
        log_c = math.log(max(f.cost_per_task, 0.0001))
        norm_c = (log_c - min_log_c) / log_c_range
        norm_s = (get_model_metric_score(f, metric) - min_s) / s_range
        frontier_pts.append((norm_c, norm_s, f.cost_per_task, get_model_metric_score(f, metric)))

    frontier_pts.sort(key=lambda p: p[0])

    for m in models:
        is_on_frontier = m.model_id in frontier_ids
        score_val = get_model_metric_score(m, metric)
        cost_val = max(m.cost_per_task, 0.0001)

        if is_on_frontier:
            m.frontier_proximity_index = 1.0
            m.epsilon_gap_cost = 0.0
            m.epsilon_gap_capability = 0.0
            m.is_near_pareto = False
        else:
            log_c = math.log(cost_val)
            norm_c = (log_c - min_log_c) / log_c_range
            norm_s = (score_val - min_s) / s_range

            min_dist = float("inf")
            if len(frontier_pts) == 1:
                fc, fs, _, _ = frontier_pts[0]
                min_dist = math.sqrt((norm_c - fc) ** 2 + (norm_s - fs) ** 2)
            else:
                for i in range(len(frontier_pts) - 1):
                    ax, ay, _, _ = frontier_pts[i]
                    bx, by, _, _ = frontier_pts[i + 1]
                    vx = bx - ax
                    vy = by - ay
                    v_len_sq = vx * vx + vy * vy
                    if v_len_sq == 0:
                        dist = math.sqrt((norm_c - ax) ** 2 + (norm_s - ay) ** 2)
                    else:
                        t = max(0.0, min(1.0, ((norm_c - ax) * vx + (norm_s - ay) * vy) / v_len_sq))
                        cx = ax + t * vx
                        cy = ay + t * vy
                        dist = math.sqrt((norm_c - cx) ** 2 + (norm_s - cy) ** 2)
                    min_dist = min(min_dist, dist)

            fpi = max(0.0, min(1.0, 1.0 - min_dist))
            m.frontier_proximity_index = round(fpi, 4)
            m.is_near_pareto = (fpi >= 0.95)

            # Epsilon-gap:
            achievable_q = frontier_pts[0][3]
            if cost_val <= frontier_pts[0][2]:
                achievable_q = frontier_pts[0][3]
            elif cost_val >= frontier_pts[-1][2]:
                achievable_q = frontier_pts[-1][3]
            else:
                for i in range(len(frontier_pts) - 1):
                    c0, q0 = frontier_pts[i][2], frontier_pts[i][3]
                    c1, q1 = frontier_pts[i + 1][2], frontier_pts[i + 1][3]
                    if c0 <= cost_val <= c1 and c1 > c0:
                        achievable_q = q0 + ((cost_val - c0) / (c1 - c0)) * (q1 - q0)
                        break
            m.epsilon_gap_capability = max(0.0, round(achievable_q - score_val, 2))

            target_c = frontier_pts[0][2]
            if score_val <= frontier_pts[0][3]:
                target_c = frontier_pts[0][2]
            elif score_val >= frontier_pts[-1][3]:
                target_c = frontier_pts[-1][2]
            else:
                for i in range(len(frontier_pts) - 1):
                    c0, q0 = frontier_pts[i][2], frontier_pts[i][3]
                    c1, q1 = frontier_pts[i + 1][2], frontier_pts[i + 1][3]
                    if q0 <= score_val <= q1 and q1 > q0:
                        target_c = c0 + ((score_val - q0) / (q1 - q0)) * (c1 - c0)
                        break
            m.epsilon_gap_cost = max(0.0, round(cost_val - target_c, 6))

        bonus = 0.0
        if m.output_speed_tps >= 110.0:
            bonus += 0.10
        elif m.output_speed_tps >= 80.0:
            bonus += 0.05
        if m.context_length >= 500_000:
            bonus += 0.08
        if 0.0 < m.latency_ttft_sec <= 0.40:
            bonus += 0.06

        m.opportunity_score = round(m.frontier_proximity_index * (1.0 + bonus), 4)

    return models


def get_top_candidates_for_tier(
    models: List[ModelBenchmarkEntry],
    tier: str = "high",
    k: int = 3,
) -> List[SpeculativeCandidate]:
    """
    Selects the Top-K candidate models for an intelligence/capability tier,
    assigning them complementary roles for speculative execution (Drafter, Challenger, Arbiter).
    """
    tier = tier.lower()

    if tier in ["local", "local_fast", "offline"]:
        local_models = [
            m
            for m in models
            if (m.tier == ModelTier.LOCAL_ZERO_COST or m.provider == "ollama")
            and (not m.domain_scores or "coding" in m.domain_scores)
            and m.metadata.get("modality", "text") not in {"image", "video"}
        ]
        if not local_models:
            local_models = models[:k]

        sorted_local = sorted(local_models, key=lambda m: (-m.output_speed_tps, -m.coding_score))
        roles = ["fast_drafter", "balanced_challenger", "frontier_arbiter"]
        candidates = []
        for i, m in enumerate(sorted_local[:k]):
            role = roles[i] if i < len(roles) else "challenger"
            candidates.append(SpeculativeCandidate(
                model_id=m.model_id,
                name=m.name,
                tier=m.tier.value,
                cost_per_task=m.cost_per_task,
                output_speed_tps=m.output_speed_tps,
                coding_score=m.coding_score,
                frontier_proximity=m.frontier_proximity_index,
                is_pareto=m.is_pareto_coding,
                role=role,
            ))
        return candidates

    prod = [m for m in models if is_production_interactive_model(m)]
    if not prod:
        prod = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]
    if not prod:
        prod = models

    compute_frontier_proximity_indices(prod, metric="coding_score")
    candidates: List[SpeculativeCandidate] = []

    if tier == "critical":
        arbiter = max(prod, key=lambda m: m.coding_score)
        ch_pool = [m for m in prod if m.model_id != arbiter.model_id and m.coding_score >= 88.0]
        if not ch_pool:
            ch_pool = [m for m in prod if m.model_id != arbiter.model_id]
        challenger = max(ch_pool, key=lambda m: m.opportunity_score) if ch_pool else arbiter

        dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id) and m.coding_score >= 86.0]
        if not dr_pool:
            dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id)]
        drafter = max(dr_pool, key=lambda m: m.output_speed_tps) if dr_pool else challenger

        for m, role in [(drafter, "fast_drafter"), (challenger, "balanced_challenger"), (arbiter, "frontier_arbiter")]:
            candidates.append(SpeculativeCandidate(
                model_id=m.model_id,
                name=m.name,
                tier=m.tier.value,
                cost_per_task=m.cost_per_task,
                output_speed_tps=m.output_speed_tps,
                coding_score=m.coding_score,
                frontier_proximity=m.frontier_proximity_index,
                is_pareto=m.is_pareto_coding,
                role=role,
            ))

    elif tier == "high":
        high_pool = [m for m in prod if m.coding_score >= 85.0]
        if not high_pool:
            high_pool = prod
        arbiter = max(high_pool, key=lambda m: (m.coding_score ** 2) / max(m.cost_per_task, 0.001))

        ch_pool = [m for m in high_pool if m.model_id != arbiter.model_id]
        if not ch_pool:
            ch_pool = prod
        challenger = max(ch_pool, key=lambda m: m.opportunity_score)

        dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id) and m.cost_per_task <= 0.01]
        if not dr_pool:
            dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id)]
        drafter = max(dr_pool, key=lambda m: m.output_speed_tps) if dr_pool else challenger

        for m, role in [(drafter, "fast_drafter"), (challenger, "balanced_challenger"), (arbiter, "frontier_arbiter")]:
            candidates.append(SpeculativeCandidate(
                model_id=m.model_id,
                name=m.name,
                tier=m.tier.value,
                cost_per_task=m.cost_per_task,
                output_speed_tps=m.output_speed_tps,
                coding_score=m.coding_score,
                frontier_proximity=m.frontier_proximity_index,
                is_pareto=m.is_pareto_coding,
                role=role,
            ))

    else:
        med_pool = [m for m in prod if m.coding_score >= 80.0]
        if not med_pool:
            med_pool = prod
        arbiter = max(med_pool, key=lambda m: m.efficiency_score_coding)

        ch_pool = [m for m in med_pool if m.model_id != arbiter.model_id]
        if not ch_pool:
            ch_pool = prod
        challenger = max(ch_pool, key=lambda m: m.opportunity_score)

        dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id)]
        drafter = min(dr_pool, key=lambda m: m.cost_per_task) if dr_pool else arbiter

        for m, role in [(drafter, "fast_drafter"), (challenger, "balanced_challenger"), (arbiter, "frontier_arbiter")]:
            candidates.append(SpeculativeCandidate(
                model_id=m.model_id,
                name=m.name,
                tier=m.tier.value,
                cost_per_task=m.cost_per_task,
                output_speed_tps=m.output_speed_tps,
                coding_score=m.coding_score,
                frontier_proximity=m.frontier_proximity_index,
                is_pareto=m.is_pareto_coding,
                role=role,
            ))

    return candidates[:k]


def evaluate_reasoning_effort(
    entry: ModelBenchmarkEntry,
    effort_level: str = "medium"
) -> Tuple[float, float]:
    """
    Models the effort scaling curve for reasoning models (e.g. Luna, DeepSeek-R1, o-series).
    Returns (effective_cost_per_task, effective_coding_score).
    """
    effort_multipliers = {
        "low": (0.6, 0.95),      # 40% cheaper, 5% lower score
        "medium": (1.0, 1.0),    # Baseline
        "high": (1.8, 1.04),     # 80% more tokens, 4% higher score
        "max": (3.2, 1.07),      # 3.2x cost, 7% higher score
    }
    cost_mult, score_mult = effort_multipliers.get(effort_level.lower(), (1.0, 1.0))
    eff_cost = round(entry.cost_per_task * cost_mult, 6)
    eff_score = min(100.0, round(entry.coding_score * score_mult, 2))
    return eff_cost, eff_score


def build_frontier_summary(
    date: str,
    models: List[ModelBenchmarkEntry]
) -> ParetoFrontierSummary:
    """Extracts Pareto optimal models and recommends key frontier archetypes."""
    prod_models = [m for m in models if is_production_interactive_model(m)]
    if not prod_models:
        prod_models = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]

    coding_frontier = compute_pareto_frontier(prod_models, metric="coding_score")
    general_frontier = compute_pareto_frontier(prod_models, metric="intelligence_score")

    coding_ids = {m.model_id for m in coding_frontier}
    general_ids = {m.model_id for m in general_frontier}

    for m in models:
        m.is_pareto_coding = m.model_id in coding_ids
        m.is_pareto_general = m.model_id in general_ids

    # Compute proximity, epsilon-gap and opportunity scores
    compute_frontier_proximity_indices(models, metric="coding_score")

    most_efficient = None
    if coding_frontier:
        cloud_coding = [m for m in coding_frontier if m.cost_per_task > 0]
        if cloud_coding:
            most_efficient = max(cloud_coding, key=lambda m: m.efficiency_score_coding)
        else:
            most_efficient = coding_frontier[0]

    highest_capability = None
    if coding_frontier:
        highest_capability = max(coding_frontier, key=lambda m: m.coding_score)

    fastest_model = None
    if models:
        fastest_model = max(models, key=lambda m: m.output_speed_tps)

    return ParetoFrontierSummary(
        date=date,
        coding_frontier=coding_frontier,
        general_frontier=general_frontier,
        most_cost_efficient_coding=most_efficient,
        highest_capability_coding=highest_capability,
        fastest_frontier_model=fastest_model,
    )


def select_best_model_for_task(
    models: List[ModelBenchmarkEntry],
    task_type: str = "coding",
    complexity: str = "medium",
    offline: bool = False,
) -> Tuple[ModelBenchmarkEntry, str]:
    """
    Selects the optimal model from the efficiency frontier for a given task and complexity.
    Returns (ModelBenchmarkEntry, reason).
    """
    task_type = task_type.lower()
    complexity = complexity.lower()

    if offline:
        local_models = [m for m in models if m.tier == ModelTier.LOCAL_ZERO_COST]
        if local_models:
            if complexity in ["high", "critical"] or task_type == "architecture":
                # Prefer deep model
                best_local = next((m for m in local_models if "deep" in m.model_id), local_models[0])
                return best_local, "Local Ollama MoE ($0 cost) selected for high-complexity offline execution."
            else:
                best_local = next((m for m in local_models if "fast" in m.model_id), local_models[0])
                return best_local, "Local Ollama Fast Worker ($0 cost) selected for offline execution."

    # Compute coding frontier on clean production models (no contributor, no free-tier rate limits)
    prod_models = [m for m in models if is_production_interactive_model(m)]
    if not prod_models:
        prod_models = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]

    coding_frontier = compute_pareto_frontier(prod_models, metric="coding_score")
    cloud_frontier = coding_frontier if coding_frontier else prod_models

    if complexity == "critical":
        # Critical architecture/debugging: pick the absolute frontier leader in capability
        best = max(cloud_frontier, key=lambda m: m.coding_score)
        return (
            best,
            f"Critical-tier capability leader: coding score {best.coding_score} (${best.cost_per_task}/task)."
        )

    elif complexity == "high" or task_type in ["architecture", "plan"]:
        # High complexity: needs strong coding capability (>= 85.0), balancing capability and efficiency
        candidates = [m for m in cloud_frontier if m.coding_score >= 85.0]
        if not candidates:
            candidates = sorted(cloud_frontier, key=lambda m: m.coding_score, reverse=True)[:3]

        # Quality-weighted efficiency (capability^2 / cost)
        best = max(candidates, key=lambda m: (m.coding_score ** 2) / max(m.cost_per_task, 0.001))
        return (
            best,
            f"High-complexity Pareto leader: coding score {best.coding_score} at ${best.cost_per_task}/task "
            f"(${best.input_cost_per_m}/M in, ${best.output_cost_per_m}/M out)."
        )

    elif complexity == "medium" or task_type in ["review", "scout"]:
        # Medium complexity: sweet spot (coding score >= 80.0), highest efficiency score
        candidates = [m for m in cloud_frontier if m.coding_score >= 80.0]
        if not candidates:
            candidates = cloud_frontier

        best = max(candidates, key=lambda m: m.efficiency_score_coding)
        return (
            best,
            f"Balanced-tier Pareto leader: coding score {best.coding_score} with optimal cost/perf ratio "
            f"({best.efficiency_score_coding} eff. score, ${best.cost_per_task}/task)."
        )

    else:
        # Low complexity: speed and economy are paramount
        candidates = [m for m in cloud_frontier if m.tier == ModelTier.FAST_ECONOMY or m.cost_per_task <= 0.015]
        if not candidates:
            candidates = sorted(cloud_frontier, key=lambda m: m.cost_per_task)[:3]

        best = max(candidates, key=lambda m: m.output_speed_tps)
        return (
            best,
            f"Fast-economy Pareto leader: {best.output_speed_tps} tps throughput, ${best.cost_per_task}/task."
        )


def compute_domain_pareto_frontiers(
    models: List[ModelBenchmarkEntry]
) -> Dict[str, List[ModelBenchmarkEntry]]:
    """
    Computes specialized Pareto efficiency frontiers across all supported benchmark domains:
    coding, deep_research, legal_contract, business_automation, formal_reasoning, multimodal_audio,
    image_gen, video_gen.
    """
    frontiers: Dict[str, List[ModelBenchmarkEntry]] = {}
    for domain_key in DOMAIN_METADATA.keys():
        prod_models = [m for m in models if is_production_interactive_model(m, domain=domain_key)]
        if not prod_models:
            prod_models = [m for m in models if domain_key in m.domain_scores]
        if not prod_models:
            prod_models = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]
        if not prod_models:
            prod_models = models
        frontiers[domain_key] = compute_pareto_frontier(prod_models, metric=domain_key)

    return frontiers


def get_domain_top3_candidates(
    models: List[ModelBenchmarkEntry],
    domain: str = "coding",
    complexity: str = "high",
    offline: bool = False,
) -> List[SpeculativeCandidate]:
    """
    Selects the Top-3 candidate models (Drafter, Challenger, Arbiter) optimized
    specifically for a given benchmark domain and task complexity.
    """
    domain = domain.lower()
    if offline:
        if domain in ["image_gen", "video_gen"]:
            local_domain_models = [
                m for m in models
                if (m.tier == ModelTier.LOCAL_ZERO_COST or m.provider in ["ollama", "local"])
                and (domain in m.domain_scores or m.metadata.get("modality") == domain.split("_")[0])
            ]
            if local_domain_models:
                sorted_local = sorted(local_domain_models, key=lambda m: -m.get_domain_score(domain))
                roles = ["fast_drafter", "balanced_challenger", "frontier_arbiter"]
                candidates = []
                for i, m in enumerate(sorted_local[:3]):
                    role = roles[i] if i < len(roles) else "challenger"
                    candidates.append(SpeculativeCandidate(
                        model_id=m.model_id,
                        name=m.name,
                        tier=m.tier.value,
                        cost_per_task=m.cost_per_task,
                        output_speed_tps=m.output_speed_tps,
                        coding_score=m.get_domain_score(domain),
                        frontier_proximity=1.0,
                        is_pareto=True,
                        role=role,
                    ))
                return candidates
        return get_top_candidates_for_tier(models, tier="local_fast", k=3)

    prod = [m for m in models if is_production_interactive_model(m, domain=domain)]
    if not prod:
        prod = [m for m in models if domain in m.domain_scores]
    if not prod:
        prod = [m for m in models if m.tier != ModelTier.LOCAL_ZERO_COST]
    if not prod:
        prod = models

    # Ensure proximity indices for this domain
    compute_frontier_proximity_indices(prod, metric=domain)

    frontier = compute_pareto_frontier(prod, metric=domain)
    if not frontier:
        frontier = prod

    # Arbiter: top capability on the domain frontier
    arbiter = max(frontier, key=lambda m: m.get_domain_score(domain))

    # Challenger: highest opportunity score or quality-weighted efficiency
    ch_pool = [m for m in prod if m.model_id != arbiter.model_id]
    if not ch_pool:
        ch_pool = prod
    challenger = max(ch_pool, key=lambda m: (m.get_domain_score(domain) ** 2) / max(m.cost_per_task, 0.001))

    # Drafter: fastest or cheapest with respectable capability in domain
    dr_pool = [m for m in prod if m.model_id not in (arbiter.model_id, challenger.model_id)]
    if not dr_pool:
        dr_pool = [m for m in prod if m.model_id != arbiter.model_id]
    drafter = min(dr_pool, key=lambda m: m.cost_per_task) if dr_pool else challenger

    roles = [
        (drafter, "fast_drafter"),
        (challenger, "balanced_challenger"),
        (arbiter, "frontier_arbiter"),
    ]

    candidates: List[SpeculativeCandidate] = []
    frontier_ids = {m.model_id for m in frontier}

    for m, role in roles:
        candidates.append(SpeculativeCandidate(
            model_id=m.model_id,
            name=m.name,
            tier=m.tier.value,
            cost_per_task=m.cost_per_task,
            output_speed_tps=m.output_speed_tps,
            coding_score=m.get_domain_score(domain),
            frontier_proximity=m.frontier_proximity_index,
            is_pareto=(m.model_id in frontier_ids),
            role=role,
        ))

    return candidates
