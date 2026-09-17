"""Pydantic v2 domain models for Portfolio Efficiency, Capacity & Routing (HF-23).

Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 and Universal Engineering Standards.
Defines contracts for weighted fair queueing, slot allocation (1 heavy, 4 light),
project budget ceilings, and paid-account-first model routing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobSlotKind(str, Enum):
    """Execution intensity category for worker slots."""
    HEAVY = "heavy"  # GPU Ollama execution, comprehensive test suites, large builds
    LIGHT = "light"  # Subagent IO, linting, single unit tests, API web research


class SlotAllocation(BaseModel):
    """Active reservation of a worker capacity slot."""
    model_config = ConfigDict(extra="ignore")

    slot_id: str
    job_id: str
    worker_id: str
    kind: JobSlotKind
    acquired_at: str = Field(default_factory=utc_now_iso)
    expires_at: str


class SlotCapacityConfig(BaseModel):
    """Concurrence limits for worker execution slots."""
    model_config = ConfigDict(extra="forbid")

    max_heavy_slots: int = 1
    max_light_slots: int = 4


class ProjectWeightConfig(BaseModel):
    """Prioritization weights and starvation limits across registered projects."""
    model_config = ConfigDict(extra="forbid")

    weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "atrium": 3.0,
            "jarvis": 2.0,
            "darkfac": 1.0,
        }
    )
    default_weight: float = 1.0
    max_starvation_ticks: int = 5


class BudgetStatus(str, Enum):
    ACTIVE = "active"
    WARNING = "warning"      # >= 80% of monthly ceiling
    LOCAL_ONLY = "local_only"  # >= 100% of monthly ceiling, forced $0 local


class ProjectBudgetConfig(BaseModel):
    """Monthly USD budget ceiling and tracking for a project."""
    model_config = ConfigDict(extra="ignore")

    project_id: str
    monthly_limit_usd: float = Field(..., ge=0.0)
    current_spent_usd: float = Field(0.0, ge=0.0)
    alert_threshold: float = 0.80
    cutoff_threshold: float = 1.00
    status: BudgetStatus = BudgetStatus.ACTIVE
    last_alert_at: Optional[str] = None
    updated_at: str = Field(default_factory=utc_now_iso)

    @property
    def utilization_pct(self) -> float:
        if self.monthly_limit_usd <= 0:
            return 0.0
        return round((self.current_spent_usd / self.monthly_limit_usd) * 100.0, 2)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, round(self.monthly_limit_usd - self.current_spent_usd, 4))


class ModelTier(str, Enum):
    """Execution model billing tiers."""
    PAID_DIRECT = "paid_direct"        # Gemini 3.8 Flash, Grok 4.6, OpenAI/Luna xhigh
    OPENROUTER_FRONTIER = "openrouter" # DeepSeek, Qwen3, frontier via OpenRouter
    LOCAL_ZERO = "local_zero"          # Ollama local (qwen-fast, qwen-deep) at $0


class RoutingPolicyResult(BaseModel):
    """Outcome of resolving the optimal model for a task."""
    model_config = ConfigDict(extra="ignore")

    project_id: str
    task_type: str
    selected_model: str
    provider: str
    tier: ModelTier
    estimated_cost_usd: float
    reason: str
    fallback_model: Optional[str] = None


class PortfolioEfficiencyReport(BaseModel):
    """Aggregated portfolio runtime status, queue metrics, and budget telemetry."""
    model_config = ConfigDict(extra="ignore")

    active_heavy_slots: int
    max_heavy_slots: int
    active_light_slots: int
    max_light_slots: int
    queued_jobs_by_project: Dict[str, int]
    starvation_ticks_by_project: Dict[str, int]
    budgets: List[ProjectBudgetConfig]
    generated_at: str = Field(default_factory=utc_now_iso)
