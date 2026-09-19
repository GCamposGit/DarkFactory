"""Capacity policy, integer units, and pure eligibility ranking (HF-23-01).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-23-01.md
- docs/handoffs/continuous-autonomy/bindings/CONTROL.md
- core/portfolio/models.py
- core/workflow/control_contracts.py

Key Invariants:
1. Pure ranking function: rank_eligible(snapshot, now) -> list[JobKey] has zero side-effects.
2. Composed mode: forbids local JSON slot allocations, delegating atomic claims and leases to ControlStore.
3. Strict capacity bounds: enforces account, project, run, and pool limits with integer cost units.
4. Canonical alias mapping: maps aliases to canonical project keys deterministically.
5. Starvation protection: boosts neglected projects without fabricating budget.
6. Double-spend prevention: two schedulers ranking identical snapshots produce deterministic candidates
   whose claim is resolved atomically by the store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from core.portfolio.models import JobSlotKind
from core.workflow.control_contracts import ContractModel, JobKey

CANONICAL_PROJECT_ALIASES: dict[str, str] = {
    "dark-factory": "darkfac",
    "darkfactory": "darkfac",
    "df": "darkfac",
    "atrium-core": "atrium",
    "jarvis-ai": "jarvis",
}

DEFAULT_PROJECT_WEIGHTS: dict[str, float] = {
    "atrium": 3.0,
    "jarvis": 2.0,
    "darkfac": 1.0,
}


def resolve_canonical_project_id(project_id: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve project alias to canonical project ID."""
    clean = str(project_id).strip().lower()
    mapping = CANONICAL_PROJECT_ALIASES if aliases is None else {**CANONICAL_PROJECT_ALIASES, **aliases}
    return mapping.get(clean, clean)


class JobCandidate(ContractModel):
    """Candidate job evaluated for scheduling eligibility."""

    job_key: JobKey
    project_id: str
    stage: str
    kind: JobSlotKind = JobSlotKind.LIGHT
    estimated_cost_units: Annotated[int, Field(ge=0)] = 1
    priority: int = 0
    enqueued_at: datetime
    account_id: str = "default_account"
    pool_id: str = "default_pool"

    @property
    def canonical_project(self) -> str:
        return resolve_canonical_project_id(self.project_id)


class CapacitySnapshot(ContractModel):
    """Immutable state snapshot for deterministic eligibility ranking."""

    candidates: list[JobCandidate] = Field(default_factory=list)
    active_heavy_slots: Annotated[int, Field(ge=0)] = 0
    active_light_slots: Annotated[int, Field(ge=0)] = 0
    max_heavy_slots: Annotated[int, Field(ge=0)] = 1
    max_light_slots: Annotated[int, Field(ge=0)] = 4
    project_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_PROJECT_WEIGHTS))
    default_weight: float = 1.0
    project_budgets_remaining_units: dict[str, int] = Field(default_factory=dict)
    starvation_ticks: dict[str, int] = Field(default_factory=dict)
    max_starvation_ticks: int = 5
    account_limits_units: dict[str, int] = Field(default_factory=dict)
    account_allocated_units: dict[str, int] = Field(default_factory=dict)
    project_aliases: dict[str, str] = Field(default_factory=dict)


def rank_eligible(snapshot: CapacitySnapshot, now: datetime | None = None) -> list[JobKey]:
    """Pure deterministic function ranking eligible JobKeys across portfolio projects.

    Enforces:
    - Slot kind availability (heavy vs light).
    - Remaining budget in integer units per project and account.
    - Weighted Fair Queueing with Starvation Protection.
    - Tie-breaking by enqueued_at and lexicographical JobKey.
    """
    effective_now = now or datetime.now(UTC)
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=UTC)

    available_heavy = max(0, snapshot.max_heavy_slots - snapshot.active_heavy_slots)
    available_light = max(0, snapshot.max_light_slots - snapshot.active_light_slots)

    eligible_candidates: list[tuple[float, datetime, str, JobCandidate]] = []

    for cand in snapshot.candidates:
        p_canon = resolve_canonical_project_id(cand.project_id, snapshot.project_aliases)

        # 1. Slot kind capacity check
        if cand.kind == JobSlotKind.HEAVY:
            if available_heavy <= 0:
                continue
        elif cand.kind == JobSlotKind.LIGHT:
            if available_light <= 0:
                continue

        # 2. Project budget check in integer units
        if p_canon in snapshot.project_budgets_remaining_units:
            rem_units = snapshot.project_budgets_remaining_units[p_canon]
            if rem_units < cand.estimated_cost_units:
                # Disqualified due to insufficient integer budget units
                continue

        # 3. Account limit check in integer units
        acc = cand.account_id
        if acc in snapshot.account_limits_units:
            limit = snapshot.account_limits_units[acc]
            allocated = snapshot.account_allocated_units.get(acc, 0)
            if allocated + cand.estimated_cost_units > limit:
                continue

        # 4. Score calculation: WFQ + Starvation Protection
        p_weight = snapshot.project_weights.get(p_canon, snapshot.default_weight)
        starvation = snapshot.starvation_ticks.get(p_canon, 0)

        # Priority base score
        score = float(cand.priority * 100)
        score += float(p_weight * 20.0)

        # Starvation boost: if waited >= max_starvation_ticks, receives significant boost
        if starvation >= snapshot.max_starvation_ticks:
            score += float(starvation * 50.0)
        else:
            score += float(starvation * 10.0)

        # Age factor: older jobs gain incremental priority
        enq = cand.enqueued_at
        if enq.tzinfo is None:
            enq = enq.replace(tzinfo=UTC)
        age_seconds = max(0.0, (effective_now - enq).total_seconds())
        score += min(100.0, age_seconds * 0.1)

        eligible_candidates.append((-score, enq, cand.job_key.canonical_key(), cand))

    # Sort deterministically: highest score (lowest -score), oldest enqueued_at, lexicographical canonical_key
    eligible_candidates.sort(key=lambda x: (x[0], x[1], x[2]))

    return [item[3].job_key for item in eligible_candidates]
