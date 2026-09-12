"""Durable execution budget manager with concurrent reservations and quota window tracking.

Implements DF-12:
- Atomic, race-free budget reservations serialized across threads and processes.
- Enforces budget ceiling, concurrency limit, maximum attempts, and deadlines.
- Enforces short and long quota windows.
- Enforces unknown-cost policies (reject, estimate, conservative_max).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from core.execution.contracts import (
    AttemptOutcome,
    AttemptRecord,
    Budget,
    BudgetWindow,
    BudgetWindowType,
    ReservationRecord,
    ReservationStatus,
    UnknownCostPolicy,
)

_Clock = Callable[[], datetime]


class BudgetError(Exception):
    """Base exception for budget and execution quota failures."""


class BudgetNotFoundError(BudgetError):
    """Raised when a referenced budget does not exist."""


class BudgetExceededError(BudgetError):
    """Raised when an operation would exceed the budget ceiling."""


class WindowBudgetExceededError(BudgetError):
    """Raised when an operation would exceed a short or long window ceiling."""


class DeadlineExceededError(BudgetError):
    """Raised when an operation is attempted after the budget deadline."""


class MaxAttemptsExceededError(BudgetError):
    """Raised when the maximum number of attempts has already been exhausted."""


class ConcurrencyLimitExceededError(BudgetError):
    """Raised when concurrent reservations exceed the allowed limit."""


class UnknownCostRejectedError(BudgetError):
    """Raised when an attempt has unknown cost and the policy is REJECT."""


class InvalidReservationError(BudgetError):
    """Raised when a reservation is invalid or in an unexpected state."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return _as_utc(dt)


