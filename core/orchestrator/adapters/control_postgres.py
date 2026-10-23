"""PostgreSQL and Cloud DBOS adapter for the HF-05 ControlStore protocol.

Governed by ADR-HF-001, CONTRACTS.md, and control.json.
Provides production PostgreSQL execution with transactional outbox,
SELECT FOR UPDATE SKIP LOCKED, monotonic fencing, and an offline mock fallback.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from core.workflow.control_contracts import (
    Claim,
    ControlError,
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
    StoreUnavailableError,
    TicketAlreadyActiveError,
    RESERVATION_PENDING_GRACE_SEC,
    fleet_managed_enabled,
    normalize_cause_code,
)
from core.workflow.control_store import ControlStore, SQLiteControlStore, _is_ready_enough, RUN_OPEN_JOB_STATUSES

logger = logging.getLogger(__name__)

POSTGRES_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(160) PRIMARY KEY,
    project_id VARCHAR(160) NOT NULL,
    demand_id VARCHAR(160) NOT NULL,
    demand_version VARCHAR(64) NOT NULL,
    runtime_owner VARCHAR(32) NOT NULL CHECK (runtime_owner IN ('hf05_sqlite', 'df11_legacy', 'cloud_dbos_postgres')),
    mode VARCHAR(32) NOT NULL CHECK (mode IN ('autonomous', 'documentary')),
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    plan_digest VARCHAR(64) NOT NULL,
    config_version VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT uq_runs_project_demand UNIQUE (project_id, demand_id, demand_version)
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs (status);

CREATE TABLE IF NOT EXISTS jobs (
    run_id VARCHAR(160) NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    ticket_id VARCHAR(64) NOT NULL,
    plan_version VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    iteration INTEGER NOT NULL CHECK (iteration >= 0),
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'succeeded', 'retry', 'replan', 'waiting_dependency', 'waiting_human', 'cancelled', 'failed')
    ),
    role VARCHAR(64) NOT NULL,
    required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    current_lease_id VARCHAR(160),
    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    cause_code VARCHAR(64),
    actual_cost NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
    output_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    not_before TIMESTAMPTZ,
    ready_at TIMESTAMPTZ,
    PRIMARY KEY (run_id, ticket_id, plan_version, stage, iteration)
);

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ready_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_jobs_claim_lookup ON jobs (status, role) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_jobs_run_id ON jobs (run_id);

CREATE TABLE IF NOT EXISTS claims (
    lease_id VARCHAR(160) PRIMARY KEY,
    run_id VARCHAR(160) NOT NULL,
    ticket_id VARCHAR(64) NOT NULL,
    plan_version VARCHAR(64) NOT NULL,
    stage VARCHAR(64) NOT NULL,
    iteration INTEGER NOT NULL,
    owner VARCHAR(160) NOT NULL,
    fencing_token INTEGER NOT NULL,
    reservation_id VARCHAR(160) NOT NULL,
    route_ref VARCHAR(160) NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'stolen')),
    FOREIGN KEY (run_id, ticket_id, plan_version, stage, iteration)
        REFERENCES jobs(run_id, ticket_id, plan_version, stage, iteration) ON DELETE RESTRICT,
    CONSTRAINT uq_claims_job_fencing UNIQUE (run_id, ticket_id, plan_version, stage, iteration, fencing_token)
);

CREATE INDEX IF NOT EXISTS idx_claims_expiry_sweep ON claims (expires_at, status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(160) NOT NULL,
    payload JSONB NOT NULL,
    target_system VARCHAR(64) NOT NULL DEFAULT 'cloud_dbos',
    status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'published', 'failed', 'dead_letter')),
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at TIMESTAMPTZ,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending_dispatch ON outbox (status, created_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS external_operations (
    operation_key VARCHAR(256) PRIMARY KEY,
    request_digest VARCHAR(64) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    external_id VARCHAR(256),
    status VARCHAR(32) NOT NULL CHECK (status IN ('prepared', 'sent', 'unknown', 'succeeded', 'failed')),
    claim_lease_id VARCHAR(160),
    fencing_token INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    response_digest VARCHAR(64),
    error_details TEXT
);

CREATE INDEX IF NOT EXISTS idx_ext_ops_provider_status ON external_operations (provider, status);

CREATE TABLE IF NOT EXISTS reconciliation_ledger (
    entry_id BIGSERIAL PRIMARY KEY,
    cycle_id VARCHAR(160) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    target_key JSONB NOT NULL,
    reason VARCHAR(256) NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recon_cycle_time ON reconciliation_ledger (cycle_id, recorded_at);

CREATE TABLE IF NOT EXISTS intake_commands (
    channel VARCHAR(64) NOT NULL,
    external_id VARCHAR(256) NOT NULL,
    project_id VARCHAR(160) NOT NULL,
    payload_digest VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    mode VARCHAR(32) NOT NULL,
    policy_ref VARCHAR(160) NOT NULL,
    demand_id VARCHAR(160) NOT NULL,
    demand_version VARCHAR(64) NOT NULL,
    run_id VARCHAR(160),
    initial_job_id VARCHAR(160),
    committed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel, external_id)
);

CREATE TABLE IF NOT EXISTS grill_answers (
    run_id VARCHAR(160) NOT NULL,
    question_id VARCHAR(160) NOT NULL,
    choice TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, question_id)
);

CREATE TABLE IF NOT EXISTS fleet_reservations (
    reservation_id VARCHAR(160) PRIMARY KEY,
    project_id VARCHAR(160) NOT NULL,
    ticket_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(160) NOT NULL,
    host_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired', 'stolen', 'draining', 'completed', 'failed', 'cancelled')),
    fencing_token INTEGER NOT NULL DEFAULT 1,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
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
    ON fleet_reservations (expires_at, status) 
    WHERE status = 'active';
"""


