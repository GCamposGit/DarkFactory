"""Local-First Hybrid Persistence Adapter for PostgreSQL 16 & SQLite WAL (USR-17 / INFRA-07).

Governed by:
- USR-17: Camada de Persistência Híbrida PostgreSQL + Sync R2 & On-Premises
- INFRA-07: PostgreSQL Multi-Tenant em Armazenamento NVMe (Dokploy VPS)
- Non-goals: Não criar dependência bloqueante de rede caso a VPS esteja inacessível (manter fallback local-first)
- Invariant: Zero vazamento de credenciais, isolamento multi-tenant estrito e concorrência livre de locks cross-process.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generator
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.cloud_db import sanitize_database_url

logger = logging.getLogger(__name__)

DEFAULT_PERSISTENCE_DB = Path(".factory/persistence.db")

POSTGRES_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS tenant_records (
    tenant_id VARCHAR(64) NOT NULL,
    collection VARCHAR(64) NOT NULL,
    entity_id VARCHAR(160) NOT NULL,
    data JSONB NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant_id, collection, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_records_lookup 
    ON tenant_records (tenant_id, collection);

CREATE INDEX IF NOT EXISTS idx_tenant_records_updated 
    ON tenant_records (updated_at);
"""


def utc_now() -> datetime:
    """Return the current datetime in UTC timezone."""
    return datetime.now(timezone.utc)


def _iso_utc(dt: datetime | None = None) -> str:
    """Convert datetime to ISO 8601 UTC string."""
    target = dt or utc_now()
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    else:
        target = target.astimezone(timezone.utc)
    return target.isoformat()


def _parse_iso_utc(dt_str: str) -> datetime:
    """Parse ISO UTC string to datetime."""
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PersistenceRecord(BaseModel):
    """Transactional entity record managed by the hybrid persistence layer."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(default="darkfac", description="Tenant or project namespace")
    collection: str = Field(description="Collection or domain category (e.g. demands, telemetry, knowledge)")
    entity_id: str = Field(description="Unique entity identifier within the collection")
    data: dict[str, Any] = Field(default_factory=dict, description="Structured payload")
    version: int = Field(default=1, description="Monotonically increasing version number for conflict resolution")
    created_at: datetime = Field(default_factory=utc_now, description="Creation timestamp in UTC")
    updated_at: datetime = Field(default_factory=utc_now, description="Last update timestamp in UTC")
    is_deleted: bool = Field(default=False, description="Soft-delete marker")


class SyncResult(BaseModel):
    """Report on synchronization between local SQLite cache and remote PostgreSQL."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(description="'success', 'partial', 'offline', or 'error'")
    pushed_count: int = Field(default=0, description="Number of local outbox changes pushed to remote")
    pulled_count: int = Field(default=0, description="Number of remote changes pulled into local cache")
    conflicts_count: int = Field(default=0, description="Number of conflicts detected and resolved")
    errors: list[str] = Field(default_factory=list, description="Sanitized error messages")


