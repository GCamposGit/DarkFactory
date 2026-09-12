"""
SQLite transactional store for AI model execution telemetry.
Operates in Write-Ahead Logging (WAL) mode for near-real-time concurrent reads and writes.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from core.paths import project_root
from core.telemetry.hardware import get_hardware_context
from core.telemetry.models import (
    BreakdownItem,
    ExecutionMode,
    TelemetryFilters,
    TelemetryQueryResult,
    TelemetryRecord,
    TelemetryRecordCreate,
    TelemetryStats,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = project_root() / ".factory" / "telemetry.db"


class TelemetryStore:
    """Thread-safe SQLite store for model runs telemetry."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=15.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _init_db(self) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=5000;")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS model_telemetry_runs (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    ticket_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    os_platform TEXT NOT NULL,
                    accelerator TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    processing_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    success INTEGER NOT NULL,
                    error_message TEXT,
                    metadata_json TEXT
                );
                """
            )

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_timestamp ON model_telemetry_runs (timestamp DESC);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_ticket ON model_telemetry_runs (ticket_id);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_model ON model_telemetry_runs (model);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_provider ON model_telemetry_runs (provider);"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_telemetry_execution_mode ON model_telemetry_runs (execution_mode);"
            )
            conn.commit()

    def record(self, record: TelemetryRecordCreate) -> TelemetryRecord:
        """Persist a single model telemetry run."""
        hw = get_hardware_context(
            explicit_mode=record.execution_mode,
            device_override=record.device_name,
        )

        device_name = record.device_name or hw.device_name
        os_platform = record.os_platform or hw.os_platform
        accelerator = record.accelerator or hw.accelerator
        execution_mode = record.execution_mode or hw.execution_mode

        total_tokens = (
            record.total_tokens
            if record.total_tokens is not None
            else (record.input_tokens + record.processing_tokens + record.output_tokens)
        )

        persisted = TelemetryRecord(
            id=record.id or "",
            timestamp=record.timestamp or "",
            ticket_id=record.ticket_id,
            provider=record.provider,
            model=record.model,
            tier=record.tier,
            harness=record.harness,
            execution_mode=execution_mode,
            device_name=device_name,
            os_platform=os_platform,
            accelerator=accelerator,
            input_tokens=record.input_tokens,
            processing_tokens=record.processing_tokens,
            output_tokens=record.output_tokens,
            total_tokens=total_tokens,
            latency_ms=round(record.latency_ms, 2),
            cost_usd=round(record.cost_usd, 6),
            success=record.success,
            error_message=record.error_message,
            metadata=record.metadata,
        )

        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_telemetry_runs (
                    id, timestamp, ticket_id, provider, model, tier, harness,
                    execution_mode, device_name, os_platform, accelerator,
                    input_tokens, processing_tokens, output_tokens, total_tokens,
                    latency_ms, cost_usd, success, error_message, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    persisted.id,
                    persisted.timestamp,
                    persisted.ticket_id,
                    persisted.provider,
                    persisted.model,
                    persisted.tier,
                    persisted.harness,
                    persisted.execution_mode.value,
                    persisted.device_name,
                    persisted.os_platform,
                    persisted.accelerator,
                    persisted.input_tokens,
                    persisted.processing_tokens,
                    persisted.output_tokens,
                    persisted.total_tokens,
                    persisted.latency_ms,
                    persisted.cost_usd,
                    1 if persisted.success else 0,
                    persisted.error_message,
                    json.dumps(persisted.metadata, ensure_ascii=False),
                ),
            )
            conn.commit()

        return persisted

    def _build_where_clause(
        self, filters: Optional[TelemetryFilters]
    ) -> tuple[str, list[Any]]:
        if not filters:
            return "", []

        clauses: list[str] = []
        params: list[Any] = []

        if filters.ticket_id:
            clauses.append("ticket_id = ?")
            params.append(filters.ticket_id)
        if filters.model:
            clauses.append("model = ?")
            params.append(filters.model)
        if filters.provider:
            clauses.append("provider = ?")
            params.append(filters.provider)
        if filters.execution_mode:
            clauses.append("execution_mode = ?")
            params.append(filters.execution_mode.value)
        if filters.success is not None:
            clauses.append("success = ?")
            params.append(1 if filters.success else 0)
        if filters.start_date:
            clauses.append("timestamp >= ?")
            params.append(filters.start_date)
        if filters.end_date:
            clauses.append("timestamp <= ?")
            params.append(filters.end_date)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, params

    def _row_to_record(self, row: sqlite3.Row) -> TelemetryRecord:
        meta = {}
        if row["metadata_json"]:
            try:
                meta = json.loads(row["metadata_json"])
            except Exception:
                meta = {}

        return TelemetryRecord(
            id=row["id"],
            timestamp=row["timestamp"],
            ticket_id=row["ticket_id"],
            provider=row["provider"],
            model=row["model"],
            tier=row["tier"],
            harness=row["harness"],
            execution_mode=ExecutionMode(row["execution_mode"]),
            device_name=row["device_name"],
            os_platform=row["os_platform"],
            accelerator=row["accelerator"],
            input_tokens=row["input_tokens"],
            processing_tokens=row["processing_tokens"],
            output_tokens=row["output_tokens"],
            total_tokens=row["total_tokens"],
            latency_ms=row["latency_ms"],
            cost_usd=row["cost_usd"],
            success=bool(row["success"]),
            error_message=row["error_message"],
            metadata=meta,
        )

    def query_runs(
        self, filters: Optional[TelemetryFilters] = None
    ) -> TelemetryQueryResult:
        """Query paginated model runs based on filters."""
        f = filters or TelemetryFilters()
        where_sql, params = self._build_where_clause(f)

        with self._lock, self._connection() as conn:
            count_cursor = conn.execute(
                f"SELECT COUNT(*) as cnt FROM model_telemetry_runs {where_sql};",
                params,
            )
            total_count = count_cursor.fetchone()["cnt"]

            query_sql = f"""
                SELECT * FROM model_telemetry_runs
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?;
            """
            rows = conn.execute(query_sql, params + [f.limit, f.offset]).fetchall()
            records = [self._row_to_record(r) for r in rows]

        return TelemetryQueryResult(
            runs=records,
            total_count=total_count,
            limit=f.limit,
            offset=f.offset,
        )

    def get_stats(
        self, filters: Optional[TelemetryFilters] = None
    ) -> TelemetryStats:
        """Calculate analytical statistics across matching telemetry runs."""
        where_sql, params = self._build_where_clause(filters)

        with self._lock, self._connection() as conn:
            agg_sql = f"""
                SELECT
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful_runs,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed_runs,
                    COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                    COALESCE(SUM(processing_tokens), 0) as total_processing_tokens,
                    COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                    COALESCE(AVG(latency_ms), 0.0) as avg_latency_ms
                FROM model_telemetry_runs
                {where_sql};
            """
            row = conn.execute(agg_sql, params).fetchone()
            total_runs = row["total_runs"] or 0
            successful_runs = row["successful_runs"] or 0
            failed_runs = row["failed_runs"] or 0
            success_rate = (
                round((successful_runs / total_runs) * 100.0, 2)
                if total_runs > 0
                else 0.0
            )

            # Calculate p95 latency
            p95_latency_ms = 0.0
            if total_runs > 0:
                p95_offset = int(total_runs * 0.95)
                p95_row = conn.execute(
                    f"SELECT latency_ms FROM model_telemetry_runs {where_sql} ORDER BY latency_ms ASC LIMIT 1 OFFSET ?;",
                    params + [p95_offset],
                ).fetchone()
                if p95_row:
                    p95_latency_ms = round(float(p95_row["latency_ms"]), 2)

            def _get_breakdown(column: str, limit: int = 10) -> list[BreakdownItem]:
                sql = f"""
                    SELECT {column} as name, COUNT(*) as cnt, COALESCE(SUM(total_tokens), 0) as tokens, COALESCE(SUM(cost_usd), 0.0) as cost
                    FROM model_telemetry_runs
                    {where_sql}
                    GROUP BY {column}
                    ORDER BY cnt DESC
                    LIMIT {limit};
                """
                b_rows = conn.execute(sql, params).fetchall()
                return [
                    BreakdownItem(
                        name=str(b["name"] or "None"),
                        count=b["cnt"],
                        total_tokens=b["tokens"],
                        total_cost_usd=round(float(b["cost"]), 6),
                    )
                    for b in b_rows
                    if b["name"] is not None
                ]

            by_model = _get_breakdown("model")
            by_provider = _get_breakdown("provider")
            by_ticket = _get_breakdown("ticket_id")
            by_execution_mode = _get_breakdown("execution_mode")
            by_hardware = _get_breakdown("accelerator")

        return TelemetryStats(
            total_runs=total_runs,
            successful_runs=successful_runs,
            failed_runs=failed_runs,
            success_rate_percent=success_rate,
            total_input_tokens=row["total_input_tokens"],
            total_processing_tokens=row["total_processing_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_tokens=row["total_tokens"],
            total_cost_usd=round(float(row["total_cost_usd"]), 6),
            avg_latency_ms=round(float(row["avg_latency_ms"]), 2),
            p95_latency_ms=p95_latency_ms,
            by_model=by_model,
            by_provider=by_provider,
            by_ticket=by_ticket,
            by_execution_mode=by_execution_mode,
            by_hardware=by_hardware,
        )

    def list_tickets(self) -> list[str]:
        """Return distinct non-null tickets that have telemetry runs."""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT ticket_id
                FROM model_telemetry_runs
                WHERE ticket_id IS NOT NULL AND ticket_id != ''
                ORDER BY ticket_id ASC;
                """
            ).fetchall()
            return [r["ticket_id"] for r in rows if r["ticket_id"]]

    def import_legacy_if_empty(self, legacy_json_path: Path) -> int:
        """Imports legacy model_usage.json events if the SQLite store is empty."""
        legacy_path = Path(legacy_json_path)
        if not legacy_path.is_file():
            return 0

        with self._lock, self._connection() as conn:
            cnt = conn.execute("SELECT COUNT(*) as cnt FROM model_telemetry_runs;").fetchone()["cnt"]
            if cnt > 0:
                return 0

        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            events = data.get("events", [])
            imported = 0
            for ev in events:
                rec = TelemetryRecordCreate(
                    id=ev.get("invocation_id"),
                    timestamp=ev.get("timestamp"),
                    ticket_id=ev.get("ticket_id"),
                    provider=ev.get("provider", "unknown"),
                    model=ev.get("model", "unknown"),
                    tier=ev.get("tier", "local"),
                    harness=ev.get("harness", "legacy"),
                    execution_mode=ExecutionMode.HEADLESS,
                    input_tokens=ev.get("input_tokens") or 0,
                    processing_tokens=0,
                    output_tokens=ev.get("output_tokens") or 0,
                    latency_ms=ev.get("latency_ms") or 0.0,
                    cost_usd=ev.get("cost_usd") or 0.0,
                    success=ev.get("success", True),
                )
                self.record(rec)
                imported += 1
            return imported
        except Exception as exc:
            logger.debug("Failed to import legacy telemetry events: %s", exc)
            return 0