class PostgresControlStore:
    """PostgreSQL implementation of ControlStore with fallback/mock capability."""

    def __init__(
        self,
        database_url: str | None = None,
        mock_mode: bool = False,
        runtime_owner: str = RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
        lease_duration_sec: int = 45,
    ) -> None:
        self.runtime_owner = runtime_owner
        self.lease_duration_sec = lease_duration_sec
        self.raw_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self._psycopg = None

        if mock_mode or not self.raw_url or self.raw_url.startswith("mock"):
            self.mock_mode = True
            self._backend: ControlStore = SQLiteControlStore(
                db_path=":memory:",
                runtime_owner=self.runtime_owner,
                lease_duration_sec=self.lease_duration_sec,
            )
        else:
            try:
                import psycopg  # type: ignore[import-not-found]
                self._psycopg = psycopg
                self.mock_mode = False
                self._init_db()
            except (ImportError, Exception) as exc:
                logger.warning(
                    "PostgreSQL unavailable (%s); falling back to in-memory mock engine.",
                    exc,
                )
                self.mock_mode = True
                self._backend = SQLiteControlStore(
                    db_path=":memory:",
                    runtime_owner=self.runtime_owner,
                    lease_duration_sec=self.lease_duration_sec,
                )

    def _init_db(self) -> None:
        """Initialize PostgreSQL tables if running against a live database."""
        if self.mock_mode:
            return
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(POSTGRES_DDL)
                conn.commit()
        except Exception as exc:
            raise StoreUnavailableError(f"Failed to initialize PostgreSQL schema: {exc}") from exc

    def accept(self, command: IntakeCommand, now: datetime) -> IntakeReceipt:
        if self.mock_mode:
            return self._backend.accept(command, now)

        now_utc = now.astimezone(UTC)
        digest = command.payload_digest
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT payload_digest, demand_id, demand_version, run_id, initial_job_id, mode, committed_at "
                        "FROM intake_commands WHERE channel = %s AND external_id = %s FOR UPDATE",
                        (command.channel, command.external_id),
                    )
                    row = cur.fetchone()
                    if row:
                        p_digest, d_id, d_ver, r_id, j_id, m_mode, c_at = row
                        if p_digest != digest:
                            raise IdempotencyConflict(
                                f"Command with channel='{command.channel}' and external_id='{command.external_id}' "
                                f"already accepted with different payload digest."
                            )
                        return IntakeReceipt(
                            demand_id=d_id,
                            demand_version=d_ver,
                            run_id=r_id if m_mode == "autonomous" else None,
                            initial_job_id=j_id if m_mode == "autonomous" else None,
                            mode=m_mode,
                            committed_at=c_at.isoformat() if hasattr(c_at, "isoformat") else str(c_at),
                        )

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
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, '1.0', %s, %s)
                            """,
                            (
                                run_id,
                                command.project_id,
                                demand_id,
                                demand_version,
                                self.runtime_owner,
                                command.mode,
                                digest,
                                now_utc,
                                now_utc,
                            ),
                        )

                        # HF-27-08 review item 9: reverted -- see the
                        # matching (detailed) comment in
                        # core.workflow.control_store.SQLiteControlStore.accept().
                        # Stamping the shared grill job broke 25 pre-existing,
                        # unrelated tests that reuse project_id="darkfac" as a
                        # generic default with no line intent, since the real
                        # registered "darkfac" project legitimately has its
                        # own repo_url/deploy config.
                        ticket_id = str(command.payload.get("ticket_id") or command.project_id)
                        cur.execute(
                            """
                            INSERT INTO jobs (
                                run_id, ticket_id, plan_version, stage, iteration, status,
                                role, required_capabilities, fencing_token, timeout_seconds,
                                retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                                created_at, updated_at, ready_at
                            ) VALUES (%s, %s, '1.0', 'grill', 0, 'pending', 'grill_engine', '[]'::jsonb, 0, 1800, 0, 3, 0.0, '[]'::jsonb, '[]'::jsonb, %s, %s, %s)
                            """,
                            (run_id, ticket_id, now_utc, now_utc, now_utc),
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
                            ) VALUES ('intake_accepted', 'run', %s, %s::jsonb, 'cloud_dbos', 'pending', 0, %s)
                            """,
                            (run_id, outbox_payload, now_utc),
                        )
                    else:
                        run_id = None
                        initial_job_id = None
                        cur.execute(
                            """
                            INSERT INTO runs (
                                run_id, project_id, demand_id, demand_version, runtime_owner,
                                mode, status, plan_digest, config_version, created_at, updated_at, completed_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s, '1.0', %s, %s, %s)
                            """,
                            (
                                f"doc-{demand_id}",
                                command.project_id,
                                demand_id,
                                demand_version,
                                self.runtime_owner,
                                command.mode,
                                digest,
                                now_utc,
                                now_utc,
                                now_utc,
                            ),
                        )

                    cur.execute(
                        """
                        INSERT INTO intake_commands (
                            channel, external_id, project_id, payload_digest, payload,
                            mode, policy_ref, demand_id, demand_version, run_id,
                            initial_job_id, committed_at
                        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
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
                            now_utc,
                        ),
                    )
                    conn.commit()

                    return IntakeReceipt(
                        demand_id=demand_id,
                        demand_version=demand_version,
                        run_id=run_id,
                        initial_job_id=initial_job_id,
                        mode=command.mode,
                        committed_at=now_utc.isoformat(),
                    )
        except IdempotencyConflict:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL accept failed: {exc}") from exc

    def claim(
        self, worker: str, capabilities: list[str], now: datetime, ready_age_sec: float = 0.0
    ) -> Claim | None:
        """`ready_age_sec` (HF-27-09, default 0.0 preserves prior behaviour) skips jobs
        created less than that many seconds ago, so a lower-priority worker only claims
        what a higher-priority one left behind."""
        if self.mock_mode:
            return self._backend.claim(worker, capabilities, now, ready_age_sec=ready_age_sec)

        now_utc = now.astimezone(UTC)
        expires_at_utc = now_utc + timedelta(seconds=self.lease_duration_sec)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    # High concurrency locking via SELECT FOR UPDATE SKIP LOCKED
                    cur.execute(
                        """
                        SELECT run_id, ticket_id, plan_version, stage, iteration, fencing_token, role,
                               required_capabilities, created_at, not_before, ready_at
                        FROM jobs
                        WHERE status = 'pending'
                        ORDER BY created_at ASC
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                    rows = cur.fetchall()
                    chosen = None
                    for r in rows:
                        req = r[7] if isinstance(r[7], list) else json.loads(r[7])
                        if not all(c in capabilities for c in req):
                            continue
                        not_before = r[9]
                        if not_before is not None:
                            nb_dt = not_before if hasattr(not_before, "astimezone") else datetime.fromisoformat(str(not_before))
                            nb_dt = nb_dt.astimezone(UTC) if nb_dt.tzinfo else nb_dt.replace(tzinfo=UTC)
                            if nb_dt > now_utc:
                                continue
                        # Subordination of stage claims to fleet cycle reservation (USR-281/USR-282)
                        cur.execute(
                            """
                            SELECT reservation_id, host_id, status, expires_at
                            FROM fleet_reservations
                            WHERE run_id = %s AND status IN ('active', 'draining')
                            """,
                            (r[0],),
                        )
                        res_row = cur.fetchone()
                        if res_row:
                            res_id, h_id, st, exp_at = res_row
                            res_expires = exp_at if hasattr(exp_at, "astimezone") else datetime.fromisoformat(str(exp_at))
                            res_expires = res_expires.astimezone(UTC) if res_expires.tzinfo else res_expires.replace(tzinfo=UTC)
                            if res_expires < now_utc:
                                continue  # Expired cycle reservation
                            res_host = str(h_id).lower()
                            worker_lower = worker.lower()
                            if res_host not in worker_lower and worker_lower not in res_host:
                                continue  # Reserved for a different host
                            matched_reservation_id = res_id
                            # Host affinity confirmed: executes immediately without fixed priority delay
                        else:
                            if fleet_managed_enabled():
                                continue  # Managed mode: no legacy path without an admitted reservation (USR-306)
                            matched_reservation_id = None
                            if ready_age_sec > 0.0:
                                ready_reference = r[10] or r[8]
                                if not _is_ready_enough(ready_reference, now_utc, ready_age_sec):
                                    continue

                        chosen = r
                        break

                    if not chosen:
                        conn.commit()
                        return None

                    run_id, ticket_id, plan_version, stage, iteration, old_token, role, _req, _created_at, _nb, _ra = chosen
                    new_token = old_token + 1
                    lease_id = f"lease-{uuid4().hex[:12]}"
                    reservation_id = matched_reservation_id or f"res-{uuid4().hex[:8]}"
                    route_ref = "default"

                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'running',
                            fencing_token = %s,
                            current_lease_id = %s,
                            started_at = %s,
                            updated_at = %s
                        WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                          AND stage = %s AND iteration = %s AND fencing_token = %s
                        """,
                        (new_token, lease_id, now_utc, now_utc, run_id, ticket_id, plan_version, stage, iteration, old_token),
                    )
                    if cur.rowcount != 1:
                        conn.rollback()
                        return None

                    cur.execute(
                        """
                        INSERT INTO claims (
                            lease_id, run_id, ticket_id, plan_version, stage, iteration,
                            owner, fencing_token, reservation_id, route_ref, acquired_at, expires_at, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active')
                        """,
                        (
                            lease_id,
                            run_id,
                            ticket_id,
                            plan_version,
                            stage,
                            iteration,
                            worker,
                            new_token,
                            reservation_id,
                            route_ref,
                            now_utc,
                            expires_at_utc,
                        ),
                    )
                    if matched_reservation_id:
                        # USR-306: claim proves host liveness; renew the cycle reservation in the same transaction.
                        cur.execute(
                            "UPDATE fleet_reservations SET heartbeat_at = %s, expires_at = %s "
                            "WHERE reservation_id = %s AND status = 'active'",
                            (now_utc, expires_at_utc, matched_reservation_id),
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
                        fencing_token=new_token,
                        acquired_at=now_utc.isoformat(),
                        expires_at=expires_at_utc.isoformat(),
                        reservation_id=reservation_id,
                        route_ref=route_ref,
                    )
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL claim failed: {exc}") from exc

    def heartbeat(self, claim: Claim, now: datetime) -> Claim:
        if self.mock_mode:
            return self._backend.heartbeat(claim, now)

        now_utc = now.astimezone(UTC)
        new_expires_utc = now_utc + timedelta(seconds=self.lease_duration_sec)
        jk = claim.job_key

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, expires_at, fencing_token FROM claims WHERE lease_id = %s FOR UPDATE",
                        (claim.lease_id,),
                    )
                    row = cur.fetchone()
                    if not row or row[0] != "active":
                        raise StaleLeaseError(f"Lease '{claim.lease_id}' is not active.")

                    exp_at = row[1] if hasattr(row[1], "astimezone") else datetime.fromisoformat(str(row[1])).astimezone(UTC)
                    if exp_at < now_utc:
                        raise StaleLeaseError(f"Lease '{claim.lease_id}' expired at {exp_at}.")

                    cur.execute(
                        "SELECT fencing_token, current_lease_id FROM jobs WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s FOR UPDATE",
                        (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
                    )
                    job_row = cur.fetchone()
                    if not job_row or job_row[0] != claim.fencing_token or job_row[1] != claim.lease_id:
                        raise StaleLeaseError("Fencing token mismatch on heartbeat.")

                    # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
                    if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                        cur.execute(
                            "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = %s FOR UPDATE",
                            (claim.reservation_id,),
                        )
                        res_row = cur.fetchone()
                        if res_row:
                            st, exp_at = res_row[0], res_row[1]
                            res_expires = exp_at if hasattr(exp_at, "astimezone") else datetime.fromisoformat(str(exp_at))
                            res_expires = res_expires.astimezone(UTC) if res_expires.tzinfo else res_expires.replace(tzinfo=UTC)
                            if st not in ("active", "draining") or res_expires < now_utc:
                                raise StaleLeaseError(f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({st}).")

                    cur.execute(
                        "UPDATE claims SET expires_at = %s WHERE lease_id = %s AND fencing_token = %s",
                        (new_expires_utc, claim.lease_id, claim.fencing_token),
                    )
                    cur.execute(
                        "UPDATE jobs SET updated_at = %s WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s",
                        (now_utc, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
                    )
                    conn.commit()

                    return Claim(
                        job_key=claim.job_key,
                        lease_id=claim.lease_id,
                        owner=claim.owner,
                        fencing_token=claim.fencing_token,
                        acquired_at=claim.acquired_at,
                        expires_at=new_expires_utc.isoformat(),
                        reservation_id=claim.reservation_id,
                        route_ref=claim.route_ref,
                    )
        except StaleLeaseError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL heartbeat failed: {exc}") from exc

    def finish(self, claim: Claim, result: StageResult, now: datetime) -> None:
        if self.mock_mode:
            return self._backend.finish(claim, result, now)

        now_utc = now.astimezone(UTC)
        jk = claim.job_key

        if result.outcome == "success" and not result.output_refs:
            raise InvalidResultError("outcome='success' requires non-empty output_refs")

        # jobs.cause_code is VARCHAR(64) on Postgres: normalize (full text is logged).
        result = result.model_copy(update={"cause_code": normalize_cause_code(result.cause_code)})

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, expires_at FROM claims WHERE lease_id = %s FOR UPDATE",
                        (claim.lease_id,),
                    )
                    claim_row = cur.fetchone()
                    if not claim_row or claim_row[0] != "active":
                        raise StaleLeaseError(f"Lease '{claim.lease_id}' is not active.")

                    exp_at = claim_row[1] if hasattr(claim_row[1], "astimezone") else datetime.fromisoformat(str(claim_row[1])).astimezone(UTC)
                    if exp_at < now_utc:
                        raise StaleLeaseError("Lease has expired.")

                    cur.execute(
                        "SELECT fencing_token, current_lease_id, retry_count, max_retries FROM jobs WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s FOR UPDATE",
                        (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
                    )
                    job_row = cur.fetchone()
                    if not job_row or job_row[0] != claim.fencing_token or job_row[1] != claim.lease_id:
                        raise StaleLeaseError("Fencing token mismatch on finish.")

                    # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
                    if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                        cur.execute(
                            "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = %s FOR UPDATE",
                            (claim.reservation_id,),
                        )
                        res_row = cur.fetchone()
                        if res_row:
                            st, exp_at = res_row[0], res_row[1]
                            res_expires = exp_at if hasattr(exp_at, "astimezone") else datetime.fromisoformat(str(exp_at))
                            res_expires = res_expires.astimezone(UTC) if res_expires.tzinfo else res_expires.replace(tzinfo=UTC)
                            if st not in ("active", "draining") or res_expires < now_utc:
                                raise StaleLeaseError(f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({st}). Old owner cannot publish.")

                    if result.outcome == "success":
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'succeeded',
                                cause_code = %s,
                                actual_cost = actual_cost + %s,
                                output_refs = %s::jsonb,
                                evidence_refs = %s::jsonb,
                                finished_at = %s,
                                updated_at = %s,
                                current_lease_id = NULL
                            WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                              AND stage = %s AND iteration = %s AND fencing_token = %s
                            """,
                            (
                                result.cause_code,
                                result.actual_cost,
                                json.dumps(result.output_refs),
                                json.dumps(result.evidence_refs),
                                now_utc,
                                now_utc,
                                jk.run_id,
                                jk.ticket_id,
                                jk.plan_version,
                                jk.stage,
                                jk.iteration,
                                claim.fencing_token,
                            ),
                        )
                        cur.execute("UPDATE claims SET status = 'released' WHERE lease_id = %s", (claim.lease_id,))

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
                            ) VALUES ('job_finished', 'run', %s, %s::jsonb, 'cloud_dbos', 'pending', 0, %s)
                            """,
                            (jk.run_id, outbox_payload, now_utc),
                        )
                    elif result.outcome == "retry":
                        retry_count, max_retries = job_row[2], job_row[3]
                        if retry_count < max_retries:
                            cur.execute(
                                """
                                UPDATE jobs
                                SET status = 'pending',
                                    retry_count = retry_count + 1,
                                    cause_code = %s,
                                    current_lease_id = NULL,
                                    updated_at = %s
                                WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                                  AND stage = %s AND iteration = %s AND fencing_token = %s
                                """,
                                (result.cause_code, now_utc, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                            )
                        else:
                            cur.execute(
                                """
                                UPDATE jobs
                                SET status = 'failed',
                                    cause_code = %s,
                                    finished_at = %s,
                                    updated_at = %s,
                                    current_lease_id = NULL
                                WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                                  AND stage = %s AND iteration = %s AND fencing_token = %s
                                """,
                                (result.cause_code or "MAX_RETRIES_EXCEEDED", now_utc, now_utc, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                            )
                        cur.execute("UPDATE claims SET status = 'released' WHERE lease_id = %s", (claim.lease_id,))
                    else:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = %s,
                                cause_code = %s,
                                finished_at = %s,
                                updated_at = %s,
                                current_lease_id = NULL
                            WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                              AND stage = %s AND iteration = %s AND fencing_token = %s
                            """,
                            (result.outcome, result.cause_code, now_utc, now_utc, jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration, claim.fencing_token),
                        )
                        cur.execute("UPDATE claims SET status = 'released' WHERE lease_id = %s", (claim.lease_id,))

                    if result.outcome != "success" and result.evidence_refs:
                        # Same contract as the SQLite store: a failed/retry/waiting stage keeps its evidence
                        # refs (failing test ids, log names); empty refs never clobber a stored value.
                        cur.execute(
                            """
                            UPDATE jobs SET evidence_refs = %s::jsonb
                            WHERE run_id = %s AND ticket_id = %s AND plan_version = %s
                              AND stage = %s AND iteration = %s AND fencing_token = %s
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
        except (StaleLeaseError, InvalidResultError):
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL finish failed: {exc}") from exc

    def materialize(self, event: dict[str, Any], now: datetime) -> None:
        if self.mock_mode:
            return self._backend.materialize(event, now)

        now_utc = now.astimezone(UTC)
        outbox_id = event.get("outbox_id") or event.get("event_id")
        if outbox_id is None:
            raise OutboxNotFoundError("Event missing outbox_id.")

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE outbox SET status = 'published', published_at = %s WHERE outbox_id = %s",
                        (now_utc, int(outbox_id)),
                    )
                    if cur.rowcount == 0:
                        cur.execute("SELECT status FROM outbox WHERE outbox_id = %s", (int(outbox_id),))
                        if not cur.fetchone():
                            raise OutboxNotFoundError(f"Outbox event '{outbox_id}' not found.")
                    conn.commit()
        except OutboxNotFoundError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL materialize failed: {exc}") from exc

    def reconcile(self, now: datetime, cursor: str | None = None, limit: int = 100) -> ReconcilePage:
        if self.mock_mode:
            return self._backend.reconcile(now, cursor, limit)

        now_utc = now.astimezone(UTC)
        cycle_id = f"cycle-{uuid4().hex[:12]}"
        repaired_keys: list[dict[str, Any]] = []

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT lease_id, run_id, ticket_id, plan_version, stage, iteration, fencing_token
                        FROM claims
                        WHERE status = 'active' AND expires_at < %s
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                        """,
                        (now_utc, limit),
                    )
                    expired_claims = cur.fetchall()

                    for c in expired_claims:
                        lease_id, run_id, ticket_id, plan_version, stage, iteration, claim_token = c
                        cur.execute("UPDATE claims SET status = 'expired' WHERE lease_id = %s", (lease_id,))

                        cur.execute(
                            "SELECT fencing_token, retry_count, max_retries FROM jobs WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s FOR UPDATE",
                            (run_id, ticket_id, plan_version, stage, iteration),
                        )
                        job_row = cur.fetchone()
                        if job_row and job_row[0] == claim_token:
                            new_token = job_row[0] + 1
                            target_key = {
                                "run_id": run_id,
                                "ticket_id": ticket_id,
                                "plan_version": plan_version,
                                "stage": stage,
                                "iteration": iteration,
                            }
                            if job_row[1] < job_row[2]:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET status = 'pending',
                                        fencing_token = %s,
                                        current_lease_id = NULL,
                                        retry_count = retry_count + 1,
                                        cause_code = 'LEASE_EXPIRED',
                                        updated_at = %s
                                    WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                                    """,
                                    (new_token, now_utc, run_id, ticket_id, plan_version, stage, iteration),
                                )
                                action = "requeued_job"
                            else:
                                cur.execute(
                                    """
                                    UPDATE jobs
                                    SET status = 'failed',
                                        fencing_token = %s,
                                        current_lease_id = NULL,
                                        cause_code = 'MAX_RETRIES_EXCEEDED',
                                        finished_at = %s,
                                        updated_at = %s
                                    WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                                    """,
                                    (new_token, now_utc, now_utc, run_id, ticket_id, plan_version, stage, iteration),
                                )
                                action = "failed_job"

                            cur.execute(
                                """
                                INSERT INTO reconciliation_ledger (
                                    cycle_id, action_type, target_key, reason, status, recorded_at
                                ) VALUES (%s, %s, %s::jsonb, 'lease_expired', 'success', %s)
                                """,
                                (cycle_id, action, json.dumps(target_key), now_utc),
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
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL reconcile failed: {exc}") from exc

    def get_operation(self, key: str) -> ExternalOperation | None:
        if self.mock_mode:
            return self._backend.get_operation(key)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT operation_key, request_digest, provider, external_id, status,
                               claim_lease_id, fencing_token, observed_at, response_digest, error_details
                        FROM external_operations
                        WHERE operation_key = %s
                        """,
                        (key,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    return ExternalOperation(
                        operation_key=row[0],
                        request_digest=row[1],
                        provider=row[2],
                        external_id=row[3],
                        status=row[4],
                        claim_lease_id=row[5],
                        fencing_token=row[6],
                        observed_at=row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                        response_digest=row[8],
                        error_details=row[9],
                    )
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL get_operation failed: {exc}") from exc

    def record_operation(self, operation: ExternalOperation, claim: Claim) -> None:
        if self.mock_mode:
            return self._backend.record_operation(operation, claim)

        now_utc = datetime.now(UTC)
        jk = claim.job_key

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, expires_at, fencing_token FROM claims WHERE lease_id = %s FOR UPDATE",
                        (claim.lease_id,),
                    )
                    claim_row = cur.fetchone()
                    if not claim_row or claim_row[0] != "active":
                        raise StaleLeaseError("Claim is not active.")
                    if claim_row[2] != claim.fencing_token:
                        raise StaleLeaseError("Fencing token mismatch.")

                    cur.execute(
                        "SELECT fencing_token, current_lease_id FROM jobs WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s FOR UPDATE",
                        (jk.run_id, jk.ticket_id, jk.plan_version, jk.stage, jk.iteration),
                    )
                    job_row = cur.fetchone()
                    if not job_row or job_row[0] != claim.fencing_token or job_row[1] != claim.lease_id:
                        raise StaleLeaseError("Job fencing token mismatch.")

                    # If claim belongs to a fleet cycle reservation, verify reservation is active and valid (USR-281)
                    if claim.reservation_id and claim.reservation_id.startswith("fres-"):
                        cur.execute(
                            "SELECT status, expires_at FROM fleet_reservations WHERE reservation_id = %s FOR UPDATE",
                            (claim.reservation_id,),
                        )
                        res_row = cur.fetchone()
                        if res_row:
                            st, exp_at = res_row[0], res_row[1]
                            res_expires = exp_at if hasattr(exp_at, "astimezone") else datetime.fromisoformat(str(exp_at))
                            res_expires = res_expires.astimezone(UTC) if res_expires.tzinfo else res_expires.replace(tzinfo=UTC)
                            if st not in ("active", "draining") or res_expires < now_utc:
                                raise StaleLeaseError(f"Fleet cycle reservation '{claim.reservation_id}' is no longer active ({st}). Old owner cannot record operation.")

                    cur.execute(
                        """
                        INSERT INTO external_operations (
                            operation_key, request_digest, provider, external_id, status,
                            claim_lease_id, fencing_token, observed_at, response_digest, error_details
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(operation_key) DO UPDATE SET
                            request_digest = EXCLUDED.request_digest,
                            provider = EXCLUDED.provider,
                            external_id = EXCLUDED.external_id,
                            status = EXCLUDED.status,
                            claim_lease_id = EXCLUDED.claim_lease_id,
                            fencing_token = EXCLUDED.fencing_token,
                            observed_at = EXCLUDED.observed_at,
                            response_digest = EXCLUDED.response_digest,
                            error_details = EXCLUDED.error_details
                        """,
                        (
                            operation.operation_key,
                            operation.request_digest,
                            operation.provider,
                            operation.external_id,
                            operation.status,
                            claim.lease_id,
                            claim.fencing_token,
                            now_utc,
                            operation.response_digest,
                            operation.error_details,
                        ),
                    )
                    conn.commit()
        except StaleLeaseError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL record_operation failed: {exc}") from exc

    def emit_outbox(self, event: OutboxEvent, now: datetime) -> OutboxEvent:
        if self.mock_mode:
            return self._backend.emit_outbox(event, now)

        now_utc = now.astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO outbox (
                            event_type, aggregate_type, aggregate_id, payload,
                            target_system, status, retry_count, created_at, published_at, error_message
                        ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
                        RETURNING outbox_id
                        """,
                        (
                            event.event_type,
                            event.aggregate_type,
                            event.aggregate_id or (event.run_id or ""),
                            json.dumps(event.payload),
                            event.target_system,
                            event.status,
                            event.retry_count,
                            now_utc,
                            event.published_at,
                            event.error_message,
                        ),
                    )
                    outbox_id = cur.fetchone()[0]
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
                        created_at=now_utc.isoformat(),
                        published_at=event.published_at,
                        error_message=event.error_message,
                    )
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL emit_outbox failed: {exc}") from exc

    def get_pending_outbox(self, limit: int = 100) -> list[dict[str, Any]]:
        """Query pending outbox events for publication or processing."""
        if self.mock_mode:
            return self._backend.get_pending_outbox(limit=limit)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT outbox_id, event_type, aggregate_type, aggregate_id, payload,
                               target_system, status, retry_count, created_at
                        FROM outbox
                        WHERE status = 'pending'
                        ORDER BY created_at ASC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = cur.fetchall()
                    out = []
                    for r in rows:
                        p = r[4] if isinstance(r[4], dict) else json.loads(r[4])
                        c_at = r[8].isoformat() if hasattr(r[8], "isoformat") else str(r[8])
                        out.append({
                            "outbox_id": r[0],
                            "event_type": r[1],
                            "aggregate_type": r[2],
                            "aggregate_id": r[3],
                            "payload": p,
                            "target_system": r[5],
                            "status": r[6],
                            "retry_count": r[7],
                            "created_at": c_at,
                        })
                    return out
        except Exception as exc:
            logger.warning("PostgreSQL get_pending_outbox failed: %s", exc)
            return []

    def get_run_identity(self, run_id: str) -> dict[str, str] | None:
        """Read identity without a telemetry join or a silent unavailable/missing conflation."""
        if self.mock_mode:
            return self._backend.get_run_identity(run_id)
        try:
            with self._psycopg.connect(
                self.raw_url, connect_timeout=5, options="-c statement_timeout=5000"
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT project_id FROM runs WHERE run_id = %s", (run_id,))
                    row = cur.fetchone()
                    return {"project_id": row[0]} if row is not None else None
        except Exception as exc:
            raise StoreUnavailableError("Run identity lookup unavailable") from exc

    def get_manual_ticket_owner(self, project_id: str, ticket_id: str, *, now: datetime) -> str | None:
        """Read manual ownership from the shared progress table, failing closed on outages."""
        from core.line.manual_ownership import active_manual_owner

        if self.mock_mode:
            return self._backend.get_manual_ticket_owner(project_id, ticket_id, now=now)
        try:
            with self._psycopg.connect(
                self.raw_url, connect_timeout=5, options="-c statement_timeout=5000"
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT to_regclass('local_run_events')")
                    if cur.fetchone()[0] is None:
                        return None  # Explicit pre-local-progress compatibility.
                    cur.execute(
                        "SELECT run_id, phase, status, at FROM local_run_events "
                        "WHERE project_id = %s AND ticket_id = %s ORDER BY event_id DESC",
                        (project_id, ticket_id),
                    )
                    rows = [dict(zip(("run_id", "phase", "status", "at"), row)) for row in cur.fetchall()]
                    return active_manual_owner(rows, now=now)
        except Exception as exc:
            raise StoreUnavailableError("Manual ownership lookup unavailable") from exc

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        """Query full status of a run and its jobs."""
        if self.mock_mode:
            conn = self._backend._connect()
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
                    if isinstance(job_d.get("output_refs"), str):
                        try:
                            job_d["output_refs"] = json.loads(job_d["output_refs"])
                        except Exception:
                            job_d["output_refs"] = []
                    if isinstance(job_d.get("evidence_refs"), str):
                        try:
                            job_d["evidence_refs"] = json.loads(job_d["evidence_refs"])
                        except Exception:
                            job_d["evidence_refs"] = []
                    jobs.append(job_d)
                res = dict(run_row)
                res["jobs"] = jobs
                return res
            finally:
                conn.close()

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT run_id, project_id, demand_id, demand_version, runtime_owner,
                               mode, status, plan_digest, config_version, created_at, updated_at, completed_at
                        FROM runs
                        WHERE run_id = %s
                        """,
                        (run_id,),
                    )
                    r = cur.fetchone()
                    if not r:
                        return None
                    run_info = {
                        "run_id": r[0],
                        "project_id": r[1],
                        "demand_id": r[2],
                        "demand_version": r[3],
                        "runtime_owner": r[4],
                        "mode": r[5],
                        "status": r[6],
                        "plan_digest": r[7],
                        "config_version": r[8],
                        "created_at": r[9].isoformat() if hasattr(r[9], "isoformat") else str(r[9]),
                        "updated_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]),
                        "completed_at": r[11].isoformat() if r[11] and hasattr(r[11], "isoformat") else (str(r[11]) if r[11] else None),
                    }
                    cur.execute(
                        """
                        SELECT ticket_id, plan_version, stage, iteration, status, role,
                               fencing_token, current_lease_id, actual_cost, output_refs, evidence_refs,
                               created_at, updated_at, started_at, finished_at,
                               cause_code, retry_count
                        FROM jobs
                        WHERE run_id = %s
                        ORDER BY created_at ASC
                        """,
                        (run_id,),
                    )
                    jobs = []
                    for j in cur.fetchall():
                        o_refs = j[9] if isinstance(j[9], list) else (json.loads(j[9]) if j[9] else [])
                        e_refs = j[10] if isinstance(j[10], list) else (json.loads(j[10]) if j[10] else [])
                        jobs.append({
                            "ticket_id": j[0],
                            "plan_version": j[1],
                            "stage": j[2],
                            "iteration": j[3],
                            "status": j[4],
                            "role": j[5],
                            "fencing_token": j[6],
                            "current_lease_id": j[7],
                            "actual_cost": float(j[8]) if j[8] is not None else 0.0,
                            "output_refs": o_refs,
                            "evidence_refs": e_refs,
                            "created_at": j[11].isoformat() if hasattr(j[11], "isoformat") else str(j[11]),
                            "updated_at": j[12].isoformat() if hasattr(j[12], "isoformat") else str(j[12]),
                            "started_at": j[13].isoformat() if j[13] and hasattr(j[13], "isoformat") else (str(j[13]) if j[13] else None),
                            "finished_at": j[14].isoformat() if j[14] and hasattr(j[14], "isoformat") else (str(j[14]) if j[14] else None),
                            # jobs.cause_code / retry_count were never selected here, so every
                            # Postgres-backed reader (the canary alert: "cause_code=None") lost why
                            # a stage failed even though the row carries it.
                            "cause_code": j[15],
                            "retry_count": j[16],
                        })
                    run_info["jobs"] = jobs
                    return run_info
        except Exception as exc:
            logger.warning("PostgreSQL get_run_status failed: %s", exc)
            return None

    def materialize(self, event: dict[str, Any], now: datetime) -> None:
        """Confirm processing and dispatch of an outbox event."""
        if self.mock_mode:
            return self._backend.materialize(event, now)

        now_utc = now.astimezone(UTC)
        outbox_id = event.get("outbox_id") or event.get("event_id")
        if outbox_id is None:
            raise OutboxNotFoundError("Event dictionary must contain 'outbox_id' or 'event_id'.")

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE outbox SET status = 'published', published_at = %s WHERE outbox_id = %s",
                        (now_utc, int(outbox_id)),
                    )
                    if cur.rowcount == 0:
                        cur.execute("SELECT status FROM outbox WHERE outbox_id = %s", (int(outbox_id),))
                        if not cur.fetchone():
                            raise OutboxNotFoundError(f"Outbox event '{outbox_id}' not found.")
                    conn.commit()
        except OutboxNotFoundError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL materialize failed: {exc}") from exc

    def list_active_projects(self, cursor: str | None = None, limit: int = 100) -> tuple[list[str], str | None]:
        """Stable paginated query of active project IDs ordered deterministically."""
        if self.mock_mode:
            return self._backend.list_active_projects(cursor=cursor, limit=limit)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    if cursor:
                        cur.execute(
                            """
                            SELECT DISTINCT project_id FROM runs
                            WHERE status = 'active' AND project_id > %s
                            ORDER BY project_id ASC
                            LIMIT %s
                            """,
                            (cursor, limit),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT DISTINCT project_id FROM runs
                            WHERE status = 'active'
                            ORDER BY project_id ASC
                            LIMIT %s
                            """,
                            (limit,),
                        )
                    rows = [r[0] for r in cur.fetchall()]
                    next_cursor = rows[-1] if len(rows) == limit else None
                    return rows, next_cursor
        except Exception as exc:
            logger.warning("PostgreSQL list_active_projects failed: %s", exc)
            return [], None

    def get_run_payload(self, run_id: str) -> dict[str, Any] | None:
        """The `IntakeCommand.payload` that created `run_id` (HF-27-08 bindings.py)."""
        if self.mock_mode:
            return self._backend.get_run_payload(run_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT payload FROM intake_commands WHERE run_id = %s", (run_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning("PostgreSQL get_run_payload failed: %s", exc)
            return None

    def find_intake_runs(self, channel: str, external_id: str) -> list[tuple[str, str | None]]:
        """`(external_id, run_id)` of every intake for `channel` whose external id is `external_id`
        or one of its retry attempts (`<external_id>:a<n>`), oldest first."""
        if self.mock_mode:
            return self._backend.find_intake_runs(channel, external_id)

        escaped = external_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT external_id, run_id FROM intake_commands "
                        "WHERE channel = %s AND (external_id = %s OR external_id LIKE %s) ORDER BY committed_at, external_id",
                        (channel, external_id, f"{escaped}:a%"),
                    )
                    return [(row[0], row[1]) for row in cur.fetchall()]
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL find_intake_runs failed: {exc}") from exc

    def record_grill_answer(self, run_id: str, question_id: str, choice: str, now: datetime) -> None:
        """Record an owner's grill answer for `(run_id, question_id)` (last answer wins)."""
        if self.mock_mode:
            return self._backend.record_grill_answer(run_id, question_id, choice, now)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO grill_answers (run_id, question_id, choice, recorded_at) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (run_id, question_id) DO UPDATE SET choice = EXCLUDED.choice, recorded_at = EXCLUDED.recorded_at
                        """,
                        (run_id, question_id, choice, now.astimezone(UTC)),
                    )
                conn.commit()
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL record_grill_answer failed: {exc}") from exc

    def get_grill_answers(self, run_id: str) -> dict[str, str]:
        """Grill answers recorded for `run_id`, as `{question_id: choice}` (raw callback choice)."""
        if self.mock_mode:
            return self._backend.get_grill_answers(run_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT question_id, choice FROM grill_answers WHERE run_id = %s", (run_id,))
                    return {row[0]: row[1] for row in cur.fetchall()}
        except Exception as exc:
            logger.warning("PostgreSQL get_grill_answers failed: %s", exc)
            return {}

    def find_job(self, run_id: str, stage: str, status: str | None = None) -> JobKey | None:
        """The highest-iteration job for `(run_id, stage)`, optionally filtered by `status`."""
        if self.mock_mode:
            return self._backend.find_job(run_id, stage, status=status)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    if status:
                        cur.execute(
                            """
                            SELECT ticket_id, plan_version, iteration FROM jobs
                            WHERE run_id = %s AND stage = %s AND status = %s
                            ORDER BY iteration DESC LIMIT 1
                            """,
                            (run_id, stage, status),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT ticket_id, plan_version, iteration FROM jobs
                            WHERE run_id = %s AND stage = %s
                            ORDER BY iteration DESC LIMIT 1
                            """,
                            (run_id, stage),
                        )
                    row = cur.fetchone()
                    if not row:
                        return None
                    return JobKey(run_id=run_id, ticket_id=row[0], plan_version=row[1], stage=stage, iteration=row[2])
        except Exception as exc:
            logger.warning("PostgreSQL find_job failed: %s", exc)
            return None

    def resume_job(self, job_key: JobKey, now: datetime) -> bool:
        """Transition a `waiting_human`/`waiting_dependency` job back to `pending` (HF-27-08 D-d)."""
        if self.mock_mode:
            return self._backend.resume_job(job_key, now)

        now_utc = now.astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'pending', ready_at = %s, not_before = NULL, updated_at = %s
                        WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                          AND status IN ('waiting_human', 'waiting_dependency')
                        """,
                        (
                            now_utc,
                            now_utc,
                            job_key.run_id,
                            job_key.ticket_id,
                            job_key.plan_version,
                            job_key.stage,
                            job_key.iteration,
                        ),
                    )
                    resumed = cur.rowcount > 0
                    if resumed:
                        # Heal legacy rows: a run closed as `completed` while this job
                        # was parked is live again (idempotent; same transaction).
                        cur.execute(
                            "UPDATE runs SET status = 'active', completed_at = NULL, updated_at = %s "
                            "WHERE run_id = %s AND status = 'completed'",
                            (now_utc, job_key.run_id),
                        )
                    conn.commit()
                    return resumed
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL resume_job failed: {exc}") from exc

    def add_job_evidence(self, job_key: JobKey, ref: str) -> bool:
        """Append `ref` to the `evidence_refs` of a `waiting_human` job; idempotent (USR-155).

        `True` when the ref is on the job afterwards, `False` for an unknown or non-`waiting_human` job.
        """
        if self.mock_mode:
            return self._backend.add_job_evidence(job_key, ref)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE jobs
                        SET evidence_refs = COALESCE(evidence_refs, '[]'::jsonb) || jsonb_build_array(%s::text)
                        WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                          AND status = 'waiting_human'
                          AND NOT (COALESCE(evidence_refs, '[]'::jsonb) @> jsonb_build_array(%s::text))
                        """,
                        (
                            ref,
                            job_key.run_id,
                            job_key.ticket_id,
                            job_key.plan_version,
                            job_key.stage,
                            job_key.iteration,
                            ref,
                        ),
                    )
                    changed = cur.rowcount > 0
                    if not changed:
                        # Either already marked (fine) or not a waiting_human job (not fine).
                        cur.execute(
                            """
                            SELECT 1 FROM jobs
                            WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s
                              AND iteration = %s AND status = 'waiting_human'
                            """,
                            (job_key.run_id, job_key.ticket_id, job_key.plan_version, job_key.stage, job_key.iteration),
                        )
                        changed = cur.fetchone() is not None
                    conn.commit()
                    return changed
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL add_job_evidence failed: {exc}") from exc

    def list_waiting_jobs(
        self, statuses: tuple[str, ...] = ("waiting_human", "waiting_dependency")
    ) -> list[JobKey]:
        """List active jobs parked in waiting states."""
        if self.mock_mode:
            return self._backend.list_waiting_jobs(statuses=statuses)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT run_id, ticket_id, plan_version, stage, iteration
                        FROM jobs
                        WHERE status = ANY(%s)
                        ORDER BY created_at ASC
                        """,
                        (list(statuses),),
                    )
                    return [
                        JobKey(
                            run_id=row[0],
                            ticket_id=row[1],
                            plan_version=row[2],
                            stage=row[3],
                            iteration=row[4],
                        )
                        for row in cur.fetchall()
                    ]
        except Exception as exc:
            logger.warning("PostgreSQL list_waiting_jobs failed: %s", exc)
            return []

    def list_active_run_ids(self) -> list[str]:
        """Ids of every run whose `runs.status` is `active`, oldest first (USR-142 orphan-run sweep)."""
        if self.mock_mode:
            return self._backend.list_active_run_ids()

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT run_id FROM runs WHERE status = 'active' ORDER BY created_at ASC")
                    return [str(row[0]) for row in cur.fetchall()]
        except Exception as exc:
            logger.warning("PostgreSQL list_active_run_ids failed: %s", exc)
            return []

    def list_project_runs(self, project_id: str, limit: int = 120) -> list[tuple[str, str]]:
        """`(run_id, created_at ISO)` of the newest runs of `project_id` (read-only, newest first)."""
        if self.mock_mode:
            return self._backend.list_project_runs(project_id, limit)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT run_id, created_at FROM runs WHERE project_id = %s ORDER BY created_at DESC LIMIT %s",
                        (project_id, int(limit)),
                    )
                    return [
                        (str(r[0]), r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]))
                        for r in cur.fetchall()
                    ]
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL list_project_runs failed: {exc}") from exc

    def max_iteration(self, run_id: str, ticket_id: str, plan_version: str, stage: str) -> int:
        """Highest `iteration` already recorded for `(run_id, ticket_id, plan_version, stage)`, or -1."""
        if self.mock_mode:
            return self._backend.max_iteration(run_id, ticket_id, plan_version, stage)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT MAX(iteration) FROM jobs WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s",
                        (run_id, ticket_id, plan_version, stage),
                    )
                    row = cur.fetchone()
                    value = row[0] if row else None
                    return int(value) if value is not None else -1
        except Exception as exc:
            logger.warning("PostgreSQL max_iteration failed: %s", exc)
            return -1

    def get_run_created_at(self, run_id: str) -> str | None:
        """ISO `created_at` of `run_id`'s `runs` row, or `None` if unknown."""
        if self.mock_mode:
            return self._backend.get_run_created_at(run_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT created_at FROM runs WHERE run_id = %s", (run_id,))
                    row = cur.fetchone()
                    if not row or row[0] is None:
                        return None
                    return row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])
        except Exception as exc:
            logger.warning("PostgreSQL get_run_created_at failed: %s", exc)
            return None

    def get_latest_success_output_refs(self, run_id: str, exclude_stage: str | None = None) -> list[str]:
        """`output_refs` of the most recently succeeded job for `run_id` (HF-27-08 D-f)."""
        if self.mock_mode:
            return self._backend.get_latest_success_output_refs(run_id, exclude_stage=exclude_stage)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    if exclude_stage:
                        cur.execute(
                            """
                            SELECT output_refs FROM jobs
                            WHERE run_id = %s AND status = 'succeeded' AND stage != %s
                            ORDER BY finished_at DESC, updated_at DESC
                            LIMIT 1
                            """,
                            (run_id, exclude_stage),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT output_refs FROM jobs
                            WHERE run_id = %s AND status = 'succeeded'
                            ORDER BY finished_at DESC, updated_at DESC
                            LIMIT 1
                            """,
                            (run_id,),
                        )
                    row = cur.fetchone()
                    if not row:
                        return []
                    refs = row[0] if isinstance(row[0], list) else json.loads(row[0])
                    return refs if isinstance(refs, list) else []
        except Exception as exc:
            logger.warning("PostgreSQL get_latest_success_output_refs failed: %s", exc)
            return []

    def get_ready_age_metrics(self, now: datetime) -> dict[str, Any]:
        """Query ready-age and claim distribution for pending jobs."""
        if self.mock_mode:
            return self._backend.get_ready_age_metrics(now)

        now_utc = now.astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM runs WHERE status = 'active'")
                    row = cur.fetchone()
                    active_runs = row[0] if row else 0

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

                    for r in rows:
                        status = r[4]
                        # HF-27-08 D-d: ready-age counts from the last time the job
                        # became claimable (ready_at), falling back to created_at
                        # for a job that has never been through waiting_* -> pending.
                        ready_raw = r[7] or r[5]
                        created_dt = ready_raw if hasattr(ready_raw, "astimezone") else datetime.fromisoformat(str(ready_raw)).astimezone(UTC)
                        age_sec = max(0.0, (now_utc - created_dt).total_seconds())
                        if status == "pending":
                            pending_count += 1
                            if age_sec > max_ready_age_sec:
                                max_ready_age_sec = age_sec
                            if age_sec >= 30.0:
                                starved_projects.add(r[1])
                        elif status == "running":
                            running_count += 1

                    return {
                        "active_runs": active_runs,
                        "pending_count": pending_count,
                        "running_count": running_count,
                        "max_ready_age_sec": max_ready_age_sec,
                        "starved_projects": sorted(starved_projects),
                    }
        except Exception as exc:
            logger.warning("PostgreSQL get_ready_age_metrics failed: %s", exc)
            return {
                "active_runs": 0,
                "pending_count": 0,
                "running_count": 0,
                "max_ready_age_sec": 0.0,
                "starved_projects": [],
            }

    def cancel_run(
        self,
        run_id: str,
        *,
        reason: str = "owner_cancelled",
        actor: str = "owner",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Cancels a line run and all its open jobs with audit logging (USR-123)."""
        if self.mock_mode:
            return self._backend.cancel_run(run_id, reason=reason, actor=actor, now=now)

        current_time = now or datetime.now(UTC)
        now_utc = current_time.astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT run_id, status FROM runs WHERE run_id = %s", (run_id,))
                    row = cur.fetchone()
                    if not row:
                        return {"ok": False, "error": f"run not found: {run_id}", "run_id": run_id}

                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = 'cancelled',
                            cause_code = %s,
                            finished_at = %s,
                            updated_at = %s,
                            current_lease_id = NULL
                        WHERE run_id = %s AND status = ANY(%s)
                        """,
                        (reason, now_utc, now_utc, run_id, list(RUN_OPEN_JOB_STATUSES)),
                    )
                    jobs_cancelled = cur.rowcount

                    cur.execute(
                        "UPDATE claims SET status = 'released' WHERE run_id = %s AND status = 'active'",
                        (run_id,),
                    )

                    # 2b. Cancel active fleet reservations for this run (USR-281)
                    cur.execute(
                        "UPDATE fleet_reservations SET status = 'cancelled', heartbeat_at = %s WHERE run_id = %s AND status IN ('active', 'draining')",
                        (now_utc, run_id),
                    )

                    cur.execute(
                        "UPDATE runs SET status = 'cancelled', completed_at = %s, updated_at = %s WHERE run_id = %s",
                        (now_utc, now_utc, run_id),
                    )

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
                        ) VALUES ('run_cancelled', 'run', %s, %s, 'audit', 'pending', 0, %s)
                        """,
                        (run_id, audit_payload, now_utc),
                    )
                    conn.commit()
                    return {
                        "ok": True,
                        "run_id": run_id,
                        "jobs_cancelled": jobs_cancelled,
                        "message": f"Run {run_id} cancelado com sucesso ({jobs_cancelled} jobs cancelados).",
                    }
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL cancel_run failed: {exc}") from exc

    @staticmethod
    def _row_to_reservation(row: tuple) -> FleetReservation:
        res_id, p_id, t_id, r_id, h_id, st, f_tok, ac_at, hb_at, exp_at, meta = row
        return FleetReservation(
            reservation_id=res_id,
            project_id=p_id,
            ticket_id=t_id,
            run_id=r_id,
            host_id=h_id,
            status=st,
            fencing_token=f_tok,
            acquired_at=ac_at.isoformat() if hasattr(ac_at, "isoformat") else str(ac_at),
            heartbeat_at=hb_at.isoformat() if hasattr(hb_at, "isoformat") else str(hb_at),
            expires_at=exp_at.isoformat() if hasattr(exp_at, "isoformat") else str(exp_at),
            metadata=meta if isinstance(meta, dict) else {},
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
        if self.mock_mode:
            return self._backend.reserve_cycle(
                project_id=project_id,
                ticket_id=ticket_id,
                run_id=run_id,
                host_id=host_id,
                now=now,
                lease_duration_sec=lease_duration_sec,
                max_fleet_slots=max_fleet_slots,
                metadata=metadata,
            )

        now_utc = now.astimezone(UTC)
        expires_at_utc = now_utc + timedelta(seconds=lease_duration_sec)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    # Advisory transaction lock serializes concurrent ceiling checks
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext('darkfac_fleet_reservation_lock'))")

                    # 1. Sweep expired active reservations to release slots
                    cur.execute(
                        """
                        UPDATE fleet_reservations
                        SET status = 'expired'
                        WHERE status = 'active' AND expires_at < %s
                        """,
                        (now_utc,),
                    )

                    # 2. Check active ticket uniqueness
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE project_id = %s AND ticket_id = %s AND status = 'active'
                        FOR UPDATE
                        """,
                        (project_id, ticket_id),
                    )
                    ticket_row = cur.fetchone()
                    if ticket_row:
                        if ticket_row[3] == run_id and ticket_row[4] == host_id:
                            return self._row_to_reservation(ticket_row)
                        raise TicketAlreadyActiveError(
                            f"Ticket '{ticket_id}' in project '{project_id}' already has active reservation "
                            f"'{ticket_row[0]}' on host '{ticket_row[4]}'."
                        )

                    # 3. Check 1 cycle per host
                    cur.execute(
                        """
                        SELECT reservation_id, ticket_id, run_id
                        FROM fleet_reservations
                        WHERE host_id = %s AND status = 'active'
                        FOR UPDATE
                        """,
                        (host_id,),
                    )
                    host_row = cur.fetchone()
                    if host_row:
                        raise HostBusyError(
                            f"Host '{host_id}' already has active reservation '{host_row[0]}' "
                            f"for ticket '{host_row[1]}'."
                        )

                    # 4. Check fleet ceiling of max 3 active cycles
                    cur.execute(
                        "SELECT COUNT(*) FROM fleet_reservations WHERE status = 'active'"
                    )
                    count = cur.fetchone()[0]
                    if count >= max_fleet_slots:
                        raise FleetCapacityExceededError(
                            f"Fleet capacity ceiling of {max_fleet_slots} active cycles reached (currently {count} active)."
                        )

                    # 5. Insert new reservation
                    reservation_id = f"fres-{uuid4().hex[:12]}"
                    cur.execute(
                        """
                        INSERT INTO fleet_reservations (
                            reservation_id, project_id, ticket_id, run_id, host_id,
                            status, fencing_token, acquired_at, heartbeat_at, expires_at, metadata
                        ) VALUES (%s, %s, %s, %s, %s, 'active', 1, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            reservation_id,
                            project_id,
                            ticket_id,
                            run_id,
                            host_id,
                            now_utc,
                            now_utc,
                            expires_at_utc,
                            json.dumps(metadata or {}),
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
                        acquired_at=now_utc.isoformat(),
                        heartbeat_at=now_utc.isoformat(),
                        expires_at=expires_at_utc.isoformat(),
                        metadata=metadata or {},
                    )
        except ReservationConflictError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "uq_fleet_reservations_active_host" in msg or "host_id" in msg:
                raise HostBusyError(f"Host '{host_id}' already has active reservation (race condition).") from exc
            if "uq_fleet_reservations_active_ticket" in msg or "ticket_id" in msg:
                raise TicketAlreadyActiveError(f"Ticket '{ticket_id}' in project '{project_id}' already has active reservation (race condition).") from exc
            raise StoreUnavailableError(f"PostgreSQL reserve_cycle failed: {exc}") from exc

    def heartbeat_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        now: datetime,
        lease_duration_sec: int | None = None,
    ) -> FleetReservation:
        """Extend active cycle reservation lease (USR-281)."""
        if self.mock_mode:
            return self._backend.heartbeat_reservation(
                reservation_id=reservation_id,
                fencing_token=fencing_token,
                now=now,
                lease_duration_sec=lease_duration_sec,
            )

        now_utc = now.astimezone(UTC)
        dur = lease_duration_sec if lease_duration_sec is not None else self.lease_duration_sec
        new_expires_utc = now_utc + timedelta(seconds=dur)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE reservation_id = %s
                        FOR UPDATE
                        """,
                        (reservation_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise StaleLeaseError(f"Reservation '{reservation_id}' not found.")
                    st, f_tok, exp_at = row[5], row[6], row[9]
                    if st not in ("active", "draining"):
                        raise StaleLeaseError(f"Reservation '{reservation_id}' is in non-active status '{st}'.")
                    if f_tok != fencing_token:
                        raise StaleLeaseError(
                            f"Fencing token mismatch on heartbeat: token={fencing_token}, active={f_tok}."
                        )
                    res_expires = exp_at if hasattr(exp_at, "astimezone") else datetime.fromisoformat(str(exp_at))
                    res_expires = res_expires.astimezone(UTC) if res_expires.tzinfo else res_expires.replace(tzinfo=UTC)
                    if res_expires < now_utc:
                        cur.execute("UPDATE fleet_reservations SET status = 'expired' WHERE reservation_id = %s", (reservation_id,))
                        conn.commit()
                        raise StaleLeaseError(f"Reservation '{reservation_id}' expired at {res_expires.isoformat()}.")

                    cur.execute(
                        """
                        UPDATE fleet_reservations
                        SET heartbeat_at = %s, expires_at = %s
                        WHERE reservation_id = %s
                        """,
                        (now_utc, new_expires_utc, reservation_id),
                    )
                    conn.commit()
                    return FleetReservation(
                        reservation_id=row[0],
                        project_id=row[1],
                        ticket_id=row[2],
                        run_id=row[3],
                        host_id=row[4],
                        status=row[5],
                        fencing_token=row[6],
                        acquired_at=row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                        heartbeat_at=now_utc.isoformat(),
                        expires_at=new_expires_utc.isoformat(),
                        metadata=row[10] if isinstance(row[10], dict) else {},
                    )
        except StaleLeaseError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL heartbeat_reservation failed: {exc}") from exc

    def release_reservation(
        self,
        reservation_id: str,
        fencing_token: int,
        status: str = "released",
        now: datetime | None = None,
    ) -> FleetReservation:
        """Release active cycle reservation (USR-281)."""
        if self.mock_mode:
            return self._backend.release_reservation(
                reservation_id=reservation_id,
                fencing_token=fencing_token,
                status=status,
                now=now,
            )

        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE reservation_id = %s
                        FOR UPDATE
                        """,
                        (reservation_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        raise StaleLeaseError(f"Reservation '{reservation_id}' not found.")
                    st, f_tok = row[5], row[6]
                    if f_tok != fencing_token:
                        raise StaleLeaseError(
                            f"Fencing token mismatch on release: token={fencing_token}, active={f_tok}."
                        )
                    if st in ("released", "completed", "failed", "cancelled"):
                        return self._row_to_reservation(row)
                    if st == "expired":
                        raise StaleLeaseError(f"Reservation '{reservation_id}' has already expired and cannot be released.")

                    cur.execute(
                        "UPDATE fleet_reservations SET status = %s, heartbeat_at = %s WHERE reservation_id = %s",
                        (status, now_dt, reservation_id),
                    )
                    conn.commit()
                    return FleetReservation(
                        reservation_id=row[0],
                        project_id=row[1],
                        ticket_id=row[2],
                        run_id=row[3],
                        host_id=row[4],
                        status=status,
                        fencing_token=row[6],
                        acquired_at=row[7].isoformat() if hasattr(row[7], "isoformat") else str(row[7]),
                        heartbeat_at=now_dt.isoformat(),
                        expires_at=row[9].isoformat() if hasattr(row[9], "isoformat") else str(row[9]),
                        metadata=row[10] if isinstance(row[10], dict) else {},
                    )
        except StaleLeaseError:
            raise
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL release_reservation failed: {exc}") from exc

    def get_reservation(self, reservation_id: str) -> FleetReservation | None:
        """Retrieve reservation by ID (USR-281)."""
        if self.mock_mode:
            return self._backend.get_reservation(reservation_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE reservation_id = %s
                        """,
                        (reservation_id,),
                    )
                    row = cur.fetchone()
                    return self._row_to_reservation(row) if row else None
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL get_reservation failed: {exc}") from exc

    def get_active_reservation_for_ticket(
        self, project_id: str, ticket_id: str
    ) -> FleetReservation | None:
        """Find active reservation for a project ticket (USR-281)."""
        if self.mock_mode:
            return self._backend.get_active_reservation_for_ticket(project_id, ticket_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE project_id = %s AND ticket_id = %s AND status = 'active'
                        """,
                        (project_id, ticket_id),
                    )
                    row = cur.fetchone()
                    return self._row_to_reservation(row) if row else None
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL get_active_reservation_for_ticket failed: {exc}") from exc

    def get_active_reservation_for_host(self, host_id: str) -> FleetReservation | None:
        """Find active reservation currently occupying a host (USR-281)."""
        if self.mock_mode:
            return self._backend.get_active_reservation_for_host(host_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE host_id = %s AND status = 'active'
                        """,
                        (host_id,),
                    )
                    row = cur.fetchone()
                    return self._row_to_reservation(row) if row else None
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL get_active_reservation_for_host failed: {exc}") from exc

    def get_active_reservation_for_run(self, run_id: str) -> FleetReservation | None:
        """Find active reservation for a run (USR-282)."""
        if self.mock_mode:
            return self._backend.get_active_reservation_for_run(run_id)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE run_id = %s AND status IN ('active', 'draining')
                        """,
                        (run_id,),
                    )
                    row = cur.fetchone()
                    return self._row_to_reservation(row) if row else None
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL get_active_reservation_for_run failed: {exc}") from exc

    def list_active_reservations(self) -> list[FleetReservation]:
        """List all active fleet reservations (USR-281)."""
        if self.mock_mode:
            return self._backend.list_active_reservations()

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                               acquired_at, heartbeat_at, expires_at, metadata
                        FROM fleet_reservations
                        WHERE status IN ('active', 'draining')
                        ORDER BY acquired_at ASC
                        """
                    )
                    rows = cur.fetchall()
                    return [self._row_to_reservation(r) for r in rows]
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL list_active_reservations failed: {exc}") from exc

    def drain_host(self, host_id: str, reason: str, now: datetime) -> int:
        """Mark host and any active reservations for drain (USR-281)."""
        if self.mock_mode:
            return self._backend.drain_host(host_id=host_id, reason=reason, now=now)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE fleet_reservations
                        SET status = 'draining',
                            metadata = metadata || jsonb_build_object('drain_reason', %s)
                        WHERE host_id = %s AND status = 'active'
                        """,
                        (reason, host_id),
                    )
                    count = cur.rowcount
                    conn.commit()
                    return count
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL drain_host failed: {exc}") from exc

    def renew_open_run_reservations(
        self, now: datetime, pending_grace_sec: int = RESERVATION_PENDING_GRACE_SEC
    ) -> int:
        """Renew active reservations of runs between stages / waiting on dependencies (USR-306)."""
        if self.mock_mode:
            return self._backend.renew_open_run_reservations(now=now, pending_grace_sec=pending_grace_sec)

        now_utc = now.astimezone(UTC)
        expires_utc = now_utc + timedelta(seconds=self.lease_duration_sec)
        cutoff_utc = now_utc - timedelta(seconds=pending_grace_sec)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE fleet_reservations
                        SET heartbeat_at = %s, expires_at = %s
                        WHERE status = 'active' AND expires_at >= %s
                          AND run_id IN (
                              SELECT run_id FROM jobs
                              WHERE status = 'waiting_dependency'
                                 OR (status = 'pending' AND updated_at >= %s)
                          )
                        """,
                        (now_utc, expires_utc, now_utc, cutoff_utc),
                    )
                    renewed = cur.rowcount
                    conn.commit()
                    return int(renewed or 0)
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL renew_open_run_reservations failed: {exc}") from exc

    def reconcile_fleet_reservations(self, now: datetime) -> list[FleetReservation]:
        """Sweep expired fleet reservations and release slots (USR-281)."""
        if self.mock_mode:
            return self._backend.reconcile_fleet_reservations(now=now)

        now_utc = now.astimezone(UTC)
        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE fleet_reservations
                        SET status = 'expired'
                        WHERE status IN ('active', 'draining') AND expires_at < %s
                        RETURNING reservation_id, project_id, ticket_id, run_id, host_id, status, fencing_token,
                                  acquired_at, heartbeat_at, expires_at, metadata
                        """,
                        (now_utc,),
                    )
                    rows = cur.fetchall()
                    expired: list[FleetReservation] = []
                    for row in rows:
                        res = self._row_to_reservation(row)
                        expired.append(res)
                        cur.execute(
                            """
                            INSERT INTO reconciliation_ledger (
                                cycle_id, action_type, target_key, reason, status, recorded_at
                            ) VALUES (%s, 'expire_fleet_reservation', %s::jsonb, 'lease_expired', 'success', %s)
                            """,
                            (
                                f"recon-{uuid4().hex[:8]}",
                                json.dumps({"reservation_id": res.reservation_id, "ticket_id": res.ticket_id, "host_id": res.host_id}),
                                now_utc,
                            ),
                        )
                    conn.commit()
                    return expired
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL reconcile_fleet_reservations failed: {exc}") from exc

    def reconcile_unknown_operations(self, now: datetime) -> list[ExternalOperation]:
        """Sweep and report operations left in 'unknown' state for reconciliation before retry (USR-281)."""
        if self.mock_mode:
            return self._backend.reconcile_unknown_operations(now=now)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
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
                            operation_key=r[0],
                            request_digest=r[1],
                            provider=r[2],
                            external_id=r[3],
                            status=r[4],
                            claim_lease_id=r[5],
                            fencing_token=r[6],
                            observed_at=r[7].isoformat() if hasattr(r[7], "isoformat") else str(r[7]),
                            response_digest=r[8],
                            error_details=r[9],
                        )
                        for r in rows
                    ]
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL reconcile_unknown_operations failed: {exc}") from exc

    def list_eligible_admission_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List pending jobs of active runs eligible for fleet cycle admission (USR-282)."""
        if self.mock_mode:
            return self._backend.list_eligible_admission_jobs(limit=limit)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT j.run_id, j.ticket_id, r.project_id, j.stage, j.status, j.created_at
                        FROM jobs j
                        JOIN runs r ON j.run_id = r.run_id
                        WHERE j.status = 'pending' AND r.status = 'active'
                        ORDER BY j.created_at ASC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = cur.fetchall()
                    cols = [desc[0] for desc in cur.description]
                    return [dict(zip(cols, r)) for r in rows]
        except Exception as exc:
            raise StoreUnavailableError(f"PostgreSQL list_eligible_admission_jobs failed: {exc}") from exc
