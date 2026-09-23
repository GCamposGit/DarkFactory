"""Read-only job board projection over the canonical HF-05 control store (USR-42).

The DarkHub must show the real workflow state. Locally that state lives in
``.factory/control.db`` (SQLite); in the cloud the coordinator and workers share
the PostgreSQL database behind ``DARKFAC_HF02_DATABASE_URL`` (or the Hub's
read-only ``DARKHUB_CONTROL_DATABASE_URL``). This module reads
both without creating files, running DDL or taking leases, and folds the job
rows into one entry per ``(run_id, ticket_id)``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.orchestrator.cloud_db import sanitize_database_url

logger = logging.getLogger(__name__)

# The Hub is internet-facing, so it may use a dedicated read-only role; the
# shared coordinator URL is the fallback.
DATABASE_URL_ENVS: tuple[str, ...] = ("DARKHUB_CONTROL_DATABASE_URL", "DARKFAC_HF02_DATABASE_URL")
DEFAULT_ROW_LIMIT = 500

ATTENTION_STATUSES: frozenset[str] = frozenset(
    {"failed", "waiting_human", "retry", "replan", "waiting_dependency"}
)

_JOB_QUERY = """
SELECT j.run_id, j.ticket_id, j.stage, j.iteration, j.status, j.role, j.cause_code,
       j.actual_cost, j.retry_count, j.max_retries, j.evidence_refs, j.updated_at,
       r.project_id, r.demand_id, r.status AS run_status, r.mode,
       (
           SELECT ic.payload FROM intake_commands ic
           WHERE ic.run_id = j.run_id
           ORDER BY ic.committed_at DESC
           LIMIT 1
       ) AS intake_payload
FROM jobs j
JOIN runs r ON r.run_id = j.run_id
ORDER BY j.updated_at DESC
LIMIT {placeholder}
"""


class JobBoardEntry(BaseModel):
    """Latest observable state of one ticket inside one workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    ticket_id: str
    project_id: str
    demand_id: str
    mode: str
    run_status: str
    stage: str
    status: str
    role: str
    iteration: int = Field(ge=0)
    retry_count: int = Field(default=0, ge=0)
    max_retries: int = Field(default=0, ge=0)
    cause_code: str | None = None
    diagnostic: str | None = None
    total_cost_usd: float = Field(default=0.0, ge=0.0)
    evidence_refs: list[str] = Field(default_factory=list)
    stages_seen: list[str] = Field(default_factory=list)
    updated_at: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _ensure_waiting_human_diagnostic(self) -> Self:
        if self.status.lower() == "waiting_human":
            cause, diag = _resolve_cause_and_diagnostic(
                status=self.status,
                stage=self.stage,
                raw_cause=self.cause_code,
                evidence_refs=self.evidence_refs,
            )
            if not self.cause_code:
                self.cause_code = cause
            if not self.diagnostic:
                self.diagnostic = diag
        elif self.cause_code and not self.diagnostic:
            self.diagnostic = self.cause_code
        return self

    @property
    def needs_attention(self) -> bool:
        return self.status in ATTENTION_STATUSES


class JobBoardSnapshot(BaseModel):
    """Result of reading the control store, including where it came from."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["postgres", "sqlite", "none"]
    source: Literal["ok", "missing", "error"]
    entries: list[JobBoardEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _decode_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
    if isinstance(value, list):
        return [item if isinstance(item, str) else json.dumps(item, sort_keys=True) for item in value]
    return [json.dumps(value, sort_keys=True)]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _extract_title(value: Any) -> str | None:
    """Extract the human ``title`` from an ``intake_commands.payload`` column value.

    The column comes back differently depending on driver and backend: SQLite always
    hands back the raw TEXT (a JSON string); psycopg may already decode Postgres JSONB
    into a ``dict``, or hand back ``bytes``. Malformed JSON, a non-object payload, or a
    missing/non-string ``title`` all resolve to ``None`` rather than raising, since the
    title is a cosmetic enhancement over the demand id and must never block the board.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    title = value.get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    return title[:240] if title else None


def _resolve_cause_and_diagnostic(
    status: str,
    stage: str,
    raw_cause: Any,
    evidence_refs: list[str],
) -> tuple[str | None, str | None]:
    """Ensure waiting_human jobs have an informative cause_code and diagnostic (DH-03 / USR-47)."""
    raw_cause_str = str(raw_cause).strip() if raw_cause is not None and str(raw_cause).strip() else None
    if str(status).lower() != "waiting_human":
        return raw_cause_str, raw_cause_str

    stage_lower = (stage or "").lower()
    has_grill = stage_lower == "grill" or any("grill" in str(ref).lower() for ref in evidence_refs)
    has_dep = "depend" in stage_lower or any("depend" in str(ref).lower() for ref in evidence_refs)

    if raw_cause_str:
        if "grill" in raw_cause_str.lower() or has_grill:
            diag = "Alinhamento Grill pendente com o Owner"
        elif "depend" in raw_cause_str.lower() or has_dep:
            diag = "Dependência manual pendente"
        else:
            diag = raw_cause_str
        return raw_cause_str, diag

    if has_grill:
        return "grill_pending", "Alinhamento Grill pendente com o Owner"
    if has_dep:
        return "waiting_dependency", "Dependência manual pendente"

    fallback_cause = f"waiting_human_{stage_lower}" if stage_lower else "manual_decision_pending"
    fallback_diag = f"Aguardando decisão manual ({stage})" if stage else "Aguardando decisão manual do Owner"
    return fallback_cause, fallback_diag


