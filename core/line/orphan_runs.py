"""Sweeper for line runs that are `active` but have nothing left to run (USR-142).

Evidence (2026-10-04, control store): the run of HF-03-08 stayed `runs.status = 'active'` for ~15 days
with a single `grill` job `succeeded` and no `planning` job, not even a `pending` one. Every other
safety net misses that shape:

* the lease reconciler (`ControlStore.reconcile`) only looks at expired claims;
* the `waiting_human` sweep (USR-105) only looks at jobs parked in a waiting state;
* the stage handlers only run when a job is claimed.

A job is finished (`ControlStore.finish`) and its successor is materialized
(`core.workflow.successors.materialize_result`) in two separate transactions, so a worker that dies
between them (or a materialization that raised) leaves exactly this orphan run. This module finds
such runs and repairs them:

1. An `active` run is an orphan when it has NO job in an open status (`pending`, `running`,
   `waiting_human`, `waiting_dependency`) and its last activity (the newest `updated_at`
   (else `finished_at`, else `created_at`) among its jobs) is older than `idle_limit` (30 min, the same
   threshold the live panel uses for "nenhuma etapa agendada").
2. The newest job decides what to do. The sweeper replays its recorded outcome through
   `materialize_result`, which is idempotent (successors are inserted `ON CONFLICT DO NOTHING`, the
   run is closed when nothing is open): a `succeeded` job schedules its DAG successor, a `failed` one
   schedules the `retrospective`, a `retry`/`replan` one its follow-up.
3. When replaying cannot produce an open job (the successor row already exists in a terminal state,
   the newest job was cancelled, the stage is not part of the line DAG, ...), the run is parked on a
   `waiting_human` job whose `cause_code` is `ORPHAN_NO_SUCCESSOR_CAUSE`, so it is visible to the
   owner instead of silently idle. A parked run has an open job, so a second sweep ignores it
   (idempotent); `ControlStore.resume_job` wakes it.

The functions only use the public `ControlStore` surface (`list_active_run_ids`, `get_run_status`,
`max_iteration`) plus `materialize_result`, so any store (or a fake) that implements it works.
`CloudWorker.sweep_orphan_runs` runs this periodically next to the USR-105 waiting-human sweep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from core.line.route_wait import parse_run_created_at
from core.workflow.control_contracts import JobKey, StageResult
from core.workflow.control_store import RUN_OPEN_JOB_STATUSES
from core.workflow.line_live import QUEUED_STALL_LIMIT
from core.workflow.successors import PRODUCTIVE_DAG, materialize_result

logger = logging.getLogger(__name__)

ORPHAN_IDLE_LIMIT: timedelta = QUEUED_STALL_LIMIT
ORPHAN_NO_SUCCESSOR_CAUSE = "orphan_run_no_successor"
MAX_ACTIONS_PER_SWEEP = 25

# Terminal job status -> the `StageResult.outcome` that produced it (`db_status` in materialize_result).
_OUTCOME_BY_JOB_STATUS: dict[str, str] = {
    "succeeded": "success",
    "retry": "retry",
    "replan": "replan",
    "failed": "failed",
}

Materializer = Callable[..., Sequence[JobKey]]


@dataclass(frozen=True)
class OrphanRun:
    """An `active` run with no open job and no recent activity."""

    run_id: str
    last_job: JobKey
    last_status: str
    last_cause_code: Optional[str]
    last_output_refs: tuple[str, ...]
    idle: timedelta


@dataclass(frozen=True)
class OrphanRunAction:
    """What the sweeper did to one orphan run."""

    run_id: str
    action: str  # "scheduled" | "closed" | "parked" | "error" | "orphan" (dry run)
    stage: str  # stage of the newest job the sweeper acted on
    detail: str = ""


def _latest_moment(job: Mapping[str, Any]) -> Optional[datetime]:
    """Last activity of a job: `updated_at`, else `finished_at`, else `created_at` (what the live panel uses)."""
    for key in ("updated_at", "finished_at", "created_at"):
        parsed = parse_run_created_at(job.get(key))
        if parsed is not None:
            return parsed
    return None


def _job_key(job: Mapping[str, Any], run_id: str) -> Optional[JobKey]:
    try:
        return JobKey(
            run_id=run_id,
            ticket_id=str(job["ticket_id"]),
            plan_version=str(job["plan_version"]),
            stage=str(job["stage"]),
            iteration=int(job["iteration"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def find_orphan_run(
    status: Mapping[str, Any],
    *,
    now: datetime,
    idle_limit: timedelta = ORPHAN_IDLE_LIMIT,
) -> Optional[OrphanRun]:
    """`OrphanRun` for a `ControlStore.get_run_status()` payload, or None when the run is fine.

    Pure function: ignores runs that are not `active`, have any open job (running, pending, parked on
    a human or a dependency), have no jobs at all, or were active less than `idle_limit` ago.
    """
    if status.get("status") != "active":
        return None
    run_id = str(status.get("run_id") or "")
    jobs: list[Mapping[str, Any]] = [j for j in (status.get("jobs") or []) if isinstance(j, Mapping)]
    if not run_id or not jobs:
        return None
    if any(job.get("status") in RUN_OPEN_JOB_STATUSES for job in jobs):
        return None

    moments = [m for job in jobs if (m := _latest_moment(job)) is not None]
    if not moments:
        return None
    idle = now - max(moments)
    if idle <= idle_limit:
        return None

    def _order(job: Mapping[str, Any]) -> tuple[datetime, int]:
        return (_latest_moment(job) or datetime.min.replace(tzinfo=timezone.utc), int(job.get("iteration") or 0))

    last = max(jobs, key=_order)
    key = _job_key(last, run_id)
    if key is None:
        return None
    refs = last.get("output_refs")
    return OrphanRun(
        run_id=run_id,
        last_job=key,
        last_status=str(last.get("status") or ""),
        last_cause_code=last.get("cause_code"),
        last_output_refs=tuple(str(r) for r in refs) if isinstance(refs, list) else (),
        idle=idle,
    )


def _has_open_job(status: Optional[Mapping[str, Any]]) -> bool:
    return bool(status) and any(
        isinstance(job, Mapping) and job.get("status") in RUN_OPEN_JOB_STATUSES for job in (status or {}).get("jobs") or []
    )


def _replay_result(orphan: OrphanRun) -> Optional[StageResult]:
    outcome = _OUTCOME_BY_JOB_STATUS.get(orphan.last_status)
    if outcome is None:
        return None
    if outcome == "success":
        refs = list(orphan.last_output_refs) or [f"orphan_sweeper:{orphan.last_job.stage}"]
        return StageResult(outcome="success", output_refs=refs)
    return StageResult(outcome=outcome, cause_code=orphan.last_cause_code)  # type: ignore[arg-type]


def _park(
    store: Any, orphan: OrphanRun, *, now: datetime, materialize: Materializer
) -> JobKey:
    """Open a `waiting_human` job (cause `ORPHAN_NO_SUCCESSOR_CAUSE`) on the stage that should follow."""
    last = orphan.last_job
    stage = PRODUCTIVE_DAG.get(last.stage, last.stage)
    try:
        iteration = int(store.max_iteration(last.run_id, last.ticket_id, last.plan_version, stage)) + 1
    except Exception:  # a store without max_iteration: continue after the newest known job
        iteration = last.iteration + 1
    parked = JobKey(
        run_id=last.run_id,
        ticket_id=last.ticket_id,
        plan_version=last.plan_version,
        stage=stage,
        iteration=max(iteration, 0),
    )
    materialize(parked, StageResult(outcome="waiting_human", cause_code=ORPHAN_NO_SUCCESSOR_CAUSE), store, now=now)
    return parked


def sweep_orphan_runs(
    store: Any,
    *,
    now: Optional[datetime] = None,
    idle_limit: timedelta = ORPHAN_IDLE_LIMIT,
    materialize: Materializer = materialize_result,
    max_actions: int = MAX_ACTIONS_PER_SWEEP,
    only_run_ids: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> list[OrphanRunAction]:
    """Repair every orphan `active` run of `store` (at most `max_actions` per call); see module docs.

    `only_run_ids` restricts the sweep to those runs; with `dry_run` nothing is written and each orphan is
    reported as an `orphan` action.

    Never raises: a store that cannot list runs yields no actions and a run that fails to repair is
    reported as an `error` action and retried by the next sweep.
    """
    effective_now = now or datetime.now(timezone.utc)
    lister = getattr(store, "list_active_run_ids", None)
    if lister is None:
        return []
    try:
        run_ids = list(lister())
    except Exception as exc:
        logger.warning("Orphan run sweep could not list active runs: %s", exc)
        return []

    if only_run_ids is not None:
        wanted = set(only_run_ids)
        run_ids = [run_id for run_id in run_ids if run_id in wanted]

    actions: list[OrphanRunAction] = []
    for run_id in run_ids:
        if len(actions) >= max_actions:
            break
        try:
            status = store.get_run_status(run_id)
            if not status:
                continue
            orphan = find_orphan_run(status, now=effective_now, idle_limit=idle_limit)
            if orphan is None:
                continue
            if dry_run:
                actions.append(OrphanRunAction(orphan.run_id, "orphan", orphan.last_job.stage, orphan.last_status))
                continue
            actions.append(_repair(store, orphan, now=effective_now, materialize=materialize))
        except Exception as exc:
            logger.warning("Orphan run sweep failed for run %s: %s", run_id, exc)
            actions.append(OrphanRunAction(run_id=str(run_id), action="error", stage="", detail=str(exc)[:200]))
    return actions


def _repair(store: Any, orphan: OrphanRun, *, now: datetime, materialize: Materializer) -> OrphanRunAction:
    stage = orphan.last_job.stage
    idle_min = int(orphan.idle.total_seconds() // 60)
    result = _replay_result(orphan)
    if result is not None:
        materialize(orphan.last_job, result, store, now=now)
        after = store.get_run_status(orphan.run_id)
        if after is not None and after.get("status") != "active":
            logger.warning(
                "Orphan run %s (last job %s %s, idle %d min) had nothing left to run; closed as %s",
                orphan.run_id, stage, orphan.last_status, idle_min, after.get("status"),
            )
            return OrphanRunAction(orphan.run_id, "closed", stage, str(after.get("status")))
        if _has_open_job(after):
            logger.warning(
                "Orphan run %s (last job %s %s, idle %d min): successor scheduled",
                orphan.run_id, stage, orphan.last_status, idle_min,
            )
            return OrphanRunAction(orphan.run_id, "scheduled", stage)

    parked = _park(store, orphan, now=now, materialize=materialize)
    logger.warning(
        "Orphan run %s (last job %s %s, idle %d min): no successor could be scheduled; parked on "
        "waiting_human(%s) at %s",
        orphan.run_id, stage, orphan.last_status, idle_min, ORPHAN_NO_SUCCESSOR_CAUSE, parked.canonical_key(),
    )
    return OrphanRunAction(orphan.run_id, "parked", stage, ORPHAN_NO_SUCCESSOR_CAUSE)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m core.line.orphan_runs [--run-id RUN_ID ...] [--dry-run]`: one sweep on the control store.

    Uses `DARKFAC_HF02_DATABASE_URL` (the production Postgres) like the worker does; without it the
    store is an empty in-memory mock and nothing is found.
    """
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Repair active runs that have no open job (USR-142).")
    parser.add_argument("--run-id", action="append", dest="run_ids", help="only this run (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="list orphan runs without changing anything")
    parser.add_argument("--idle-minutes", type=float, default=ORPHAN_IDLE_LIMIT.total_seconds() / 60.0)
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = PostgresControlStore()
    actions = sweep_orphan_runs(
        store,
        idle_limit=timedelta(minutes=args.idle_minutes),
        only_run_ids=args.run_ids,
        dry_run=args.dry_run,
    )
    print(json.dumps([action.__dict__ for action in actions], ensure_ascii=False, indent=1))
    return 1 if any(action.action == "error" for action in actions) else 0


__all__ = [
    "MAX_ACTIONS_PER_SWEEP",
    "ORPHAN_IDLE_LIMIT",
    "ORPHAN_NO_SUCCESSOR_CAUSE",
    "OrphanRun",
    "OrphanRunAction",
    "find_orphan_run",
    "main",
    "sweep_orphan_runs",
]


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
