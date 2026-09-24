"""Canonical successor materialization for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md and ticket HF-05-04, extended by
HF-27-08 for the lean production-line DAG (`docs/handoffs/production-line/
HF-27-08.md`, decisions D-a/D-b/D-c/D-e):

- D-a: the line DAG is `grill -> planning -> development -> validation ->
  independent_review -> integration -> build_deploy -> retrospective`.
  `target_journey` is merged into `build_deploy` (HF-27-07); the old
  `environment`/`research` branch point is dropped for the line (those
  stages remain valid `StageResult` targets for any other caller that still
  emits them directly, they are simply no longer a DAG destination here).
- D-b: a `retry` outcome routes to another stage via the cause_code
  convention `retry:<stage>` (optionally followed by `\n<log>`), at
  `iteration = current_max_iteration_of_that_stage_in_this_run + 1`. A plain
  `retry` (no `retry:<stage>` prefix) keeps the historical same-stage
  `iteration + 1` behaviour.
- D-c: a `not_before=<iso>` token anywhere in a retry cause_code (e.g.
  `ci_pending:not_before=...` from `stage_integration.py`) is persisted on
  the successor job's `not_before` column; `ControlStore.claim()` skips it
  until then.
- D-e: the per-stage `memory_observation` fan-out is removed. A single
  `retrospective` job (iteration 0, naturally idempotent via the jobs PK) is
  emitted when `build_deploy` succeeds, or when any stage reaches terminal
  `failed` (the line's DAG is strictly linear, so any stage's `failed` is
  the run's terminal failure). `LEARNING_STAGES` and its anti-recursion
  guard are kept as-is for any legacy caller that still enqueues
  `memory_observation`/`learning_eval` directly.
- Loop guard: no `RunCaps` field maps to "max iterations for a single
  stage", so `MAX_STAGE_ITERATIONS` below is a documented module constant.
  A retry whose resolved target iteration would exceed it never creates a
  successor; the current job is recorded `failed(loop_cap)` instead.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Optional

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

# HF-27-08 D-a: the lean production-line DAG. `build_deploy` intentionally
# has no entry here — its successor (`retrospective`) is decided by the
# dedicated `emit_retrospective` logic below, not a simple next-stage lookup.
PRODUCTIVE_DAG: dict[str, str] = {
    "grill": "planning",
    "planning": "development",
    "development": "validation",
    "validation": "independent_review",
    "independent_review": "integration",
    "integration": "build_deploy",
    # Legacy learning chain (HF-10), kept for any caller that still enqueues
    # memory_observation directly (outside the HF-27-08 line, which no
    # longer fans out to it automatically — see D-e in the module docstring).
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
    "retrospective": "retrospective_agent",
}

# HF-27-08 loop guard (see module docstring, review item 2): only a
# cross-stage bounce (`retry:<stage>`) counts against MAX_STAGE_ITERATIONS --
# two stages disagreeing indefinitely is the failure mode this guards
# against. A plain same-stage retry (e.g. a transient agent/deploy error, or
# CI polling via `ci_pending:not_before=...`) gets its own, much larger cap
# (MAX_SAME_STAGE_RETRIES) so a 5-minute CI poll window or a flaky deploy
# adapter does not hit `loop_cap` well before the run's own wall-clock
# budget. A same-stage retry that additionally carries `not_before` is
# bounded by `RunCaps.wall_clock_hours` (from the run's `created_at`)
# instead of a count at all -- see `_run_wall_clock_exceeded`.
MAX_STAGE_ITERATIONS = 10
MAX_SAME_STAGE_RETRIES = 30

_RETRY_TARGET_RE = re.compile(r"^retry:(?P<stage>[A-Za-z_][A-Za-z0-9_]*)(?:\n(?P<log>[\s\S]*))?$")
_NOT_BEFORE_RE = re.compile(r"not_before=(?P<iso>\S+)")


def _parse_retry_cause_code(cause_code: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Parse a `retry` outcome's cause_code for D-b/D-c.

    Returns `(target_stage, not_before_iso)`. `target_stage` is `None` for a
    plain same-stage retry (no `retry:<stage>` prefix). `not_before_iso` is
    the value of a `not_before=<iso>` token found anywhere in the
    cause_code, present or absent independently of `target_stage`.
    """
    if not cause_code:
        return None, None
    target_stage: Optional[str] = None
    match = _RETRY_TARGET_RE.match(cause_code)
    if match:
        target_stage = match.group("stage")
    not_before_match = _NOT_BEFORE_RE.search(cause_code)
    not_before = not_before_match.group("iso") if not_before_match else None
    return target_stage, not_before