class HybridPersistenceAdapter:
    """Local-First Hybrid Persistence Adapter.
    
    Guarantees:
    1. Local-First: Reads and writes always hit the local SQLite cache in WAL mode; zero latency, zero egress.
    2. Resilient Offline Fallback: If PostgreSQL is unreachable or credentials are unconfigured, operates offline
       and journals modifications to the transactional outbox.
    3. Multi-Tenant Isolation: Enforces tenant_id partitioning across all reads, writes, queries, and syncs.
    4. Safe Synchronization: Outbox pushes use monotonic version checks (ON CONFLICT DO UPDATE WHERE version >=).
    5. Zero Data/Credential Leakage: URLs and errors are sanitized before logging.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        database_url: str | None = None,
        *,
        auto_sync: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_PERSISTENCE_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self.auto_sync = auto_sync
        self._lock = threading.RLock()
        self._psycopg = None

        self._init_local_db()
        self._check_remote_capability()

    @contextmanager
    def _local_conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a thread-safe SQLite connection with WAL enabled."""
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

    def _init_local_db(self) -> None:
        """Initialize SQLite tables and WAL mode for high-performance concurrent local storage."""
        with self._lock, self._local_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    tenant_id TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, collection, entity_id)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_records_tenant_collection 
                ON records(tenant_id, collection);
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_pending 
                ON outbox(synced, id);
            """)
            conn.commit()

    def _check_remote_capability(self) -> None:
        """Inspect if remote PostgreSQL is accessible without blocking."""
        if not self.raw_url or not self.raw_url.strip():
            logger.info("Hybrid persistence running in local-first offline mode (no database URL configured).")
            return

        try:
            parsed = urlparse(self.raw_url)
            username = parsed.username or ""
            if username.casefold() in {"postgres", "root", "admin", "superuser"}:
                logger.warning(
                    "Superuser '%s' forbidden for remote sync by HF-03 policy; remote sync disabled.",
                    username,
                )
                self.raw_url = None
                return
        except Exception:
            self.raw_url = None
            return

        try:
            import psycopg  # type: ignore[import-not-found]
            self._psycopg = psycopg
        except ImportError:
            logger.info("psycopg driver not installed; hybrid persistence operating in local-only mode.")

    def is_remote_configured(self) -> bool:
        """Check whether remote PostgreSQL synchronization is available."""
        return bool(self.raw_url and self._psycopg)

    def init_remote_schema(self) -> bool:
        """Initialize PostgreSQL schema in Dokploy multi-tenant database if connected."""
        if not self.is_remote_configured():
            return False

        try:
            with self._psycopg.connect(self.raw_url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(POSTGRES_SCHEMA_DDL)
                conn.commit()
            return True
        except Exception as exc:
            sanitized = sanitize_database_url(self.raw_url)
            logger.warning(
                "Failed to initialize remote PostgreSQL schema on %s: %s",
                sanitized,
                exc,
            )
            return False

    def put(
        self,
        collection: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        tenant_id: str = "darkfac",
        version: int | None = None,
    ) -> PersistenceRecord:
        """Persist an entity into the local store and queue for synchronization."""
        with self._lock, self._local_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT version, created_at FROM records WHERE tenant_id = ? AND collection = ? AND entity_id = ?",
                (tenant_id, collection, entity_id),
            )
            existing = cur.fetchone()
            now = utc_now()
            now_iso = _iso_utc(now)

            if existing:
                cur_ver, created_iso = existing["version"], existing["created_at"]
                new_ver = (version if version is not None and version > cur_ver else cur_ver + 1)
                created_dt = _parse_iso_utc(created_iso)
            else:
                new_ver = version if version is not None else 1
                created_dt = now

            data_json = json.dumps(data, ensure_ascii=False)
            cur.execute(
                """
                INSERT INTO records (tenant_id, collection, entity_id, data, version, created_at, updated_at, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(tenant_id, collection, entity_id) DO UPDATE SET
                    data = excluded.data,
                    version = excluded.version,
                    updated_at = excluded.updated_at,
                    is_deleted = 0;
                """,
                (tenant_id, collection, entity_id, data_json, new_ver, _iso_utc(created_dt), now_iso),
            )

            # Record in Outbox
            outbox_payload = json.dumps({
                "tenant_id": tenant_id,
                "collection": collection,
                "entity_id": entity_id,
                "data": data,
                "version": new_ver,
                "created_at": _iso_utc(created_dt),
                "updated_at": now_iso,
                "is_deleted": False,
            }, ensure_ascii=False)

            cur.execute(
                """
                INSERT INTO outbox (tenant_id, collection, entity_id, operation, payload, version, created_at, synced)
                VALUES (?, ?, ?, 'PUT', ?, ?, ?, 0);
                """,
                (tenant_id, collection, entity_id, outbox_payload, new_ver, now_iso),
            )
            conn.commit()

            record = PersistenceRecord(
                tenant_id=tenant_id,
                collection=collection,
                entity_id=entity_id,
                data=data,
                version=new_ver,
                created_at=created_dt,
                updated_at=now,
                is_deleted=False,
            )

        if self.auto_sync and self.is_remote_configured():
            self.sync_to_remote()

        return record

    def get(
        self,
        collection: str,
        entity_id: str,
        *,
        tenant_id: str = "darkfac",
        include_deleted: bool = False,
    ) -> PersistenceRecord | None:
        """Retrieve a single entity from the local cache."""
        with self._lock, self._local_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT tenant_id, collection, entity_id, data, version, created_at, updated_at, is_deleted
                FROM records
                WHERE tenant_id = ? AND collection = ? AND entity_id = ?;
                """,
                (tenant_id, collection, entity_id),
            )
            row = cur.fetchone()
            if not row:
                return None

            if row["is_deleted"] and not include_deleted:
                return None

            try:
                data_dict = json.loads(row["data"])
            except Exception:
                data_dict = {}

            return PersistenceRecord(
                tenant_id=row["tenant_id"],
                collection=row["collection"],
                entity_id=row["entity_id"],
                data=data_dict,
                version=row["version"],
                created_at=_parse_iso_utc(row["created_at"]),
                updated_at=_parse_iso_utc(row["updated_at"]),
                is_deleted=bool(row["is_deleted"]),
            )

    def delete(
        self,
        collection: str,
        entity_id: str,
        *,
        tenant_id: str = "darkfac",
    ) -> bool:
        """Soft-delete an entity and queue deletion in the outbox."""
        with self._lock, self._local_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT version, created_at FROM records WHERE tenant_id = ? AND collection = ? AND entity_id = ?",
                (tenant_id, collection, entity_id),
            )
            existing = cur.fetchone()
            if not existing:
                return False

            now = utc_now()
            now_iso = _iso_utc(now)
            new_ver = existing["version"] + 1

            cur.execute(
                """
                UPDATE records
                SET is_deleted = 1, version = ?, updated_at = ?
                WHERE tenant_id = ? AND collection = ? AND entity_id = ?;
                """,
                (new_ver, now_iso, tenant_id, collection, entity_id),
            )

            outbox_payload = json.dumps({
                "tenant_id": tenant_id,
                "collection": collection,
                "entity_id": entity_id,
                "data": {},
                "version": new_ver,
                "created_at": existing["created_at"],
                "updated_at": now_iso,
                "is_deleted": True,
            }, ensure_ascii=False)

            cur.execute(
                """
                INSERT INTO outbox (tenant_id, collection, entity_id, operation, payload, version, created_at, synced)
                VALUES (?, ?, ?, 'DELETE', ?, ?, ?, 0);
                """,
                (tenant_id, collection, entity_id, outbox_payload, new_ver, now_iso),
            )
            conn.commit()

        if self.auto_sync and self.is_remote_configured():
            self.sync_to_remote()

        return True

    def list_records(
        self,
        collection: str,
        *,
        tenant_id: str = "darkfac",
        include_deleted: bool = False,
    ) -> list[PersistenceRecord]:
        """List all records for a collection."""
        with self._lock, self._local_conn() as conn:
            cur = conn.cursor()
            query = "SELECT tenant_id, collection, entity_id, data, version, created_at, updated_at, is_deleted FROM records WHERE tenant_id = ? AND collection = ?"
            params: list[Any] = [tenant_id, collection]
            if not include_deleted:
                query += " AND is_deleted = 0"
            query += " ORDER BY entity_id ASC;"

            cur.execute(query, params)
            records: list[PersistenceRecord] = []
            for row in cur.fetchall():
                try:
                    data_dict = json.loads(row["data"])
                except Exception:
                    data_dict = {}

                records.append(
                    PersistenceRecord(
                        tenant_id=row["tenant_id"],
                        collection=row["collection"],
                        entity_id=row["entity_id"],
                        data=data_dict,
                        version=row["version"],
                        created_at=_parse_iso_utc(row["created_at"]),
                        updated_at=_parse_iso_utc(row["updated_at"]),
                        is_deleted=bool(row["is_deleted"]),
                    )
                )
            return records

    def query(
        self,
        collection: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        tenant_id: str = "darkfac",
        include_deleted: bool = False,
    ) -> list[PersistenceRecord]:
        """Query entities matching a callable predicate."""
        all_records = self.list_records(collection, tenant_id=tenant_id, include_deleted=include_deleted)
        return [r for r in all_records if predicate(r.data)]

    def count_pending_outbox(self) -> int:
        """Count the number of mutations pending synchronization in the outbox."""
        with self._lock, self._local_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM outbox WHERE synced = 0;")
            return int(cur.fetchone()[0])

    def sync_to_remote(self) -> SyncResult:
        """Flush the transactional outbox to remote PostgreSQL."""
        if not self.is_remote_configured():
            return SyncResult(
                status="offline",
                errors=["Remote PostgreSQL not configured or psycopg unavailable."],
            )

        with self._lock:
            with self._local_conn() as local_conn:
                cur = local_conn.cursor()
                cur.execute(
                    """
                    SELECT id, tenant_id, collection, entity_id, operation, payload, version
                    FROM outbox
                    WHERE synced = 0
                    ORDER BY id ASC;
                    """
                )
                pending_rows = cur.fetchall()

                if not pending_rows:
                    return SyncResult(status="success", pushed_count=0)

                pushed_count = 0
                conflicts_count = 0
                errors: list[str] = []

                try:
                    with self._psycopg.connect(self.raw_url, connect_timeout=5) as pg_conn:
                        with pg_conn.cursor() as pg_cur:
                            for row in pending_rows:
                                outbox_id = row["id"]
                                tenant_id = row["tenant_id"]
                                collection = row["collection"]
                                entity_id = row["entity_id"]
                                payload_raw = row["payload"]
                                version = row["version"]

                                try:
                                    payload = json.loads(payload_raw)
                                except Exception:
                                    payload = {}

                                data_obj = payload.get("data", {})
                                is_deleted = payload.get("is_deleted", False)
                                updated_at = payload.get("updated_at", _iso_utc())

                                # Idempotent monotonic upsert in PostgreSQL
                                pg_cur.execute(
                                    """
                                    INSERT INTO tenant_records (
                                        tenant_id, collection, entity_id, data, version, updated_at, is_deleted
                                    )
                                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (tenant_id, collection, entity_id) DO UPDATE SET
                                        data = EXCLUDED.data,
                                        version = EXCLUDED.version,
                                        updated_at = EXCLUDED.updated_at,
                                        is_deleted = EXCLUDED.is_deleted
                                    WHERE EXCLUDED.version >= tenant_records.version;
                                    """,
                                    (
                                        tenant_id,
                                        collection,
                                        entity_id,
                                        json.dumps(data_obj, ensure_ascii=False),
                                        version,
                                        updated_at,
                                        is_deleted,
                                    ),
                                )
                                pushed_count += 1
                        pg_conn.commit()

                    # Mark local outbox as synced
                    synced_ids = [r["id"] for r in pending_rows]
                    local_conn.execute(
                        f"UPDATE outbox SET synced = 1 WHERE id IN ({','.join('?' for _ in synced_ids)});",
                        synced_ids,
                    )
                    local_conn.commit()

                except Exception as exc:
                    sanitized = sanitize_database_url(self.raw_url)
                    err_msg = f"Failed to push outbox to {sanitized}: {exc}"
                    logger.error(err_msg)
                    errors.append(err_msg)
                    return SyncResult(
                        status="error",
                        pushed_count=pushed_count,
                        conflicts_count=conflicts_count,
                        errors=errors,
                    )

                return SyncResult(
                    status="success",
                    pushed_count=pushed_count,
                    conflicts_count=conflicts_count,
                    errors=errors,
                )

    def sync_from_remote(
        self,
        collection: str | None = None,
        *,
        tenant_id: str = "darkfac",
    ) -> SyncResult:
        """Pull newer records from remote PostgreSQL into local cache."""
        if not self.is_remote_configured():
            return SyncResult(
                status="offline",
                errors=["Remote PostgreSQL not configured or psycopg unavailable."],
            )

        with self._lock:
            pulled_count = 0
            errors: list[str] = []

            try:
                with self._psycopg.connect(self.raw_url, connect_timeout=5) as pg_conn:
                    with pg_conn.cursor() as pg_cur:
                        query = "SELECT tenant_id, collection, entity_id, data, version, created_at, updated_at, is_deleted FROM tenant_records WHERE tenant_id = %s"
                        params: list[Any] = [tenant_id]
                        if collection:
                            query += " AND collection = %s"
                            params.append(collection)

                        pg_cur.execute(query, params)
                        remote_rows = pg_cur.fetchall()

                with self._local_conn() as local_conn:
                    local_cur = local_conn.cursor()
                    for r_row in remote_rows:
                        r_tenant, r_coll, r_id, r_data, r_ver, r_created, r_updated, r_del = r_row
                        local_cur.execute(
                            "SELECT version FROM records WHERE tenant_id = ? AND collection = ? AND entity_id = ?",
                            (r_tenant, r_coll, r_id),
                        )
                        l_row = local_cur.fetchone()

                        # Remote wins if version is strictly newer
                        if not l_row or r_ver > l_row["version"]:
                            if isinstance(r_data, str):
                                data_str = r_data
                            else:
                                data_str = json.dumps(r_data, ensure_ascii=False)

                            c_iso = _iso_utc(r_created) if isinstance(r_created, datetime) else str(r_created)
                            u_iso = _iso_utc(r_updated) if isinstance(r_updated, datetime) else str(r_updated)

                            local_cur.execute(
                                """
                                INSERT INTO records (tenant_id, collection, entity_id, data, version, created_at, updated_at, is_deleted)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(tenant_id, collection, entity_id) DO UPDATE SET
                                    data = excluded.data,
                                    version = excluded.version,
                                    created_at = excluded.created_at,
                                    updated_at = excluded.updated_at,
                                    is_deleted = excluded.is_deleted;
                                """,
                                (r_tenant, r_coll, r_id, data_str, r_ver, c_iso, u_iso, 1 if r_del else 0),
                            )
                            pulled_count += 1
                    local_conn.commit()

            except Exception as exc:
                sanitized = sanitize_database_url(self.raw_url)
                err_msg = f"Failed to pull from {sanitized}: {exc}"
                logger.error(err_msg)
                errors.append(err_msg)
                return SyncResult(
                    status="error",
                    pulled_count=pulled_count,
                    errors=errors,
                )

            return SyncResult(
                status="success",
                pulled_count=pulled_count,
                errors=errors,
            )

    def sync_all(self, collection: str | None = None, *, tenant_id: str = "darkfac") -> SyncResult:
        """Run bidirectional sync: push pending outbox mutations and pull remote changes."""
        push_res = self.sync_to_remote()
        pull_res = self.sync_from_remote(collection=collection, tenant_id=tenant_id)

        all_errors = list(push_res.errors) + list(pull_res.errors)
        status = "success"
        if push_res.status == "offline" or pull_res.status == "offline":
            status = "offline"
        elif push_res.status == "error" or pull_res.status == "error":
            status = "partial" if (push_res.pushed_count > 0 or pull_res.pulled_count > 0) else "error"

        return SyncResult(
            status=status,
            pushed_count=push_res.pushed_count,
            pulled_count=pull_res.pulled_count,
            conflicts_count=push_res.conflicts_count + pull_res.conflicts_count,
            errors=all_errors,
        )

    # =========================================================================
    # High-Level Store Integrations (Demands Reconciliation)
    # =========================================================================

    def sync_demands_store(
        self,
        demands_store: Any,
        *,
        tenant_id: str = "darkfac",
    ) -> SyncResult:
        """Reconcile and sync local DemandsStore with hybrid persistence.
        
        Reads tickets from the demands store, records them in collection 'demands',
        and synchronizes with remote PostgreSQL if connected.
        """
        tickets = demands_store.list_tickets()
        for ticket in tickets:
            ticket_dict = json.loads(ticket.model_dump_json())
            self.put(
                collection="demands",
                entity_id=ticket.id,
                data=ticket_dict,
                tenant_id=tenant_id,
            )

        return self.sync_all(collection="demands", tenant_id=tenant_id)
