"""Canonical persistence protocol and SQLite implementation for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md, HF-05-02 binding, and control.json.
Governed by ADR-HF-001.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.workflow.control_contracts import (
    Claim,
    ExternalOperation,
    FleetCapacityExceededError,
    FleetReservation,
    HostBusyError,
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
    InvalidResultError,
    JobKey,
    OutboxEvent,
    OutboxNotFoundError,
    ReconcilePage,
    ReservationConflictError,
    RuntimeOwner,
    StageResult,
    StaleLeaseError,
    TicketAlreadyActiveError,
    RESERVATION_PENDING_GRACE_SEC,
    fleet_managed_enabled,
    normalize_cause_code,
)


# Job statuses that keep a run open. `waiting_human` / `waiting_dependency` are
# parked-but-resumable (the owner still has to answer), so a run whose only
# non-terminal job is parked must NOT be closed as `completed`. `retry` and
# `replan` are deliberately absent: they are superseded markers (the retry /
# planning successor job is materialized as a new row). Terminal: succeeded,
# failed, cancelled.
RUN_OPEN_JOB_STATUSES: tuple[str, ...] = ("pending", "running", "waiting_human", "waiting_dependency")
_RUN_OPEN_JOB_STATUSES_SQL = ", ".join(f"'{s}'" for s in RUN_OPEN_JOB_STATUSES)


def _is_ready_enough(created_at_raw: Any, now: datetime, ready_age_sec: float) -> bool:
    """True if `created_at_raw` (an isoformat timestamp) is at least `ready_age_sec` old.

    Any parse failure is treated as "ready" so a malformed/legacy timestamp
    never blocks a claim outright (HF-27-09 filter is a soft priority hint,
    not a correctness gate).
    """
    try:
        created_at = datetime.fromisoformat(str(created_at_raw))
    except (TypeError, ValueError):
        return True
    if created_at.tzinfo is None and now.tzinfo is not None:
        created_at = created_at.replace(tzinfo=now.tzinfo)
    elif created_at.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=created_at.tzinfo)
    return (now - created_at).total_seconds() >= ready_age_sec


class ControlStore(Protocol):
    """Canonical transactional control store interface abstracting storage backends."""

    def get_run_identity(self, run_id: str) -> dict[str, str] | None:
        """Read the persisted project independently of job telemetry; storage errors must raise."""
        ...

    def accept(self, command: IntakeCommand, now: datetime) -> IntakeReceipt:
        """Accept an intake command transactionally with idempotency checking."""
        ...

    def claim(
        self, worker: str, capabilities: list[str], now: datetime, ready_age_sec: float = 0.0
    ) -> Claim | None:
        """Atomically query and lock the next pending job matching worker capabilities.

        `ready_age_sec` (HF-27-09, default 0.0 preserves prior behaviour) skips
        jobs created less than that many seconds ago, so a lower-priority
        worker only claims what a higher-priority one left behind.
        """
        ...

    def heartbeat(self, claim: Claim, now: datetime) -> Claim:
        """Extend claim expiration to now + lease_duration_sec."""
        ...

    def finish(self, claim: Claim, result: StageResult, now: datetime) -> None:
        """Conclude active job execution in a single atomic transaction."""
        ...

    def materialize(self, event: dict[str, Any], now: datetime) -> None:
        """Confirm processing and dispatch of an outbox event."""
        ...

    def reconcile(self, now: datetime, cursor: str | None = None, limit: int = 100) -> ReconcilePage:
        """Sweep expired leases and re-enqueue orphaned jobs."""
        ...

    def get_operation(self, key: str) -> ExternalOperation | None:
        """Retrieve recorded external operation by idempotency key."""
        ...

    def record_operation(self, operation: ExternalOperation, claim: Claim) -> None:
        """Atomically record or update an external operation within active lease."""
        ...

    def emit_outbox(self, event: OutboxEvent, now: datetime) -> OutboxEvent:
        """Atomically emit an event to the outbox queue."""
        ...

    def list_active_projects(self, cursor: str | None = None, limit: int = 100) -> tuple[list[str], str | None]:
        """Stable paginated query of active project IDs ordered deterministically."""
        ...

    def get_ready_age_metrics(self, now: datetime) -> dict[str, Any]:
        """Query ready-age and claim distribution for pending jobs."""
        ...

    def get_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Query pending outbox events for publication/materialization."""
        ...

    def list_waiting_jobs(
        self, statuses: tuple[str, ...] = ("waiting_human", "waiting_dependency")
    ) -> list[JobKey]:
        """List active jobs parked in waiting states."""
        ...

    def list_active_run_ids(self) -> list[str]:
        """Ids of every run whose `runs.status` is `active`, oldest first (USR-142 orphan-run sweep)."""
        ...

    def list_eligible_admission_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pending jobs of active runs eligible for fleet cycle admission (USR-282)."""
        ...

    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str = "owner_cancelled",
        actor: str = "owner",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cancel an in-flight run and all its open jobs with audit logging (USR-123)."""
        ...

    def reserve_cycle(
        self,
        project_id: str,
        ticket_id: str,
        run_id: str,
        host_id: str,
        now: datetime,
        lease_duration_sec: int = 45,
        max_fleet_slots: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> FleetReservation:
        """Atomically reserve a demand cycle on a specific fleet host (USR-281)."""
        ...

    def heartbeat_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration_sec: int | None = None,
    ) -> FleetReservation:
        """Extend active cycle reservation lease (USR-281)."""
        ...

    def release_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        status: str = "released",
        now: datetime | None = None,
    ) -> FleetReservation:
        """Release active cycle reservation (USR-281)."""
        ...

    def get_reservation(self, reservation_id: str) -> FleetReservation | None:
        """Retrieve reservation by ID (USR-281)."""
        ...

    def get_active_reservation_for_ticket(
        self, project_id: str, ticket_id: str
    ) -> FleetReservation | None:
        """Find active reservation for a project ticket (USR-281)."""
        ...

    def get_active_reservation_for_host(self, host_id: str) -> FleetReservation | None:
        """Find active reservation currently occupying a host (USR-281)."""
        ...

    def get_active_reservation_for_run(self, run_id: str) -> FleetReservation | None:
        """Find active reservation for a run (USR-282)."""
        ...

    def list_active_reservations(self) -> list[FleetReservation]:
        """List all active fleet reservations (USR-281)."""
        ...

    def drain_host(self, host_id: str, reason: str, now: datetime) -> int:
        """Mark host and any active reservations for drain (USR-281)."""
        ...

    def reconcile_fleet_reservations(self, now: datetime) -> list[FleetReservation]:
        """Sweep expired fleet reservations and release slots (USR-281)."""
        ...


