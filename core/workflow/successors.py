"""Canonical successor materialization for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md and ticket HF-05-04.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from core.workflow.control_contracts import (
    InvalidResultError,
    JobKey,
    OutboxEvent,
    StageResult,
)
from core.workflow.control_store import ControlStore

logger = logging.getLogger(__name__)

LEARNING_STAGES: frozenset[str] = frozenset(
    {"memory_observation", "learning_eval", "promotion"}
)

PRODUCTIVE_DAG: dict[str, str] = {
    "grill": "planning",
    "environment": "development",
    "development": "validation",
    "validation": "independent_review",
    "independent_review": "integration",
    "integration": "build_deploy",
    "build_deploy": "target_journey",
    "memory_observation": "learning_eval",
}

STAGE_ROLES: dict[str, str] = {
    "grill": "grill_engine",
    "planning": "planner",
    "research": "researcher",
    "environment": "environment_probe",
    "development": "developer",
    "validation": "validator",
    "independent_review": "reviewer",
    "integration": "integrator",
    "build_deploy": "deployer",
    "target_journey": "journey_tester",
    "memory_observation": "memory_agent",
    "learning_eval": "evaluator",
    "catalog_refresh": "benchmarker",
}


def materialize_result(
    job_key: JobKey,
    result: StageResult,
    store: ControlStore,
    *,
    now: datetime | None = None,
    manifest_requires_environment: bool = False,
) -> list[JobKey]:
    """Atomically record result and materialize successors.

    Invariants:
    - Enforces fail-closed: outcome == "success" requires non-empty output_refs.
    - Transitions through canonical productive DAG.
    - Automatic memory fan-out: non-learning stages emit memory_observation.
    - Anti-recursion: stages in LEARNING_STAGES never spawn memory_observation.
    - Sibling isolation: WAITING_HUMAN outcome does not suspend or cancel siblings.
    - Idempotency: duplicate calls produce identical JobKey lists and converge safely.
    - If retry: increment iteration (iteration + 1).
    - If replan: enqueue planning stage.
    """
    if result.outcome == "success" and not result.output_refs:
        raise InvalidResultError(
            f"Stage '{job_key.stage}' succeeded with empty output_refs, violating fail-closed contract."
        )

    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()

    successors: list[JobKey] = []

    # 1. Determine successor list based on outcome
    if result.outcome == "success":
        # Productive transition
        next_stage: str | None = None
        if job_key.stage == "planning":
            needs_env = manifest_requires_environment or any(
                "environment" in ref.lower() for ref in result.output_refs
            )
            next_stage = "environment" if needs_env else "development"
        else:
            next_stage = PRODUCTIVE_DAG.get(job_key.stage)

        if next_stage is not None:
            successors.append(
                JobKey(
                    run_id=job_key.run_id,
                    ticket_id=job_key.ticket_id,
                    plan_version=job_key.plan_version,
                    stage=next_stage,
                    iteration=0,
                )
            )

        # Automatic memory fan-out with anti-recursion
        if job_key.stage not in LEARNING_STAGES:
            successors.append(
                JobKey(
                    run_id=job_key.run_id,
                    ticket_id=job_key.ticket_id,
                    plan_version=job_key.plan_version,
                    stage="memory_observation",
                    iteration=job_key.iteration,
                )
            )

    elif result.outcome == "retry":
        successors.append(
            JobKey(
                run_id=job_key.run_id,
                ticket_id=job_key.ticket_id,
                plan_version=job_key.plan_version,
                stage=job_key.stage,
                iteration=job_key.iteration + 1,
            )
        )

    elif result.outcome == "replan":
        successors.append(
            JobKey(
                run_id=job_key.run_id,
                ticket_id=job_key.ticket_id,
                plan_version=job_key.plan_version,
                stage="planning",
                iteration=0,
            )
        )

    elif result.outcome == "waiting_human":
        # Sibling isolation: waiting_human produces no successors, siblings continue running
        successors = []

    else:
        # failed, cancelled, waiting_dependency
        successors = []

    # 2. Atomically persist to storage backend
    sqlite_backend = getattr(store, "_backend", None) if getattr(store, "mock_mode", False) else (store if hasattr(store, "_connect") else None)
    is_postgres = hasattr(store, "raw_url") and not getattr(store, "mock_mode", False) and hasattr(store, "_psycopg")

    if sqlite_backend is not None:
        conn = sqlite_backend._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.cursor()

            # Ensure run exists in runs table to satisfy foreign key constraint
            cur.execute("SELECT run_id FROM runs WHERE run_id = ?", (job_key.run_id,))
            if not cur.fetchone():
                runtime_owner = getattr(store, "runtime_owner", "hf05_sqlite")
                cur.execute(
                    """
                    INSERT INTO runs (
                        run_id, project_id, demand_id, demand_version, runtime_owner,
                        mode, status, plan_digest, config_version, created_at, updated_at
                    ) VALUES (?, ?, ?, '1.0', ?, 'autonomous', 'active', 'plan-digest-default', '1.0', ?, ?)
                    ON CONFLICT (run_id) DO NOTHING
                    """,
                    (
                        job_key.run_id,
                        job_key.ticket_id,
                        f"dem-{job_key.run_id}",
                        runtime_owner,
                        now_iso,
                        now_iso,
                    ),
                )

            # A. Update/record the current job status
            cur.execute(
                """
                SELECT status, fencing_token, current_lease_id
                FROM jobs
                WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                """,
                job_key.to_tuple(),
            )
            job_row = cur.fetchone()
            db_status = "succeeded" if result.outcome == "success" else result.outcome
            finished_at = now_iso if db_status in ("succeeded", "failed", "cancelled") else None

            if job_row:
                cur_status = job_row["status"]
                if cur_status in ("pending", "running"):
                    cur.execute(
                        """
                        UPDATE jobs
                        SET status = ?,
                            cause_code = ?,
                            actual_cost = actual_cost + ?,
                            output_refs = ?,
                            evidence_refs = ?,
                            finished_at = COALESCE(?, finished_at),
                            updated_at = ?,
                            current_lease_id = NULL
                        WHERE run_id = ? AND ticket_id = ? AND plan_version = ? AND stage = ? AND iteration = ?
                        """,
                        (
                            db_status,
                            result.cause_code,
                            result.actual_cost,
                            json.dumps(result.output_refs),
                            json.dumps(result.evidence_refs),
                            finished_at,
                            now_iso,
                            *job_key.to_tuple(),
                        ),
                    )
                    if job_row["current_lease_id"]:
                        cur.execute(
                            "UPDATE claims SET status = 'released' WHERE lease_id = ?",
                            (job_row["current_lease_id"],),
                        )
            else:
                # Job not present in jobs table, insert it
                cur.execute(
                    """
                    INSERT INTO jobs (
                        run_id, ticket_id, plan_version, stage, iteration, status,
                        role, required_capabilities, fencing_token, timeout_seconds,
                        retry_count, max_retries, cause_code, actual_cost, output_refs, evidence_refs,
                        created_at, updated_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', 0, 1800, 0, 3, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                    """,
                    (
                        job_key.run_id,
                        job_key.ticket_id,
                        job_key.plan_version,
                        job_key.stage,
                        job_key.iteration,
                        db_status,
                        STAGE_ROLES.get(job_key.stage, f"{job_key.stage}_worker"),
                        result.cause_code,
                        result.actual_cost,
                        json.dumps(result.output_refs),
                        json.dumps(result.evidence_refs),
                        now_iso,
                        now_iso,
                        finished_at,
                    ),
                )

            # B. Idempotently insert successor jobs
            for succ in successors:
                succ_role = STAGE_ROLES.get(succ.stage, f"{succ.stage}_worker")
                cur.execute(
                    """
                    INSERT INTO jobs (
                        run_id, ticket_id, plan_version, stage, iteration, status,
                        role, required_capabilities, fencing_token, timeout_seconds,
                        retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, '[]', 0, 1800, 0, 3, 0.0, '[]', '[]', ?, ?)
                    ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                    """,
                    (
                        succ.run_id,
                        succ.ticket_id,
                        succ.plan_version,
                        succ.stage,
                        succ.iteration,
                        succ_role,
                        now_iso,
                        now_iso,
                    ),
                )

            # C. Outbox record for materialization
            outbox_payload = {
                "run_id": job_key.run_id,
                "ticket_id": job_key.ticket_id,
                "source_job": job_key.canonical_key(),
                "outcome": result.outcome,
                "successors": [s.canonical_key() for s in successors],
            }
            cur.execute(
                """
                INSERT INTO outbox (
                    event_type, aggregate_type, aggregate_id, payload,
                    target_system, status, retry_count, created_at
                ) VALUES ('stage_materialized', 'run', ?, ?, 'cloud_dbos', 'pending', 0, ?)
                """,
                (job_key.run_id, json.dumps(outbox_payload), now_iso),
            )

            # D. Check if workflow run has completed or failed
            cur.execute(
                "SELECT COUNT(*) FROM jobs WHERE run_id = ? AND status IN ('pending', 'running')",
                (job_key.run_id,),
            )
            active_jobs_remaining = cur.fetchone()[0]
            if active_jobs_remaining == 0:
                cur.execute(
                    "SELECT COUNT(*) FROM jobs WHERE run_id = ? AND status = 'failed'",
                    (job_key.run_id,),
                )
                failed_jobs_count = cur.fetchone()[0]
                final_run_status = "failed" if failed_jobs_count > 0 else "completed"
                cur.execute(
                    "UPDATE runs SET status = ?, completed_at = ?, updated_at = ? WHERE run_id = ?",
                    (final_run_status, now_iso, now_iso, job_key.run_id),
                )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    elif is_postgres:
        now_utc = now_dt.astimezone(UTC)
        try:
            with store._psycopg.connect(store.raw_url) as conn:
                with conn.cursor() as cur:
                    # Ensure run exists in runs table
                    cur.execute("SELECT run_id FROM runs WHERE run_id = %s", (job_key.run_id,))
                    if not cur.fetchone():
                        runtime_owner = getattr(store, "runtime_owner", "cloud_dbos_postgres")
                        cur.execute(
                            """
                            INSERT INTO runs (
                                run_id, project_id, demand_id, demand_version, runtime_owner,
                                mode, status, plan_digest, config_version, created_at, updated_at
                            ) VALUES (%s, %s, %s, '1.0', %s, 'autonomous', 'active', 'plan-digest-default', '1.0', %s, %s)
                            ON CONFLICT (run_id) DO NOTHING
                            """,
                            (
                                job_key.run_id,
                                job_key.ticket_id,
                                f"dem-{job_key.run_id}",
                                runtime_owner,
                                now_utc,
                                now_utc,
                            ),
                        )

                    # A. Update/record the current job status
                    cur.execute(
                        """
                        SELECT status, fencing_token, current_lease_id
                        FROM jobs
                        WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                        FOR UPDATE
                        """,
                        job_key.to_tuple(),
                    )
                    job_row = cur.fetchone()
                    db_status = "succeeded" if result.outcome == "success" else result.outcome
                    finished_at_utc = now_utc if db_status in ("succeeded", "failed", "cancelled") else None

                    if job_row:
                        cur_status = job_row[0]
                        if cur_status in ("pending", "running"):
                            cur.execute(
                                """
                                UPDATE jobs
                                SET status = %s,
                                    cause_code = %s,
                                    actual_cost = actual_cost + %s,
                                    output_refs = %s::jsonb,
                                    evidence_refs = %s::jsonb,
                                    finished_at = COALESCE(%s, finished_at),
                                    updated_at = %s,
                                    current_lease_id = NULL
                                WHERE run_id = %s AND ticket_id = %s AND plan_version = %s AND stage = %s AND iteration = %s
                                """,
                                (
                                    db_status,
                                    result.cause_code,
                                    result.actual_cost,
                                    json.dumps(result.output_refs),
                                    json.dumps(result.evidence_refs),
                                    finished_at_utc,
                                    now_utc,
                                    *job_key.to_tuple(),
                                ),
                            )
                            if job_row[2]:
                                cur.execute(
                                    "UPDATE claims SET status = 'released' WHERE lease_id = %s",
                                    (job_row[2],),
                                )
                    else:
                        cur.execute(
                            """
                            INSERT INTO jobs (
                                run_id, ticket_id, plan_version, stage, iteration, status,
                                role, required_capabilities, fencing_token, timeout_seconds,
                                retry_count, max_retries, cause_code, actual_cost, output_refs, evidence_refs,
                                created_at, updated_at, finished_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, '[]'::jsonb, 0, 1800, 0, 3, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                            ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                            """,
                            (
                                job_key.run_id,
                                job_key.ticket_id,
                                job_key.plan_version,
                                job_key.stage,
                                job_key.iteration,
                                db_status,
                                STAGE_ROLES.get(job_key.stage, f"{job_key.stage}_worker"),
                                result.cause_code,
                                result.actual_cost,
                                json.dumps(result.output_refs),
                                json.dumps(result.evidence_refs),
                                now_utc,
                                now_utc,
                                finished_at_utc,
                            ),
                        )

                    # B. Idempotently insert successor jobs
                    for succ in successors:
                        succ_role = STAGE_ROLES.get(succ.stage, f"{succ.stage}_worker")
                        cur.execute(
                            """
                            INSERT INTO jobs (
                                run_id, ticket_id, plan_version, stage, iteration, status,
                                role, required_capabilities, fencing_token, timeout_seconds,
                                retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                                created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, '[]'::jsonb, 0, 1800, 0, 3, 0.0, '[]'::jsonb, '[]'::jsonb, %s, %s)
                            ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                            """,
                            (
                                succ.run_id,
                                succ.ticket_id,
                                succ.plan_version,
                                succ.stage,
                                succ.iteration,
                                succ_role,
                                now_utc,
                                now_utc,
                            ),
                        )

                    # C. Outbox record for materialization
                    outbox_payload = {
                        "run_id": job_key.run_id,
                        "ticket_id": job_key.ticket_id,
                        "source_job": job_key.canonical_key(),
                        "outcome": result.outcome,
                        "successors": [s.canonical_key() for s in successors],
                    }
                    cur.execute(
                        """
                        INSERT INTO outbox (
                            event_type, aggregate_type, aggregate_id, payload,
                            target_system, status, retry_count, created_at
                        ) VALUES ('stage_materialized', 'run', %s, %s::jsonb, 'cloud_dbos', 'pending', 0, %s)
                        """,
                        (job_key.run_id, json.dumps(outbox_payload), now_utc),
                    )

                    # D. Check if workflow run has completed or failed
                    cur.execute(
                        "SELECT COUNT(*) FROM jobs WHERE run_id = %s AND status IN ('pending', 'running')",
                        (job_key.run_id,),
                    )
                    row = cur.fetchone()
                    active_jobs_remaining = row[0] if row else 0
                    if active_jobs_remaining == 0:
                        cur.execute(
                            "SELECT COUNT(*) FROM jobs WHERE run_id = %s AND status = 'failed'",
                            (job_key.run_id,),
                        )
                        f_row = cur.fetchone()
                        failed_jobs_count = f_row[0] if f_row else 0
                        final_run_status = "failed" if failed_jobs_count > 0 else "completed"
                        cur.execute(
                            "UPDATE runs SET status = %s, completed_at = %s, updated_at = %s WHERE run_id = %s",
                            (final_run_status, now_utc, now_utc, job_key.run_id),
                        )

                    conn.commit()
        except Exception as exc:
            logger.error("PostgreSQL materialize_result failed: %s", exc)
            raise

    elif hasattr(store, "emit_outbox"):
        store.emit_outbox(
            OutboxEvent(
                event_type="stage_materialized",
                aggregate_type="run",
                aggregate_id=job_key.run_id,
                run_id=job_key.run_id,
                payload={
                    "run_id": job_key.run_id,
                    "ticket_id": job_key.ticket_id,
                    "source_job": job_key.canonical_key(),
                    "outcome": result.outcome,
                    "successors": [s.canonical_key() for s in successors],
                },
                target_system="cloud_dbos",
                status="pending",
            ),
            now=now_dt,
        )

    return successors
