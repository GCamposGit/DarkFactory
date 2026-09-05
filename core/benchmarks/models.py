"""
Domain models and data structures for Model Benchmarks & Efficiency Frontier.
Strictly typed for compatibility with Universal Engineering Standards (AGENTS.md).
"""

from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


class ModelProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    XAI = "xai"
    META = "meta"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    ZHIPU = "zhipu"
    MOONSHOT = "moonshot"
    MINIMAX = "minimax"
    MISTRAL = "mistral"
    LOCAL_OLLAMA = "ollama"
    BLACK_FOREST = "black_forest"
    RECRAFT = "recraft"
    STABILITY = "stability"
    KUAISHOU = "kuaishou"
    LUMA = "luma"
    RUNWAY = "runway"
    LOCAL = "local"
    OTHER = "other"


class ModelTier(str, Enum):
    FRONTIER_HIGH = "frontier_high"        # High complexity architecture, challenging debugging, SWE-bench leaders
    BALANCED_MID = "balanced_mid"          # Medium complexity coding, general work, high Pareto efficiency
    FAST_ECONOMY = "fast_economy"          # Low latency, high throughput, boilerplate, simple fixes
    LOCAL_ZERO_COST = "local_zero_cost"    # Local Ollama cluster ($0 token cost)


class TaskComplexity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class BenchmarkDomain(str, Enum):
    CODING = "coding"                           # SWE-bench Verified, HumanEval, repo debugging
    DEEP_RESEARCH = "deep_research"             # GAIA, BrowseBench, live search, multi-hop reasoning
    LEGAL_CONTRACT = "legal_contract"           # LegalBench (Stanford 162 tasks), contract review, CUAD
    BUSINESS_AUTOMATION = "business_automation" # OSWorld, ToolBench, computer-use, JSON/API workflows
    FORMAL_REASONING = "formal_reasoning"       # AIME 2024/2025, MATH-500, Olympiad
    MULTIMODAL_AUDIO = "multimodal_audio"       # AudioBench, FLEURS WER, dual-channel transcription
    IMAGE_GENERATION = "image_gen"              # Artificial Analysis Text-to-Image ELO, ImageArena & GenEval
    VIDEO_GENERATION = "video_gen"              # VBench 2.0 & VideoArena ELO


@dataclass
class BenchmarkDomainMetadata:
    """Metadata describing a domain-specific evaluation benchmark."""
    domain: BenchmarkDomain
    display_name: str
    canonical_benchmark: str
    evaluates: str
    source_authority: str
    target_metric_scale: str = "0-100 (Higher is better)"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["domain"] = self.domain.value
        return data


@dataclass
class BenchmarkRoutingDecision:
    """Decision output by the TaskBenchmarkRouter selecting the optimal model and benchmark for a requirement."""
    requirement: str
    detected_domain: str
    confidence: float
    intent_explanation: str
    canonical_benchmark: str
    optimal_model_id: str
    optimal_model_name: str
    domain_score: float
    cost_per_task: float
    speculative_candidates: List[Dict[str, Any]]
    local_empirical_stats: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ModelBenchmarkEntry:
    """Represents benchmark, pricing, and performance metrics for a single LLM."""
    model_id: str
    name: str
    provider: str
    context_length: int
    input_cost_per_m: float       # USD per 1M prompt tokens
    output_cost_per_m: float      # USD per 1M completion tokens
    cost_per_task: float          # USD for standard engineering task (25k prompt + 2.5k completion)
    coding_score: float           # Artificial Analysis Coding Agent Index / SWE-bench composite (0-100)
    intelligence_score: float     # Artificial Analysis Intelligence Index v4.2 composite (0-100)
    output_speed_tps: float       # Output speed in tokens per second
    latency_ttft_sec: float       # Time to first token (seconds)
    tokens_per_task: int          # Average tokens generated per benchmark task
    efficiency_score_coding: float = 0.0    # Coding capability per unit of cost
    efficiency_score_general: float = 0.0   # General intelligence per unit of cost
    is_pareto_coding: bool = False          # True if on coding Pareto frontier
    is_pareto_general: bool = False         # True if on general intelligence Pareto frontier
    frontier_proximity_index: float = 0.0   # 0.0 to 1.0 (1.0 = on frontier)
    epsilon_gap_cost: float = 0.0          # USD delta to reach Pareto frontier at same capability
    epsilon_gap_capability: float = 0.0    # Score delta to reach Pareto frontier at same cost
    opportunity_score: float = 0.0         # Proximity adjusted by TPS, context length, latency advantages
    is_near_pareto: bool = False           # True if proximity >= 0.95 and not already pareto optimal
    empirical_pass_rate: Optional[float] = None # Empirical Pass@1 on DarkFac tasks (0.0 to 1.0)
    empirical_elo: Optional[float] = None  # Empirical Bradley-Terry Elo rating on factory tasks
    tier: ModelTier = ModelTier.BALANCED_MID
    benchmark_source: str = "artificial_analysis"
    release_date: Optional[str] = None
    cache_read_cost_per_m: Optional[float] = None
    has_subscription_plan: bool = False     # True if available in user's active paid subscriptions
    subscription_name: Optional[str] = None # e.g. "OpenAI Codex / Plus", "Google Gemini Advanced", "xAI Grok Premium+"
    marginal_cost_plan: float = 0.0         # Marginal cost per task under active subscription quota ($0.00)
    domain_scores: Dict[str, float] = field(default_factory=dict) # Specialized scores across domains (0-100)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_domain_score(self, domain: str) -> float:
        """Returns benchmark score for a specific domain, with graceful fallback to composite scores."""
        dom = domain.lower()
        if self.domain_scores and dom in self.domain_scores:
            return float(self.domain_scores[dom])
        if dom == "coding":
            return float(self.coding_score)
        if dom in ["formal_reasoning", "deep_research"]:
            return float(self.intelligence_score)
        if dom == "legal_contract":
            return round((float(self.intelligence_score) * 0.7 + float(self.coding_score) * 0.3), 1)
        if dom == "business_automation":
            return round((float(self.coding_score) * 0.6 + float(self.intelligence_score) * 0.4), 1)
        if dom == "multimodal_audio":
            return round(float(self.intelligence_score) * 0.95, 1)
        if dom in ["image_gen", "image_generation"]:
            return float(self.domain_scores.get("image_gen", self.intelligence_score))
        if dom in ["video_gen", "video_generation"]:
            return float(self.domain_scores.get("video_gen", self.intelligence_score))
        return float(self.coding_score)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["tier"] = self.tier.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelBenchmarkEntry":
        d = dict(data)
        if "tier" in d and isinstance(d["tier"], str):
            d["tier"] = ModelTier(d["tier"])
        return cls(**d)


