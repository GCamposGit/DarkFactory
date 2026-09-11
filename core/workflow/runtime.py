"""Durable, headless runtime primitives for the local HF-05 slice.

The runtime deliberately owns one SQLite store per execution boundary.  It
does not call workers or external services; it records the state and effects
that those adapters will reconcile in a later HF-05 package.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NoReturn
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.workflow.contracts import WorkflowState
from core.workflow.readiness import ReadinessError, validate_transition


class RuntimeModel(BaseModel):
    """Closed JSON contracts crossing the runtime boundary."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JobStatus(str, Enum):
    READY = "ready"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    WAITING_BUDGET = "waiting_budget"
    CANCELLED = "cancelled"


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class EventStatus(str, Enum):
    PENDING = "pending"
    ACKED = "acked"


class JobOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunRecord(RuntimeModel):
    run_id: str = Field(min_length=1, max_length=160)
    project_id: str = Field(min_length=1, max_length=160)
    state: WorkflowState
    budget_ceiling: float = Field(ge=0.0)
    budget_reserved: float = Field(ge=0.0)
    budget_spent: float = Field(ge=0.0)
    created_at: datetime
    updated_at: datetime

    @property
    def budget_available(self) -> float:
        return max(0.0, round(self.budget_ceiling - self.budget_reserved - self.budget_spent, 8))


class JobSpec(RuntimeModel):
    job_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    project_id: str = Field(min_length=1, max_length=160)
    stage: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=0, ge=0, le=1_000_000)
    depends_on: list[str] = Field(default_factory=list)
    conflict_keys: list[str] = Field(default_factory=list)
    estimated_cost: float = Field(default=0.0, ge=0.0)
    max_attempts: int = Field(default=3, ge=1, le=100)
    ready_at: datetime | None = None
    successor_event: str | None = Field(default=None, min_length=1, max_length=160)
    dedupe_key: str | None = Field(default=None, min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_collections(self) -> "JobSpec":
        for label, values in (("dependency IDs", self.depends_on), ("conflict keys", self.conflict_keys)):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} are not allowed")
        if self.job_id in self.depends_on:
            raise ValueError("a job cannot depend on itself")
        return self

    @property
    def effective_dedupe_key(self) -> str:
        return self.dedupe_key or f"job:{self.job_id}"


class JobRecord(JobSpec):
    status: JobStatus
    attempt: int = Field(ge=0)
    lease_id: str | None = None
    owner: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class LeaseRecord(RuntimeModel):
    lease_id: str = Field(min_length=1, max_length=160)
    job_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    worker_id: str = Field(min_length=1, max_length=160)
    stage: str = Field(min_length=1, max_length=80)
    attempt: int = Field(ge=1)
    status: LeaseStatus
    acquired_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    outcome: JobOutcome | None = None


class OutboxEvent(RuntimeModel):
    event_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    event_type: str = Field(min_length=1, max_length=160)
    dedupe_key: str = Field(min_length=1, max_length=240)
    payload: dict[str, Any]
    status: EventStatus
    created_at: datetime
    acked_at: datetime | None = None


class OperationRecord(RuntimeModel):
    operation_key: str = Field(min_length=1, max_length=240)
    operation_type: str = Field(min_length=1, max_length=80)
    subject_id: str = Field(min_length=1, max_length=160)
    payload_canonical: dict[str, Any]
    result_json: str
    version: int = Field(default=1, ge=1)
    created_at: datetime


class ReconciliationReport(RuntimeModel):
    expired_leases: int = Field(ge=0)
    retry_scheduled: int = Field(ge=0)
    failed_jobs: int = Field(ge=0)
    requeued_jobs: int = Field(ge=0)


class RuntimeErrorBase(RuntimeError):
    """Base exception for deterministic runtime failures."""


class RuntimeConflictError(RuntimeErrorBase):
    """A stable idempotency key was reused for another effect."""


class LeaseOwnershipError(RuntimeErrorBase):
    """A worker attempted to use a lease it does not own."""


