"""Deterministic token forecasting and quota-aware execution policy."""

from __future__ import annotations

import math
from enum import Enum
from typing import Iterable, Optional

from pydantic import BaseModel, Field

from core.usage.models import (
    AccountConnectionStatus,
    ProviderAccountUsage,
    ProviderFamily,
)


class TokenPressure(str, Enum):
    """Pressure applied to subscription token windows."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    GUARDED = "guarded"
    STRESSED = "stressed"
    CRITICAL = "critical"


class TaskTokenEstimate(BaseModel):
    """Small, explainable estimate used before a task starts."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)
    method: str


class TokenStressPlan(BaseModel):
    """Execution controls selected from task cost and account headroom."""

    pressure: TokenPressure
    remaining_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    selected_provider: Optional[str] = None
    selected_account_label: Optional[str] = None
    evaluated_accounts: int = Field(ge=0)
    failover_required: bool = False
    use_paid_api: bool = False
    prefer_local: bool = False
    prefer_scripts: bool = False
    defer_frontier_work: bool = False
    modular_delivery: bool = False
    reasoning_effort: str
    max_output_tokens: int = Field(ge=128)
    block_output_tokens: int = Field(ge=128)
    task_priority: str
    selection_objective: str
    estimate: TaskTokenEstimate
    rationale: str


_BASE_TOKENS = {
    "architecture": (2200, 1600),
    "plan": (1900, 1300),
    "prd": (2100, 1500),
    "research": (2600, 1300),
    "topic_research": (3200, 1700),
    "coding": (1600, 1200),
    "implement": (1600, 1200),
    "review": (1800, 900),
    "testing": (700, 450),
    "test": (700, 450),
    "validate": (500, 300),
    "anti_slop": (650, 500),
    "content": (900, 1000),
    "visual": (500, 350),
}
_COMPLEXITY_FACTOR = {"low": 0.65, "medium": 1.0, "high": 1.55, "critical": 2.15}
_LOCAL_FRIENDLY_TASKS = {
    "anti_slop",
    "anti_slop_scrub",
    "boilerplate",
    "lint_content",
    "script",
    "testing",
    "test",
    "validate",
}
_FRONTIER_HEAVY_TASKS = {"architecture", "plan", "prd", "research", "topic_research", "papers"}
_PAID_API_PROVIDERS = {"deepseek", "openrouter", "siliconflow"}


def estimate_task_tokens(
    task_type: str,
    complexity: str = "medium",
    description: str = "",
    expected_steps: int = 1,
) -> TaskTokenEstimate:
    """Forecast token use with a cheap deterministic heuristic.

    The estimate intentionally needs no tokenizer or model call. It combines a
    task-class baseline, roughly four characters per prompt token, complexity,
    and a bounded multi-step penalty. Runtime telemetry can replace the
    baselines later without changing the public contract.
    """

    normalized_task = task_type.strip().lower()
    normalized_complexity = complexity.strip().lower()
    base_input, base_output = _BASE_TOKENS.get(normalized_task, (1200, 800))
    factor = _COMPLEXITY_FACTOR.get(normalized_complexity, 1.0)
    prompt_tokens = math.ceil(len(description) / 4)
    bounded_steps = max(1, min(expected_steps, 12))
    input_multiplier = 1.0 + (bounded_steps - 1) * 0.12
    output_multiplier = 1.0 + (bounded_steps - 1) * 0.08
    input_tokens = max(128, math.ceil((base_input + prompt_tokens) * factor * input_multiplier))
    output_tokens = max(128, math.ceil(base_output * factor * output_multiplier))
    confidence = 0.72 if normalized_task in _BASE_TOKENS else 0.55
    return TaskTokenEstimate(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        confidence=confidence,
        method="task baseline + chars/4 + complexity + bounded step multiplier",
    )


def _quota_headroom(account: ProviderAccountUsage) -> Optional[float]:
    """Return the most restrictive known quota window.

    A healthy short window cannot compensate for an exhausted weekly/monthly
    window (or vice versa), so routing must respect both horizons.
    """
    known = [window for window in account.windows if window.remaining_percent is not None]
    if not known:
        return None
    return min(float(window.remaining_percent) for window in known if window.remaining_percent is not None)


def _pressure_for(remaining_percent: Optional[float]) -> TokenPressure:
    if remaining_percent is None:
        return TokenPressure.UNKNOWN
    if remaining_percent <= 10.0:
        return TokenPressure.CRITICAL
    if remaining_percent <= 25.0:
        return TokenPressure.STRESSED
    if remaining_percent <= 50.0:
        return TokenPressure.GUARDED
    return TokenPressure.HEALTHY


