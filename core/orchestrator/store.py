"""Durable SQLite state for recoverable orchestrator runs.

The store is deliberately independent from ``state.py`` and ``usage/store.py``.
The former owns the DF-01 lifecycle ledger; the latter owns DF-10 telemetry. This
module owns only execution runs, checkpoints, and the current fencing lease for a
task.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


DEFAULT_DATABASE_PATH = Path(".factory/orchestrator.sqlite3")
_Clock = Callable[[], datetime]


class StoreError(RuntimeError):
    """Base class for durable orchestrator store failures."""


class LeaseConflictError(StoreError):
    """A task already has a non-expired lease owned by another worker."""


class StaleLeaseError(StoreError):
    """A lease no longer has authority to mutate its run."""


class RunNotFoundError(StoreError):
    """A requested run does not exist."""


class RunAlreadyCompletedError(StoreError):
    """A caller attempted to resume a terminal run."""


class CheckpointError(StoreError):
    """A checkpoint is invalid or cannot be serialized as JSON."""


class StoreCorruptionError(StoreError):
    """The database contains a row that violates the store contract."""


class RunStatus(str, Enum):
    """Persisted lifecycle states for an execution run."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_datetime(value: str) -> datetime:
    try:
        return _as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError) as exc:
        raise StoreCorruptionError(f"invalid timestamp in orchestrator store: {value!r}") from exc


class RunRecord(BaseModel):
    """Versioned execution state that can be resumed after a process crash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: RunStatus
    step_index: int = Field(default=0, ge=0)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    recovery_count: int = Field(default=0, ge=0)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class LeaseClaim(BaseModel):
    """Capability returned by a successful atomic claim.

    ``token`` identifies this particular claim. ``fencing_token`` is monotonic
    for a task and makes an old worker distinguishable even if it retains a
    stale copy of the token. Both values are required for every mutation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    token: str = Field(min_length=1)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    recovered: bool = False

    @field_validator("owner")
    @classmethod
    def _owner_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("owner must not be blank")
        return normalized

    def is_valid(self, now: datetime | None = None) -> bool:
        """Return whether this claim is still the current live capability."""
        current = _as_utc(now or _utc_now())
        return self.released_at is None and self.expires_at > current


# Short aliases make the contract discoverable for callers that call this a
# lease rather than a claim, without creating a second representation.
Lease = LeaseClaim