def _max_iteration_for_stage(
    store: ControlStore, run_id: str, ticket_id: str, plan_version: str, stage: str
) -> int:
    """Highest `iteration` already recorded for `stage` in this run, or -1 if none.

    Used to resolve a cross-stage retry's target iteration (D-b): the
    successor is `iteration = this + 1`, so a first-ever bounce back to a
    stage that only ran once at iteration 0 lands on iteration 1.

    Delegates to `ControlStore.max_iteration()` (a real store method on both
    adapters, review item 10) rather than duck-typing store internals here.
    Any store missing that method (a test double, for instance) falls back
    to -1, matching the historical "no prior iteration" default.
    """
    method = getattr(store, "max_iteration", None)
    if method is None:
        return -1
    try:
        return method(run_id, ticket_id, plan_version, stage)
    except Exception:  # pragma: no cover - defensive, must never break materialization
        return -1


def _run_wall_clock_exceeded(store: ControlStore, run_id: str, now_dt: datetime) -> bool:
    """True if `run_id` has been open longer than `RunCaps.wall_clock_hours` (review item 2).

    A `not_before`-carrying same-stage retry (CI polling, transient deploy
    retries) is bounded by wall-clock time from the run's `created_at`, not
    by a retry count -- a 5-minute CI poll window alone would hit any
    reasonable count cap well before a slow CI run finishes.
    """
    method = getattr(store, "get_run_created_at", None)
    if method is None:
        return False
    try:
        created_raw = method(run_id)
    except Exception:  # pragma: no cover - defensive
        return False
    if not created_raw:
        return False
    try:
        created_at = datetime.fromisoformat(str(created_raw))
    except ValueError:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    compare_now = now_dt if now_dt.tzinfo is not None else now_dt.replace(tzinfo=UTC)

    wall_clock_hours = 6.0
    try:
        from core.line.routing import load_routing_config

        wall_clock_hours = load_routing_config().run_caps.wall_clock_hours
    except Exception:  # pragma: no cover - defensive, core.line may be unavailable
        pass

    elapsed_hours = (compare_now - created_at).total_seconds() / 3600.0
    return elapsed_hours > wall_clock_hours


