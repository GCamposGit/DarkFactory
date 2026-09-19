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
    StoreUnavailableError,
)
from core.workflow.control_store import ControlStore, SQLiteControlStore

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
    PRIMARY KEY (run_id, ticket_id, plan_version, stage, iteration)
);

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

                        cur.execute(
                            """
                            INSERT INTO jobs (
                                run_id, ticket_id, plan_version, stage, iteration, status,
                                role, required_capabilities, fencing_token, timeout_seconds,
                                retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                                created_at, updated_at
                            ) VALUES (%s, %s, '1.0', 'grill', 0, 'pending', 'grill_engine', '[]'::jsonb, 0, 1800, 0, 3, 0.0, '[]'::jsonb, '[]'::jsonb, %s, %s)
                            """,
                            (run_id, command.project_id, now_utc, now_utc),
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

    def claim(self, worker: str, capabilities: list[str], now: datetime) -> Claim | None:
        if self.mock_mode:
            return self._backend.claim(worker, capabilities, now)

        now_utc = now.astimezone(UTC)
        expires_at_utc = now_utc + timedelta(seconds=self.lease_duration_sec)

        try:
            with self._psycopg.connect(self.raw_url) as conn:
                with conn.cursor() as cur:
                    # High concurrency locking via SELECT FOR UPDATE SKIP LOCKED
                    cur.execute(
                        """
                        SELECT run_id, ticket_id, plan_version, stage, iteration, fencing_token, role, required_capabilities
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
                        if all(c in capabilities for c in req):
                            chosen = r
                            break

                    if not chosen:
                        conn.commit()
                        return None

                    run_id, ticket_id, plan_version, stage, iteration, old_token, role, _ = chosen
                    new_token = old_token + 1
                    lease_id = f"lease-{uuid4().hex[:12]}"
                    reservation_id = f"res-{uuid4().hex[:8]}"
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