class OrchestratorStore:
    """SQLite-backed run, checkpoint, and fencing-lease store.

    Every mutating operation opens a fresh connection and uses ``BEGIN
    IMMEDIATE``. SQLite therefore serializes claims across threads and
    processes before the current lease is inspected, preventing two workers
    from observing the same task as claimable.
    """

    schema_version = 1

    def __init__(
        self,
        path: Path | str = DEFAULT_DATABASE_PATH,
        *,
        clock: _Clock | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.path = Path(path)
        self._clock = clock or _utc_now
        self._timeout_seconds = timeout_seconds
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def __enter__(self) -> "OrchestratorStore":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if str(self.path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS orchestrator_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                step_index INTEGER NOT NULL DEFAULT 0,
                checkpoint_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                recovery_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS runs_by_task_and_status
                ON runs(task_id, status, created_at DESC);

            CREATE TABLE IF NOT EXISTS leases (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                owner TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                fencing_token INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT
            );

            CREATE INDEX IF NOT EXISTS leases_by_expiration
                ON leases(expires_at);
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO orchestrator_meta(key, value) VALUES('schema_version', ?)",
            (str(OrchestratorStore.schema_version),),
        )

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            yield connection
        finally:
            connection.close()

    def _now(self, value: datetime | None = None) -> datetime:
        return _as_utc(value or self._clock())

    @staticmethod
    def _json(value: Any, *, field_name: str) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise CheckpointError(f"{field_name} must be JSON serializable") from exc

    @staticmethod
    def _load_json(raw: str | None, *, field_name: str, default: Any = None) -> Any:
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StoreCorruptionError(f"invalid {field_name} JSON in orchestrator store") from exc

    @classmethod
    def _run_from_row(cls, row: sqlite3.Row) -> RunRecord:
        checkpoint = cls._load_json(row["checkpoint_json"], field_name="checkpoint", default={})
        if not isinstance(checkpoint, dict):
            raise StoreCorruptionError("checkpoint must be a JSON object")
        return RunRecord(
            run_id=row["run_id"],
            task_id=row["task_id"],
            status=RunStatus(row["status"]),
            step_index=row["step_index"],
            checkpoint=checkpoint,
            result=cls._load_json(row["result_json"], field_name="result"),
            recovery_count=row["recovery_count"],
            last_error=row["last_error"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    @classmethod
    def _lease_from_row(cls, row: sqlite3.Row, *, recovered: bool = False) -> LeaseClaim:
        return LeaseClaim(
            task_id=row["task_id"],
            run_id=row["run_id"],
            owner=row["owner"],
            token=row["token"],
            fencing_token=row["fencing_token"],
            acquired_at=_parse_datetime(row["acquired_at"]),
            renewed_at=_parse_datetime(row["renewed_at"]),
            expires_at=_parse_datetime(row["expires_at"]),
            released_at=(
                _parse_datetime(row["released_at"]) if row["released_at"] is not None else None
            ),
            recovered=recovered,
        )

    @staticmethod
    def _validate_task_id(task_id: str) -> str:
        normalized = task_id.strip()
        if not normalized:
            raise ValueError("task_id must not be blank")
        return normalized

    @staticmethod
    def _validate_lease_seconds(lease_seconds: float) -> float:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        return float(lease_seconds)

    def create_run(
        self,
        task_id: str,
        *,
        run_id: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> RunRecord:
        """Create a standalone running run, normally used by tests or adapters."""
        normalized_task = self._validate_task_id(task_id)
        selected_run_id = (run_id or uuid4().hex).strip()
        if not selected_run_id:
            raise ValueError("run_id must not be blank")
        selected_checkpoint = dict(checkpoint or {})
        checkpoint_json = self._json(selected_checkpoint, field_name="checkpoint")
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, task_id, status, step_index, checkpoint_json,
                        recovery_count, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, 0, ?, ?)
                    """,
                    (
                        selected_run_id,
                        normalized_task,
                        RunStatus.RUNNING.value,
                        checkpoint_json,
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"run {selected_run_id!r} already exists") from exc
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (selected_run_id,)
            ).fetchone()
            assert row is not None
            return self._run_from_row(row)

    def get_run(self, run_id: str) -> RunRecord:
        with self._read_connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id!r} was not found")
        return self._run_from_row(row)

    def get_active_run(self, task_id: str) -> RunRecord | None:
        normalized_task = self._validate_task_id(task_id)
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE task_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_task, RunStatus.RUNNING.value),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def get_latest_run(self, task_id: str) -> RunRecord | None:
        normalized_task = self._validate_task_id(task_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (normalized_task,),
            ).fetchone()
        return self._run_from_row(row) if row is not None else None

    def claim(
        self,
        task_id: str,
        *,
        owner: str,
        lease_seconds: float = 30.0,
        run_id: str | None = None,
        now: datetime | None = None,
    ) -> LeaseClaim:
        """Atomically claim a task or reclaim its expired run.

        A live lease causes ``LeaseConflictError``. An expired lease is replaced
        in the same transaction with a new token and incremented fencing token;
        the old worker can no longer renew, checkpoint, or complete the run.
        """
        normalized_task = self._validate_task_id(task_id)
        normalized_owner = owner.strip()
        if not normalized_owner:
            raise ValueError("owner must not be blank")
        duration = self._validate_lease_seconds(lease_seconds)
        timestamp = self._now(now)
        expiry = timestamp + timedelta(seconds=duration)

        with self._write_transaction() as connection:
            lease_row = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (normalized_task,)
            ).fetchone()
            if lease_row is not None:
                released_at = lease_row["released_at"]
                expires_at = _parse_datetime(lease_row["expires_at"])
                if released_at is None and expires_at > timestamp:
                    raise LeaseConflictError(
                        f"task {normalized_task!r} already has a live lease "
                        f"owned by {lease_row['owner']!r}"
                    )

            selected_run: sqlite3.Row | None
            recovered = False
            if run_id is not None:
                selected_run = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ? AND task_id = ?",
                    (run_id, normalized_task),
                ).fetchone()
                if selected_run is None:
                    raise RunNotFoundError(
                        f"run {run_id!r} for task {normalized_task!r} was not found"
                    )
                if selected_run["status"] != RunStatus.RUNNING.value:
                    raise RunAlreadyCompletedError(
                        f"run {run_id!r} is already {selected_run['status']}"
                    )
                recovered = lease_row is not None
            else:
                selected_run = connection.execute(
                    """
                    SELECT * FROM runs
                    WHERE task_id = ? AND status = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (normalized_task, RunStatus.RUNNING.value),
                ).fetchone()
                recovered = selected_run is not None and lease_row is not None

            if selected_run is None:
                selected_run_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, task_id, status, step_index, checkpoint_json,
                        recovery_count, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, '{}', 0, ?, ?)
                    """,
                    (
                        selected_run_id,
                        normalized_task,
                        RunStatus.RUNNING.value,
                        _iso(timestamp),
                        _iso(timestamp),
                    ),
                )
            else:
                selected_run_id = selected_run["run_id"]
                if recovered:
                    connection.execute(
                        """
                        UPDATE runs
                        SET recovery_count = recovery_count + 1, updated_at = ?
                        WHERE run_id = ?
                        """,
                        (_iso(timestamp), selected_run_id),
                    )

            fencing_token = (int(lease_row["fencing_token"]) + 1) if lease_row else 1
            claim_token = uuid4().hex
            if lease_row is None:
                connection.execute(
                    """
                    INSERT INTO leases(
                        task_id, run_id, owner, token, fencing_token,
                        acquired_at, renewed_at, expires_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        normalized_task,
                        selected_run_id,
                        normalized_owner,
                        claim_token,
                        fencing_token,
                        _iso(timestamp),
                        _iso(timestamp),
                        _iso(expiry),
                    ),
                )
            else:
                if lease_row["run_id"] != selected_run_id and selected_run is not None:
                    raise LeaseConflictError(
                        f"task {normalized_task!r} has another recoverable run "
                        f"{lease_row['run_id']!r}"
                    )
                connection.execute(
                    """
                    UPDATE leases
                    SET run_id = ?, owner = ?, token = ?, fencing_token = ?,
                        acquired_at = ?, renewed_at = ?, expires_at = ?, released_at = NULL
                    WHERE task_id = ?
                    """,
                    (
                        selected_run_id,
                        normalized_owner,
                        claim_token,
                        fencing_token,
                        _iso(timestamp),
                        _iso(timestamp),
                        _iso(expiry),
                        normalized_task,
                    ),
                )

            row = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (normalized_task,)
            ).fetchone()
            assert row is not None
            return self._lease_from_row(row, recovered=recovered)

    # ``acquire_lease`` is an explicit spelling for integrations that do not
    # use the shorter claim terminology.
    acquire_lease = claim

    @staticmethod
    def _identity(
        claim_or_task_id: LeaseClaim | str,
        owner: str | None,
        token: str | None,
        fencing_token: int | None,
    ) -> tuple[str, str, str, int, str]:
        if isinstance(claim_or_task_id, LeaseClaim):
            return (
                claim_or_task_id.task_id,
                claim_or_task_id.owner,
                claim_or_task_id.token,
                claim_or_task_id.fencing_token,
                claim_or_task_id.run_id,
            )
        if owner is None or token is None or fencing_token is None:
            raise TypeError(
                "owner, token, and fencing_token are required when a task_id is supplied"
            )
        return (claim_or_task_id, owner, token, fencing_token, "")

    def _assert_current_lease(
        self,
        connection: sqlite3.Connection,
        claim_or_task_id: LeaseClaim | str,
        *,
        owner: str | None,
        token: str | None,
        fencing_token: int | None,
        timestamp: datetime,
    ) -> tuple[sqlite3.Row, str, str, str, int, str]:
        task_id, expected_owner, expected_token, expected_fence, expected_run_id = self._identity(
            claim_or_task_id, owner, token, fencing_token
        )
        row = connection.execute("SELECT * FROM leases WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            raise StaleLeaseError(f"task {task_id!r} has no current lease")
        current_expiry = _parse_datetime(row["expires_at"])
        if row["released_at"] is not None or current_expiry <= timestamp:
            raise StaleLeaseError(f"lease for task {task_id!r} is stale or expired")
        if (
            row["owner"] != expected_owner
            or row["token"] != expected_token
            or int(row["fencing_token"]) != expected_fence
            or (expected_run_id and row["run_id"] != expected_run_id)
        ):
            raise StaleLeaseError(
                f"lease for task {task_id!r} does not match the current fencing token"
            )
        return row, task_id, expected_owner, expected_token, expected_fence, row["run_id"]

    def renew(
        self,
        claim_or_task_id: LeaseClaim | str,
        owner: str | None = None,
        token: str | None = None,
        fencing_token: int | None = None,
        *,
        lease_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> LeaseClaim:
        """Renew only the current, unexpired lease with matching fencing data."""
        duration = self._validate_lease_seconds(lease_seconds)
        timestamp = self._now(now)
        expiry = timestamp + timedelta(seconds=duration)
        with self._write_transaction() as connection:
            row, task_id, _, _, _, _ = self._assert_current_lease(
                connection,
                claim_or_task_id,
                owner=owner,
                token=token,
                fencing_token=fencing_token,
                timestamp=timestamp,
            )
            connection.execute(
                "UPDATE leases SET renewed_at = ?, expires_at = ? WHERE task_id = ?",
                (_iso(timestamp), _iso(expiry), task_id),
            )
            refreshed = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert refreshed is not None
            recovered = claim_or_task_id.recovered if isinstance(claim_or_task_id, LeaseClaim) else False
            return self._lease_from_row(refreshed, recovered=recovered)

    renew_lease = renew

    def save_checkpoint(
        self,
        claim_or_task_id: LeaseClaim | str,
        checkpoint: dict[str, Any],
        *,
        step_index: int,
        owner: str | None = None,
        token: str | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> RunRecord:
        """Persist a monotonic checkpoint while the caller still owns the lease."""
        if step_index < 0:
            raise ValueError("step_index must not be negative")
        if not isinstance(checkpoint, dict):
            raise CheckpointError("checkpoint must be a JSON object")
        checkpoint_json = self._json(checkpoint, field_name="checkpoint")
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            _, task_id, _, _, _, run_id = self._assert_current_lease(
                connection,
                claim_or_task_id,
                owner=owner,
                token=token,
                fencing_token=fencing_token,
                timestamp=timestamp,
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {run_id!r} was not found")
            if row["status"] != RunStatus.RUNNING.value:
                raise StaleLeaseError(f"run {run_id!r} is no longer running")
            if step_index < int(row["step_index"]):
                raise CheckpointError(
                    f"checkpoint step {step_index} regresses persisted step {row['step_index']}"
                )
            connection.execute(
                """
                UPDATE runs
                SET step_index = ?, checkpoint_json = ?, updated_at = ?
                WHERE run_id = ? AND task_id = ? AND status = ?
                """,
                (
                    step_index,
                    checkpoint_json,
                    _iso(timestamp),
                    run_id,
                    task_id,
                    RunStatus.RUNNING.value,
                ),
            )
            updated = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert updated is not None
            return self._run_from_row(updated)

    checkpoint = save_checkpoint

    def complete(
        self,
        claim_or_task_id: LeaseClaim | str,
        result: Any = None,
        owner: str | None = None,
        token: str | None = None,
        fencing_token: int | None = None,
        *,
        now: datetime | None = None,
    ) -> RunRecord:
        """Complete a run only if its lease is still current and unexpired."""
        result_json = self._json(result, field_name="result")
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            _, task_id, _, _, _, run_id = self._assert_current_lease(
                connection,
                claim_or_task_id,
                owner=owner,
                token=token,
                fencing_token=fencing_token,
                timestamp=timestamp,
            )
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {run_id!r} was not found")
            if row["status"] != RunStatus.RUNNING.value:
                raise StaleLeaseError(f"run {run_id!r} is no longer running")
            connection.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, updated_at = ?, last_error = NULL
                WHERE run_id = ? AND task_id = ? AND status = ?
                """,
                (
                    RunStatus.SUCCEEDED.value,
                    result_json,
                    _iso(timestamp),
                    run_id,
                    task_id,
                    RunStatus.RUNNING.value,
                ),
            )
            connection.execute(
                "UPDATE leases SET released_at = ? WHERE task_id = ?",
                (_iso(timestamp), task_id),
            )
            completed = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert completed is not None
            return self._run_from_row(completed)

    complete_run = complete

    def fail(
        self,
        claim_or_task_id: LeaseClaim | str,
        error: str,
        owner: str | None = None,
        token: str | None = None,
        fencing_token: int | None = None,
        *,
        now: datetime | None = None,
    ) -> RunRecord:
        """Mark a run failed explicitly and release its current lease."""
        if not error.strip():
            raise ValueError("error must not be blank")
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            _, task_id, _, _, _, run_id = self._assert_current_lease(
                connection,
                claim_or_task_id,
                owner=owner,
                token=token,
                fencing_token=fencing_token,
                timestamp=timestamp,
            )
            connection.execute(
                """
                UPDATE runs SET status = ?, last_error = ?, updated_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (RunStatus.FAILED.value, error.strip(), _iso(timestamp), run_id, RunStatus.RUNNING.value),
            )
            connection.execute(
                "UPDATE leases SET released_at = ? WHERE task_id = ?",
                (_iso(timestamp), task_id),
            )
            failed = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            assert failed is not None
            return self._run_from_row(failed)

    def release(
        self,
        claim_or_task_id: LeaseClaim | str,
        owner: str | None = None,
        token: str | None = None,
        fencing_token: int | None = None,
        *,
        now: datetime | None = None,
    ) -> LeaseClaim:
        """Release a lease without discarding its recoverable running state."""
        timestamp = self._now(now)
        with self._write_transaction() as connection:
            _, task_id, _, _, _, _ = self._assert_current_lease(
                connection,
                claim_or_task_id,
                owner=owner,
                token=token,
                fencing_token=fencing_token,
                timestamp=timestamp,
            )
            connection.execute(
                "UPDATE leases SET released_at = ? WHERE task_id = ?",
                (_iso(timestamp), task_id),
            )
            released = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
            assert released is not None
            recovered = claim_or_task_id.recovered if isinstance(claim_or_task_id, LeaseClaim) else False
            return self._lease_from_row(released, recovered=recovered)

    def recoverable_runs(self, *, now: datetime | None = None) -> list[RunRecord]:
        """List running runs whose lease is absent, released, or expired."""
        timestamp = self._now(now)
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE status = ? ORDER BY updated_at ASC",
                (RunStatus.RUNNING.value,),
            ).fetchall()
            result: list[RunRecord] = []
            for row in rows:
                lease = connection.execute(
                    "SELECT * FROM leases WHERE task_id = ?", (row["task_id"],)
                ).fetchone()
                if lease is None or lease["released_at"] is not None or _parse_datetime(lease["expires_at"]) <= timestamp:
                    result.append(self._run_from_row(row))
            return result

    list_recoverable_runs = recoverable_runs

    def current_lease(self, task_id: str) -> LeaseClaim | None:
        normalized_task = self._validate_task_id(task_id)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (normalized_task,)
            ).fetchone()
        return self._lease_from_row(row) if row is not None else None

    def is_lease_valid(self, claim: LeaseClaim, *, now: datetime | None = None) -> bool:
        """Check the durable current lease, not only the caller's local expiry."""
        timestamp = self._now(now)
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE task_id = ?", (claim.task_id,)
            ).fetchone()
            if row is None:
                return False
            return (
                row["released_at"] is None
                and _parse_datetime(row["expires_at"]) > timestamp
                and row["run_id"] == claim.run_id
                and row["owner"] == claim.owner
                and row["token"] == claim.token
                and int(row["fencing_token"]) == claim.fencing_token
            )


# Friendly names for adapters and tests that describe the object as a runtime
# store or durable run store.
RuntimeStore = OrchestratorStore
LeaseError = StoreError


__all__ = [
    "CheckpointError",
    "DEFAULT_DATABASE_PATH",
    "Lease",
    "LeaseClaim",
    "LeaseConflictError",
    "LeaseError",
    "OrchestratorStore",
    "RunAlreadyCompletedError",
    "RunNotFoundError",
    "RunRecord",
    "RunStatus",
    "RuntimeStore",
    "StaleLeaseError",
    "StoreCorruptionError",
    "StoreError",
]