def _required_capabilities_json(ticket_id: str, stage: str) -> str:
    """Best-effort `required_capabilities` for a freshly created job (HF-27-08 item D).

    `ticket_id` holds the project id for line runs (`ControlStore.accept()`
    stores it there). Lazily imports `core.line.bindings` so this generic
    HF-05 module has no hard dependency on the line package; any failure
    (project unknown, core.line unavailable, non-line ticket_id) falls back
    to the historical `'[]'` (no gating), matching pre-HF-27-08 behaviour
    for every caller outside the production line.
    """
    try:
        from core.line.bindings import required_caps
        from core.projects.registry import get_project_registry

        project = get_project_registry().get_project(ticket_id)
        if project is None:
            return "[]"
        return json.dumps(required_caps(project, stage))
    except Exception:  # pragma: no cover - defensive, must never break materialization
        return "[]"


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
    - Transitions through the canonical productive DAG (HF-27-08 D-a).
    - `retrospective` fan-out replaces the old per-stage `memory_observation`
      fan-out (HF-27-08 D-e); `LEARNING_STAGES` anti-recursion is preserved
      for any legacy caller that still enqueues those stages directly.
    - Sibling isolation: WAITING_HUMAN outcome does not suspend or cancel siblings.
    - Idempotency: duplicate calls produce identical JobKey lists and converge safely.
    - retry: same-stage `iteration + 1`, or (D-b) `retry:<stage>` routes to
      `<stage>` at that stage's current max iteration + 1, optionally
      carrying a `not_before=<iso>` claim gate (D-c). A resolved iteration
      beyond `MAX_STAGE_ITERATIONS` is terminal (`failed(loop_cap)`).
    - replan: enqueue planning stage at iteration 0.
    """
    if result.outcome == "success" and not result.output_refs:
        raise InvalidResultError(
            f"Stage '{job_key.stage}' succeeded with empty output_refs, violating fail-closed contract."
        )

    now_dt = now or datetime.now(UTC)
    now_iso = now_dt.isoformat()

    successors: list[JobKey] = []
    # HF-27-08 D-c: not_before to apply to the (single, if any) retry
    # successor. Keyed by nothing else since a retry produces at most one
    # non-retrospective successor.
    retry_not_before: Optional[str] = None
    loop_cap_exceeded = False
    emit_retrospective = False

    # 1. Determine successor list based on outcome
    if result.outcome == "success":
        # manifest_requires_environment is accepted for call-site
        # compatibility but no longer routes anywhere (HF-27-08 D-a dropped
        # the environment branch point from the line DAG).
        del manifest_requires_environment
        next_stage = PRODUCTIVE_DAG.get(job_key.stage)
        if next_stage is not None:
            successors.append(
                JobKey(
                    run_id=job_key.run_id,
                    ticket_id=job_key.ticket_id,
                    plan_version=job_key.plan_version,
                    stage=next_stage,
                    # HF-27-08 review item 1/11(d): mirror the *source*
                    # job's own iteration, not a hardcoded 0. A D-b
                    # cross-stage bounce back to development creates it at
                    # iteration N>0; its own eventual success must route
                    # through validation/independent_review/integration/
                    # build_deploy at that same iteration N, or the
                    # successor JobKey collides with the already-succeeded
                    # iteration-0 row from the first pass (ON CONFLICT DO
                    # NOTHING would silently swallow it, stalling the run).
                    # Still fully idempotent: replaying the SAME job_key's
                    # success always derives the same iteration, independent
                    # of store state.
                    iteration=job_key.iteration,
                )
            )
        if job_key.stage == "build_deploy":
            emit_retrospective = True

    elif result.outcome == "retry":
        target_stage, retry_not_before = _parse_retry_cause_code(result.cause_code)
        if target_stage:
            # Cross-stage bounce (D-b): counts against MAX_STAGE_ITERATIONS.
            base_iteration = _max_iteration_for_stage(
                store, job_key.run_id, job_key.ticket_id, job_key.plan_version, target_stage
            )
            new_iteration = base_iteration + 1
            successor_stage = target_stage
            cap_exceeded = new_iteration > MAX_STAGE_ITERATIONS
        else:
            # Same-stage retry: either bounded by wall-clock time (a
            # not_before-carrying transient wait, e.g. CI polling) or by a
            # much larger count cap (review item 2).
            new_iteration = job_key.iteration + 1
            successor_stage = job_key.stage
            if retry_not_before:
                cap_exceeded = _run_wall_clock_exceeded(store, job_key.run_id, now_dt)
            else:
                cap_exceeded = new_iteration > MAX_SAME_STAGE_RETRIES

        if cap_exceeded:
            loop_cap_exceeded = True
            emit_retrospective = True
            retry_not_before = None
        else:
            successors.append(
                JobKey(
                    run_id=job_key.run_id,
                    ticket_id=job_key.ticket_id,
                    plan_version=job_key.plan_version,
                    stage=successor_stage,
                    iteration=new_iteration,
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

    elif result.outcome == "failed":
        # HF-27-08 D-e: the line DAG is strictly linear, so any stage's
        # terminal failure is the run's terminal failure.
        successors = []
        emit_retrospective = True

    else:
        # cancelled, waiting_dependency
        successors = []

    if emit_retrospective:
        successors.append(
            JobKey(
                run_id=job_key.run_id,
                ticket_id=job_key.ticket_id,
                plan_version=job_key.plan_version,
                stage="retrospective",
                iteration=0,
            )
        )

    db_status = "succeeded" if result.outcome == "success" else result.outcome
    effective_cause_code = result.cause_code
    if loop_cap_exceeded:
        db_status = "failed"
        effective_cause_code = f"loop_cap:{result.cause_code}" if result.cause_code else "loop_cap"

    def _not_before_for(successor: JobKey) -> Optional[str]:
        if retry_not_before and successor.stage != "retrospective" and result.outcome == "retry":
            return retry_not_before
        return None

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
                            effective_cause_code,
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
                        effective_cause_code,
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
                        created_at, updated_at, not_before, ready_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0, 1800, 0, 3, 0.0, '[]', '[]', ?, ?, ?, ?)
                    ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                    """,
                    (
                        succ.run_id,
                        succ.ticket_id,
                        succ.plan_version,
                        succ.stage,
                        succ.iteration,
                        succ_role,
                        _required_capabilities_json(succ.ticket_id, succ.stage),
                        now_iso,
                        now_iso,
                        _not_before_for(succ),
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
                                    effective_cause_code,
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
                                effective_cause_code,
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
                                created_at, updated_at, not_before, ready_at
                            ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s::jsonb, 0, 1800, 0, 3, 0.0, '[]'::jsonb, '[]'::jsonb, %s, %s, %s, %s)
                            ON CONFLICT (run_id, ticket_id, plan_version, stage, iteration) DO NOTHING
                            """,
                            (
                                succ.run_id,
                                succ.ticket_id,
                                succ.plan_version,
                                succ.stage,
                                succ.iteration,
                                succ_role,
                                _required_capabilities_json(succ.ticket_id, succ.stage),
                                now_utc,
                                now_utc,
                                _not_before_for(succ),
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
