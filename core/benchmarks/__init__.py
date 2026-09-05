"""
Dark Factory Model Benchmark & Efficiency Frontier Engine.
Automated, daily evaluation of LLM models, costs, and Pareto efficiency frontiers.
"""

__version__ = "1.2.0"

from core.benchmarks.models import (
    ModelBenchmarkEntry,
    DailyBenchmarkLedger,
    ParetoFrontierSummary,
    SpeculativeCandidate,
    SpeculativeRaceResult,
    EmpiricalModelStats,
    BenchmarkDomain,
    BenchmarkDomainMetadata,
    BenchmarkRoutingDecision,
    ModelTier,
    TaskComplexity,
)
from core.benchmarks.frontier import (
    DOMAIN_METADATA,
    compute_pareto_frontier,
    compute_domain_pareto_frontiers,
    compute_frontier_proximity_indices,
    get_top_candidates_for_tier,
    get_domain_top3_candidates,
    evaluate_reasoning_effort,
    build_frontier_summary,
    select_best_model_for_task,
    is_production_interactive_model,
)
from core.benchmarks.fetcher import (
    DailyBenchmarkService,
    get_daily_benchmark_service,
    ensure_daily_benchmark,
)
from core.benchmarks.racing import (
    EmpiricalBenchmarkLedger,
    SpeculativeRacingEngine,
)
from core.benchmarks.router import (
    TaskBenchmarkRouter,
    get_benchmark_router,
)

__all__ = [
    "ModelBenchmarkEntry",
    "DailyBenchmarkLedger",
    "ParetoFrontierSummary",
    "SpeculativeCandidate",
    "SpeculativeRaceResult",
    "EmpiricalModelStats",
    "BenchmarkDomain",
    "BenchmarkDomainMetadata",
    "BenchmarkRoutingDecision",
    "ModelTier",
    "TaskComplexity",
    "DOMAIN_METADATA",
    "compute_pareto_frontier",
    "compute_domain_pareto_frontiers",
    "compute_frontier_proximity_indices",
    "get_top_candidates_for_tier",
    "get_domain_top3_candidates",
    "evaluate_reasoning_effort",
    "build_frontier_summary",
    "select_best_model_for_task",
    "is_production_interactive_model",
    "DailyBenchmarkService",
    "get_daily_benchmark_service",
    "ensure_daily_benchmark",
    "EmpiricalBenchmarkLedger",
    "SpeculativeRacingEngine",
    "TaskBenchmarkRouter",
    "get_benchmark_router",
]

