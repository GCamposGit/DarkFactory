"""Unit and concurrency tests for execution budget and quota controls (DF-12)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.execution.budget import (
    BudgetExceededError,
    BudgetNotFoundError,
    ConcurrencyLimitExceededError,
    DeadlineExceededError,
    ExecutionBudgetManager,
    InvalidReservationError,
    MaxAttemptsExceededError,
    UnknownCostRejectedError,
    WindowBudgetExceededError,
)
from core.execution.contracts import (
    AttemptOutcome,
    AttemptRecord,
    Budget,
    BudgetWindow,
    BudgetWindowType,
    ReservationStatus,
    UnknownCostPolicy,
)
from core.router.token_budget import (
    derive_task_budget,
    estimate_cost_from_tokens,
    estimate_task_tokens,
)


class MutableClock:
    def __init__(self, initial: datetime | None = None) -> None:
        self.current = initial or datetime(2026, 9, 7, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _make_attempt(
    attempt_id: str,
    *,
    measured_cost: float | None = 0.05,
    estimated_cost: float = 0.05,
    clock: MutableClock | None = None,
) -> AttemptRecord:
    ts = clock() if clock else datetime.now(UTC)
    return AttemptRecord(
        attempt_id=attempt_id,
        invocation_id=f"inv-{attempt_id}",
        input_artifact_hash="input-sha-abc1234",
        output_artifact_hash="output-sha-def5678",
        mode="live",
        tokens=1500,
        measured_cost=measured_cost,
        estimated_cost=estimated_cost,
        latency=1.25,
        outcome=AttemptOutcome.SUCCEEDED,
        timestamp=ts,
    )


def test_budget_contracts_and_properties() -> None:
    budget = Budget(
        currency="USD",
        ceiling=1.00,
        reserved=0.20,
        spent=0.30,
        unknown_cost_policy=UnknownCostPolicy.REJECT,
        max_attempts=3,
        deadline=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        concurrency_limit=2,
    )

    assert budget.available_amount == 0.50
    assert not budget.is_expired(datetime(2026, 9, 7, 10, 0, tzinfo=UTC))
    assert budget.is_expired(datetime(2026, 9, 7, 13, 0, tzinfo=UTC))


def test_basic_lifecycle_reserve_commit_and_release() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    budget = Budget(
        currency="USD",
        ceiling=2.00,
        unknown_cost_policy=UnknownCostPolicy.REJECT,
        max_attempts=5,
        concurrency_limit=3,
    )
    manager.register_budget("task-1", budget)

    # 1. Reserve
    res = manager.reserve("task-1", "attempt-1", 0.50, lease_seconds=60)
    assert res.amount == 0.50
    assert res.status == ReservationStatus.ACTIVE

    state = manager.get_budget("task-1")
    assert state is not None
    assert state.reserved == 0.50
    assert state.spent == 0.0
    assert state.available_amount == 1.50

    # 2. Commit
    attempt = _make_attempt("attempt-1", measured_cost=0.42, clock=clock)
    charged = manager.commit(res.reservation_id, attempt)
    assert charged == 0.42

    state = manager.get_budget("task-1")
    assert state is not None
    assert state.reserved == 0.0
    assert state.spent == 0.42
    assert state.available_amount == 1.58

    # 3. Reserve and Release
    res2 = manager.reserve("task-1", "attempt-2", 0.50, lease_seconds=60)
    state = manager.get_budget("task-1")
    assert state is not None
    assert state.reserved == 0.50

    manager.release(res2.reservation_id, reason="cancelled")
    state = manager.get_budget("task-1")
    assert state is not None
    assert state.reserved == 0.0
    assert state.spent == 0.42


def test_concurrent_reservations_cannot_exceed_ceiling() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    # Ceiling is exactly $1.00, each wants $0.10. At most 10 can succeed.
    budget = Budget(
        currency="USD",
        ceiling=1.00,
        concurrency_limit=25,
        max_attempts=50,
    )
    manager.register_budget("task-conc", budget)

    results: list[tuple[str, object]] = []

    def attempt_reservation(worker_id: int) -> tuple[str, object]:
        try:
            res = manager.reserve(
                "task-conc",
                f"attempt-{worker_id}",
                0.10,
                lease_seconds=300,
            )
            return ("success", res)
        except BudgetExceededError as exc:
            return ("exceeded", exc)

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt_reservation, range(20)))

    successes = [r for r in results if r[0] == "success"]
    failures = [r for r in results if r[0] == "exceeded"]

    assert len(successes) == 10
    assert len(failures) == 10

    state = manager.get_budget("task-conc")
    assert state is not None
    assert state.reserved == 1.00
    assert state.available_amount == 0.0


def test_concurrency_limit_enforced() -> None:
    manager = ExecutionBudgetManager()
    budget = Budget(
        currency="USD",
        ceiling=10.00,
        concurrency_limit=2,
        max_attempts=10,
    )
    manager.register_budget("task-limit", budget)

    res1 = manager.reserve("task-limit", "att-1", 1.0)
    res2 = manager.reserve("task-limit", "att-2", 1.0)

    # Third concurrent attempt fails because active reservations == concurrency_limit
    with pytest.raises(ConcurrencyLimitExceededError):
        manager.reserve("task-limit", "att-3", 1.0)

    # Committing one frees a concurrency slot
    manager.commit(res1.reservation_id, _make_attempt("att-1"))
    res3 = manager.reserve("task-limit", "att-3", 1.0)
    assert res3.status == ReservationStatus.ACTIVE


def test_max_attempts_limit_enforced() -> None:
    manager = ExecutionBudgetManager()
    budget = Budget(
        currency="USD",
        ceiling=10.00,
        concurrency_limit=5,
        max_attempts=2,
    )
    manager.register_budget("task-attempts", budget)

    # Attempt 1
    r1 = manager.reserve("task-attempts", "att-1", 1.0)
    manager.commit(r1.reservation_id, _make_attempt("att-1"))

    # Attempt 2
    r2 = manager.reserve("task-attempts", "att-2", 1.0)
    manager.commit(r2.reservation_id, _make_attempt("att-2"))

    # Attempt 3 must fail due to max attempts
    with pytest.raises(MaxAttemptsExceededError):
        manager.reserve("task-attempts", "att-3", 1.0)


def test_deadline_enforcement_with_clock() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    deadline = clock() + timedelta(seconds=100)
    budget = Budget(
        currency="USD",
        ceiling=5.00,
        deadline=deadline,
        max_attempts=5,
    )
    manager.register_budget("task-deadline", budget)

    # Before deadline: succeeds
    res = manager.reserve("task-deadline", "att-1", 1.0)
    manager.commit(res.reservation_id, _make_attempt("att-1", clock=clock))

    # Advance clock past deadline
    clock.advance(101)
    with pytest.raises(DeadlineExceededError):
        manager.reserve("task-deadline", "att-2", 1.0)


def test_short_and_long_window_ceilings() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    short_window = BudgetWindow(
        window_type=BudgetWindowType.SHORT,
        duration_seconds=3600,  # 1 hour
        ceiling=0.50,
        spent=0.0,
        reserved=0.0,
    )
    long_window = BudgetWindow(
        window_type=BudgetWindowType.LONG,
        duration_seconds=86400,  # 24 hours
        ceiling=2.00,
        spent=0.0,
        reserved=0.0,
    )

    budget = Budget(
        currency="USD",
        ceiling=10.00,
        short_window=short_window,
        long_window=long_window,
        max_attempts=10,
    )
    manager.register_budget("task-window", budget)

    # Reserve & commit $0.40 in the short window
    r1 = manager.reserve("task-window", "att-1", 0.40)
    manager.commit(r1.reservation_id, _make_attempt("att-1", measured_cost=0.40, clock=clock))

    # Reserving another $0.20 would make 0.40 + 0.20 = 0.60 > 0.50 (short window limit)
    with pytest.raises(WindowBudgetExceededError, match="short window"):
        manager.reserve("task-window", "att-2", 0.20)

    # Advance clock by 3601 seconds (past 1 hour short window)
    clock.advance(3601)

    # Now the previous $0.40 is outside the short window, so $0.20 is accepted
    r2 = manager.reserve("task-window", "att-2", 0.20)
    assert r2.status == ReservationStatus.ACTIVE


def test_unknown_cost_policy_handling() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    # 1. REJECT Policy
    b_reject = Budget(
        currency="USD",
        ceiling=5.00,
        unknown_cost_policy=UnknownCostPolicy.REJECT,
    )
    manager.register_budget("b-reject", b_reject)
    r1 = manager.reserve("b-reject", "att-1", 0.50)
    att_unknown = _make_attempt("att-1", measured_cost=None, estimated_cost=0.10, clock=clock)
    with pytest.raises(UnknownCostRejectedError):
        manager.commit(r1.reservation_id, att_unknown)

    # 2. ESTIMATE Policy
    b_est = Budget(
        currency="USD",
        ceiling=5.00,
        unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
    )
    manager.register_budget("b-est", b_est)
    r2 = manager.reserve("b-est", "att-2", 0.50)
    att_est = _make_attempt("att-2", measured_cost=None, estimated_cost=0.10, clock=clock)
    charged = manager.commit(r2.reservation_id, att_est)
    assert charged == 0.10  # Uses estimated_cost

    # 3. CONSERVATIVE_MAX Policy
    b_max = Budget(
        currency="USD",
        ceiling=5.00,
        unknown_cost_policy=UnknownCostPolicy.CONSERVATIVE_MAX,
    )
    manager.register_budget("b-max", b_max)
    r3 = manager.reserve("b-max", "att-3", 0.50)
    att_max = _make_attempt("att-3", measured_cost=None, estimated_cost=0.10, clock=clock)
    charged_max = manager.commit(r3.reservation_id, att_max)
    # max(0.50 reservation, 0.10 * 1.5 = 0.15) = 0.50
    assert charged_max == 0.50


def test_stale_reservations_expire_and_restore_balance() -> None:
    clock = MutableClock()
    manager = ExecutionBudgetManager(clock=clock)

    budget = Budget(currency="USD", ceiling=1.00)
    manager.register_budget("task-expire", budget)

    # Reserve with 30s lease
    res = manager.reserve("task-expire", "att-1", 0.60, lease_seconds=30)
    state = manager.get_budget("task-expire")
    assert state is not None
    assert state.reserved == 0.60

    # Advance clock by 35s
    clock.advance(35)

    # Calling get_budget or expire_stale_reservations restores balance
    expired_count = manager.expire_stale_reservations("task-expire")
    assert expired_count == 1

    state = manager.get_budget("task-expire")
    assert state is not None
    assert state.reserved == 0.0
    assert state.available_amount == 1.00


def test_persistence_across_manager_restarts(tmp_path: Path) -> None:
    db_file = tmp_path / "budget.sqlite3"
    manager1 = ExecutionBudgetManager(database_path=db_file)

    budget = Budget(
        currency="USD",
        ceiling=3.00,
        max_attempts=4,
    )
    manager1.register_budget("task-persist", budget)
    res = manager1.reserve("task-persist", "att-1", 1.00)
    manager1.commit(res.reservation_id, _make_attempt("att-1", measured_cost=0.75))
    manager1.close()

    # Re-open with new manager instance
    manager2 = ExecutionBudgetManager(database_path=db_file)
    restored = manager2.get_budget("task-persist")
    assert restored is not None
    assert restored.spent == 0.75
    assert restored.reserved == 0.0
    assert restored.available_amount == 2.25

    attempts = manager2.list_attempts("task-persist")
    assert len(attempts) == 1
    assert attempts[0].attempt_id == "att-1"
    assert attempts[0].measured_cost == 0.75
    manager2.close()


def test_router_integration_derive_budget() -> None:
    budget = derive_task_budget(
        task_type="coding",
        complexity="high",
        ceiling_usd=0.25,
        concurrency_limit=2,
        max_attempts=4,
    )
    assert budget.currency == "USD"
    assert budget.ceiling == 0.25
    assert budget.concurrency_limit == 2
    assert budget.max_attempts == 4
    assert budget.short_window is not None
    assert budget.short_window.ceiling == 0.30
    assert budget.long_window is not None
    assert budget.long_window.ceiling == 0.75

    # Token cost estimation helper
    tokens = estimate_task_tokens("coding", "high")
    cost_gemini = estimate_cost_from_tokens(tokens, provider="google")
    cost_anthropic = estimate_cost_from_tokens(tokens, provider="anthropic")
    assert cost_gemini > 0.0
    assert cost_anthropic > cost_gemini