class LeaseExpiredError(RuntimeErrorBase):
    """A worker attempted to use an expired lease."""


class RuntimeNotFoundError(RuntimeErrorBase):
    """A requested run, job, lease or event does not exist."""


_LOCAL_RUNTIME_DELIVERY_FORBIDDEN = "LOCAL_RUNTIME_DELIVERY_FORBIDDEN"


def _raise_local_delivery_forbidden() -> NoReturn:
    """Keep local runtime delivery rejection stable and sanitized."""

    raise ReadinessError(_LOCAL_RUNTIME_DELIVERY_FORBIDDEN)


_TERMINAL_RUN_STATES = frozenset(
    {
        WorkflowState.DELIVERED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.SKIPPED_BY_POLICY,
    }
)
_DISPATCHABLE_RUN_STATES = frozenset(
    {
        WorkflowState.IMPLEMENTING_ECONOMY,
        WorkflowState.VALIDATING,
        WorkflowState.INDEPENDENT_REVIEW,
        WorkflowState.RETRYABLE,
        WorkflowState.RETRY_SCHEDULED,
        WorkflowState.WAITING_CAPACITY,
        WorkflowState.WAITING_DEPENDENCY,
    }
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkflowRuntime:
    """Transactional local store and dispatcher for one workflow control plane."""

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        stage_limits: Mapping[str, int] | None = None,
        retry_delay_seconds: float = 30.0,
    ) -> None:
        self.database_path = Path(database_path)
        if str(self.database_path) != ":memory:":
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _utc_now
        self._stage_limits = {key: value for key, value in (stage_limits or {}).items()}
        if any(value < 1 for value in self._stage_limits.values()):
            raise ValueError("stage limits must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry delay must not be negative")
        self._retry_delay_seconds = retry_delay_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.database_path),
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _initialize_schema(self) -> None:
        with self._lock:
            version_row = self._connection.execute("PRAGMA user_version").fetchone()
            current_version = int(version_row[0]) if version_row else 0
            if current_version < 1:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        budget_ceiling REAL NOT NULL CHECK (budget_ceiling >= 0),
                        budget_reserved REAL NOT NULL CHECK (budget_reserved >= 0),
                        budget_spent REAL NOT NULL CHECK (budget_spent >= 0),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        project_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        depends_on TEXT NOT NULL,
                        conflict_keys TEXT NOT NULL,
                        estimated_cost REAL NOT NULL CHECK (estimated_cost >= 0),
                        max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                        ready_at TEXT,
                        status TEXT NOT NULL,
                        attempt INTEGER NOT NULL CHECK (attempt >= 0),
                        lease_id TEXT,
                        owner TEXT,
                        last_error TEXT,
                        successor_event TEXT,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        spec_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS leases (
                        lease_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id),
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        worker_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        completed_at TEXT,
                        outcome TEXT
                    );
                    CREATE TABLE IF NOT EXISTS outbox (
                        event_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id),
                        event_type TEXT NOT NULL,
                        dedupe_key TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        acked_at TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_jobs_dispatch
                        ON jobs(status, ready_at, priority, created_at);
                    CREATE INDEX IF NOT EXISTS idx_leases_active
                        ON leases(status, expires_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_one_active_job
                        ON leases(job_id) WHERE status = 'active';
                    CREATE INDEX IF NOT EXISTS idx_outbox_pending
                        ON outbox(status, created_at);
                    """
                )
            if current_version < 2:
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runtime_operations (
                        operation_key TEXT PRIMARY KEY,
                        operation_type TEXT NOT NULL,
                        subject_id TEXT NOT NULL,
                        payload_canonical TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_operations_subject
                        ON runtime_operations(operation_type, subject_id);
                    """
                )
                self._connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _get_operation(connection: sqlite3.Connection, operation_key: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM runtime_operations WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()

    @staticmethod
    def _record_operation(
        connection: sqlite3.Connection,
        *,
        operation_key: str,
        operation_type: str,
        subject_id: str,
        payload_canonical: dict[str, Any],
        result_json: str,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_operations (
                operation_key, operation_type, subject_id, payload_canonical,
                result_json, version, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                operation_key,
                operation_type,
                subject_id,
                _json(payload_canonical),
                result_json,
                _iso(created_at),
            ),
        )

    def register_run(
        self,
        run_id: str,
        project_id: str,
        *,
        initial_state: WorkflowState = WorkflowState.READY_FOR_HANDOFF,
        budget_ceiling: float = 0.0,
    ) -> RunRecord:
        if not run_id.strip() or not project_id.strip():
            raise ValueError("run_id and project_id are required")
        if budget_ceiling < 0:
            raise ValueError("budget ceiling must not be negative")
        if initial_state is WorkflowState.DELIVERED:
            _raise_local_delivery_forbidden()
        now = self._clock()
        with self._transaction() as connection:
            existing = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if existing is not None:
                record = self._run_from_row(existing)
                if record.project_id != project_id or record.budget_ceiling != budget_ceiling:
                    raise RuntimeConflictError(f"run {run_id!r} already exists with different identity")
                return record
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, state, budget_ceiling, budget_reserved,
                    budget_spent, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (run_id, project_id, initial_state.value, budget_ceiling, _iso(now), _iso(now)),
            )
            return self._run_from_row(connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def get_run(self, run_id: str) -> RunRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RuntimeNotFoundError(f"run not found: {run_id}")
        return self._run_from_row(row)

    def transition_run(
        self,
        run_id: str,
        target_state: WorkflowState,
        *,
        idempotency_key: str | None = None,
    ) -> RunRecord:
        if target_state is WorkflowState.DELIVERED:
            _raise_local_delivery_forbidden()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RuntimeNotFoundError(f"run not found: {run_id}")
            current = WorkflowState(row["state"])
            if current is WorkflowState.DELIVERED:
                _raise_local_delivery_forbidden()

            op_key = idempotency_key or f"run:{run_id}:{current.value}:{target_state.value}"
            canonical_payload = {
                "run_id": run_id,
                "target_state": target_state.value,
            }

            existing_op = self._get_operation(connection, op_key)
            if existing_op is not None:
                expected_canonical = _json(canonical_payload)
                if (
                    existing_op["operation_type"] != "run_transition"
                    or existing_op["subject_id"] != run_id
                    or existing_op["payload_canonical"] != expected_canonical
                ):
                    raise RuntimeConflictError(
                        f"idempotency key {op_key!r} already used for a conflicting operation"
                    )
                saved_data = json.loads(existing_op["result_json"])
                return RunRecord.model_validate(saved_data)

            if current is target_state:
                return self._run_from_row(row)

            validate_transition(current, target_state)
            now = self._clock()
            dedupe_key = op_key
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.transitioned",
                dedupe_key=dedupe_key,
                payload={"from_state": current.value, "to_state": target_state.value},
                created_at=now,
            )
            connection.execute(
                "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                (target_state.value, _iso(now), run_id),
            )
            updated_row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            updated_record = self._run_from_row(updated_row)
            self._record_operation(
                connection,
                operation_key=op_key,
                operation_type="run_transition",
                subject_id=run_id,
                payload_canonical=canonical_payload,
                result_json=updated_record.model_dump_json(),
                created_at=now,
            )
            return updated_record

    def enqueue_job(self, spec: JobSpec) -> JobRecord:
        now = self._clock()
        ready_at = spec.ready_at or now
        spec_payload = spec.model_dump(mode="json")
        spec_json = _json(spec_payload)
        with self._transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (spec.run_id,)).fetchone()
            if run is None:
                raise RuntimeNotFoundError(f"run not found: {spec.run_id}")
            if run["project_id"] != spec.project_id:
                raise RuntimeConflictError("job project_id must match its run")
            existing = connection.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (spec.effective_dedupe_key,)).fetchone()
            if existing is not None:
                if existing["spec_json"] != spec_json:
                    raise RuntimeConflictError("job dedupe key was reused with a different specification")
                return self._job_from_row(existing)
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, run_id, project_id, stage, priority, depends_on,
                    conflict_keys, estimated_cost, max_attempts, ready_at, status,
                    attempt, lease_id, owner, last_error, successor_event,
                    dedupe_key, spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    spec.job_id,
                    spec.run_id,
                    spec.project_id,
                    spec.stage,
                    spec.priority,
                    _json(spec.depends_on),
                    _json(spec.conflict_keys),
                    spec.estimated_cost,
                    spec.max_attempts,
                    _iso(ready_at),
                    JobStatus.READY.value,
                    spec.successor_event,
                    spec.effective_dedupe_key,
                    spec_json,
                    _iso(now),
                    _iso(now),
                ),
            )
            self._insert_event(
                connection,
                run_id=spec.run_id,
                event_type="job.queued",
                dedupe_key=f"job:{spec.job_id}:queued",
                payload={"job_id": spec.job_id, "stage": spec.stage},
                created_at=now,
            )
            return self._job_from_row(connection.execute("SELECT * FROM jobs WHERE job_id = ?", (spec.job_id,)).fetchone())

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeNotFoundError(f"job not found: {job_id}")
        return self._job_from_row(row)

    def claim_next(self, worker_id: str, *, lease_seconds: float = 30.0) -> LeaseRecord | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as connection:
            now = self._clock()
            self._reconcile_unlocked(connection, now)
            candidates = self._eligible_jobs(connection, now)
            if not candidates:
                return None
            active_by_project = self._active_by_project(connection)
            active_by_stage = self._active_by_stage(connection)
            active_conflicts = self._active_conflicts(connection)
            ranked: list[tuple[tuple[int, int, str, str], sqlite3.Row]] = []
            for row in candidates:
                limit = self._stage_limits.get(row["stage"])
                if limit is not None and active_by_stage.get(row["stage"], 0) >= limit:
                    continue
                conflicts = set(json.loads(row["conflict_keys"]))
                if conflicts.intersection(active_conflicts):
                    continue
                dependencies = json.loads(row["depends_on"])
                if not self._dependencies_succeeded(connection, dependencies):
                    continue
                run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
                if run is None or WorkflowState(run["state"]) not in _DISPATCHABLE_RUN_STATES:
                    continue
                available = float(run["budget_ceiling"]) - float(run["budget_reserved"]) - float(run["budget_spent"])
                if available + 1e-9 < float(row["estimated_cost"]):
                    if row["status"] != JobStatus.WAITING_BUDGET.value:
                        self._set_job_status(connection, row["job_id"], JobStatus.WAITING_BUDGET, now, "budget unavailable")
                        self._insert_event(
                            connection,
                            run_id=row["run_id"],
                            event_type="job.waiting_budget",
                            dedupe_key=f"job:{row['job_id']}:waiting-budget",
                            payload={"job_id": row["job_id"], "estimated_cost": row["estimated_cost"]},
                            created_at=now,
                        )
                    continue
                ranked.append(
                    (
                        (
                            active_by_project.get(row["project_id"], 0),
                            -int(row["priority"]),
                            row["created_at"],
                            row["job_id"],
                        ),
                        row,
                    )
                )
            if not ranked:
                return None
            _, row = min(ranked, key=lambda item: item[0])
            attempt = int(row["attempt"]) + 1
            lease_id = f"lease_{uuid4().hex}"
            expires_at = now + timedelta(seconds=lease_seconds)
            connection.execute(
                """
                INSERT INTO leases (
                    lease_id, job_id, run_id, worker_id, stage, attempt, status,
                    acquired_at, expires_at, completed_at, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    lease_id,
                    row["job_id"],
                    row["run_id"],
                    worker_id,
                    row["stage"],
                    attempt,
                    LeaseStatus.ACTIVE.value,
                    _iso(now),
                    _iso(expires_at),
                ),
            )
            connection.execute(
                "UPDATE jobs SET status = ?, attempt = ?, lease_id = ?, owner = ?, updated_at = ? WHERE job_id = ?",
                (JobStatus.CLAIMED.value, attempt, lease_id, worker_id, _iso(now), row["job_id"]),
            )
            connection.execute(
                "UPDATE runs SET budget_reserved = budget_reserved + ?, updated_at = ? WHERE run_id = ?",
                (float(row["estimated_cost"]), _iso(now), row["run_id"]),
            )
            self._insert_event(
                connection,
                run_id=row["run_id"],
                event_type="job.claimed",
                dedupe_key=f"job:{row['job_id']}:attempt:{attempt}:claimed",
                payload={"job_id": row["job_id"], "lease_id": lease_id, "worker_id": worker_id},
                created_at=now,
            )
            return self._lease_from_row(connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())

    def complete_job(
        self,
        lease_id: str,
        worker_id: str,
        outcome: JobOutcome,
        *,
        actual_cost: float | None = None,
        error: str | None = None,
    ) -> JobRecord:
        if actual_cost is not None and actual_cost < 0:
            raise ValueError("actual_cost must not be negative")
        normalized_error = error.strip() if error and error.strip() else None
        op_key = f"lease:{lease_id}:complete"

        with self._transaction() as connection:
            lease = connection.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
            if lease is None:
                raise RuntimeNotFoundError(f"lease not found: {lease_id}")
            if lease["worker_id"] != worker_id:
                raise LeaseOwnershipError(f"lease {lease_id} belongs to another worker")

            job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (lease["job_id"],)).fetchone()
            if job is None:
                raise RuntimeNotFoundError(f"job not found: {lease['job_id']}")

            charged = float(job["estimated_cost"]) if actual_cost is None else float(actual_cost)
            canonical_payload = {
                "lease_id": lease_id,
                "worker_id": worker_id,
                "outcome": outcome.value,
                "actual_cost": actual_cost,
                "charged_cost": charged,
                "error": normalized_error,
            }

            existing_op = self._get_operation(connection, op_key)
            if existing_op is not None:
                expected_canonical = _json(canonical_payload)
                if (
                    existing_op["operation_type"] != "job_completion"
                    or existing_op["subject_id"] != lease["job_id"]
                    or existing_op["payload_canonical"] != expected_canonical
                ):
                    raise RuntimeConflictError(
                        f"lease completion {lease_id} replay had conflicting parameters"
                    )
                saved_data = json.loads(existing_op["result_json"])
                return JobRecord.model_validate(saved_data)

            if lease["status"] == LeaseStatus.COMPLETED.value:
                if lease["outcome"] != outcome.value:
                    raise RuntimeConflictError("completed lease replay used a different outcome")
                job_record = self._job_from_row(job)
                self._record_operation(
                    connection,
                    operation_key=op_key,
                    operation_type="job_completion",
                    subject_id=lease["job_id"],
                    payload_canonical=canonical_payload,
                    result_json=job_record.model_dump_json(),
                    created_at=_parse_datetime(lease["completed_at"] or lease["expires_at"]),
                )
                return job_record

            if lease["status"] != LeaseStatus.ACTIVE.value:
                raise LeaseExpiredError(f"lease {lease_id} is {lease['status']}")
            now = self._clock()
            if _parse_datetime(lease["expires_at"]) <= now:
                self._reconcile_unlocked(connection, now)
                raise LeaseExpiredError(f"lease {lease_id} expired")
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (lease["run_id"],)).fetchone()
            if run is None:
                raise RuntimeNotFoundError(f"run not found: {lease['run_id']}")
            connection.execute(
                "UPDATE runs SET budget_reserved = MAX(0, budget_reserved - ?), budget_spent = budget_spent + ?, updated_at = ? WHERE run_id = ?",
                (float(job["estimated_cost"]), charged, _iso(now), lease["run_id"]),
            )
            next_status = JobStatus.SUCCEEDED
            next_ready_at = None
            next_error = normalized_error
            if outcome is JobOutcome.FAILED:
                if int(job["attempt"]) < int(job["max_attempts"]):
                    next_status = JobStatus.RETRY_SCHEDULED
                    next_ready_at = now + timedelta(seconds=self._retry_delay_seconds)
                else:
                    next_status = JobStatus.FAILED
            elif outcome is JobOutcome.CANCELLED:
                next_status = JobStatus.CANCELLED
            connection.execute(
                "UPDATE leases SET status = ?, completed_at = ?, outcome = ? WHERE lease_id = ?",
                (LeaseStatus.COMPLETED.value, _iso(now), outcome.value, lease_id),
            )
            connection.execute(
                """
                UPDATE jobs SET status = ?, ready_at = ?, lease_id = NULL, owner = NULL,
                    last_error = ?, updated_at = ? WHERE job_id = ?
                """,
                (next_status.value, _iso(next_ready_at) if next_ready_at else None, next_error, _iso(now), job["job_id"]),
            )
            self._insert_event(
                connection,
                run_id=job["run_id"],
                event_type=f"job.{outcome.value}",
                dedupe_key=f"job:{job['job_id']}:attempt:{job['attempt']}:{outcome.value}",
                payload={"job_id": job["job_id"], "lease_id": lease_id, "attempt": job["attempt"], "error": normalized_error},
                created_at=now,
            )
            if outcome is JobOutcome.SUCCEEDED and job["successor_event"]:
                self._insert_event(
                    connection,
                    run_id=job["run_id"],
                    event_type=job["successor_event"],
                    dedupe_key=f"job:{job['job_id']}:attempt:{job['attempt']}:successor",
                    payload={"job_id": job["job_id"], "run_id": job["run_id"]},
                    created_at=now,
                )
            completed_job = self._job_from_row(
                connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone()
            )
            self._record_operation(
                connection,
                operation_key=op_key,
                operation_type="job_completion",
                subject_id=job["job_id"],
                payload_canonical=canonical_payload,
                result_json=completed_job.model_dump_json(),
                created_at=now,
            )
            return completed_job

    def reconcile(self) -> ReconciliationReport:
        with self._transaction() as connection:
            return self._reconcile_unlocked(connection, self._clock())

    def cancel_run(self, run_id: str, *, reason: str = "owner requested cancellation") -> RunRecord:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise RuntimeNotFoundError(f"run not found: {run_id}")
            current = WorkflowState(row["state"])
            if current is WorkflowState.CANCELLED:
                return self._run_from_row(row)
            if current in _TERMINAL_RUN_STATES:
                return self._run_from_row(row)
            validate_transition(current, WorkflowState.CANCELLED)
            now = self._clock()
            active_jobs = connection.execute(
                "SELECT job_id, estimated_cost FROM jobs WHERE run_id = ? AND status = ?",
                (run_id, JobStatus.CLAIMED.value),
            ).fetchall()
            for job in active_jobs:
                connection.execute(
                    "UPDATE leases SET status = ?, completed_at = ? WHERE job_id = ? AND status = ?",
                    (LeaseStatus.CANCELLED.value, _iso(now), job["job_id"], LeaseStatus.ACTIVE.value),
                )
            connection.execute(
                "UPDATE jobs SET status = ?, lease_id = NULL, owner = NULL, last_error = ?, updated_at = ? WHERE run_id = ? AND status NOT IN (?, ?, ?)",
                (
                    JobStatus.CANCELLED.value,
                    reason,
                    _iso(now),
                    run_id,
                    JobStatus.SUCCEEDED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                ),
            )
            connection.execute(
                "UPDATE runs SET state = ?, budget_reserved = 0, updated_at = ? WHERE run_id = ?",
                (WorkflowState.CANCELLED.value, _iso(now), run_id),
            )
            self._insert_event(
                connection,
                run_id=run_id,
                event_type="run.cancelled",
                dedupe_key=f"run:{run_id}:cancelled",
                payload={"reason": reason},
                created_at=now,
            )
            return self._run_from_row(connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def pending_events(self, *, limit: int = 100) -> list[OutboxEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM outbox WHERE status = ? ORDER BY created_at, event_id LIMIT ?",
                (EventStatus.PENDING.value, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def ack_event(self, event_id: str) -> OutboxEvent:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM outbox WHERE event_id = ?", (event_id,)).fetchone()
            if row is None:
                raise RuntimeNotFoundError(f"event not found: {event_id}")
            if row["status"] == EventStatus.PENDING.value:
                connection.execute(
                    "UPDATE outbox SET status = ?, acked_at = ? WHERE event_id = ?",
                    (EventStatus.ACKED.value, _iso(self._clock()), event_id),
                )
            return self._event_from_row(connection.execute("SELECT * FROM outbox WHERE event_id = ?", (event_id,)).fetchone())

    def _eligible_jobs(self, connection: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) AND (ready_at IS NULL OR ready_at <= ?) ORDER BY created_at, job_id",
            (JobStatus.READY.value, JobStatus.WAITING_BUDGET.value, _iso(now)),
        ).fetchall()
        return list(rows)

    def _dependencies_succeeded(self, connection: sqlite3.Connection, dependency_ids: list[str]) -> bool:
        for dependency_id in dependency_ids:
            row = connection.execute("SELECT status FROM jobs WHERE job_id = ?", (dependency_id,)).fetchone()
            if row is None or row["status"] != JobStatus.SUCCEEDED.value:
                return False
        return True

    def _active_by_project(self, connection: sqlite3.Connection) -> dict[str, int]:
        rows = connection.execute(
            "SELECT project_id, COUNT(*) AS count FROM jobs WHERE status = ? GROUP BY project_id",
            (JobStatus.CLAIMED.value,),
        ).fetchall()
        return {row["project_id"]: int(row["count"]) for row in rows}

    def _active_by_stage(self, connection: sqlite3.Connection) -> dict[str, int]:
        rows = connection.execute(
            "SELECT stage, COUNT(*) AS count FROM jobs WHERE status = ? GROUP BY stage",
            (JobStatus.CLAIMED.value,),
        ).fetchall()
        return {row["stage"]: int(row["count"]) for row in rows}

    def _active_conflicts(self, connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute(
            "SELECT conflict_keys FROM jobs WHERE status = ?",
            (JobStatus.CLAIMED.value,),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            keys.update(json.loads(row["conflict_keys"]))
        return keys

    def _reconcile_unlocked(self, connection: sqlite3.Connection, now: datetime) -> ReconciliationReport:
        expired = connection.execute(
            "SELECT * FROM leases WHERE status = ? AND expires_at <= ? ORDER BY expires_at, lease_id",
            (LeaseStatus.ACTIVE.value, _iso(now)),
        ).fetchall()
        retry_scheduled = 0
        failed_jobs = 0
        for lease in expired:
            job = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (lease["job_id"],)).fetchone()
            if job is None:
                continue
            connection.execute(
                "UPDATE leases SET status = ?, completed_at = ? WHERE lease_id = ?",
                (LeaseStatus.EXPIRED.value, _iso(now), lease["lease_id"]),
            )
            connection.execute(
                "UPDATE runs SET budget_reserved = MAX(0, budget_reserved - ?), updated_at = ? WHERE run_id = ?",
                (float(job["estimated_cost"]), _iso(now), lease["run_id"]),
            )
            if int(job["attempt"]) < int(job["max_attempts"]):
                retry_scheduled += 1
                ready_at = now + timedelta(seconds=self._retry_delay_seconds)
                status = JobStatus.RETRY_SCHEDULED
                error = "lease expired; retry scheduled"
                event_type = "job.retry_scheduled"
            else:
                failed_jobs += 1
                ready_at = None
                status = JobStatus.FAILED
                error = "lease expired after maximum attempts"
                event_type = "job.failed"
            connection.execute(
                "UPDATE jobs SET status = ?, ready_at = ?, lease_id = NULL, owner = NULL, last_error = ?, updated_at = ? WHERE job_id = ?",
                (status.value, _iso(ready_at) if ready_at else None, error, _iso(now), job["job_id"]),
            )
            self._insert_event(
                connection,
                run_id=job["run_id"],
                event_type=event_type,
                dedupe_key=f"lease:{lease['lease_id']}:expired",
                payload={"job_id": job["job_id"], "lease_id": lease["lease_id"], "attempt": job["attempt"]},
                created_at=now,
            )
        due = connection.execute(
            "SELECT job_id FROM jobs WHERE status = ? AND ready_at <= ?",
            (JobStatus.RETRY_SCHEDULED.value, _iso(now)),
        ).fetchall()
        for row in due:
            self._set_job_status(connection, row["job_id"], JobStatus.READY, now, None)
        return ReconciliationReport(
            expired_leases=len(expired),
            retry_scheduled=retry_scheduled,
            failed_jobs=failed_jobs,
            requeued_jobs=len(due),
        )

    def _set_job_status(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        status: JobStatus,
        now: datetime,
        error: str | None,
    ) -> None:
        connection.execute(
            "UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE job_id = ?",
            (status.value, error, _iso(now), job_id),
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        dedupe_key: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> str:
        encoded = _json(dict(payload))
        existing = connection.execute("SELECT * FROM outbox WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
        if existing is not None:
            if existing["run_id"] != run_id or existing["event_type"] != event_type or existing["payload"] != encoded:
                raise RuntimeConflictError(f"event dedupe key was reused: {dedupe_key}")
            return str(existing["event_id"])
        event_id = f"evt_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO outbox (
                event_id, run_id, event_type, dedupe_key, payload, status, created_at, acked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (event_id, run_id, event_type, dedupe_key, encoded, EventStatus.PENDING.value, _iso(created_at)),
        )
        return event_id

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        state = WorkflowState(row["state"])
        if state is WorkflowState.DELIVERED:
            _raise_local_delivery_forbidden()
        return RunRecord(
            run_id=row["run_id"],
            project_id=row["project_id"],
            state=state,
            budget_ceiling=row["budget_ceiling"],
            budget_reserved=row["budget_reserved"],
            budget_spent=row["budget_spent"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            run_id=row["run_id"],
            project_id=row["project_id"],
            stage=row["stage"],
            priority=row["priority"],
            depends_on=json.loads(row["depends_on"]),
            conflict_keys=json.loads(row["conflict_keys"]),
            estimated_cost=row["estimated_cost"],
            max_attempts=row["max_attempts"],
            ready_at=_parse_datetime(row["ready_at"]) if row["ready_at"] else None,
            successor_event=row["successor_event"],
            dedupe_key=row["dedupe_key"],
            status=JobStatus(row["status"]),
            attempt=row["attempt"],
            lease_id=row["lease_id"],
            owner=row["owner"],
            last_error=row["last_error"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> LeaseRecord:
        return LeaseRecord(
            lease_id=row["lease_id"],
            job_id=row["job_id"],
            run_id=row["run_id"],
            worker_id=row["worker_id"],
            stage=row["stage"],
            attempt=row["attempt"],
            status=LeaseStatus(row["status"]),
            acquired_at=_parse_datetime(row["acquired_at"]),
            expires_at=_parse_datetime(row["expires_at"]),
            completed_at=_parse_datetime(row["completed_at"]) if row["completed_at"] else None,
            outcome=JobOutcome(row["outcome"]) if row["outcome"] else None,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_type=row["event_type"],
            dedupe_key=row["dedupe_key"],
            payload=json.loads(row["payload"]),
            status=EventStatus(row["status"]),
            created_at=_parse_datetime(row["created_at"]),
            acked_at=_parse_datetime(row["acked_at"]) if row["acked_at"] else None,
        )


__all__ = [
    "EventStatus",
    "JobOutcome",
    "JobRecord",
    "JobSpec",
    "JobStatus",
    "LeaseExpiredError",
    "LeaseOwnershipError",
    "LeaseRecord",
    "LeaseStatus",
    "OperationRecord",
    "OutboxEvent",
    "ReconciliationReport",
    "RunRecord",
    "RuntimeConflictError",
    "RuntimeNotFoundError",
    "WorkflowRuntime",
]
