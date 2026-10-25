"""Token forecasting and quota-stress routing tests."""

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