def plan_token_stress(
    task_type: str,
    complexity: str = "medium",
    description: str = "",
    expected_steps: int = 1,
    accounts: Iterable[ProviderAccountUsage] = (),
    preferred_provider: Optional[str] = None,
    remaining_hourly_percent: Optional[float] = None,
    offline: bool = False,
) -> TokenStressPlan:
    """Evaluate all accounts and return a quota-aware execution plan."""

    account_list = list(accounts)
    estimate = estimate_task_tokens(task_type, complexity, description, expected_steps)
    normalized_task = task_type.strip().lower()
    normalized_complexity = complexity.strip().lower()
    usable = [
        account
        for account in account_list
        if account.family != ProviderFamily.LOCAL
        and account.status in {AccountConnectionStatus.CONNECTED, AccountConnectionStatus.LIMITED}
    ]
    headrooms = [(account, _quota_headroom(account)) for account in usable]
    known = [(account, value) for account, value in headrooms if value is not None]
    best_account: Optional[ProviderAccountUsage] = None
    best_remaining = remaining_hourly_percent
    if known:
        best_account, detected_remaining = max(known, key=lambda item: float(item[1]))
        if best_remaining is None:
            best_remaining = detected_remaining

    preferred = next(
        (item for item in headrooms if preferred_provider and item[0].provider_id == preferred_provider),
        None,
    )
    preferred_remaining = preferred[1] if preferred else None
    failover_required = bool(
        best_account
        and preferred_provider
        and best_account.provider_id != preferred_provider
        and preferred_remaining is not None
        and best_remaining is not None
        and best_remaining > preferred_remaining
    )

    paid_candidates = [
        account
        for account in usable
        if account.provider_id in _PAID_API_PROVIDERS
        or (account.family == ProviderFamily.GATEWAY and not account.quota_supported)
    ]
    use_paid_api = bool(not offline and paid_candidates and best_remaining is not None and best_remaining <= 15.0)
    if use_paid_api:
        best_account = paid_candidates[0]
        failover_required = best_account.provider_id != preferred_provider

    pressure = TokenPressure.CRITICAL if offline else _pressure_for(best_remaining)
    stressed = pressure in {TokenPressure.STRESSED, TokenPressure.CRITICAL}
    frontier_heavy = normalized_task in _FRONTIER_HEAVY_TASKS or normalized_complexity == "critical"
    local_friendly = normalized_task in _LOCAL_FRIENDLY_TASKS
    prefer_local = offline or (stressed and (local_friendly or not frontier_heavy) and not use_paid_api)
    prefer_scripts = offline or local_friendly or pressure == TokenPressure.CRITICAL
    defer_frontier_work = stressed and frontier_heavy
    modular_delivery = pressure in {TokenPressure.GUARDED, TokenPressure.STRESSED, TokenPressure.CRITICAL}

    output_ratio = {
        TokenPressure.UNKNOWN: 0.85,
        TokenPressure.HEALTHY: 1.0,
        TokenPressure.GUARDED: 0.72,
        TokenPressure.STRESSED: 0.50,
        TokenPressure.CRITICAL: 0.32,
    }[pressure]
    max_output_tokens = max(128, math.ceil(estimate.output_tokens * output_ratio))
    block_output_tokens = min(max_output_tokens, 768 if stressed else 1200)
    reasoning_effort = {
        TokenPressure.UNKNOWN: "medium",
        TokenPressure.HEALTHY: "medium" if normalized_complexity in {"low", "medium"} else "high",
        TokenPressure.GUARDED: "low",
        TokenPressure.STRESSED: "low",
        TokenPressure.CRITICAL: "minimal",
    }[pressure]
    task_priority = "normal"
    if stressed:
        task_priority = "high" if local_friendly else ("defer" if frontier_heavy else "normal")

    if offline:
        rationale = "Offline mode forces local models/scripts and the smallest modular token budget."
    elif use_paid_api:
        rationale = "Subscription reserve reached; use a configured paid API before exhausting account quotas."
    elif failover_required:
        rationale = "Preferred account is lower on hourly quota; fail over to the account with more headroom."
    elif stressed:
        rationale = "Hourly quota is under stress; trade speed and model quality for lower token use and modular delivery."
    else:
        rationale = "Quota headroom permits the normal model route with an explicit output budget."

    return TokenStressPlan(
        pressure=pressure,
        remaining_percent=best_remaining,
        selected_provider=best_account.provider_id if best_account else None,
        selected_account_label=best_account.account_label if best_account else None,
        evaluated_accounts=len(account_list),
        failover_required=failover_required,
        use_paid_api=use_paid_api,
        prefer_local=prefer_local,
        prefer_scripts=prefer_scripts,
        defer_frontier_work=defer_frontier_work,
        modular_delivery=modular_delivery,
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
        block_output_tokens=block_output_tokens,
        task_priority=task_priority,
        selection_objective="minimize output tokens, then marginal cost, then latency",
        estimate=estimate,
        rationale=rationale,
    )
