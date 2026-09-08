"""Typed contracts for task execution, budgets and attempt telemetry.

Conforms to Section 3 (Contratos mínimos) of the Dark Factory Development Plan:
- Budget: currency, ceiling, reserved, spent, unknown_cost_policy, max_attempts,
  deadline, concurrency_limit.
- Attempt: attempt_id, invocation_id, input_artifact_hash, output_artifact_hash,
  mode, tokens, measured_cost, estimated_cost, latency, outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UnknownCostPolicy(str, Enum):
    """Behavior when a provider returns no token or cost measurement."""

    REJECT = "reject"
    ESTIMATE = "estimate"
    CONSERVATIVE_MAX = "conservative_max"


class AttemptOutcome(str, Enum):
    """Terminal or in-flight outcome of an execution attempt."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"


class ReservationStatus(str, Enum):
    """Lifecycle state of a budget reservation."""

    ACTIVE = "active"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class BudgetWindowType(str, Enum):
    """Type of rolling expenditure window."""

    SHORT = "short"  # e.g., 1-5 hours
    LONG = "long"    # e.g., 7 days (weekly)


class BudgetWindow(BaseModel):
    """Rolling consumption window to prevent quota exhaustion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    window_type: BudgetWindowType
    duration_seconds: int = Field(ge=1)
    ceiling: float = Field(ge=0.0)
    spent: float = Field(ge=0.0, default=0.0)
    reserved: float = Field(ge=0.0, default=0.0)

    @property
    def available(self) -> float:
        return max(0.0, round(self.ceiling - (self.spent + self.reserved), 8))


class Budget(BaseModel):
    """Deterministic budget envelope for a task or execution session.

    Enforces currency, ceiling, concurrency limit, maximum attempts,
    deadline, and unknown-cost policy.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USD", min_length=1)
    ceiling: float = Field(ge=0.0)
    reserved: float = Field(ge=0.0, default=0.0)
    spent: float = Field(ge=0.0, default=0.0)
    unknown_cost_policy: UnknownCostPolicy = UnknownCostPolicy.REJECT
    max_attempts: int = Field(ge=1, default=3)
    deadline: datetime | None = None
    concurrency_limit: int = Field(ge=1, default=1)
    short_window: BudgetWindow | None = None
    long_window: BudgetWindow | None = None

    @property
    def available_amount(self) -> float:
        return max(0.0, round(self.ceiling - (self.spent + self.reserved), 8))

    def is_expired(self, at_time: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        reference = at_time or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        deadline_utc = self.deadline if self.deadline.tzinfo is not None else self.deadline.replace(tzinfo=UTC)
        return reference >= deadline_utc


class ReservationRecord(BaseModel):
    """Lease on a portion of the budget held by an active attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_id: str = Field(min_length=1)
    budget_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    amount: float = Field(ge=0.0)
    status: ReservationStatus = ReservationStatus.ACTIVE
    created_at: datetime
    expires_at: datetime | None = None
    committed_cost: float | None = None


class AttemptRecord(BaseModel):
    """Immutable audit record of a single execution attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    invocation_id: str | None = None
    input_artifact_hash: str = Field(min_length=1)
    output_artifact_hash: str | None = None
    mode: str = "live"
    tokens: int = Field(ge=0, default=0)
    measured_cost: float | None = None
    estimated_cost: float = Field(ge=0.0, default=0.0)
    latency: float = Field(ge=0.0, default=0.0)
    outcome: AttemptOutcome = AttemptOutcome.SUCCEEDED
    timestamp: datetime
