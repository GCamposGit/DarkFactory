"""Read-only "live line" projection over the canonical control store (USR-138).

``job_board`` folds everything into a single row per run. The live line board needs
the opposite: a stage-by-stage view of every recent run (all iterations, active
claims, timings, stalls, an event feed and KPIs). This module reads the same
control store (PostgreSQL in the cloud, ``control.db`` locally) *without* running
DDL, creating files or taking leases, and projects it onto a stable JSON contract
consumed by the Hub frontend (``/api/line/live``).

The contract (field names, literals) is shared with a frontend written in
parallel: do not rename fields without updating both sides.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.line.bindings import LINE_STAGES
from core.orchestrator.cloud_db import sanitize_database_url
from core.workflow.job_board import (
    DATABASE_URL_ENVS,
    decode_refs,
    extract_title,
    resolve_cause_and_diagnostic,
)

logger = logging.getLogger(__name__)

RUN_LIMIT = 150
RECENT_RUN_WINDOW = timedelta(days=7)
MAX_EVENTS = 60
MAX_EVIDENCE_REFS = 12
MAX_NEXT_BACKLOG = 5
RUNNING_IDLE_LIMIT = timedelta(minutes=20)
QUEUED_STALL_LIMIT = timedelta(minutes=30)

STAGE_LABELS: dict[str, str] = {
    "grill": "Grill",
    "planning": "Planejamento",
    "development": "Desenvolvimento",
    "validation": "Validação",
    "independent_review": "Revisão",
    "integration": "Integração",
    "build_deploy": "Deploy",
    "retrospective": "Retrospectiva",
    "target_journey": "Jornada-alvo",
}

_ATTENTION_STAGE_STATUSES: frozenset[str] = frozenset(
    {"waiting_human", "failed", "retry", "replan", "waiting_dependency"}
)
_KNOWN_STAGE_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "succeeded",
        "retry",
        "replan",
        "waiting_dependency",
        "waiting_human",
        "cancelled",
        "failed",
    }
)
_TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"completed", "succeeded", "failed", "cancelled", "canceled", "aborted", "closed"}
)
_OFF_LINE_STATUSES: frozenset[str] = frozenset({"implementing", "in_progress"})
_HORIZON_RANK: dict[str, int] = {"now": 0, "next": 1, "later": 2, "exploratory": 3, "unscheduled": 4}

StageStatus = Literal[
    "not_reached",
    "pending",
    "running",
    "succeeded",
    "retry",
    "replan",
    "waiting_dependency",
    "waiting_human",
    "cancelled",
    "failed",
]
RunState = Literal["attention", "running", "queued", "succeeded", "failed", "cancelled"]
EventKind = Literal[
    "stage_started", "stage_succeeded", "stage_failed", "waiting_human", "retry", "run_completed"
]


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveSource(_Model):
    backend: Literal["postgres", "sqlite", "none"]
    status: Literal["ok", "missing", "error"]


class LiveAttempt(_Model):
    iteration: int
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    cost_usd: float = 0.0
    cause_code: str | None = None
    updated_at: str | None = None


class LiveStage(_Model):
    stage: str
    label: str
    status: StageStatus
    attempts: int = 0
    retry_count: int = 0
    max_retries: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    cost_usd: float = 0.0
    cause_code: str | None = None
    diagnostic: str | None = None
    role: str | None = None
    worker: str | None = None
    route: str | None = None
    lease_expires_at: str | None = None
    timeout_seconds: int | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    history: list[LiveAttempt] = Field(default_factory=list)


class LiveAttention(_Model):
    stage: str
    cause_code: str | None = None
    diagnostic: str | None = None
    since: str | None = None


class LiveRun(_Model):
    run_id: str
    ticket_id: str
    demand_id: str
    project_id: str
    title: str
    mode: str
    run_status: str
    state: RunState
    current_stage: str | None = None
    current_stage_status: StageStatus | None = None
    attention: LiveAttention | None = None
    stalled: bool = False
    stalled_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    age_seconds: float | None = None
    cycle_seconds: float | None = None
    total_cost_usd: float = 0.0
    iterations_total: int = 0
    stages: list[LiveStage] = Field(default_factory=list)


class LiveEvent(_Model):
    at: str
    run_id: str
    ticket_id: str
    title: str
    project_id: str
    stage: str | None = None
    kind: EventKind
    message: str


class LiveThroughputDay(_Model):
    date: str
    completed: int


class LiveKpis(_Model):
    active_runs: int = 0
    attention: int = 0
    running_stages: int = 0
    completed_24h: int = 0
    failed_24h: int = 0
    median_cycle_seconds_7d: float | None = None
    p85_cycle_seconds_7d: float | None = None
    cost_24h_usd: float = 0.0
    throughput_7d: list[LiveThroughputDay] = Field(default_factory=list)
    planned_backlog: int = 0


class LiveBacklogNext(_Model):
    id: str
    title: str
    horizon: str


class LiveBacklogProject(_Model):
    project_id: str
    planned: int = 0
    implementing: int = 0
    completed: int = 0
    total: int = 0
    next: list[LiveBacklogNext] = Field(default_factory=list)


class LiveTicket(_Model):
    id: str
    project_id: str
    title: str
    status: str
    updated_at: str | None = None


class LineLiveSnapshot(_Model):
    generated_at: str
    version: str
    source: LiveSource
    warnings: list[str] = Field(default_factory=list)
    stage_order: list[str] = Field(default_factory=list)
    stage_labels: dict[str, str] = Field(default_factory=dict)
    runs: list[LiveRun] = Field(default_factory=list)
    events: list[LiveEvent] = Field(default_factory=list)
    kpis: LiveKpis = Field(default_factory=LiveKpis)
    backlog: list[LiveBacklogProject] = Field(default_factory=list)
    off_line: list[LiveTicket] = Field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    """Parse a driver value (datetime or ISO text, naive means UTC) into an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds()), 3)


