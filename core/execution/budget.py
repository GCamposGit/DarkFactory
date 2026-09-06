"""Thread-safe, fail-closed execution-budget reservations."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Callable
from uuid import uuid4

from core.execution.contracts import Budget, BudgetReservation, BudgetSnapshot, ReservationStatus, UnknownCostPolicy


class BudgetError(RuntimeError):
    """Base error for rejected budget transitions."""


class BudgetExceeded(BudgetError):
    """The requested transition exceeds a configured budget limit."""


class BudgetDeadlineExceeded(BudgetExceeded):
    """The budget deadline has passed."""


class UnknownCostRejected(BudgetExceeded):
    """Unknown cost is forbidden by the configured policy."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _cost(value: Decimal | int | float | str) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("cost must be a finite non-negative amount")
    return parsed


class ExecutionBudget:
    """Atomically reserve, settle and release a fixed execution envelope."""

    def __init__(self, spec: Budget, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.spec = spec
        self._clock = clock
        self._lock = RLock()
        self._reservations: dict[str, BudgetReservation] = {}
        self._base_reserved = spec.reserved
        self._spent = spec.spent
        self._attempts = 0

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("budget clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _active(self) -> tuple[BudgetReservation, ...]:
        return tuple(
            item
            for item in self._reservations.values()
            if item.status is ReservationStatus.ACTIVE
        )

    def _reserved(self) -> Decimal:
        return self._base_reserved + sum((item.reserved_cost for item in self._active()), start=Decimal("0"))

    def _available(self) -> Decimal:
        return self.spec.ceiling - self._spent - self._reserved()

    def _assert_open(self, now: datetime) -> None:
        if now >= self.spec.deadline:
            raise BudgetDeadlineExceeded("execution budget deadline has passed")
        if self._attempts >= self.spec.max_attempts:
            raise BudgetExceeded("execution budget max_attempts reached")
        if len(self._active()) >= self.spec.concurrency_limit:
            raise BudgetExceeded("execution budget concurrency_limit reached")

    def reserve(
        self,
        estimated_cost: Decimal | int | float | str | None,
        *,
        reservation_id: str | None = None,
    ) -> BudgetReservation:
        """Atomically reserve capacity, rejecting ambiguous work by policy."""
        with self._lock:
            now = self._now()
            self._assert_open(now)
            identity = (reservation_id or uuid4().hex).strip()
            if not identity:
                raise ValueError("reservation_id must not be blank")
            if identity in self._reservations:
                raise BudgetError(f"reservation_id {identity!r} already exists")
            available = self._available()
            if estimated_cost is None:
                if self.spec.unknown_cost_policy is UnknownCostPolicy.REJECT:
                    raise UnknownCostRejected("unknown cost is rejected by budget policy")
                amount = available
            else:
                amount = _cost(estimated_cost)
            if amount > available:
                raise BudgetExceeded(f"reservation {amount} exceeds available {available} {self.spec.currency}")
            reservation = BudgetReservation(reservation_id=identity, reserved_cost=amount, created_at=now)
            self._reservations[identity] = reservation
            self._attempts += 1
            return reservation

    def settle(
        self,
        reservation_id: str,
        actual_cost: Decimal | int | float | str | None,
    ) -> BudgetReservation:
        """Replace an active reservation with measured spend atomically."""
        with self._lock:
            reservation = self._require_active(reservation_id)
            if actual_cost is None:
                raise UnknownCostRejected("cannot settle a reservation with unknown actual cost")
            amount = _cost(actual_cost)
            other_reserved = self._reserved() - reservation.reserved_cost
            if self._spent + other_reserved + amount > self.spec.ceiling:
                raise BudgetExceeded("actual cost would exceed execution budget ceiling")
            settled = reservation.model_copy(
                update={
                    "actual_cost": amount,
                    "status": ReservationStatus.SETTLED,
                    "settled_at": self._now(),
                }
            )
            self._reservations[reservation_id] = settled
            self._spent += amount
            return settled

    def release(self, reservation_id: str) -> BudgetReservation:
        """Release unused capacity without reducing the attempt counter."""
        with self._lock:
            reservation = self._require_active(reservation_id)
            released = reservation.model_copy(
                update={
                    "status": ReservationStatus.RELEASED,
                    "settled_at": self._now(),
                }
            )
            self._reservations[reservation_id] = released
            return released

    def _require_active(self, reservation_id: str) -> BudgetReservation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None:
            raise BudgetError(f"reservation_id {reservation_id!r} does not exist")
        if reservation.status is not ReservationStatus.ACTIVE:
            raise BudgetError(f"reservation_id {reservation_id!r} is not active")
        return reservation

    def snapshot(self) -> BudgetSnapshot:
        """Return all counters from one consistent lock acquisition."""
        with self._lock:
            reserved = self._reserved()
            return BudgetSnapshot(
                currency=self.spec.currency,
                ceiling=self.spec.ceiling,
                reserved=reserved,
                spent=self._spent,
                available=self.spec.ceiling - self._spent - reserved,
                attempts=self._attempts,
                active_reservations=len(self._active()),
                deadline=self.spec.deadline,
            )