def fold_job_rows(rows: Iterable[dict[str, Any]]) -> list[JobBoardEntry]:
    """Fold job rows (newest first) into one entry per run and ticket."""
    entries: dict[tuple[str, str], JobBoardEntry] = {}
    for row in rows:
        key = (str(row["run_id"]), str(row["ticket_id"]))
        cost = max(0.0, float(row.get("actual_cost") or 0.0))
        stage = str(row["stage"])
        current = entries.get(key)
        if current is None:
            refs = _decode_refs(row.get("evidence_refs"))[:12]
            status_val = str(row["status"])
            resolved_cause, resolved_diag = _resolve_cause_and_diagnostic(
                status=status_val,
                stage=stage,
                raw_cause=row.get("cause_code"),
                evidence_refs=refs,
            )
            entries[key] = JobBoardEntry(
                run_id=key[0],
                ticket_id=key[1],
                project_id=str(row.get("project_id") or "unknown"),
                demand_id=str(row.get("demand_id") or key[1]),
                mode=str(row.get("mode") or "autonomous"),
                run_status=str(row.get("run_status") or "active"),
                stage=stage,
                status=status_val,
                role=str(row.get("role") or "unknown"),
                iteration=int(row.get("iteration") or 0),
                retry_count=int(row.get("retry_count") or 0),
                max_retries=int(row.get("max_retries") or 0),
                cause_code=resolved_cause,
                diagnostic=resolved_diag,
                total_cost_usd=cost,
                evidence_refs=refs,
                stages_seen=[stage],
                updated_at=_as_text(row.get("updated_at")),
                title=_extract_title(row.get("intake_payload")),
            )
            continue
        current.total_cost_usd = round(current.total_cost_usd + cost, 8)
        if stage not in current.stages_seen:
            current.stages_seen.append(stage)
    for entry in entries.values():
        entry.stages_seen.reverse()
    return list(entries.values())


def read_sqlite_job_board(db_path: Path, *, limit: int = DEFAULT_ROW_LIMIT) -> JobBoardSnapshot:
    """Read ``control.db`` in read-only mode; a missing file is not an error."""
    if not db_path.exists():
        return JobBoardSnapshot(backend="sqlite", source="missing")
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(_JOB_QUERY.format(placeholder="?"), (limit,))
            rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("Control store SQLite unreadable: %s", exc)
        return JobBoardSnapshot(backend="sqlite", source="error", warnings=[f"control store unreadable: {exc}"])
    return JobBoardSnapshot(backend="sqlite", source="ok", entries=fold_job_rows(rows))


def read_postgres_job_board(database_url: str, *, limit: int = DEFAULT_ROW_LIMIT) -> JobBoardSnapshot:
    """Read the cloud control store; failures never leak credentials."""
    try:
        import psycopg  # type: ignore[import-not-found]
        from psycopg.rows import dict_row  # type: ignore[import-not-found]
    except ImportError:
        return JobBoardSnapshot(
            backend="postgres",
            source="error",
            warnings=["control store: psycopg not installed in this image"],
        )
    try:
        with psycopg.connect(database_url, connect_timeout=5, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(_JOB_QUERY.format(placeholder="%s"), (limit,))
                rows = list(cur.fetchall())
    except Exception as exc:  # psycopg raises many driver-specific errors
        logger.warning("Control store PostgreSQL unreadable: %s", sanitize_database_url(str(exc)))
        return JobBoardSnapshot(
            backend="postgres",
            source="error",
            warnings=[f"control store unreadable ({type(exc).__name__})"],
        )
    return JobBoardSnapshot(backend="postgres", source="ok", entries=fold_job_rows(rows))


def read_job_board(
    sqlite_path: Path | None,
    *,
    database_url: str | None = None,
    limit: int = DEFAULT_ROW_LIMIT,
) -> JobBoardSnapshot:
    """Prefer the shared cloud store when configured, else the local SQLite file."""
    if database_url is None:
        database_url = next((os.environ[name] for name in DATABASE_URL_ENVS if os.environ.get(name)), "")
    url = database_url
    if url and not url.startswith("mock"):
        return read_postgres_job_board(url, limit=limit)
    if sqlite_path is None:
        return JobBoardSnapshot(backend="none", source="missing")
    return read_sqlite_job_board(sqlite_path, limit=limit)
