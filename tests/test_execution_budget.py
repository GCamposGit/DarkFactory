from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.execution.budget import (
    BudgetDeadlineExceeded,
    BudgetExceeded,
    ExecutionBudget,
    UnknownCostRejected,
)
from core.execution.contracts import Budget, UnknownCostPolicy
from core.router.token_budget import TokenPressure, plan_token_stress
from core.usage.models import (
    AccountConnectionStatus,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)


def _now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _budget(**overrides: object) -> Budget:
    values: dict[str, object] = {
        "currency": "USD",
        "ceiling": Decimal("5.00"),
        "unknown_cost_policy": UnknownCostPolicy.REJECT,
        "max_attempts": 20,
        "deadline": _now() + timedelta(hours=1),
        "concurrency_limit": 20,
    }
    values.update(overrides)
    return Budget(**values)


def test_concurrent_reservations_never_exceed_ceiling() -> None:
    ledger = ExecutionBudget(_budget(), clock=_now)

    def reserve(index: int) -> bool:
        try:
            ledger.reserve(Decimal("1.00"), reservation_id=f"run-{index}")
        except BudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=20) as executor:
        accepted = list(executor.map(reserve, range(20)))

    assert accepted.count(True) == 5
    assert ledger.snapshot().reserved == Decimal("5.00")
    assert ledger.snapshot().spent + ledger.snapshot().reserved <= ledger.spec.ceiling


def test_settle_and_release_update_capacity_atomically() -> None:
    ledger = ExecutionBudget(_budget(concurrency_limit=2), clock=_now)
    first = ledger.reserve(Decimal("2.00"), reservation_id="first")
    second = ledger.reserve(Decimal("2.00"), reservation_id="second")

    with pytest.raises(BudgetExceeded, match="concurrency"):
        ledger.reserve(Decimal("1.00"), reservation_id="third")

    settled = ledger.settle(first.reservation_id, Decimal("1.50"))
    ledger.release(second.reservation_id)
    replacement = ledger.reserve(Decimal("3.50"), reservation_id="replacement")

    assert settled.actual_cost == Decimal("1.50")
    assert replacement.reserved_cost == Decimal("3.50")
    assert ledger.snapshot().spent == Decimal("1.50")
    assert ledger.snapshot().reserved == Decimal("3.50")


def test_unknown_cost_policy_and_deadline_fail_closed() -> None:
    rejecting = ExecutionBudget(_budget(), clock=_now)
    with pytest.raises(UnknownCostRejected):
        rejecting.reserve(None, reservation_id="unknown")

    conservative = ExecutionBudget(
        _budget(unknown_cost_policy=UnknownCostPolicy.RESERVE_REMAINDER),
        clock=_now,
    )
    reservation = conservative.reserve(None, reservation_id="unknown")
    assert reservation.reserved_cost == Decimal("5.00")
    with pytest.raises(BudgetExceeded):
        conservative.reserve(Decimal("0.01"), reservation_id="other")

    expired = ExecutionBudget(_budget(deadline=_now()), clock=_now)
    with pytest.raises(BudgetDeadlineExceeded):
        expired.reserve(Decimal("1.00"), reservation_id="late")


def test_short_and_long_quota_windows_both_constrain_routing() -> None:
    account = ProviderAccountUsage(
        provider_id="anthropic",
        provider_name="Anthropic",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="test",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="anthropic:short",
                label="five hours",
                remaining_percent=80.0,
                window_duration_minutes=300,
            ),
            QuotaWindow(
                quota_id="anthropic:long",
                label="weekly",
                remaining_percent=9.0,
                window_duration_minutes=10_080,
            ),
        ],
        message="test account",
    )

    plan = plan_token_stress("architecture", "high", accounts=[account])

    assert plan.remaining_percent == 9.0
    assert plan.pressure is TokenPressure.CRITICAL
    assert plan.defer_frontier_work is True
