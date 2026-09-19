"""Canonical persistence protocol and SQLite implementation for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md, HF-05-02 binding, and control.json.
Governed by ADR-HF-001.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from core.workflow.control_contracts import (
    Claim,
    ExternalOperation,
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
    InvalidResultError,
    JobKey,
    OutboxEvent,
    OutboxNotFoundError,
    ReconcilePage,
    RuntimeOwner,
    StageResult,
    StaleLeaseError,
)


class ControlStore(Protocol):
    """Canonical transactional control store interface abstracting storage backends."""

    def accept(self, command: IntakeCommand, now: datetime) -> IntakeReceipt:
        """Accept an intake command transactionally with idempotency checking."""
        ...

    def claim(self, worker: str, capabilities: list[str], now: datetime) -> Claim | None:
        """Atomically query and lock the next pending job matching worker capabilities."""
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
                """
            )

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

                cur.execute(
                    """
                    INSERT INTO jobs (
                        run_id, ticket_id, plan_version, stage, iteration, status,
                        role, required_capabilities, fencing_token, timeout_seconds,
                        retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                        created_at, updated_at
                    ) VALUES (?, ?, '1.0', 'grill', 0, 'pending', 'grill_engine', '[]', 0, 1800, 0, 3, 0.0, '[]', '[]', ?, ?)
                    """,
                    (
                        run_id,
                        command.project_id,
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

    def claim(self, worker: str, capabilities: list[str], now: datetime) -> Claim | None:
        """Atomically query and lock the next pending job matching worker capabilities."""
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=self.lease_duration_sec)).isoformat()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()
            cur.execute(
                """
                SELECT run_id, ticket_id, plan_version, stage, iteration, fencing_token, role, required_capabilities
                FROM jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                """
            )
            rows = cur.fetchall()
            chosen_row = None
            for row in rows:
                req = json.loads(row["required_capabilities"])
                if all(c in capabilities for c in req):
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
            reservation_id = f"res-{uuid4().hex[:8]}"
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
                    "SELECT COUNT(*) FROM jobs WHERE run_id = ? AND status IN ('pending', 'running')",
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