class ExecutionBudgetManager:
    """Manages budgets, atomic reservations and attempt ledger using SQLite."""

    def __init__(
        self,
        database_path: Path | str | None = None,
        *,
        clock: _Clock | None = None,
    ) -> None:
        self.database_path = ":memory:" if database_path is None else str(database_path)
        self._clock: _Clock = clock or _utc_now
        self._thread_lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._thread_lock:
            with self._conn:
                self._conn.execute("PRAGMA journal_mode = WAL;")
                self._conn.execute("PRAGMA foreign_keys = ON;")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS budgets (
                        budget_id TEXT PRIMARY KEY,
                        currency TEXT NOT NULL,
                        ceiling REAL NOT NULL,
                        reserved REAL NOT NULL DEFAULT 0.0,
                        spent REAL NOT NULL DEFAULT 0.0,
                        unknown_cost_policy TEXT NOT NULL,
                        max_attempts INTEGER NOT NULL,
                        deadline TEXT,
                        concurrency_limit INTEGER NOT NULL,
                        short_window_duration INTEGER,
                        short_window_ceiling REAL,
                        long_window_duration INTEGER,
                        long_window_ceiling REAL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reservations (
                        reservation_id TEXT PRIMARY KEY,
                        budget_id TEXT NOT NULL REFERENCES budgets(budget_id),
                        attempt_id TEXT NOT NULL,
                        amount REAL NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT,
                        committed_cost REAL
                    );
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS attempts (
                        attempt_id TEXT PRIMARY KEY,
                        budget_id TEXT NOT NULL REFERENCES budgets(budget_id),
                        invocation_id TEXT,
                        input_artifact_hash TEXT NOT NULL,
                        output_artifact_hash TEXT,
                        mode TEXT NOT NULL,
                        tokens INTEGER NOT NULL,
                        measured_cost REAL,
                        estimated_cost REAL NOT NULL,
                        charged_cost REAL NOT NULL,
                        latency REAL NOT NULL,
                        outcome TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    );
                    """
                )

    def close(self) -> None:
        with self._thread_lock:
            self._conn.close()

    def register_budget(self, budget_id: str, budget: Budget) -> Budget:
        """Register or replace an execution budget."""
        now_str = _iso(self._clock())
        short_dur = budget.short_window.duration_seconds if budget.short_window else None
        short_ceil = budget.short_window.ceiling if budget.short_window else None
        long_dur = budget.long_window.duration_seconds if budget.long_window else None
        long_ceil = budget.long_window.ceiling if budget.long_window else None

        with self._thread_lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO budgets (
                        budget_id, currency, ceiling, reserved, spent,
                        unknown_cost_policy, max_attempts, deadline,
                        concurrency_limit, short_window_duration, short_window_ceiling,
                        long_window_duration, long_window_ceiling, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(budget_id) DO UPDATE SET
                        currency = excluded.currency,
                        ceiling = excluded.ceiling,
                        unknown_cost_policy = excluded.unknown_cost_policy,
                        max_attempts = excluded.max_attempts,
                        deadline = excluded.deadline,
                        concurrency_limit = excluded.concurrency_limit,
                        short_window_duration = excluded.short_window_duration,
                        short_window_ceiling = excluded.short_window_ceiling,
                        long_window_duration = excluded.long_window_duration,
                        long_window_ceiling = excluded.long_window_ceiling,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        budget_id,
                        budget.currency,
                        budget.ceiling,
                        budget.reserved,
                        budget.spent,
                        budget.unknown_cost_policy.value,
                        budget.max_attempts,
                        _iso(budget.deadline),
                        budget.concurrency_limit,
                        short_dur,
                        short_ceil,
                        long_dur,
                        long_ceil,
                        now_str,
                        now_str,
                    ),
                )
        return self.get_budget(budget_id)  # type: ignore[return-value]

    def get_budget(self, budget_id: str) -> Budget | None:
        """Retrieve the current state of a budget."""
        with self._thread_lock:
            self._expire_stale_unlocked(budget_id)
            cursor = self._conn.execute(
                "SELECT * FROM budgets WHERE budget_id = ?", (budget_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            short_window = None
            if row["short_window_duration"] is not None and row["short_window_ceiling"] is not None:
                short_spent = self._compute_window_spent_unlocked(budget_id, row["short_window_duration"])
                short_window = BudgetWindow(
                    window_type=BudgetWindowType.SHORT,
                    duration_seconds=row["short_window_duration"],
                    ceiling=row["short_window_ceiling"],
                    spent=short_spent,
                    reserved=row["reserved"],
                )

            long_window = None
            if row["long_window_duration"] is not None and row["long_window_ceiling"] is not None:
                long_spent = self._compute_window_spent_unlocked(budget_id, row["long_window_duration"])
                long_window = BudgetWindow(
                    window_type=BudgetWindowType.LONG,
                    duration_seconds=row["long_window_duration"],
                    ceiling=row["long_window_ceiling"],
                    spent=long_spent,
                    reserved=row["reserved"],
                )

            return Budget(
                currency=row["currency"],
                ceiling=row["ceiling"],
                reserved=row["reserved"],
                spent=row["spent"],
                unknown_cost_policy=UnknownCostPolicy(row["unknown_cost_policy"]),
                max_attempts=row["max_attempts"],
                deadline=_parse_datetime(row["deadline"]),
                concurrency_limit=row["concurrency_limit"],
                short_window=short_window,
                long_window=long_window,
            )

    def reserve(
        self,
        budget_id: str,
        attempt_id: str,
        amount: float,
        *,
        lease_seconds: float = 60.0,
    ) -> ReservationRecord:
        """Atomically reserve a portion of the budget under concurrency constraints."""
        if amount < 0.0:
            raise ValueError("reservation amount must be non-negative")

        now = self._clock()
        now_str = _iso(now)
        expires_at = now + timedelta(seconds=lease_seconds) if lease_seconds > 0 else None
        expires_str = _iso(expires_at)

        with self._thread_lock:
            with self._conn:
                self._expire_stale_unlocked(budget_id)
                cursor = self._conn.execute(
                    "SELECT * FROM budgets WHERE budget_id = ?", (budget_id,)
                )
                row = cursor.fetchone()
                if not row:
                    raise BudgetNotFoundError(f"budget not found: {budget_id}")

                # 1. Deadline verification
                deadline = _parse_datetime(row["deadline"])
                if deadline is not None and now >= deadline:
                    raise DeadlineExceededError(
                        f"budget deadline {deadline.isoformat()} reached at {now.isoformat()}"
                    )

                # 2. Maximum attempts verification
                cur_attempts = self._conn.execute(
                    "SELECT count(*) as count FROM attempts WHERE budget_id = ?",
                    (budget_id,),
                ).fetchone()["count"]
                if cur_attempts >= row["max_attempts"]:
                    raise MaxAttemptsExceededError(
                        f"budget {budget_id} reached max attempts limit of {row['max_attempts']}"
                    )

                # 3. Concurrency limit verification
                cur_active = self._conn.execute(
                    "SELECT count(*) as count FROM reservations WHERE budget_id = ? AND status = ?",
                    (budget_id, ReservationStatus.ACTIVE.value),
                ).fetchone()["count"]
                if cur_active >= row["concurrency_limit"]:
                    raise ConcurrencyLimitExceededError(
                        f"budget {budget_id} reached active concurrency limit of {row['concurrency_limit']}"
                    )

                # 4. Overall ceiling verification
                current_spent = float(row["spent"])
                current_reserved = float(row["reserved"])
                ceiling = float(row["ceiling"])
                if round(current_spent + current_reserved + amount, 8) > ceiling:
                    raise BudgetExceededError(
                        f"reservation of {amount} would exceed ceiling {ceiling} "
                        f"(spent={current_spent}, reserved={current_reserved})"
                    )

                # 5. Short and long window limits verification
                if row["short_window_duration"] is not None and row["short_window_ceiling"] is not None:
                    short_spent = self._compute_window_spent_unlocked(budget_id, row["short_window_duration"])
                    short_ceiling = float(row["short_window_ceiling"])
                    if round(short_spent + current_reserved + amount, 8) > short_ceiling:
                        raise WindowBudgetExceededError(
                            f"reservation of {amount} would exceed short window ceiling {short_ceiling} "
                            f"(window_spent={short_spent}, reserved={current_reserved})"
                        )

                if row["long_window_duration"] is not None and row["long_window_ceiling"] is not None:
                    long_spent = self._compute_window_spent_unlocked(budget_id, row["long_window_duration"])
                    long_ceiling = float(row["long_window_ceiling"])
                    if round(long_spent + current_reserved + amount, 8) > long_ceiling:
                        raise WindowBudgetExceededError(
                            f"reservation of {amount} would exceed long window ceiling {long_ceiling} "
                            f"(window_spent={long_spent}, reserved={current_reserved})"
                        )

                # 6. Apply reservation
                reservation_id = f"res-{uuid4().hex[:12]}"
                new_reserved = round(current_reserved + amount, 8)
                self._conn.execute(
                    """
                    INSERT INTO reservations (
                        reservation_id, budget_id, attempt_id, amount,
                        status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        budget_id,
                        attempt_id,
                        amount,
                        ReservationStatus.ACTIVE.value,
                        now_str,
                        expires_str,
                    ),
                )
                self._conn.execute(
                    "UPDATE budgets SET reserved = ?, updated_at = ? WHERE budget_id = ?",
                    (new_reserved, now_str, budget_id),
                )

                return ReservationRecord(
                    reservation_id=reservation_id,
                    budget_id=budget_id,
                    attempt_id=attempt_id,
                    amount=amount,
                    status=ReservationStatus.ACTIVE,
                    created_at=now,
                    expires_at=expires_at,
                )

    def commit(
        self,
        reservation_id: str,
        attempt: AttemptRecord,
    ) -> float:
        """Commit an active reservation, settling actual costs per unknown-cost policy."""
        now = self._clock()
        now_str = _iso(now)

        with self._thread_lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT * FROM reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                res_row = cursor.fetchone()
                if not res_row:
                    raise InvalidReservationError(f"reservation not found: {reservation_id}")

                # Check for idempotent attempt replay
                existing_att = self._conn.execute(
                    "SELECT charged_cost, outcome FROM attempts WHERE attempt_id = ?",
                    (attempt.attempt_id,),
                ).fetchone()
                if existing_att:
                    if existing_att["outcome"] == attempt.outcome.value:
                        return float(existing_att["charged_cost"])
                    raise InvalidReservationError(
                        f"attempt {attempt.attempt_id} already committed with different outcome"
                    )

                if res_row["status"] != ReservationStatus.ACTIVE.value:
                    raise InvalidReservationError(
                        f"cannot commit reservation in status {res_row['status']}"
                    )

                budget_id = res_row["budget_id"]
                budget_row = self._conn.execute(
                    "SELECT * FROM budgets WHERE budget_id = ?",
                    (budget_id,),
                ).fetchone()
                if not budget_row:
                    raise BudgetNotFoundError(f"budget not found: {budget_id}")

                policy = UnknownCostPolicy(budget_row["unknown_cost_policy"])

                # Determine charged cost based on policy
                if attempt.measured_cost is not None:
                    charged_cost = float(attempt.measured_cost)
                else:
                    if policy == UnknownCostPolicy.REJECT:
                        raise UnknownCostRejectedError(
                            f"attempt {attempt.attempt_id} has no measured cost and policy is REJECT"
                        )
                    elif policy == UnknownCostPolicy.ESTIMATE:
                        charged_cost = float(attempt.estimated_cost)
                    elif policy == UnknownCostPolicy.CONSERVATIVE_MAX:
                        charged_cost = max(float(res_row["amount"]), float(attempt.estimated_cost) * 1.5)
                    else:
                        charged_cost = float(attempt.estimated_cost)

                charged_cost = round(charged_cost, 8)
                res_amount = float(res_row["amount"])

                # Record attempt
                self._conn.execute(
                    """
                    INSERT INTO attempts (
                        attempt_id, budget_id, invocation_id, input_artifact_hash,
                        output_artifact_hash, mode, tokens, measured_cost,
                        estimated_cost, charged_cost, latency, outcome, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt.attempt_id,
                        budget_id,
                        attempt.invocation_id,
                        attempt.input_artifact_hash,
                        attempt.output_artifact_hash,
                        attempt.mode,
                        attempt.tokens,
                        attempt.measured_cost,
                        attempt.estimated_cost,
                        charged_cost,
                        attempt.latency,
                        attempt.outcome.value,
                        _iso(attempt.timestamp),
                    ),
                )

                # Update reservation
                self._conn.execute(
                    "UPDATE reservations SET status = ?, committed_cost = ? WHERE reservation_id = ?",
                    (ReservationStatus.COMMITTED.value, charged_cost, reservation_id),
                )

                # Update budget balances
                new_reserved = max(0.0, round(float(budget_row["reserved"]) - res_amount, 8))
                new_spent = round(float(budget_row["spent"]) + charged_cost, 8)
                self._conn.execute(
                    "UPDATE budgets SET reserved = ?, spent = ?, updated_at = ? WHERE budget_id = ?",
                    (new_reserved, new_spent, now_str, budget_id),
                )

                return charged_cost

    def release(self, reservation_id: str, *, reason: str = "") -> None:
        """Release an active reservation without charging costs."""
        now_str = _iso(self._clock())
        with self._thread_lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT * FROM reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                res_row = cursor.fetchone()
                if not res_row:
                    raise InvalidReservationError(f"reservation not found: {reservation_id}")

                if res_row["status"] != ReservationStatus.ACTIVE.value:
                    raise InvalidReservationError(
                        f"cannot release reservation in status {res_row['status']}"
                    )

                budget_id = res_row["budget_id"]
                res_amount = float(res_row["amount"])

                self._conn.execute(
                    "UPDATE reservations SET status = ? WHERE reservation_id = ?",
                    (ReservationStatus.RELEASED.value, reservation_id),
                )

                budget_row = self._conn.execute(
                    "SELECT reserved FROM budgets WHERE budget_id = ?",
                    (budget_id,),
                ).fetchone()
                if budget_row:
                    new_reserved = max(0.0, round(float(budget_row["reserved"]) - res_amount, 8))
                    self._conn.execute(
                        "UPDATE budgets SET reserved = ?, updated_at = ? WHERE budget_id = ?",
                        (new_reserved, now_str, budget_id),
                    )

    def upgrade_reservation(
        self,
        reservation_id: str,
        new_amount: float,
    ) -> ReservationRecord:
        """Atomically upgrade an active reservation amount (e.g. on cloud fallback)."""
        if new_amount < 0.0:
            raise ValueError("reservation amount must be non-negative")

        now = self._clock()
        now_str = _iso(now)

        with self._thread_lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT * FROM reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
                res_row = cursor.fetchone()
                if not res_row:
                    raise InvalidReservationError(f"reservation not found: {reservation_id}")

                if res_row["status"] != ReservationStatus.ACTIVE.value:
                    raise InvalidReservationError(
                        f"cannot upgrade reservation in status {res_row['status']}"
                    )

                budget_id = res_row["budget_id"]
                budget_row = self._conn.execute(
                    "SELECT * FROM budgets WHERE budget_id = ?",
                    (budget_id,),
                ).fetchone()
                if not budget_row:
                    raise BudgetNotFoundError(f"budget not found: {budget_id}")

                old_amount = float(res_row["amount"])
                additional = round(new_amount - old_amount, 8)

                if additional > 0:
                    current_spent = float(budget_row["spent"])
                    current_reserved = float(budget_row["reserved"])
                    ceiling = float(budget_row["ceiling"])

                    if round(current_spent + current_reserved + additional, 8) > ceiling:
                        raise BudgetExceededError(
                            f"reservation upgrade of +{additional} would exceed ceiling {ceiling} "
                            f"(spent={current_spent}, reserved={current_reserved})"
                        )

                    # Check windows if configured
                    if budget_row["short_window_duration"] is not None and budget_row["short_window_ceiling"] is not None:
                        short_spent = self._compute_window_spent_unlocked(budget_id, budget_row["short_window_duration"])
                        short_ceiling = float(budget_row["short_window_ceiling"])
                        if round(short_spent + current_reserved + additional, 8) > short_ceiling:
                            raise WindowBudgetExceededError(
                                f"reservation upgrade of +{additional} would exceed short window ceiling {short_ceiling}"
                            )

                    if budget_row["long_window_duration"] is not None and budget_row["long_window_ceiling"] is not None:
                        long_spent = self._compute_window_spent_unlocked(budget_id, budget_row["long_window_duration"])
                        long_ceiling = float(budget_row["long_window_ceiling"])
                        if round(long_spent + current_reserved + additional, 8) > long_ceiling:
                            raise WindowBudgetExceededError(
                                f"reservation upgrade of +{additional} would exceed long window ceiling {long_ceiling}"
                            )

                new_reserved = max(0.0, round(float(budget_row["reserved"]) + additional, 8))
                self._conn.execute(
                    "UPDATE reservations SET amount = ? WHERE reservation_id = ?",
                    (new_amount, reservation_id),
                )
                self._conn.execute(
                    "UPDATE budgets SET reserved = ?, updated_at = ? WHERE budget_id = ?",
                    (new_reserved, now_str, budget_id),
                )

                return ReservationRecord(
                    reservation_id=reservation_id,
                    budget_id=budget_id,
                    attempt_id=res_row["attempt_id"],
                    amount=new_amount,
                    status=ReservationStatus.ACTIVE,
                    created_at=_parse_datetime(res_row["created_at"]),  # type: ignore[arg-type]
                    expires_at=_parse_datetime(res_row["expires_at"]),
                )

    def release_if_active(self, reservation_id: str, *, reason: str = "") -> bool:
        """Idempotently release a reservation if active; return True if released, False otherwise."""
        try:
            self.release(reservation_id, reason=reason)
            return True
        except (InvalidReservationError, BudgetNotFoundError):
            return False

    def expire_stale_reservations(self, budget_id: str | None = None) -> int:
        """Transition timed-out active reservations to EXPIRED and restore reserved balance."""
        with self._thread_lock:
            with self._conn:
                return self._expire_stale_unlocked(budget_id)

    def _expire_stale_unlocked(self, budget_id: str | None) -> int:
        now_str = _iso(self._clock())
        query = (
            "SELECT reservation_id, budget_id, amount FROM reservations "
            "WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?"
        )
        params: list[Any] = [ReservationStatus.ACTIVE.value, now_str]
        if budget_id is not None:
            query += " AND budget_id = ?"
            params.append(budget_id)

        rows = self._conn.execute(query, params).fetchall()
        if not rows:
            return 0

        for row in rows:
            self._conn.execute(
                "UPDATE reservations SET status = ? WHERE reservation_id = ?",
                (ReservationStatus.EXPIRED.value, row["reservation_id"]),
            )
            # Recompute total active reservations for this budget to guarantee consistency
            active_sum = self._conn.execute(
                "SELECT coalesce(sum(amount), 0.0) as active_sum FROM reservations WHERE budget_id = ? AND status = ?",
                (row["budget_id"], ReservationStatus.ACTIVE.value),
            ).fetchone()["active_sum"]
            self._conn.execute(
                "UPDATE budgets SET reserved = ?, updated_at = ? WHERE budget_id = ?",
                (round(float(active_sum), 8), now_str, row["budget_id"]),
            )
        return len(rows)

    def _compute_window_spent_unlocked(self, budget_id: str, duration_seconds: int) -> float:
        cutoff = self._clock() - timedelta(seconds=duration_seconds)
        cutoff_str = _iso(cutoff)
        row = self._conn.execute(
            "SELECT coalesce(sum(charged_cost), 0.0) as window_spent FROM attempts "
            "WHERE budget_id = ? AND timestamp >= ?",
            (budget_id, cutoff_str),
        ).fetchone()
        return round(float(row["window_spent"]), 8)

    def list_attempts(self, budget_id: str) -> list[AttemptRecord]:
        """List all recorded attempts for a budget in chronological order."""
        with self._thread_lock:
            rows = self._conn.execute(
                "SELECT * FROM attempts WHERE budget_id = ? ORDER BY timestamp ASC",
                (budget_id,),
            ).fetchall()
            return [
                AttemptRecord(
                    attempt_id=row["attempt_id"],
                    invocation_id=row["invocation_id"],
                    input_artifact_hash=row["input_artifact_hash"],
                    output_artifact_hash=row["output_artifact_hash"],
                    mode=row["mode"],
                    tokens=row["tokens"],
                    measured_cost=row["measured_cost"],
                    estimated_cost=row["estimated_cost"],
                    latency=row["latency"],
                    outcome=AttemptOutcome(row["outcome"]),
                    timestamp=_parse_datetime(row["timestamp"]),  # type: ignore[arg-type]
                )
                for row in rows
            ]

    def list_reservations(self, budget_id: str) -> list[ReservationRecord]:
        """List all reservations for a budget."""
        with self._thread_lock:
            rows = self._conn.execute(
                "SELECT * FROM reservations WHERE budget_id = ? ORDER BY created_at ASC",
                (budget_id,),
            ).fetchall()
            return [
                ReservationRecord(
                    reservation_id=row["reservation_id"],
                    budget_id=row["budget_id"],
                    attempt_id=row["attempt_id"],
                    amount=row["amount"],
                    status=ReservationStatus(row["status"]),
                    created_at=_parse_datetime(row["created_at"]),  # type: ignore[arg-type]
                    expires_at=_parse_datetime(row["expires_at"]),
                    committed_cost=row["committed_cost"],
                )
                for row in rows
            ]