def _cost(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = round(seconds)
    if total < 60:
        return f"{total} s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest:02d} min"


def _label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 3)


def _enum_text(value: Any) -> str:
    inner = getattr(value, "value", value)
    return str(inner) if inner is not None else ""


# --------------------------------------------------------------------------- raw loading


@dataclass
class _RawStore:
    runs: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    intake: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_RUN_QUERY = (
    "SELECT run_id, project_id, demand_id, mode, status, created_at, updated_at, completed_at "
    "FROM runs ORDER BY updated_at DESC LIMIT {limit}"
)
_JOB_COLUMNS = (
    "run_id, ticket_id, stage, iteration, status, role, cause_code, actual_cost, retry_count, "
    "max_retries, evidence_refs, timeout_seconds, created_at, updated_at, started_at, finished_at"
)
_CLAIM_COLUMNS = "run_id, stage, iteration, owner, route_ref, acquired_at, expires_at"


def _is_terminal(run_status: str) -> bool:
    return run_status.strip().lower() in _TERMINAL_RUN_STATUSES


def _select_runs(rows: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    cutoff = now - RECENT_RUN_WINDOW
    selected: list[dict[str, Any]] = []
    for row in rows:
        updated = _parse_dt(row.get("updated_at"))
        if not _is_terminal(str(row.get("status") or "active")) or (updated is not None and updated >= cutoff):
            selected.append(row)
    return selected


def _load_with(
    execute: Callable[[str, Sequence[Any]], list[dict[str, Any]]],
    in_clause: Callable[[str, Sequence[str]], tuple[str, list[Any]]],
    now: datetime,
) -> _RawStore:
    raw = _RawStore()
    runs = _select_runs(execute(_RUN_QUERY.format(limit=RUN_LIMIT), ()), now)
    raw.runs = runs
    run_ids = [str(row["run_id"]) for row in runs]
    if not run_ids:
        return raw
    clause, params = in_clause("run_id", run_ids)
    raw.jobs = execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE {clause}", params)
    # Claims and intake titles are cosmetic/enrichment: their failure must not hide the board.
    try:
        clause, params = in_clause("run_id", run_ids)
        raw.claims = execute(
            f"SELECT {_CLAIM_COLUMNS} FROM claims WHERE status = 'active' AND {clause}", params
        )
    except Exception as exc:  # noqa: BLE001 - driver specific errors
        logger.warning("Live line: claims unreadable: %s", sanitize_database_url(str(exc)))
        raw.warnings.append(f"claims indisponíveis ({type(exc).__name__})")
    try:
        clause, params = in_clause("run_id", run_ids)
        raw.intake = execute(
            f"SELECT run_id, payload, committed_at FROM intake_commands WHERE {clause} "
            "ORDER BY committed_at DESC",
            params,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live line: intake titles unreadable: %s", sanitize_database_url(str(exc)))
        raw.warnings.append(f"títulos de intake indisponíveis ({type(exc).__name__})")
    return raw


def _sqlite_in_clause(column: str, values: Sequence[str]) -> tuple[str, list[Any]]:
    return f"{column} IN ({','.join('?' for _ in values)})", list(values)


def _postgres_in_clause(column: str, values: Sequence[str]) -> tuple[str, list[Any]]:
    return f"{column} = ANY(%s)", [list(values)]


def _read_sqlite(db_path: Path, now: datetime) -> tuple[_RawStore | None, LiveSource, list[str]]:
    if not db_path.exists():
        return None, LiveSource(backend="sqlite", status="missing"), []
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row

            def execute(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
                return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

            raw = _load_with(execute, _sqlite_in_clause, now)
    except sqlite3.Error as exc:
        logger.warning("Live line: SQLite control store unreadable: %s", exc)
        return None, LiveSource(backend="sqlite", status="error"), [f"control store unreadable: {exc}"]
    return raw, LiveSource(backend="sqlite", status="ok"), list(raw.warnings)


def _read_postgres(database_url: str, now: datetime) -> tuple[_RawStore | None, LiveSource, list[str]]:
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError:
        return (
            None,
            LiveSource(backend="postgres", status="error"),
            ["control store: psycopg not installed in this image"],
        )
    try:
        with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row, autocommit=True) as conn:

            def execute(sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
                with conn.cursor() as cur:
                    cur.execute(sql, tuple(params))
                    return list(cur.fetchall())

            raw = _load_with(execute, _postgres_in_clause, now)
    except Exception as exc:  # noqa: BLE001 - psycopg raises many driver-specific errors
        logger.warning("Live line: PostgreSQL control store unreadable: %s", sanitize_database_url(str(exc)))
        return (
            None,
            LiveSource(backend="postgres", status="error"),
            [f"control store unreadable ({type(exc).__name__})"],
        )
    return raw, LiveSource(backend="postgres", status="ok"), list(raw.warnings)


# --------------------------------------------------------------------------- projection


@dataclass
class _Attempt:
    row: dict[str, Any]
    iteration: int
    status: str
    started: datetime | None
    finished: datetime | None
    updated: datetime | None
    created: datetime | None
    cost: float


def _stage_status(value: Any) -> StageStatus:
    text = str(value or "pending").strip().lower()
    return text if text in _KNOWN_STAGE_STATUSES else "pending"  # type: ignore[return-value]


def _attempt_duration(attempt: _Attempt, now: datetime) -> float | None:
    if attempt.started is None:
        return None
    if attempt.finished is not None:
        return _seconds(attempt.started, attempt.finished)
    if attempt.status == "running":
        return _seconds(attempt.started, now)
    return None


def _build_stage(
    stage: str,
    attempts: list[_Attempt],
    claim: dict[str, Any] | None,
    now: datetime,
) -> LiveStage:
    ordered = sorted(attempts, key=lambda a: (a.iteration, a.updated or datetime.min.replace(tzinfo=timezone.utc)))
    latest = ordered[-1]
    row = latest.row
    refs = decode_refs(row.get("evidence_refs"))[:MAX_EVIDENCE_REFS]
    cause, diagnostic = resolve_cause_and_diagnostic(
        status=latest.status, stage=stage, raw_cause=row.get("cause_code"), evidence_refs=refs
    )
    timeout = row.get("timeout_seconds")
    lease_expires = _parse_dt(claim.get("expires_at")) if claim else None
    history = [
        LiveAttempt(
            iteration=item.iteration,
            status=item.status,
            started_at=_iso(item.started),
            finished_at=_iso(item.finished),
            duration_seconds=_attempt_duration(item, now),
            cost_usd=round(item.cost, 6),
            cause_code=(str(item.row.get("cause_code")) if item.row.get("cause_code") else None),
            updated_at=_iso(item.updated),
        )
        for item in ordered
    ]
    return LiveStage(
        stage=stage,
        label=_label(stage),
        status=_stage_status(latest.status),
        attempts=len(ordered),
        retry_count=_int(row.get("retry_count")),
        max_retries=_int(row.get("max_retries")),
        started_at=_iso(latest.started),
        finished_at=_iso(latest.finished),
        duration_seconds=_attempt_duration(latest, now),
        cost_usd=round(sum(item.cost for item in ordered), 6),
        cause_code=cause,
        diagnostic=diagnostic,
        role=(str(row.get("role")) if row.get("role") else None),
        worker=(str(claim.get("owner")) if claim and claim.get("owner") else None),
        route=(str(claim.get("route_ref")) if claim and claim.get("route_ref") else None),
        lease_expires_at=_iso(lease_expires),
        timeout_seconds=_int(timeout) if timeout is not None else None,
        evidence_refs=refs,
        history=history,
    )


def _not_reached(stage: str) -> LiveStage:
    return LiveStage(stage=stage, label=_label(stage), status="not_reached")


_ATTENTION_FALLBACK = {
    "failed": "Falhou em {label}",
    "retry": "Nova tentativa pendente em {label}",
    "replan": "Replanejamento pendente em {label}",
    "waiting_dependency": "Aguardando dependência em {label}",
    "waiting_human": "Aguardando decisão manual em {label}",
}


def _run_state(
    *, active: bool, run_status: str, stages: list[LiveStage]
) -> RunState:
    reached = [item for item in stages if item.status != "not_reached"]
    statuses = [item.status for item in reached]
    has_failed = "failed" in statuses
    if not active:
        lowered = run_status.strip().lower()
        if lowered in {"cancelled", "canceled", "aborted"}:
            return "cancelled"
        if lowered == "failed" or has_failed:
            return "failed"
        if lowered in {"completed", "succeeded", "closed"}:
            return "succeeded"
        if statuses and all(s == "cancelled" for s in statuses):
            return "cancelled"
        return "succeeded" if statuses and all(s == "succeeded" for s in statuses) else "failed"
    if any(s in _ATTENTION_STAGE_STATUSES for s in statuses):
        return "attention"
    if "running" in statuses:
        return "running"
    if not statuses or all(s == "pending" for s in statuses):
        return "queued"
    if all(s == "succeeded" for s in statuses) and all(
        any(item.stage == line_stage and item.status == "succeeded" for item in stages)
        for line_stage in LINE_STAGES
    ):
        return "succeeded"
    # Between stages (a job succeeded, the next one is not scheduled yet) the run is still moving.
    return "running"


def _current_stage(stages: list[LiveStage]) -> LiveStage | None:
    reached = [item for item in stages if item.status != "not_reached"]
    if not reached:
        return None
    for item in reached:
        if item.status == "running" or item.status in _ATTENTION_STAGE_STATUSES:
            return item
    return reached[-1]


def _stall_reason(
    *,
    state: RunState,
    stages: list[LiveStage],
    attempts_by_stage: Mapping[str, list[_Attempt]],
    now: datetime,
) -> str | None:
    for item in stages:
        if item.status != "running":
            continue
        latest = max(attempts_by_stage[item.stage], key=lambda a: a.iteration)
        started = latest.started or latest.updated
        if started is not None and item.timeout_seconds and (now - started).total_seconds() > item.timeout_seconds:
            return f"{item.label} excedeu o timeout de {_fmt_duration(item.timeout_seconds)}"
        lease = _parse_dt(item.lease_expires_at)
        if lease is not None and lease < now:
            return f"lease de {item.label} expirou há {_fmt_duration((now - lease).total_seconds())}"
        if latest.updated is not None and now - latest.updated > RUNNING_IDLE_LIMIT:
            return f"{item.label} sem atividade há {_fmt_duration((now - latest.updated).total_seconds())}"
    if state == "queued":
        pending = [
            attempt.updated or attempt.created
            for attempts in attempts_by_stage.values()
            for attempt in attempts
            if attempt.status == "pending"
        ]
        pending_times = [moment for moment in pending if moment is not None]
        if pending_times:
            waited = now - min(pending_times)
            if waited > QUEUED_STALL_LIMIT:
                return f"na fila há {_fmt_duration(waited.total_seconds())}"
    return None


def _attempt_events(
    *,
    run: LiveRun,
    stage: str,
    attempt: _Attempt,
    diagnostic: str | None,
    now: datetime,
) -> list[LiveEvent]:
    label = _label(stage)
    events: list[LiveEvent] = []

    def add(at: datetime | None, kind: EventKind, message: str) -> None:
        if at is None:
            return
        events.append(
            LiveEvent(
                at=at.isoformat(),
                run_id=run.run_id,
                ticket_id=run.ticket_id,
                title=run.title,
                project_id=run.project_id,
                stage=stage,
                kind=kind,
                message=message,
            )
        )

    if attempt.started is not None:
        if attempt.iteration > 0:
            add(attempt.started, "stage_started", f"reiniciou {label} (tentativa {attempt.iteration + 1})")
        else:
            add(attempt.started, "stage_started", f"entrou em {label}")
    cause = str(attempt.row.get("cause_code") or "").strip()
    if attempt.status == "succeeded" and attempt.finished is not None:
        took = _seconds(attempt.started, attempt.finished)
        suffix = f" em {_fmt_duration(took)}" if took is not None else ""
        add(attempt.finished, "stage_succeeded", f"concluiu {label}{suffix}")
    elif attempt.status == "failed":
        suffix = f": {cause}" if cause else ""
        add(attempt.finished or attempt.updated, "stage_failed", f"falhou em {label}{suffix}")
    elif attempt.status == "waiting_human":
        add(attempt.updated, "waiting_human", f"aguarda você: {diagnostic or 'decisão manual pendente'}")
    elif attempt.status in {"retry", "replan"}:
        verb = "nova tentativa em" if attempt.status == "retry" else "replanejando"
        suffix = f" ({cause})" if cause else ""
        add(attempt.updated, "retry", f"{verb} {label}{suffix}")
    return events


def _build_runs(
    raw: _RawStore,
    *,
    now: datetime,
    titles: Mapping[str, str],
) -> tuple[list[LiveRun], list[LiveEvent], dict[str, list[tuple[datetime, float]]]]:
    jobs_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in raw.jobs:
        jobs_by_run[str(job["run_id"])].append(job)

    claims_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    for claim in sorted(raw.claims, key=lambda c: _parse_dt(c.get("acquired_at")) or now):
        claims_by_key[(str(claim["run_id"]), str(claim["stage"]), _int(claim.get("iteration")))] = claim

    intake_titles: dict[str, str] = {}
    for item in raw.intake:  # already ordered newest first
        run_id = item.get("run_id")
        if run_id is None or str(run_id) in intake_titles:
            continue
        title = extract_title(item.get("payload"))
        if title:
            intake_titles[str(run_id)] = title

    runs: list[LiveRun] = []
    events: list[LiveEvent] = []
    cost_points: dict[str, list[tuple[datetime, float]]] = {}

    for run_row in raw.runs:
        run_id = str(run_row["run_id"])
        run_status = str(run_row.get("status") or "active")
        active = not _is_terminal(run_status)
        project_id = str(run_row.get("project_id") or "unknown")
        demand_id = str(run_row.get("demand_id") or run_id)
        created = _parse_dt(run_row.get("created_at"))
        updated = _parse_dt(run_row.get("updated_at"))
        completed = _parse_dt(run_row.get("completed_at"))

        attempts_by_stage: dict[str, list[_Attempt]] = {}
        stage_first_seen: list[str] = []
        latest_job_row: dict[str, Any] | None = None
        latest_job_updated: datetime | None = None
        points: list[tuple[datetime, float]] = []
        run_jobs = sorted(
            jobs_by_run.get(run_id, []),
            key=lambda j: (_parse_dt(j.get("created_at")) or now, _int(j.get("iteration"))),
        )
        for job in run_jobs:
            stage = str(job["stage"])
            attempt = _Attempt(
                row=job,
                iteration=_int(job.get("iteration")),
                status=str(job.get("status") or "pending").strip().lower(),
                started=_parse_dt(job.get("started_at")),
                finished=_parse_dt(job.get("finished_at")),
                updated=_parse_dt(job.get("updated_at")),
                created=_parse_dt(job.get("created_at")),
                cost=_cost(job.get("actual_cost")),
            )
            if stage not in attempts_by_stage:
                attempts_by_stage[stage] = []
                stage_first_seen.append(stage)
            attempts_by_stage[stage].append(attempt)
            moment = attempt.finished or attempt.updated
            if moment is not None and attempt.cost > 0:
                points.append((moment, attempt.cost))
            if latest_job_row is None or (
                attempt.updated is not None
                and (latest_job_updated is None or attempt.updated >= latest_job_updated)
            ):
                latest_job_row = job
                latest_job_updated = attempt.updated
        cost_points[run_id] = points

        extra_stages = [name for name in stage_first_seen if name not in LINE_STAGES]
        stages: list[LiveStage] = []
        for stage in (*LINE_STAGES, *extra_stages):
            attempts = attempts_by_stage.get(stage)
            if not attempts:
                if stage in LINE_STAGES:
                    stages.append(_not_reached(stage))
                continue
            latest_iteration = max(item.iteration for item in attempts)
            claim = claims_by_key.get((run_id, stage, latest_iteration))
            stages.append(_build_stage(stage, attempts, claim, now))

        state = _run_state(active=active, run_status=run_status, stages=stages)
        current = _current_stage(stages)
        attention: LiveAttention | None = None
        if state == "attention" and current is not None:
            fallback = _ATTENTION_FALLBACK.get(current.status, "{label}").format(label=current.label)
            since = None
            if current.history:
                since = current.history[-1].updated_at
            attention = LiveAttention(
                stage=current.stage,
                cause_code=current.cause_code,
                diagnostic=current.diagnostic or fallback,
                since=since,
            )
        stall = (
            _stall_reason(state=state, stages=stages, attempts_by_stage=attempts_by_stage, now=now)
            if active
            else None
        )
        ticket_id = str((latest_job_row or {}).get("ticket_id") or demand_id)
        title = (
            intake_titles.get(run_id)
            or titles.get(demand_id)
            or titles.get(ticket_id)
            or demand_id
        )[:240]
        age = _seconds(created, now) if active else None
        cycle = None
        if not active:
            cycle = _seconds(created, completed or updated)

        run = LiveRun(
            run_id=run_id,
            ticket_id=ticket_id,
            demand_id=demand_id,
            project_id=project_id,
            title=title,
            mode=str(run_row.get("mode") or "autonomous"),
            run_status=run_status,
            state=state,
            current_stage=current.stage if current else None,
            current_stage_status=current.status if current else None,
            attention=attention,
            stalled=stall is not None,
            stalled_reason=stall,
            created_at=_iso(created),
            updated_at=_iso(updated),
            completed_at=_iso(completed),
            age_seconds=age,
            cycle_seconds=cycle,
            total_cost_usd=round(sum(p[1] for p in points), 6),
            iterations_total=len(run_jobs),
            stages=stages,
        )
        runs.append(run)

        for stage_item in stages:
            for attempt in attempts_by_stage.get(stage_item.stage, []):
                events.extend(
                    _attempt_events(
                        run=run,
                        stage=stage_item.stage,
                        attempt=attempt,
                        diagnostic=stage_item.diagnostic,
                        now=now,
                    )
                )
        if completed is not None:
            message = "run concluída" if state == "succeeded" else "run encerrada"
            if state == "failed":
                message = "run encerrada com falha"
            if cycle is not None and state == "succeeded":
                message += f" em {_fmt_duration(cycle)}"
            events.append(
                LiveEvent(
                    at=completed.isoformat(),
                    run_id=run.run_id,
                    ticket_id=run.ticket_id,
                    title=run.title,
                    project_id=run.project_id,
                    stage=None,
                    kind="run_completed",
                    message=message,
                )
            )

    runs.sort(key=_run_sort_key)
    events.sort(key=lambda e: (e.at, e.run_id, e.stage or "", e.kind), reverse=True)
    return runs, events[:MAX_EVENTS], cost_points


_STATE_RANK: dict[str, int] = {"attention": 0, "running": 1, "queued": 2}


def _run_sort_key(run: LiveRun) -> tuple[int, float, str]:
    rank = _STATE_RANK.get(run.state)
    if rank is not None:
        return (rank, -(run.age_seconds or 0.0), run.run_id)
    updated = _parse_dt(run.updated_at)
    stamp = updated.timestamp() if updated is not None else 0.0
    return (3, -stamp, run.run_id)


def _build_kpis(
    runs: list[LiveRun],
    cost_points: Mapping[str, list[tuple[datetime, float]]],
    *,
    now: datetime,
    planned_backlog: int,
) -> LiveKpis:
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    def finished_at(run: LiveRun) -> datetime | None:
        return _parse_dt(run.completed_at) or _parse_dt(run.updated_at)

    completed_24h = failed_24h = 0
    cycles: list[float] = []
    today = now.date()
    per_day: dict[date, int] = {today - timedelta(days=offset): 0 for offset in range(7)}
    for run in runs:
        if run.state not in {"succeeded", "failed"}:
            continue
        moment = finished_at(run)
        if moment is None:
            continue
        if moment >= day_ago:
            if run.state == "succeeded":
                completed_24h += 1
            else:
                failed_24h += 1
        if run.state == "succeeded" and moment >= week_ago:
            if run.cycle_seconds is not None:
                cycles.append(run.cycle_seconds)
            if moment.date() in per_day:
                per_day[moment.date()] += 1

    cost_24h = sum(
        amount
        for points in cost_points.values()
        for moment, amount in points
        if moment >= day_ago
    )
    active = [run for run in runs if run.state in _STATE_RANK]
    return LiveKpis(
        active_runs=len(active),
        attention=sum(1 for run in active if run.state == "attention"),
        running_stages=sum(1 for run in active for item in run.stages if item.status == "running"),
        completed_24h=completed_24h,
        failed_24h=failed_24h,
        median_cycle_seconds_7d=_percentile(cycles, 0.5),
        p85_cycle_seconds_7d=_percentile(cycles, 0.85),
        cost_24h_usd=round(cost_24h, 4),
        throughput_7d=[
            LiveThroughputDay(date=day.isoformat(), completed=per_day[day]) for day in sorted(per_day)
        ],
        planned_backlog=planned_backlog,
    )


def _ticket_field(ticket: Mapping[str, Any], name: str) -> str:
    return _enum_text(ticket.get(name)).strip()


def _build_backlog(
    tickets: Sequence[Mapping[str, Any]], runs: list[LiveRun]
) -> tuple[list[LiveBacklogProject], list[LiveTicket], int]:
    per_project: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for ticket in tickets:
        per_project[_ticket_field(ticket, "project_id") or "unknown"].append(ticket)

    projects: list[LiveBacklogProject] = []
    planned_total = 0
    for project_id in sorted(per_project):
        items = per_project[project_id]
        planned = [t for t in items if _ticket_field(t, "status") == "planned"]
        planned.sort(
            key=lambda t: (
                _HORIZON_RANK.get(_ticket_field(t, "horizon"), len(_HORIZON_RANK)),
                _natural_key(_ticket_field(t, "id")),
            )
        )
        planned_total += len(planned)
        projects.append(
            LiveBacklogProject(
                project_id=project_id,
                planned=len(planned),
                implementing=sum(1 for t in items if _ticket_field(t, "status") in _OFF_LINE_STATUSES),
                completed=sum(1 for t in items if _ticket_field(t, "status") == "completed"),
                total=len(items),
                next=[
                    LiveBacklogNext(
                        id=_ticket_field(t, "id"),
                        title=_ticket_field(t, "title")[:240],
                        horizon=_ticket_field(t, "horizon") or "unscheduled",
                    )
                    for t in planned[:MAX_NEXT_BACKLOG]
                ],
            )
        )

    in_line: set[str] = set()
    for run in runs:
        if run.state in _STATE_RANK:
            in_line.update({run.demand_id, run.ticket_id})
    off_line = [
        LiveTicket(
            id=_ticket_field(t, "id"),
            project_id=_ticket_field(t, "project_id") or "unknown",
            title=_ticket_field(t, "title")[:240],
            status=_ticket_field(t, "status"),
            updated_at=_iso(_parse_dt(t.get("updated_at"))),
        )
        for t in tickets
        if _ticket_field(t, "status") in _OFF_LINE_STATUSES and _ticket_field(t, "id") not in in_line
    ]
    off_line.sort(key=lambda t: (t.updated_at or "", t.id), reverse=True)
    return projects, off_line, planned_total


def _natural_key(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    buffer = ""
    for char in value:
        if char.isdigit():
            buffer += char
            continue
        if buffer:
            parts.append((1, int(buffer), ""))
            buffer = ""
        parts.append((0, 0, char))
    if buffer:
        parts.append((1, int(buffer), ""))
    return tuple(parts)


def compute_version(payload: Mapping[str, Any]) -> str:
    """Stable digest of the snapshot, ignoring everything that merely depends on the clock.

    ``generated_at``, run ages, the live duration of running stages/attempts and the
    minute counters inside ``stalled_reason`` change every second without any data
    change; leaving them out makes the version change only when the store does.
    """
    data = json.loads(json.dumps(payload, default=str))
    data.pop("generated_at", None)
    data.pop("version", None)
    for run in data.get("runs", []):
        run["age_seconds"] = None
        if run.get("stalled_reason"):
            run["stalled_reason"] = "stalled"
        for stage in run.get("stages", []):
            if stage.get("status") == "running":
                stage["duration_seconds"] = None
            for attempt in stage.get("history", []):
                if attempt.get("finished_at") is None:
                    attempt["duration_seconds"] = None
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_line_live(
    raw: _RawStore | None,
    source: LiveSource,
    warnings: Sequence[str],
    *,
    now: datetime,
    titles: Mapping[str, str],
    tickets: Sequence[Mapping[str, Any]],
) -> LineLiveSnapshot:
    runs: list[LiveRun] = []
    events: list[LiveEvent] = []
    cost_points: dict[str, list[tuple[datetime, float]]] = {}
    if raw is not None:
        runs, events, cost_points = _build_runs(raw, now=now, titles=titles)
    backlog, off_line, planned_total = _build_backlog(tickets, runs)
    kpis = _build_kpis(runs, cost_points, now=now, planned_backlog=planned_total)
    stage_order = list(LINE_STAGES)
    extra = [item.stage for run in runs for item in run.stages if item.stage not in LINE_STAGES]
    labels = {stage: _label(stage) for stage in (*stage_order, *dict.fromkeys(extra))}
    labels.update({stage: label for stage, label in STAGE_LABELS.items() if stage not in labels})
    snapshot = LineLiveSnapshot(
        generated_at=now.isoformat(),
        version="0" * 16,
        source=source,
        warnings=list(warnings),
        stage_order=stage_order,
        stage_labels=labels,
        runs=runs,
        events=events,
        kpis=kpis,
        backlog=backlog,
        off_line=off_line,
    )
    snapshot.version = compute_version(snapshot.model_dump(mode="json"))
    return snapshot


def read_line_live(
    sqlite_path: Path | None,
    *,
    database_url: str | None = None,
    now: datetime | None = None,
    titles: Mapping[str, str] | None = None,
    tickets: Sequence[dict[str, Any]] | None = None,
) -> LineLiveSnapshot:
    """Read the control store and project the live line; never raises, never writes."""
    moment = _parse_dt(now) if now is not None else _utc_now()
    assert moment is not None
    if database_url is None:
        database_url = next((os.environ[name] for name in DATABASE_URL_ENVS if os.environ.get(name)), "")
    raw: _RawStore | None
    try:
        if database_url and not database_url.startswith("mock"):
            raw, source, warnings = _read_postgres(database_url, moment)
        elif sqlite_path is None:
            raw, source, warnings = None, LiveSource(backend="none", status="missing"), []
        else:
            raw, source, warnings = _read_sqlite(sqlite_path, moment)
    except Exception as exc:  # noqa: BLE001 - the panel must survive any source failure
        logger.warning("Live line: unexpected read failure: %s", sanitize_database_url(str(exc)))
        raw = None
        source = LiveSource(backend="postgres" if database_url else "sqlite", status="error")
        warnings = [f"control store unreadable ({type(exc).__name__})"]
    try:
        return build_line_live(
            raw, source, warnings, now=moment, titles=titles or {}, tickets=tickets or []
        )
    except Exception as exc:  # projection bug must not take the Hub down
        logger.exception("Live line: projection failed")
        return build_line_live(
            None,
            LiveSource(backend=source.backend, status="error"),
            [*warnings, f"projection failed ({type(exc).__name__})"],
            now=moment,
            titles={},
            tickets=[],
        )
