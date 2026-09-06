"""Closed Pydantic contracts for execution-budget accounting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UnknownCostPolicy(str, Enum):
    """Fail-closed policies for work whose price cannot be estimated."""

    REJECT = "reject"
    RESERVE_REMAINDER = "reserve_remainder"


class ReservationStatus(str, Enum):
    """Lifecycle of one cost reservation."""

    ACTIVE = "active"
    SETTLED = "settled"
    RELEASED = "released"


class Budget(BaseModel):
    """Immutable execution envelope configured by the operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    ceiling: Decimal = Field(gt=0)
    reserved: Decimal = Field(default=Decimal("0"), ge=0)
    spent: Decimal = Field(default=Decimal("0"), ge=0)
    unknown_cost_policy: UnknownCostPolicy = UnknownCostPolicy.REJECT
    max_attempts: int = Field(ge=1)
    deadline: datetime
    concurrency_limit: int = Field(ge=1)

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def totals_must_fit_ceiling(self) -> "Budget":
        if self.spent + self.reserved > self.ceiling:
            raise ValueError("spent plus reserved cannot exceed ceiling")
        return self


class BudgetReservation(BaseModel):
    """Immutable evidence for one accepted reservation or settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reservation_id: str = Field(min_length=1, max_length=200)
    reserved_cost: Decimal = Field(ge=0)
    actual_cost: Decimal | None = Field(default=None, ge=0)
    status: ReservationStatus = ReservationStatus.ACTIVE
    created_at: datetime
    settled_at: datetime | None = None


class BudgetSnapshot(BaseModel):
    """Consistent point-in-time view of the mutable budget ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: str
    ceiling: Decimal = Field(gt=0)
    reserved: Decimal = Field(ge=0)
    spent: Decimal = Field(ge=0)
    available: Decimal = Field(ge=0)
    attempts: int = Field(ge=0)
    active_reservations: int = Field(ge=0)
    deadline: datetime