@dataclass
class SpeculativeCandidate:
    """Represents a candidate model in a speculative execution race."""
    model_id: str
    name: str
    tier: str
    cost_per_task: float
    output_speed_tps: float
    coding_score: float
    frontier_proximity: float
    is_pareto: bool
    role: str  # "fast_drafter", "balanced_challenger", "frontier_arbiter"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SpeculativeRaceResult:
    """Result of an A/B speculative execution race across top candidates."""
    race_id: str
    timestamp: str
    task_id: str
    complexity: str
    candidates: List[str]
    winner_model_id: str
    winner_role: str
    escalation_occurred: bool
    duration_ms: float
    total_cost_usd: float
    cost_saved_usd: float
    verification_passed: bool
    verification_details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EmpiricalModelStats:
    """Cumulative empirical performance stats for a model on real DarkFac tasks."""
    model_id: str
    total_tasks_run: int = 0
    successful_tasks: int = 0
    pass_rate_at_1: float = 0.0
    total_cost_spent: float = 0.0
    avg_latency_ms: float = 0.0
    avg_tokens_generated: float = 0.0
    elo_rating: float = 1200.0
    last_updated: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmpiricalModelStats":
        return cls(**data)


@dataclass
class DailyBenchmarkLedger:
    """Snapshot of model benchmarks and efficiency frontiers for a given calendar day."""
    date: str                                       # YYYY-MM-DD
    updated_at: str                                 # ISO-8601 timestamp
    total_models_scanned: int
    frontier_models_count: int
    pareto_coding_models: List[str]                 # Model IDs on coding Pareto frontier
    pareto_general_models: List[str]                # Model IDs on general Pareto frontier
    models: Dict[str, ModelBenchmarkEntry] = field(default_factory=dict)
    recommendations_by_tier: Dict[str, str] = field(default_factory=dict)
    frontiers_by_domain: Dict[str, List[str]] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "updated_at": self.updated_at,
            "total_models_scanned": self.total_models_scanned,
            "frontier_models_count": self.frontier_models_count,
            "pareto_coding_models": self.pareto_coding_models,
            "pareto_general_models": self.pareto_general_models,
            "frontiers_by_domain": self.frontiers_by_domain,
            "models": {mid: m.to_dict() for mid, m in self.models.items()},
            "recommendations_by_tier": self.recommendations_by_tier,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DailyBenchmarkLedger":
        models_dict = {}
        for mid, mdata in data.get("models", {}).items():
            models_dict[mid] = ModelBenchmarkEntry.from_dict(mdata)

        return cls(
            date=data.get("date", ""),
            updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
            total_models_scanned=data.get("total_models_scanned", len(models_dict)),
            frontier_models_count=data.get("frontier_models_count", len(models_dict)),
            pareto_coding_models=data.get("pareto_coding_models", []),
            pareto_general_models=data.get("pareto_general_models", []),
            frontiers_by_domain=data.get("frontiers_by_domain", {}),
            models=models_dict,
            recommendations_by_tier=data.get("recommendations_by_tier", {}),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ParetoFrontierSummary:
    """Summary of Pareto-optimal models for quick decision making in the router."""
    date: str
    coding_frontier: List[ModelBenchmarkEntry]
    general_frontier: List[ModelBenchmarkEntry]
    most_cost_efficient_coding: Optional[ModelBenchmarkEntry]
    highest_capability_coding: Optional[ModelBenchmarkEntry]
    fastest_frontier_model: Optional[ModelBenchmarkEntry]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "coding_frontier": [m.to_dict() for m in self.coding_frontier],
            "general_frontier": [m.to_dict() for m in self.general_frontier],
            "most_cost_efficient_coding": self.most_cost_efficient_coding.to_dict() if self.most_cost_efficient_coding else None,
            "highest_capability_coding": self.highest_capability_coding.to_dict() if self.highest_capability_coding else None,
            "fastest_frontier_model": self.fastest_frontier_model.to_dict() if self.fastest_frontier_model else None,
        }