class SQLiteControlStore:
    """Canonical SQLite implementation of ControlStore for local-first execution.

    Features:
    - Transactional WAL mode (PRAGMA journal_mode=WAL)
    - Short ACID transactions with BEGIN IMMEDIATE
    - Monotonic fencing tokens per job
    - Lease timeout enforcement (45s default)
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        runtime_owner: str = RuntimeOwner.HF05_SQLITE.value,
        lease_duration_sec: int = 45,
    ) -> None:
        self.runtime_owner = runtime_owner
        self.lease_duration_sec = lease_duration_sec
        self._raw_path = str(db_path)
        self._is_memory = self._raw_path == ":memory:"

        if self._is_memory:
            # Use unique shared-memory URI with a persistent connection so tables persist
            self._mem_id = f"mem_hf05_{uuid4().hex}"
            self._conn_string = f"file:{self._mem_id}?mode=memory&cache=shared"
            self._is_uri = True
            self._keepalive_conn = sqlite3.connect(self._conn_string, uri=True)
        else:
            path_obj = Path(self._raw_path).resolve()
            path_obj.parent.mkdir(parents=True, exist_ok=True)
            self._conn_string = str(path_obj)
            self._is_uri = False
            self._keepalive_conn = None

        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create a configured connection with WAL and busy timeout."""
        conn = sqlite3.connect(self._conn_string, timeout=5.0, uri=self._is_uri)
        conn.row_factory = sqlite3.Row
        if not self._is_memory:
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        """Initialize the canonical schema."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    demand_id TEXT NOT NULL,
                    demand_version TEXT NOT NULL,
                    runtime_owner TEXT NOT NULL CHECK (runtime_owner IN ('hf05_sqlite', 'df11_legacy', 'cloud_dbos_postgres')),
                    mode TEXT NOT NULL CHECK (mode IN ('autonomous', 'documentary')),
                    status TEXT NOT NULL DEFAULT 'active',
                    plan_digest TEXT NOT NULL,
                    config_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    CONSTRAINT uq_runs_project_demand UNIQUE (project_id, demand_id, demand_version)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);

                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
                    ticket_id TEXT NOT NULL,
                    plan_version TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    iteration INTEGER NOT NULL CHECK (iteration >= 0),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (
                        status IN ('pending', 'running', 'succeeded', 'retry', 'replan', 'waiting_dependency', 'waiting_human', 'cancelled', 'failed')
                    ),
                    role TEXT NOT NULL,
                    required_capabilities TEXT NOT NULL DEFAULT '[]',
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    current_lease_id TEXT,
                    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    cause_code TEXT,
                    actual_cost REAL NOT NULL DEFAULT 0.0,
                    output_refs TEXT NOT NULL DEFAULT '[]',
                    evidence_refs TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    not_before TEXT,
                    ready_at TEXT,
                    PRIMARY KEY (run_id, ticket_id, plan_version, stage, iteration)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_claim_lookup ON jobs (status, role);
                CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs (run_id);

                CREATE TABLE IF NOT EXISTS claims (
                    lease_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    plan_version TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    owner TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    reservation_id TEXT NOT NULL,
                    route_ref TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'stolen')),
                    FOREIGN KEY (run_id, ticket_id, plan_version, stage, iteration)
                        REFERENCES jobs(run_id, ticket_id, plan_version, stage, iteration) ON DELETE RESTRICT,
                    CONSTRAINT uq_claims_job_fencing UNIQUE (run_id, ticket_id, plan_version, stage, iteration, fencing_token)
                );

                CREATE INDEX IF NOT EXISTS idx_claims_expiry_sweep ON claims (expires_at, status);

                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    target_system TEXT NOT NULL DEFAULT 'cloud_dbos',
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published', 'failed', 'dead_letter')),
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    error_message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_pending_dispatch ON outbox (status, created_at);

                CREATE TABLE IF NOT EXISTS external_operations (
                    operation_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    external_id TEXT,
                    status TEXT NOT NULL CHECK (status IN ('prepared', 'sent', 'unknown', 'succeeded', 'failed')),
                    claim_lease_id TEXT,
                    fencing_token INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    response_digest TEXT,
                    error_details TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_ext_ops_provider_status ON external_operations (provider, status);

                CREATE TABLE IF NOT EXISTS reconciliation_ledger (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recon_cycle_time ON reconciliation_ledger (cycle_id, recorded_at);

                CREATE TABLE IF NOT EXISTS intake_commands (
                    channel TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    policy_ref TEXT NOT NULL,
                    demand_id TEXT NOT NULL,
                    demand_version TEXT NOT NULL,
                    run_id TEXT,
                    initial_job_id TEXT,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (channel, external_id)
                );

                CREATE TABLE IF NOT EXISTS grill_answers (
                    run_id TEXT NOT NULL,
                    question_id TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, question_id)
                );

                CREATE TABLE IF NOT EXISTS fleet_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    host_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'stolen', 'draining', 'completed', 'failed', 'cancelled')),
                    fencing_token INTEGER NOT NULL DEFAULT 1,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_fleet_reservations_active_ticket 
                    ON fleet_reservations (project_id, ticket_id) 
                    WHERE status = 'active';

                CREATE UNIQUE INDEX IF NOT EXISTS uq_fleet_reservations_active_host 
                    ON fleet_reservations (host_id) 
                    WHERE status = 'active';

                CREATE INDEX IF NOT EXISTS idx_fleet_reservations_run_id 
                    ON fleet_reservations (run_id);

                CREATE INDEX IF NOT EXISTS idx_fleet_reservations_expiry 
                    ON fleet_reservations (expires_at, status);
                """
            )
            self._migrate_jobs_columns(conn)

    def _migrate_jobs_columns(self, conn: sqlite3.Connection) -> None:
        """Idempotent ALTERs for columns added after the initial CREATE TABLE (HF-27-08).

        `CREATE TABLE IF NOT EXISTS` never retrofits an existing table, so a
        DB created before `not_before`/`ready_at` existed needs an explicit
        `ALTER TABLE`. Each is wrapped so an already-migrated DB (or a
        brand-new one where the CREATE above already included the column)
        is a silent no-op.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for column in ("not_before", "ready_at"):
            if column not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")

    def accept(self, command: IntakeCommand, now: datetime) -> IntakeReceipt:
        """Accept an intake command transactionally with idempotency checking."""
        now_iso = now.isoformat()
        digest = command.payload_digest

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                "SELECT payload_digest, demand_id, demand_version, run_id, initial_job_id, mode, committed_at "
                "FROM intake_commands WHERE channel = ? AND external_id = ?",
                (command.channel, command.external_id),
            )
            existing = cur.fetchone()
            if existing:
                if existing["payload_digest"] != digest:
                    conn.rollback()
                    raise IdempotencyConflict(
                        f"Command with channel='{command.channel}' and external_id='{command.external_id}' "
                        f"already accepted with different payload digest."
                    )
                conn.commit()
                return IntakeReceipt(
                    demand_id=existing["demand_id"],
                    demand_version=existing["demand_version"],
                    run_id=existing["run_id"] if existing["mode"] == "autonomous" else None,
                    initial_job_id=existing["initial_job_id"] if existing["mode"] == "autonomous" else None,
                    mode=existing["mode"],
                    committed_at=existing["committed_at"],
                )

            # New intake
            demand_id = f"dem-{uuid4().hex[:12]}"
            demand_version = "1.0"

            if command.mode == "autonomous":
                run_id = f"run-{uuid4().hex[:12]}"
                initial_job_id = f"job-{uuid4().hex[:12]}"

                cur.execute(
                    """
                    INSERT INTO runs (
                        run_id, project_id, demand_id, demand_version, runtime_owner,
                        mode, status, plan_digest, config_version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, '1.0', ?, ?)
                    """,
                    (
                        run_id,
                        command.project_id,
                        demand_id,
                        demand_version,
                        self.runtime_owner,
                        command.mode,
                        digest,
                        now_iso,
                        now_iso,
                    ),
                )

                # HF-27-08 review item 9: attempted (see git history) to
                # stamp the initial grill job's required_capabilities from
                # the registered project, mirroring every later line stage.
                # REVERTED: ControlStore.accept() is the shared HF-05 intake
                # entrypoint, used by many non-line callers/tests with
                # project_id="darkfac" as a convenient default string, and
                # the *real* registered "darkfac" project (.factory/projects.json)
                # legitimately has its own repo_url/deploy/smoke config --
                # the factory manages itself through the line. There is no
                # reliable signal at accept() time to tell "a genuine line
                # demand for a line-capable project" apart from "a generic
                # HF-05 test/caller that happens to reuse project_id=darkfac
                # with no intention of the line's capability gating"; gating
                # unconditionally broke 25 pre-existing, unrelated tests
                # across test_control_store.py, test_public_autonomous_intake.py,
                # etc. Left as a documented gap for a future ticket to close
                # properly (e.g. an explicit is_line flag on IntakeCommand).
                ticket_id = str(command.payload.get("ticket_id") or command.project_id)
                cur.execute(
                    """
                    INSERT INTO jobs (
                        run_id, ticket_id, plan_version, stage, iteration, status,
                        role, required_capabilities, fencing_token, timeout_seconds,
                        retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                        created_at, updated_at, ready_at
                    ) VALUES (?, ?, '1.0', 'grill', 0, 'pending', 'grill_engine', '[]', 0, 1800, 0, 3, 0.0, '[]', '[]', ?, ?, ?)
                    """,
                    (
                        run_id,
                        ticket_id,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )

                outbox_payload = json.dumps(
                    {
                        "run_id": run_id,
                        "project_id": command.project_id,
                        "demand_id": demand_id,
                        "stage": "grill",
                    }
                )
                cur.execute(
                    """
                    INSERT INTO outbox (
                        event_type, aggregate_type, aggregate_id, payload,
                        target_system, status, retry_count, created_at
                    ) VALUES ('intake_accepted', 'run', ?, ?, 'cloud_dbos', 'pending', 0, ?)
                    """,
                    (run_id, outbox_payload, now_iso),
                )
            else:
                # Documentary mode
                run_id = None
                initial_job_id = None
                cur.execute(
                    """
                    INSERT INTO runs (
                        run_id, project_id, demand_id, demand_version, runtime_owner,
                        mode, status, plan_digest, config_version, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, '1.0', ?, ?, ?)
                    """,
                    (
                        f"doc-{demand_id}",
                        command.project_id,
                        demand_id,
                        demand_version,
                        self.runtime_owner,
                        command.mode,
                        digest,
                        now_iso,
                        now_iso,
                        now_iso,
                    ),
                )

            cur.execute(
                """
                INSERT INTO intake_commands (
                    channel, external_id, project_id, payload_digest, payload,
                    mode, policy_ref, demand_id, demand_version, run_id,
                    initial_job_id, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.channel,
                    command.external_id,
                    command.project_id,
                    digest,
                    json.dumps(command.payload),
                    command.mode,
                    command.policy_ref,
                    demand_id,
                    demand_version,
                    run_id,
                    initial_job_id,
                    now_iso,
                ),
            )
            conn.commit()
            return IntakeReceipt(
                demand_id=demand_id,
                demand_version=demand_version,
                run_id=run_id,
                initial_job_id=initial_job_id,
                mode=command.mode,
                committed_at=now_iso,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim(
        self, worker: str, capabilities: list[str], now: datetime, ready_age_sec: float = 0.0
    ) -> Claim | None:
        """Atomically query and lock the next pending job matching worker capabilities.

        `ready_age_sec` (HF-27-09, default 0.0 preserves prior behaviour) skips
        jobs created less than that many seconds ago.
        """
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=self.lease_duration_sec)).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT run_id, ticket_id, plan_version, stage, iteration, fencing_token, role,
                       required_capabilities, created_at, not_before, ready_at
                FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            )
            rows = cur.fetchall()
            chosen_row = None
            for row in rows:
                req = json.loads(row["required_capabilities"])
                if not all(c in capabilities for c in req):
                    continue
                not_before = row["not_before"]
                if not_before:
                    try:
                        not_before_dt = datetime.fromisoformat(str(not_before))
                    except ValueError:
                        not_before_dt = None
                    if not_before_dt is not None:
                        compare_now = now
                        if not_before_dt.tzinfo is None and compare_now.tzinfo is not None:
                            not_before_dt = not_before_dt.replace(tzinfo=compare_now.tzinfo)
                        elif not_before_dt.tzinfo is not None and compare_now.tzinfo is None:
                            compare_now = compare_now.replace(tzinfo=not_before_dt.tzinfo)
                        if not_before_dt > compare_now:
                            continue
                # Subordination of stage claims to fleet cycle reservation (USR-281/USR-282)
                cur.execute(
                    """
                    SELECT reservation_id, host_id, status, expires_at
                    FROM fleet_reservations
                    WHERE run_id = ? AND status IN ('active', 'draining')
                    """,
                    (row["run_id"],),
                )
                res_row = cur.fetchone()
                if res_row:
                    res_expires = datetime.fromisoformat(str(res_row["expires_at"]))
                    if res_expires.tzinfo is None and now.tzinfo is not None:
                        res_expires = res_expires.replace(tzinfo=now.tzinfo)
                    elif res_expires.tzinfo is not None and now.tzinfo is None:
                        now_comp = now.replace(tzinfo=res_expires.tzinfo)
                    else:
                        now_comp = now
                    if res_expires < now_comp:
                        continue  # Expired cycle reservation
                    res_host = str(res_row["host_id"]).lower()
                    worker_lower = worker.lower()
                    if res_host not in worker_lower and worker_lower not in res_host:
                        continue  # Reserved for a different host machine
                    matched_reservation_id = res_row["reservation_id"]
                    # Host affinity confirmed: executes immediately without fixed priority delay
                else:
                    if fleet_managed_enabled():
                        continue  # Managed mode: no legacy path without an admitted reservation (USR-306)
                    matched_reservation_id = None
                    if ready_age_sec > 0.0:
                        ready_reference = row["ready_at"] or row["created_at"]
                        if not _is_ready_enough(ready_reference, now, ready_age_sec):
                            continue

                chosen_row = row
                break

            if not chosen_row:
                conn.commit()
                return None

            run_id = chosen_row["run_id"]
            ticket_id = chosen_row["ticket_id"]
            plan_version = chosen_row["plan_version"]
            stage = chosen_row["stage"]
            iteration = chosen_row["iteration"]
            old_fencing_token = chosen_row["fencing_token"]
            new_fencing_token = old_fencing_token + 1
            lease_id = f"lease-{uuid4().hex[:12]}"
            reservation_id = matched_reservation_id or f"res-{uuid4().hex[:8]}"
            route_ref = "default"

            cur.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    fencing_token = ?,
                    current_lease_id = ?,
                    started_at = ?,
                    updated_at = ?
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                  AND stage = ? AND iteration = ? AND fencing_token = ?
                """,
                (
                    new_fencing_token,
                    lease_id,
                    now_iso,
                    now_iso,
                    run_id,
                    ticket_id,
                    plan_version,
                    stage,
                    iteration,
                    old_fencing_token,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None

            cur.execute(
                """
                INSERT INTO claims (
                    lease_id, run_id, ticket_id, plan_version, stage, iteration,
                    owner, fencing_token, reservation_id, route_ref, acquired_at, expires_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (
                    lease_id,
                    run_id,
                    ticket_id,
                    plan_version,
                    stage,
                    iteration,
                    worker,
                    new_fencing_token,
                    reservation_id,
                    route_ref,
                    now_iso,
                    expires_at,
                ),
            )
            if matched_reservation_id:
                # USR-306: the claim itself proves the host is alive; renew the cycle reservation right away so a
                # short stage (< heartbeat interval) cannot leave it un-renewed until it expires.
                cur.execute(
                    "UPDATE fleet_reservations SET heartbeat_at = ?, expires_at = ? "
                    "WHERE reservation_id = ? AND status = 'active'",
                    (now_iso, expires_at, matched_reservation_id),
                )
            conn.commit()

            return Claim(
                job_key=JobKey(
                    run_id=run_id,
                    ticket_id=ticket_id,
                    plan_version=plan_version,
                    stage=stage,
                    iteration=iteration,
                ),
                lease_id=lease_id,
                owner=worker,
                fencing_token=new_fencing_token,
                acquired_at=now_iso,
                expires_at=expires_at,
                reservation_id=reservation_id,
                route_ref=route_ref,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat(self, claim: Claim, now: datetime) -> Claim:
        """Extend claim expiration to now + lease_duration_sec."""
        now_iso = now.isoformat()
        new_expires_at = (now + timedelta(seconds=self.lease_duration_sec)).isoformat()
        jk = claim.job_key

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Verify active claim
            cur.execute(
                "SELECT status, expires_at, fencing_token FROM claims WHERE lease_id = ?",
                (claim.lease_id,),
            )
            claim_row = cur.fetchone()
            if not claim_row or claim_row["status"] != "active":
                conn.rollback()
                raise StaleLeaseError(f"Lease '{claim.lease_id}' is not active in store.")

            if datetime.fromisoformat(claim_row["expires_at"]) < now:
                conn.rollback()
                raise StaleLeaseError(f"Lease '{claim.lease_id}' expired at {claim_row['expires_at']}.")

            # Verify current job fencing
            cur.execute(
                """
                SELECT fencing_token, current_lease_id
                FROM jobs
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                """,
                (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
            )
            job_row = cur.fetchone()
            if not job_row:
                conn.rollback()
                raise StaleLeaseError(f"Job '{jk.canonical_key()}' not found.")

            if (
                job_row["fencing_token"] != claim.fencing_token
                or job_row["current_lease_id"] != claim.lease_id
            ):
                conn.rollback()
                raise StaleLeaseError(
                    f"Fencing token mismatch for job: job={job_row['fencing_token']}, claim={claim.fencing_token}"
                )

            # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
            if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                cur.execute(
                    "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = ?",
                    (claim.reservation_id,),
                )
                res_row = cur.fetchone()
                if res_row:
                    res_expires = datetime.fromisoformat(str(res_row["expires_at"]))
                    if res_expires.tzinfo is None and now.tzinfo is not None:
                        res_expires = res_expires.replace(tzinfo=now.tzinfo)
                    elif res_expires.tzinfo is not None and now.tzinfo is None:
                        now_comp = now.replace(tzinfo=res_expires.tzinfo)
                    else:
                        now_comp = now
                    if res_row["status"] not in ("active", "draining") or res_expires < now_comp:
                        conn.rollback()
                        raise StaleLeaseError(f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({res_row['status']}).")

            # Update expiration
            cur.execute(
                "UPDATE claims SET expires_at = ? WHERE lease_id = ? AND fencing_token = ? AND status = 'active'",
                (new_expires_at, claim.lease_id, claim.fencing_token),
            )
            cur.execute(
                """
                UPDATE jobs SET updated_at = ?
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ? AND fencing_token = ?
                """,
                (now_iso, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
            )
            conn.commit()

            return Claim(
                job_key=claim.job_key,
                lease_id=claim.lease_id,
                owner=claim.owner,
                fencing_token=claim.fencing_token,
                acquired_at=claim.acquired_at,
                expires_at=new_expires_at,
                reservation_id=claim.reservation_id,
                route_ref=claim.route_ref,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish(self, claim: Claim, result: StageResult, now: datetime) -> None:
        """Conclude active job execution in a single atomic transaction."""
        now_iso = now.isoformat()
        jk = claim.job_key

        if result.outcome == "success" and not result.output_refs:
            raise InvalidResultError("outcome='success' requires non-empty output_refs")

        # jobs.cause_code is VARCHAR(64) on Postgres: normalize (full text is logged).
        result = result.model_copy(update={"cause_code": normalize_cause_code(result.cause_code)})

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Verify claim
            cur.execute(
                "SELECT status, expires_at FROM claims WHERE lease_id = ?",
                (claim.lease_id,),
            )
            claim_row = cur.fetchone()
            if not claim_row or claim_row["status"] != "active":
                conn.rollback()
                raise StaleLeaseError(f"Lease '{claim.lease_id}' is not active.")

            if datetime.fromisoformat(claim_row["expires_at"]) < now:
                conn.rollback()
                raise StaleLeaseError(f"Lease '{claim.lease_id}' has expired at {claim_row['expires_at']}.")

            # Verify job fencing
            cur.execute(
                """
                SELECT fencing_token, current_lease_id, status, retry_count, max_retries
                FROM jobs
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                """,
                (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
            )
            job_row = cur.fetchone()
            if not job_row:
                conn.rollback()
                raise StaleLeaseError("Job not found.")

            if (
                job_row["fencing_token"] != claim.fencing_token
                or job_row["current_lease_id"] != claim.lease_id
            ):
                conn.rollback()
                raise StaleLeaseError(
                    f"Fencing token mismatch on finish: job={job_row['fencing_token']}, claim={claim.fencing_token}"
                )

            # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
            if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                cur.execute(
                    "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = ?",
                    (claim.reservation_id,),
                )
                res_row = cur.fetchone()
                if res_row:
                    res_expires = datetime.fromisoformat(str(res_row["expires_at"]))
                    if res_expires.tzinfo is None and now.tzinfo is not None:
                        res_expires = res_expires.replace(tzinfo=now.tzinfo)
                    elif res_expires.tzinfo is not None and now.tzinfo is None:
                        now_comp = now.replace(tzinfo=res_expires.tzinfo)
                    else:
                        now_comp = now
                    if res_row["status"] not in ("active", "draining") or res_expires < now_comp:
                        conn.rollback()
                        raise StaleLeaseError(
                            f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({res_row['status']}). Old owner cannot publish."
                        )

            # Apply state update
            if result.outcome == "success":
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'succeeded',
                        cause_code = ?,
                        actual_cost = actual_cost + ?,
                        output_refs = ?,
                        evidence_refs = ?,
                        finished_at = ?,
                        updated_at = ?,
                        current_lease_id = NULL
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                      AND stage = ? AND iteration = ? AND fencing_token = ?
                    """,
                    (
                        result.cause_code,
                        result.actual_cost,
                        json.dumps(result.output_refs),
                        json.dumps(result.evidence_refs),
                        now_iso,
                        now_iso,
                        jk.run_id,
                        jk.ticket_id,
                        jk.plan_version,
                        jk.stage,
                        jk.iteration,
                        claim.fencing_token,
                    ),
                )
                cur.execute(
                    "UPDATE claims SET status = 'released' WHERE lease_id = ?",
                    (claim.lease_id,),
                )

                # Outbox event
                outbox_payload = json.dumps(
                    {
                        "run_id": jk.run_id,
                        "ticket_id": jk.ticket_id,
                        "stage": jk.stage,
                        "iteration": jk.iteration,
                        "outcome": result.outcome,
                        "output_refs": result.output_refs,
                    }
                )
                cur.execute(
                    """
                    INSERT INTO outbox (
                        event_type, aggregate_type, aggregate_id, payload,
                        target_system, status, retry_count, created_at
                    ) VALUES ('job_finished', 'run', ?, ?, 'cloud_dbos', 'pending', 0, ?)
                    """,
                    (jk.run_id, outbox_payload, now_iso),
                )

                # Check if all jobs in run completed
                cur.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE run_id = ? AND status IN ({_RUN_OPEN_JOB_STATUSES_SQL})",
                    (jk.run_id,),
                )
                active_count = cur.fetchone()[0]
                if active_count == 0:
                    cur.execute(
                        "UPDATE runs SET status = 'completed', completed_at = ?, updated_at = ? WHERE run_id = ?",
                        (now_iso, now_iso, jk.run_id),
                    )

            elif result.outcome == "retry":
                retry_count = job_row["retry_count"]
                max_retries = job_row["max_retries"]
                if retry_count < max_retries:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending',
                            retry_count = retry_count + 1,
                            cause_code = ?,
                            current_lease_id = NULL,
                            updated_at = ?
                        WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                          AND stage = ? AND iteration = ? AND fencing_token = ?
                        """,
                        (result.cause_code, now_iso, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'failed',
                            cause_code = ?,
                            finished_at = ?,
                            updated_at = ?,
                            current_lease_id = NULL
                        WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                          AND stage = ? AND iteration = ? AND fencing_token = ?
                        """,
                        (result.cause_code or "MAX_RETRIES_EXCEEDED", now_iso, now_iso, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                    )
                cur.execute(
                    "UPDATE claims SET status = 'released' WHERE lease_id = ?",
                    (claim.lease_id,),
                )

            elif result.outcome in ("failed", "cancelled"):
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        cause_code = ?,
                        finished_at = ?,
                        updated_at = ?,
                        current_lease_id = NULL
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                      AND stage = ? AND iteration = ? AND fencing_token = ?
                    """,
                    (result.outcome, result.cause_code, now_iso, now_iso, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                )
                cur.execute(
                    "UPDATE claims SET status = 'released' WHERE lease_id = ?",
                    (claim.lease_id,),
                )

            else:
                # replan, waiting_dependency, waiting_human
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        cause_code = ?,
                        updated_at = ?,
                        current_lease_id = NULL
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                      AND stage = ? AND iteration = ? AND fencing_token = ?
                    """,
                    (result.outcome, result.cause_code, now_iso, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                )
                cur.execute(
                    "UPDATE claims SET status = 'released' WHERE lease_id = ?",
                    (claim.lease_id,),
                )

            if result.outcome != "success" and result.evidence_refs:
                # A failed/retry/waiting stage explains itself through its evidence refs (failing test ids,
                # log names): the success branch persists them, the others used to drop them, so the live
                # board showed a bare `validate_exhausted` (USR-62 pilot). Empty refs never clobber a value.
                cur.execute(
                    """
                    UPDATE jobs SET evidence_refs = ?
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ?
                      AND stage = ? AND iteration = ? AND fencing_token = ?
                    """,
                    (
                        json.dumps(result.evidence_refs),
                        jk.run_id,
                        jk.ticket_id,
                        jk.plan_version,
                        jk.stage,
                        jk.iteration,
                        claim.fencing_token,
                    ),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def materialize(self, event: dict[str, Any], now: datetime) -> None:
        """Confirm processing and dispatch of an outbox event."""
        now_iso = now.isoformat()
        outbox_id = event.get("outbox_id") or event.get("event_id")
        if outbox_id is None:
            raise OutboxNotFoundError("Event dictionary must contain 'outbox_id' or 'event_id'.")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                "UPDATE outbox SET status = 'published', published_at = ? WHERE outbox_id = ?",
                (now_iso, int(outbox_id)),
            )
            if cur.rowcount == 0:
                cur.execute("SELECT status FROM outbox WHERE outbox_id = ?", (int(outbox_id),))
                if not cur.fetchone():
                    conn.rollback()
                    raise OutboxNotFoundError(f"Outbox event '{outbox_id}' not found.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile(self, now: datetime, cursor: str | None = None, limit: int = 100) -> ReconcilePage:
        """Sweep expired leases and re-enqueue orphaned jobs."""
        now_iso = now.isoformat()
        cycle_id = f"cycle-{uuid4().hex[:12]}"
        repaired_keys: list[dict[str, Any]] = []

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Find active claims with expires_at < now
            cur.execute(
                """
                SELECT lease_id, run_id, ticket_id, plan_version, stage, iteration, fencing_token
                FROM claims
                WHERE status = 'active' AND expires_at < ?
                LIMIT ?
                """,
                (now_iso, limit),
            )
            expired_claims = cur.fetchall()

            for c in expired_claims:
                lease_id = c["lease_id"]
                run_id = c["run_id"]
                ticket_id = c["ticket_id"]
                plan_version = c["plan_version"]
                stage = c["stage"]
                iteration = c["iteration"]
                claim_token = c["fencing_token"]

                cur.execute(
                    "UPDATE claims SET status = 'expired' WHERE lease_id = ?",
                    (lease_id,),
                )

                cur.execute(
                    """
                    SELECT fencing_token, retry_count, max_retries
                    FROM jobs
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                    """,
                    (run_id, ticket_id, plan_version, stage, iteration),
                )
                job_row = cur.fetchone()
                if job_row and job_row["fencing_token"] == claim_token:
                    new_token = job_row["fencing_token"] + 1
                    target_key = {
                        "run_id": run_id,
                        "ticket_id": ticket_id,
                        "plan_version": plan_version,
                        "stage": stage,
                        "iteration": iteration,
                    }

                    if job_row["retry_count"] < job_row["max_retries"]:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'pending',
                                fencing_token = ?,
                                current_lease_id = NULL,
                                retry_count = retry_count + 1,
                                cause_code = 'LEASE_EXPIRED',
                                updated_at = ?
                            WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                            """,
                            (new_token, now_iso, run_id, ticket_id, plan_version, stage, iteration),
                        )
                        action = "requeued_job"
                    else:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'failed',
                                fencing_token = ?,
                                current_lease_id = NULL,
                                cause_code = 'MAX_RETRIES_EXCEEDED',
                                finished_at = ?,
                                updated_at = ?
                            WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                            """,
                            (new_token, now_iso, now_iso, run_id, ticket_id, plan_version, stage, iteration),
                        )
                        action = "failed_job"

                    cur.execute(
                        """
                        INSERT INTO reconciliation_ledger (
                            cycle_id, action_type, target_key, reason, status, recorded_at
                        ) VALUES (?, ?, ?, 'lease_expired', 'success', ?)
                        """,
                        (cycle_id, action, json.dumps(target_key), now_iso),
                    )
                    repaired_keys.append({"action": action, "job_key": target_key, "lease_id": lease_id})

            cur.execute("SELECT DISTINCT project_id FROM runs WHERE status = 'active'")
            visited_projects = [r[0] for r in cur.fetchall()]

            conn.commit()
            return ReconcilePage(
                cursor=cursor,
                visited_projects=visited_projects,
                repaired_keys=repaired_keys,
                next_cursor=None,
                cycle_id=cycle_id,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_operation(self, key: str) -> ExternalOperation | None:
        """Retrieve recorded external operation by idempotency key."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT operation_key, request_digest, provider, external_id, status,
                       claim_lease_id, fencing_token, observed_at, response_digest, error_details
                FROM external_operations
                WHERE operation_key = ?
                """,
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return ExternalOperation(
                operation_key=row["operation_key"],
                request_digest=row["request_digest"],
                provider=row["provider"],
                external_id=row["external_id"],
                status=row["status"],
                claim_lease_id=row["claim_lease_id"],
                fencing_token=row["fencing_token"],
                observed_at=row["observed_at"],
                response_digest=row["response_digest"],
                error_details=row["error_details"],
            )
        finally:
            conn.close()

    def record_operation(self, operation: ExternalOperation, claim: Claim) -> None:
        """Atomically record or update an external operation within active lease."""
        jk = claim.job_key

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Verify claim is active
            cur.execute(
                "SELECT status, expires_at, fencing_token FROM claims WHERE lease_id = ?",
                (claim.lease_id,),
            )
            claim_row = cur.fetchone()
            if not claim_row or claim_row["status"] != "active":
                conn.rollback()
                raise StaleLeaseError(f"Lease '{claim.lease_id}' is not active.")

            if claim_row["fencing_token"] != claim.fencing_token:
                conn.rollback()
                raise StaleLeaseError("Fencing token mismatch on claim.")

            cur.execute(
                """
                SELECT fencing_token, current_lease_id
                FROM jobs
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                """,
                (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
            )
            job_row = cur.fetchone()
            if not job_row:
                conn.rollback()
                raise StaleLeaseError("Job not found.")

            if (
                job_row["fencing_token"] != claim.fencing_token
                or job_row["current_lease_id"] != claim.lease_id
            ):
                conn.rollback()
                raise StaleLeaseError("Job fencing token mismatch.")

            # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
            if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                cur.execute(
                    "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = ?",
                    (claim.reservation_id,),
                )
                res_row = cur.fetchone()
                if res_row:
                    res_expires = datetime.fromisoformat(str(res_row["expires_at"]))
                    now_op = datetime.fromisoformat(str(operation.observed_at)) if operation.observed_at else datetime.now(UTC)
                    if res_expires.tzinfo is None and now_op.tzinfo is not None:
                        res_expires = res_expires.replace(tzinfo=now_op.tzinfo)
                    elif res_expires.tzinfo is not None and now_op.tzinfo is None:
                        now_op = now_op.replace(tzinfo=res_expires.tzinfo)
                    if res_row["status"] not in ("active", "draining") or res_expires < now_op:
                        conn.rollback()
                        raise StaleLeaseError(
                            f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({res_row['status']}). Old owner cannot record operation."
                        )

            cur.execute(
                """
                INSERT INTO external_operations (
                    operation_key, request_digest, provider, external_id, status,
                    claim_lease_id, fencing_token, observed_at, response_digest, error_details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_key) DO UPDATE SET
                    request_digest = excluded.request_digest,
                    provider = excluded.provider,
                    external_id = excluded.external_id,
                    status = excluded.status,
                    claim_lease_id = excluded.claim_lease_id,
                    fencing_token = excluded.fencing_token,
                    observed_at = excluded.observed_at,
                    response_digest = excluded.response_digest,
                    error_details = excluded.error_details
                """,
                (
                    operation.operation_key,
                    operation.request_digest,
                    operation.provider,
                    operation.external_id,
                    operation.status,
                    claim.lease_id,
                    claim.fencing_token,
                    operation.observed_at,
                    operation.response_digest,
                    operation.error_details,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def emit_outbox(self, event: OutboxEvent, now: datetime) -> OutboxEvent:
        """Atomically emit an event to the outbox queue."""
        now_iso = now.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO outbox (
                    event_type, aggregate_type, aggregate_id, payload,
                    target_system, status, retry_count, created_at, published_at, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_type,
                    event.aggregate_type,
                    event.aggregate_id or (event.run_id or ""),
                    json.dumps(event.payload),
                    event.target_system,
                    event.status,
                    event.retry_count,
                    event.created_at or now_iso,
                    event.published_at,
                    event.error_message,
                ),
            )
            outbox_id = cur.lastrowid
            conn.commit()
            return OutboxEvent(
                outbox_id=outbox_id,
                event_type=event.event_type,
                aggregate_type=event.aggregate_type,
                aggregate_id=event.aggregate_id or (event.run_id or ""),
                run_id=event.run_id,
                payload=event.payload,
                target_system=event.target_system,
                status=event.status,
                retry_count=event.retry_count,
                created_at=event.created_at or now_iso,
                published_at=event.published_at,
                error_message=event.error_message,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_run_payload(self, run_id: str) -> dict[str, Any] | None:
        """The `IntakeCommand.payload` that created `run_id` (HF-27-08 bindings.py).

        Line stage handlers (grill, planning) resolve the demand text and
        `parent_grill` (for milestone child runs) from this instead of
        carrying them in `StageContext`, which has no payload field.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM intake_commands WHERE run_id = ?", (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            try:
                data = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None
        finally:
            conn.close()

    def get_run_identity(self, run_id: str) -> dict[str, str] | None:
        """Return the run identity, distinguishing absent rows from storage failure."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT project_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return {"project_id": row["project_id"]} if row is not None else None
        finally:
            conn.close()

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        """Full status of a run and its jobs (HF-27-10 canary observer).

        Mirrors `PostgresControlStore.get_run_status`'s mock-mode branch so
        callers that only need read-only stage telemetry (timings, cost,
        retries, cause_code) can use either backend interchangeably without
        reimplementing the join.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                return None
            cur.execute("SELECT * FROM jobs WHERE run_id = ? ORDER BY created_at ASC", (run_id,))
            jobs = []
            for j in cur.fetchall():
                job_d = dict(j)
                for ref_key in ("output_refs", "evidence_refs"):
                    if isinstance(job_d.get(ref_key), str):
                        try:
                            job_d[ref_key] = json.loads(job_d[ref_key])
                        except (TypeError, json.JSONDecodeError):
                            job_d[ref_key] = []
                jobs.append(job_d)
            res = dict(run_row)
            res["jobs"] = jobs
            return res
        finally:
            conn.close()

    def find_intake_runs(self, channel: str, external_id: str) -> list[tuple[str, str | None]]:
        """`(external_id, run_id)` of every intake for `channel` whose external id is `external_id`
        or one of its retry attempts (`<external_id>:a<n>`), oldest first.

        Used by the owner intake to find the latest attempt of a ticket without a second table.
        """
        escaped = external_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT external_id, run_id FROM intake_commands "
                "WHERE channel = ? AND (external_id = ? OR external_id LIKE ? ESCAPE '\\') ORDER BY committed_at, external_id",
                (channel, external_id, f"{escaped}:a%"),
            )
            return [(row["external_id"], row["run_id"]) for row in cur.fetchall()]
        finally:
            conn.close()

    def record_grill_answer(self, run_id: str, question_id: str, choice: str, now: datetime) -> None:
        """Record an owner's grill answer for `(run_id, question_id)` (last answer wins).

        The answer lives in the control store, not on the run's git branch, so a process
        without git/workspace access (the DarkHub) can record it; the worker applies it to the
        pending grill on the next claim (`stage_grill.run_grill(answers=...)`).
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO grill_answers (run_id, question_id, choice, recorded_at) VALUES (?, ?, ?, ?)
                ON CONFLICT (run_id, question_id) DO UPDATE SET choice = excluded.choice, recorded_at = excluded.recorded_at
                """,
                (run_id, question_id, choice, now.isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_grill_answers(self, run_id: str) -> dict[str, str]:
        """Grill answers recorded for `run_id`, as `{question_id: choice}` (raw callback choice)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT question_id, choice FROM grill_answers WHERE run_id = ?", (run_id,))
            return {row["question_id"]: row["choice"] for row in cur.fetchall()}
        finally:
            conn.close()

    def find_job(self, run_id: str, stage: str, status: str | None = None) -> JobKey | None:
        """The highest-iteration job for `(run_id, stage)`, optionally filtered by `status`.

        Used by `core.line.human` and the Telegram callback routing to
        resolve the exact `JobKey` (including its current `iteration`) to
        resume for a `waiting_human` job, without either side guessing it.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            if status:
                cur.execute(
                    """
                    SELECT ticket_id, plan_version, iteration FROM jobs
                    WHERE run_id = ? AND stage = ? AND status = ?
                    ORDER BY iteration DESC LIMIT 1
                    """,
                    (run_id, stage, status),
                )
            else:
                cur.execute(
                    """
                    SELECT ticket_id, plan_version, iteration FROM jobs
                    WHERE run_id = ? AND stage = ?
                    ORDER BY iteration DESC LIMIT 1
                    """,
                    (run_id, stage),
                )
            row = cur.fetchone()
            if not row:
                return None
            return JobKey(
                run_id=run_id,
                ticket_id=row["ticket_id"],
                plan_version=row["plan_version"],
                stage=stage,
                iteration=row["iteration"],
            )
        finally:
            conn.close()

    def resume_job(self, job_key: JobKey, now: datetime) -> bool:
        """Transition a `waiting_human`/`waiting_dependency` job back to `pending` (HF-27-08 D-d).

        Sets `ready_at = now` so the resumed job's claim-wait is measured from
        this resume, not from its original `created_at`. Returns `True` if a
        matching job in a waiting state was found and resumed, `False`
        otherwise (already resumed, wrong stage, or unknown job).
        """
        now_iso = now.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE jobs
                SET status = 'pending', ready_at = ?, not_before = NULL, updated_at = ?
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                  AND status IN ('waiting_human', 'waiting_dependency')
                """,
                (now_iso, now_iso, job_key.run_id, job_key.ticket_id, job_key.plan_version, job_key.stage, job_key.iteration),
            )
            resumed = cur.rowcount > 0
            if resumed:
                # Heal legacy rows: a run closed as `completed` while this job was
                # parked is live again (idempotent; same transaction).
                cur.execute(
                    "UPDATE runs SET status = 'active', completed_at = NULL, updated_at = ? "
                    "WHERE run_id = ? AND status = 'completed'",
                    (now_iso, job_key.run_id),
                )
            conn.commit()
            return resumed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_job_evidence(self, job_key: JobKey, ref: str) -> bool:
        """Append `ref` to the `evidence_refs` of a job parked in `waiting_human` (USR-155).

        Idempotent and status-guarded: returns `True` when the ref is persisted on the job (now or
        already), `False` when the job is unknown or not `waiting_human`. `updated_at` is left alone so
        the marker never looks like job activity. Used as the durable "owner already notified" mark.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT evidence_refs FROM jobs
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                  AND status = 'waiting_human'
                """,
                job_key.to_tuple(),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            try:
                refs = json.loads(row["evidence_refs"] or "[]")
            except (TypeError, json.JSONDecodeError):
                refs = []
            if not isinstance(refs, list):
                refs = []
            if ref not in refs:
                refs.append(ref)
                cur.execute(
                    """
                    UPDATE jobs SET evidence_refs = ?
                    WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                    """,
                    (json.dumps(refs), *job_key.to_tuple()),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_waiting_jobs(
        self, statuses: tuple[str, ...] = ("waiting_human", "waiting_dependency")
    ) -> list[JobKey]:
        """List all active jobs parked in waiting states."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            placeholders = ", ".join("?" for _ in statuses)
            cur.execute(
                f"""
                SELECT run_id, ticket_id, plan_version, stage, iteration
                FROM jobs
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC
                """,
                statuses,
            )
            return [
                JobKey(
                    run_id=row["run_id"],
                    ticket_id=row["ticket_id"],
                    plan_version=row["plan_version"],
                    stage=row["stage"],
                    iteration=row["iteration"],
                )
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def list_active_run_ids(self) -> list[str]:
        """Ids of every run whose `runs.status` is `active`, oldest first (USR-142 orphan-run sweep)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT run_id FROM runs WHERE status = 'active' ORDER BY created_at ASC")
            return [str(row["run_id"]) for row in cur.fetchall()]
        finally:
            conn.close()

    def max_iteration(self, run_id: str, ticket_id: str, plan_version: str, stage: str) -> int:
        """Highest `iteration` already recorded for `(run_id, ticket_id, plan_version, stage)`, or -1.

        Used by `core.workflow.successors.materialize_result` to resolve a
        cross-stage retry's (`retry:<stage>`, HF-27-08 D-b) target iteration
        without duck-typing store internals.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT MAX(iteration) FROM jobs WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ?",
                (run_id, ticket_id, plan_version, stage),
            )
            row = cur.fetchone()
            value = row[0] if row else None
            return int(value) if value is not None else -1
        finally:
            conn.close()

    def get_run_created_at(self, run_id: str) -> str | None:
        """ISO `created_at` of `run_id`'s `runs` row, or `None` if unknown.

        Used to bound `not_before`-carrying retries (e.g. `ci_pending`,
        transient deploy retries) by `RunCaps.wall_clock_hours` instead of a
        retry count (HF-27-08 review item 2).
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT created_at FROM runs WHERE run_id = ?", (run_id,))
            row = cur.fetchone()
            return row["created_at"] if row else None
        finally:
            conn.close()

    def get_latest_success_output_refs(self, run_id: str, exclude_stage: str | None = None) -> list[str]:
        """`output_refs` of the most recently succeeded job for `run_id` (HF-27-08 D-f).

        Used to build the next stage handler's `StageContext.input_refs` from
        its predecessor's outputs without either side depending on process
        memory. `exclude_stage` skips jobs on that stage (e.g. so a stage
        cannot pick up its own prior success as its own input on a retry).
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            if exclude_stage:
                cur.execute(
                    """
                    SELECT output_refs FROM jobs
                    WHERE run_id = ? AND status = 'succeeded' AND stage != ?
                    ORDER BY finished_at DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (run_id, exclude_stage),
                )
            else:
                cur.execute(
                    """
                    SELECT output_refs FROM jobs
                    WHERE run_id = ? AND status = 'succeeded'
                    ORDER BY finished_at DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (run_id,),
                )
            row = cur.fetchone()
            if not row:
                return []
            try:
                refs = json.loads(row["output_refs"])
            except (TypeError, json.JSONDecodeError):
                return []
            return refs if isinstance(refs, list) else []
        finally:
            conn.close()

    def list_active_projects(self, cursor: str | None = None, limit: int = 100) -> tuple[list[str], str | None]:
        """Stable paginated query of active project IDs ordered deterministically."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            if cursor:
                cur.execute(
                    """
                    SELECT DISTINCT project_id FROM runs
                    WHERE status = 'active' AND project_id > ?
                    ORDER BY project_id ASC
                    LIMIT ?
                    """,
                    (cursor, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT DISTINCT project_id FROM runs
                    WHERE status = 'active'
                    ORDER BY project_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = [r[0] for r in cur.fetchall()]
            next_cursor = rows[-1] if len(rows) == limit else None
            return rows, next_cursor
        finally:
            conn.close()

    def get_ready_age_metrics(self, now: datetime) -> dict[str, Any]:
        """Query ready-age and claim distribution for pending jobs."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM runs WHERE status = 'active'")
            active_runs = cur.fetchone()[0]

            cur.execute(
                """
                SELECT j.run_id, r.project_id, j.ticket_id, j.stage, j.status, j.created_at, j.updated_at,
                       j.ready_at
                FROM jobs j
                JOIN runs r ON j.run_id = r.run_id
                WHERE j.status IN ('pending', 'running')
                """
            )
            rows = cur.fetchall()

            pending_count = 0
            running_count = 0
            max_ready_age_sec = 0.0
            starved_projects: set[str] = set()

            for row in rows:
                status = row["status"]
                # HF-27-08 D-d: ready-age counts from ready_at (reset on every
                # waiting_* -> pending transition), falling back to created_at.
                created_dt = datetime.fromisoformat(row["ready_at"] or row["created_at"])
                age_sec = max(0.0, (now - created_dt).total_seconds())
                if status == "pending":
                    pending_count += 1
                    if age_sec > max_ready_age_sec:
                        max_ready_age_sec = age_sec
                    if age_sec >= 30.0:
                        starved_projects.add(row["project_id"])
                elif status == "running":
                    running_count += 1

            return {
                "active_runs": active_runs,
                "pending_count": pending_count,
                "running_count": running_count,
                "max_ready_age_sec": max_ready_age_sec,
                "starved_projects": sorted(starved_projects),
            }
        finally:
            conn.close()

    def get_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Query pending outbox events for publication/materialization."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT outbox_id, event_type, aggregate_type, aggregate_id, payload,
                       target_system, status, retry_count, created_at
                FROM outbox
                WHERE status = 'pending'
                ORDER BY outbox_id ASC
                LIMIT ?
                """,
                (limit,),
            )
            out: list[dict[str, Any]] = []
            for row in cur.fetchall():
                out.append({
                    "outbox_id": row["outbox_id"],
                    "event_type": row["event_type"],
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": row["aggregate_id"],
                    "payload": json.loads(row["payload"]),
                    "target_system": row["target_system"],
                    "status": row["status"],
                    "retry_count": row["retry_count"],
                    "created_at": row["created_at"],
                })
            return out
        finally:
            conn.close()

    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str = "owner_cancelled",
        actor: str = "owner",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cancels a line run and all its open jobs with audit logging (USR-123)."""
        current_time = now or datetime.now(UTC)
        now_iso = current_time.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute("SELECT run_id, status FROM runs WHERE run_id = ?", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                conn.rollback()
                return {"ok": False, "error": f"run not found: {run_id}", "run_id": run_id}

            # 1. Cancel all open jobs (pending, running, waiting_human, waiting_dependency)
            cur.execute(
                f"""
                UPDATE jobs
                SET status = 'cancelled',
                    cause_code = ?,
                    finished_at = ?,
                    updated_at = ?,
                    current_lease_id = NULL
                WHERE run_id = ? AND status IN ({_RUN_OPEN_JOB_STATUSES_SQL})
                """,
                (reason, now_iso, now_iso, run_id),
            )
            jobs_cancelled = cur.rowcount

            # 2. Release active claims
            cur.execute(
                "UPDATE claims SET status = 'released' WHERE run_id = ? AND status = 'active'",
                (run_id,),
            )

            # 2b. Cancel active fleet reservations for this run (USR-281)
            cur.execute(
                "UPDATE fleet_reservations SET status = 'cancelled', heartbeat_at = ? WHERE run_id = ? AND status IN ('active', 'draining')",
                (now_iso, run_id),
            )

            # 3. Cancel the run
            cur.execute(
                "UPDATE runs SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE run_id = ?",
                (now_iso, now_iso, run_id),
            )

            # 4. Record audit event
            audit_payload = json.dumps({
                "reason": reason,
                "actor": actor,
                "jobs_cancelled": jobs_cancelled,
            })
            cur.execute(
                """
                INSERT INTO outbox (
                    event_type, aggregate_type, aggregate_id, payload,
                    target_system, status, retry_count, created_at
                ) VALUES ('run_cancelled', 'run', ?, ?, 'audit', 'pending', 0, ?)
                """,
                (run_id, audit_payload, now_iso),
            )

            conn.commit()
            return {
                "ok": True,
                "run_id": run_id,
                "jobs_cancelled": jobs_cancelled,
                "message": f"Run {run_id} cancelado com sucesso ({jobs_cancelled} jobs cancelados).",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _row_to_reservation(row: sqlite3.Row) -> FleetReservation:
        raw_meta = row["metadata"]
        try:
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
        except Exception:
            meta = {}
        return FleetReservation(
            reservation_id=row["reservation_id"],
            project_id=row["project_id"],
            ticket_id=row["ticket_id"],
            run_id=row["run_id"],
            host_id=row["host_id"],
            status=row["status"],
            fencing_token=row["fencing_token"],
            acquired_at=str(row["acquired_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["expires_at"]),
            metadata=meta,
        )

    def reserve_cycle(
        self,
        project_id: str,
        ticket_id: str,
        run_id: str,
        host_id: str,
        now: datetime,
        lease_duration_sec: int = 45,
        max_fleet_slots: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> FleetReservation:
        """Atomically reserve a demand cycle on a specific fleet host (USR-281)."""
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=lease_duration_sec)).isoformat()

        max_retries = 10
        for attempt in range(max_retries):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.cursor()

                # 1. Sweep expired active reservations to release slots
                cur.execute(
                    """
                    UPDATE fleet_reservations
                    SET status = 'expired'
                    WHERE status = 'active' AND expires_at < ?
                    """,
                    (now_iso,),
                )

                # 2. Check active ticket uniqueness
                cur.execute(
                    """
                    SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                           acquired_at, heartbeat_at, expires_at, metadata
                    FROM fleet_reservations
                    WHERE project_id = ? AND ticket_id = ? AND status = 'active'
                    """,
                    (project_id, ticket_id),
                )
                ticket_row = cur.fetchone()
                if ticket_row:
                    if ticket_row["run_id"] == run_id and ticket_row["host_id"] == host_id:
                        conn.commit()
                        return self._row_to_reservation(ticket_row)
                    conn.rollback()
                    raise TicketAlreadyActiveError(
                        f"Ticket '{ticket_id}' in project '{project_id}' already has active reservation "
                        f"'{ticket_row['reservation_id']}' on host '{ticket_row['host_id']}'."
                    )

                # 3. Check 1 cycle per host
                cur.execute(
                    """
                    SELECT reservation_id, ticket_id, run_id
                    FROM fleet_reservations
                    WHERE host_id = ? AND status = 'active'
                    """,
                    (host_id,),
                )
                host_row = cur.fetchone()
                if host_row:
                    conn.rollback()
                    raise HostBusyError(
                        f"Host '{host_id}' already has active reservation '{host_row['reservation_id']}' "
                        f"for ticket '{host_row['ticket_id']}'."
                    )

                # 4. Check fleet ceiling of max 3 active cycles
                cur.execute(
                    "SELECT COUNT(*) as count FROM fleet_reservations WHERE status = 'active'"
                )
                count = cur.fetchone()["count"]
                if count >= max_fleet_slots:
                    conn.rollback()
                    raise FleetCapacityExceededError(
                        f"Fleet capacity ceiling of {max_fleet_slots} active cycles reached (currently {count} active)."
                    )

                # 5. Insert new reservation
                reservation_id = f"fres-{uuid4().hex[:12]}"
                meta_json = json.dumps(metadata or {})
                cur.execute(
                    """
                    INSERT INTO fleet_reservations (
                        reservation_id, project_id, ticket_id, run_id, host_id,
                        status, fencing_token, acquired_at, heartbeat_at, expires_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
                    """,
                    (
                        reservation_id,
                        project_id,
                        ticket_id,
                        run_id,
                        host_id,
                        now_iso,
                        now_iso,
                        expires_at,
                        meta_json,
                    ),
                )
                conn.commit()
                return FleetReservation(
                    reservation_id=reservation_id,
                    project_id=project_id,
                    ticket_id=ticket_id,
                    run_id=run_id,
                    host_id=host_id,
                    status="active",
                    fencing_token=1,
                    acquired_at=now_iso,
                    heartbeat_at=now_iso,
                    expires_at=expires_at,
                    metadata=metadata or {},
                )
            except sqlite3.IntegrityError as ie:
                conn.rollback()
                msg = str(ie).lower()
                if "host" in msg:
                    raise HostBusyError(f"Host '{host_id}' already has active reservation.") from ie
                elif "ticket" in msg:
                    raise TicketAlreadyActiveError(f"Ticket '{ticket_id}' in project '{project_id}' already has active reservation.") from ie
                raise ReservationConflictError(f"Fleet reservation integrity conflict: {ie}") from ie
            except sqlite3.OperationalError as oe:
                conn.rollback()
                err_msg = str(oe).lower()
                if ("locked" in err_msg or "busy" in err_msg) and attempt < max_retries - 1:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                if "locked" in err_msg or "busy" in err_msg:
                    raise HostBusyError(f"Host '{host_id}' lock contention: {oe}") from oe
                raise
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def heartbeat_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration_sec: int | None = None,
    ) -> FleetReservation:
        """Extend active cycle reservation lease (USR-281)."""
        now_iso = now.isoformat()
        dur = lease_duration_sec if lease_duration_sec is not None else self.lease_duration_sec
        new_expires_at = (now + timedelta(seconds=dur)).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise StaleLeaseError(f"Reservation '{reservation_id}' not found.")
            if row["status"] not in ("active", "draining"):
                conn.rollback()
                raise StaleLeaseError(f"Reservation '{reservation_id}' is in non-active status '{row['status']}'.")
            if row["fencing_token"] != fencing_token:
                conn.rollback()
                raise StaleLeaseError(
                    f"Fencing token mismatch on heartbeat: token={fencing_token}, active={row['fencing_token']}."
                )
            res_expires = datetime.fromisoformat(str(row["expires_at"]))
            if res_expires.tzinfo is None and now.tzinfo is not None:
                res_expires = res_expires.replace(tzinfo=now.tzinfo)
            elif res_expires.tzinfo is not None and now.tzinfo is None:
                now_comp = now.replace(tzinfo=res_expires.tzinfo)
            else:
                now_comp = now
            if res_expires < now_comp:
                cur.execute("UPDATE fleet_reservations SET status = 'expired' WHERE reservation_id = ?", (reservation_id,))
                conn.commit()
                raise StaleLeaseError(f"Reservation '{reservation_id}' expired at {row['expires_at']}.")

            cur.execute(
                """
                UPDATE fleet_reservations
                SET heartbeat_at = ?, expires_at = ?
                WHERE reservation_id = ?
                """,
                (now_iso, new_expires_at, reservation_id),
            )
            conn.commit()
            return FleetReservation(
                reservation_id=row["reservation_id"],
                project_id=row["project_id"],
                ticket_id=row["ticket_id"],
                run_id=row["run_id"],
                host_id=row["host_id"],
                status=row["status"],
                fencing_token=row["fencing_token"],
                acquired_at=str(row["acquired_at"]),
                heartbeat_at=now_iso,
                expires_at=new_expires_at,
                metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        status: str = "released",
        now: datetime | None = None,
    ) -> FleetReservation:
        """Release active cycle reservation (USR-281)."""
        now_dt = now or datetime.now(UTC)
        now_iso = now_dt.isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise StaleLeaseError(f"Reservation '{reservation_id}' not found.")
            if row["fencing_token"] != fencing_token:
                conn.rollback()
                raise StaleLeaseError(
                    f"Fencing token mismatch on release: token={fencing_token}, active={row['fencing_token']}."
                )
            if row["status"] in ("released", "completed", "failed", "cancelled"):
                conn.commit()
                return self._row_to_reservation(row)
            if row["status"] == "expired":
                conn.rollback()
                raise StaleLeaseError(f"Reservation '{reservation_id}' has already expired and cannot be released.")

            cur.execute(
                "UPDATE fleet_reservations SET status = ?, heartbeat_at = ? WHERE reservation_id = ?",
                (status, now_iso, reservation_id),
            )
            conn.commit()
            return FleetReservation(
                reservation_id=row["reservation_id"],
                project_id=row["project_id"],
                ticket_id=row["ticket_id"],
                run_id=row["run_id"],
                host_id=row["host_id"],
                status=status,
                fencing_token=row["fencing_token"],
                acquired_at=str(row["acquired_at"]),
                heartbeat_at=now_iso,
                expires_at=str(row["expires_at"]),
                metadata=json.loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {}),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_reservation(self, reservation_id: str) -> FleetReservation | None:
        """Retrieve reservation by ID (USR-281)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            )
            row = cur.fetchone()
            return self._row_to_reservation(row) if row else None
        finally:
            conn.close()

    def get_active_reservation_for_ticket(
        self, project_id: str, ticket_id: str
    ) -> FleetReservation | None:
        """Find active reservation for a project ticket (USR-281)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE project_id = ? AND ticket_id = ? AND status = 'active'
                """,
                (project_id, ticket_id),
            )
            row = cur.fetchone()
            return self._row_to_reservation(row) if row else None
        finally:
            conn.close()

    def get_active_reservation_for_host(self, host_id: str) -> FleetReservation | None:
        """Find active reservation currently occupying a host (USR-281)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE host_id = ? AND status = 'active'
                """,
                (host_id,),
            )
            row = cur.fetchone()
            return self._row_to_reservation(row) if row else None
        finally:
            conn.close()

    def get_active_reservation_for_run(self, run_id: str) -> FleetReservation | None:
        """Find active reservation for a run (USR-282)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE run_id = ? AND status IN ('active', 'draining')
                """,
                (run_id,),
            )
            row = cur.fetchone()
            return self._row_to_reservation(row) if row else None
        finally:
            conn.close()

    def list_active_reservations(self) -> list[FleetReservation]:
        """List all active fleet reservations (USR-281)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE status IN ('active', 'draining')
                ORDER BY acquired_at ASC
                """
            )
            return [self._row_to_reservation(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def drain_host(self, host_id: str, reason: str, now: datetime) -> int:
        """Mark host and any active reservations for drain (USR-281)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, metadata
                FROM fleet_reservations
                WHERE host_id = ? AND status = 'active'
                """,
                (host_id,),
            )
            rows = cur.fetchall()
            for r in rows:
                meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})
                meta["drain_reason"] = reason
                cur.execute(
                    "UPDATE fleet_reservations SET status = 'draining', metadata = ? WHERE reservation_id = ?",
                    (json.dumps(meta), r["reservation_id"]),
                )
            conn.commit()
            return len(rows)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew_open_run_reservations(
        self, now: datetime, pending_grace_sec: int = RESERVATION_PENDING_GRACE_SEC
    ) -> int:
        """Renew active reservations whose run is between stages or waiting on CI/dependencies (USR-306).

        Without this the reservation is only renewed while a stage handler runs, so any gap between stages
        (successor job pending, waiting_dependency) let it expire un-renewed. Running jobs are renewed by their
        worker heartbeat; pending jobs only within `pending_grace_sec` of becoming ready (dead host => expires).
        """
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=self.lease_duration_sec)).isoformat()
        cutoff_iso = (now - timedelta(seconds=pending_grace_sec)).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE fleet_reservations
                SET heartbeat_at = ?, expires_at = ?
                WHERE status = 'active' AND expires_at >= ?
                  AND run_id IN (
                      SELECT run_id FROM jobs
                      WHERE status = 'waiting_dependency'
                         OR (status = 'pending' AND updated_at >= ?)
                  )
                """,
                (now_iso, expires_iso, now_iso, cutoff_iso),
            )
            renewed = cur.rowcount
            conn.commit()
            return int(renewed or 0)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_fleet_reservations(self, now: datetime) -> list[FleetReservation]:
        """Sweep expired fleet reservations and release slots (USR-281)."""
        now_iso = now.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                       acquired_at, heartbeat_at, expires_at, metadata
                FROM fleet_reservations
                WHERE status IN ('active', 'draining') AND expires_at < ?
                """,
                (now_iso,),
            )
            rows = cur.fetchall()
            expired: list[FleetReservation] = []
            for r in rows:
                res = self._row_to_reservation(r)
                expired.append(res)
                cur.execute(
                    "UPDATE fleet_reservations SET status = 'expired' WHERE reservation_id = ?",
                    (r["reservation_id"],),
                )
                cur.execute(
                    """
                    INSERT INTO reconciliation_ledger (
                        cycle_id, action_type, target_key, reason, status, recorded_at
                    ) VALUES (?, 'expire_fleet_reservation', ?, 'lease_expired', 'success', ?)
                    """,
                    (
                        f"recon-{uuid4().hex[:8]}",
                        json.dumps({"reservation_id": res.reservation_id, "ticket_id": res.ticket_id, "host_id": res.host_id}),
                        now_iso,
                    ),
                )
            conn.commit()
            return expired
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reconcile_unknown_operations(self, now: datetime) -> list[ExternalOperation]:
        """Sweep and report operations left in 'unknown' state for reconciliation before retry (USR-281)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT operation_key, request_digest, provider, external_id, status,
                       claim_lease_id, fencing_token, observed_at, response_digest, error_details
                FROM external_operations
                WHERE status = 'unknown'
                """
            )
            rows = cur.fetchall()
            return [
                ExternalOperation(
                    operation_key=row["operation_key"],
                    request_digest=row["request_digest"],
                    provider=row["provider"],
                    external_id=row["external_id"],
                    status=row["status"],
                    claim_lease_id=row["claim_lease_id"],
                    fencing_token=row["fencing_token"],
                    observed_at=row["observed_at"],
                    response_digest=row["response_digest"],
                    error_details=row["error_details"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def list_eligible_admission_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pending jobs of active runs eligible for fleet cycle admission (USR-282)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT j.run_id, j.ticket_id, r.project_id, j.stage, j.status, j.created_at
                FROM jobs j
                JOIN runs r ON j.run_id = r.run_id
                WHERE j.status = 'pending' AND r.status = 'active'
                ORDER BY j.created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
