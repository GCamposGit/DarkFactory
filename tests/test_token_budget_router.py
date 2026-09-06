"""Token forecasting and quota-stress routing tests."""

from core.router.model_router import recommend_model
from core.router.token_budget import TokenPressure, estimate_task_tokens, plan_token_stress
from core.usage.models import (
    AccountConnectionStatus,
    AccountUsageReport,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)


def _account(
    provider_id: str,
    remaining: float | None,
    *,
    family: ProviderFamily = ProviderFamily.FRONTIER,
    quota_supported: bool = True,
) -> ProviderAccountUsage:
    windows = []
    if remaining is not None:
        windows.append(
            QuotaWindow(
                quota_id=f"{provider_id}:hourly",
                label="hourly",
                remaining_percent=remaining,
                used_percent=100.0 - remaining,
                window_duration_minutes=60,
            )
        )
    return ProviderAccountUsage(
        provider_id=provider_id,
        provider_name=provider_id.title(),
        family=family,
        status=AccountConnectionStatus.CONNECTED,
        adapter="test",
        account_label=f"{provider_id}-test",
        quota_supported=quota_supported,
        windows=windows,
        message="test account",
    )


def _report(*accounts: ProviderAccountUsage) -> AccountUsageReport:
    return AccountUsageReport(
        accounts=list(accounts),
        connected_count=len(accounts),
        limited_count=0,
        disconnected_count=0,
    )


def test_estimator_is_deterministic_and_scales_with_task_shape() -> None:
    small = estimate_task_tokens("testing", "low", "run unit tests", expected_steps=1)
    large = estimate_task_tokens(
        "architecture",
        "critical",
        "design a multi-provider orchestration platform",
        expected_steps=8,
    )

    assert small.total_tokens == estimate_task_tokens("testing", "low", "run unit tests").total_tokens
    assert large.total_tokens > small.total_tokens
    assert large.output_tokens > small.output_tokens


def test_router_evaluates_all_accounts_and_moves_to_healthier_account() -> None:
    usage = _report(_account("anthropic", 12.0), _account("google", 76.0))

    recommendation = recommend_model("architecture", "high", usage_report=usage)

    assert recommendation["provider"] == "google"
    assert recommendation["model"] == "gemini-3.8-flash"
    budget = recommendation["token_budget"]
    assert budget["evaluated_accounts"] == 2
    assert budget["failover_required"] is True
    assert budget["remaining_percent"] == 76.0


def test_paid_gateway_is_used_before_subscription_is_exhausted() -> None:
    usage = _report(
        _account("anthropic", 8.0),
        _account(
            "openrouter",
            None,
            family=ProviderFamily.GATEWAY,
            quota_supported=False,
        ),
    )

    recommendation = recommend_model("architecture", "critical", usage_report=usage)

    assert recommendation["provider"] == "openrouter"
    assert recommendation["model"] == "deepseek/deepseek-v4-pro"
    assert recommendation["token_budget"]["use_paid_api"] is True
    assert recommendation["token_budget"]["defer_frontier_work"] is True


def test_stressed_medium_task_prefers_local_and_short_low_effort_blocks() -> None:
    recommendation = recommend_model(
        "coding",
        "medium",
        task_description="implement a typed parser and tests",
        expected_steps=4,
        remaining_hourly_percent=15.0,
    )

    budget = recommendation["token_budget"]
    assert recommendation["provider"] == "ollama"
    assert budget["pressure"] == TokenPressure.STRESSED.value
    assert budget["prefer_local"] is True
    assert budget["reasoning_effort"] == "low"
    assert budget["modular_delivery"] is True
    assert budget["block_output_tokens"] <= 768


def test_critical_local_friendly_task_prioritizes_scripts() -> None:
    plan = plan_token_stress(
        "testing",
        "medium",
        remaining_hourly_percent=5.0,
    )

    assert plan.pressure == TokenPressure.CRITICAL
    assert plan.task_priority == "high"
    assert plan.prefer_scripts is True
    assert plan.reasoning_effort == "minimal"
