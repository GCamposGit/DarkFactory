"""Distributed resource leases for fleet execution (USR-285 / F6).

Provides exclusive resource lease coordination for integration and deployment stages:
1. Integration: scoped by (repository, target_branch) to ensure that only one run
   rebases, resolves conflicts, checks CI and merges into the shared branch at a time.
2. Deploy: scoped by deployment destination (target type + host/app/compose id) to ensure
   that only one run deploys to a shared production target at a time, while independent
   destinations proceed concurrently without interference.
3. Fencing tokens and heartbeat: detect crashes, lease expiration and stolen leases,
   preventing late/stale side-effects from zombies or severed network partitions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Generator, Optional

from pydantic import BaseModel, Field

from core.projects.models import DeployTargetType, ProjectDescriptor

logger = logging.getLogger(__name__)

DEFAULT_LEASE_TTL_S = 180


class ResourceLease(BaseModel):
    """Distributed lease awarded to an identified holder for a specific resource."""

    resource_id: str
    resource_type: str  # 'repo' or 'deploy'
    holder_id: str  # e.g. run_id, host_id
    fencing_token: int = 1
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceBusyError(Exception):
    """Raised when a requested resource is currently held by another active holder."""

    def __init__(self, resource_id: str, holder_id: str, expires_at: str) -> None:
        self.resource_id = resource_id
        self.holder_id = holder_id
        self.expires_at = expires_at
        super().__init__(
            f"Resource '{resource_id}' is busy: currently held by '{holder_id}' until {expires_at}."
        )


class LeaseLostError(Exception):
    """Raised when a holder detects it has lost ownership of a lease (expired or stolen)."""

    def __init__(self, resource_id: str, holder_id: str, fencing_token: int) -> None:
        self.resource_id = resource_id
        self.holder_id = holder_id
        self.fencing_token = fencing_token
        super().__init__(
            f"Lease for '{resource_id}' (token={fencing_token}) was lost by '{holder_id}'."
        )


def integration_resource_id(project: ProjectDescriptor, branch: str | None = None) -> str:
    """Generate the canonical resource ID for git branch integration."""
    target_branch = branch or project.default_branch or "main"
    if project.repo_url:
        from core.projects.registry import normalize_repo_url

        repo_key = normalize_repo_url(project.repo_url).lower().rstrip("/")
    else:
        repo_key = project.id.lower()
    return f"repo:{repo_key}:{target_branch}"


def deploy_resource_id(project: ProjectDescriptor) -> str:
    """Generate the canonical resource ID for project deployment destination."""
    deploy = getattr(project, "deploy", None)
    if not deploy or not deploy.type or deploy.type == DeployTargetType.NONE:
        return f"deploy:none:{project.id.lower()}"

    params = deploy.params or {}
    deploy_type = deploy.type.value if hasattr(deploy.type, "value") else str(deploy.type)

    dest_key = (
        params.get("compose_id")
        or params.get("application_id")
        or params.get("host")
        or params.get("server")
        or params.get("service_name")
        or project.id
    )
    return f"deploy:{deploy_type.lower()}:{str(dest_key).lower()}"


class ResourceLeaseCoordinator:
    """Transactional coordinator for distributed resource leases."""

    _SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS resource_leases (
        resource_id TEXT PRIMARY KEY,
        resource_type TEXT NOT NULL,
        holder_id TEXT NOT NULL,
        fencing_token INTEGER NOT NULL DEFAULT 1,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired')),
        metadata TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_resource_leases_status ON resource_leases (status, expires_at);
    """

    def __init__(
        self,
        *,
        db_path: Path | str | None = None,
        store: Any = None,
        database_url: str | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self.database_url: str | None = None
        self.db_path: Path | str | None = None
        self._mem_conn: sqlite3.Connection | None = None

        if store is not None:
            if hasattr(store, "database_url") and store.database_url:
                self.database_url = store.database_url
            elif hasattr(store, "_backend") and hasattr(store._backend, "db_path"):
                self.db_path = store._backend.db_path
            elif hasattr(store, "db_path"):
                self.db_path = store.db_path

        if not self.database_url and not self.db_path:
            if database_url:
                self.database_url = database_url
            elif db_path:
                self.db_path = Path(db_path) if db_path != ":memory:" else ":memory:"
            else:
                env_url = os.environ.get("DARKFAC_HF02_DATABASE_URL")
                if env_url:
                    self.database_url = env_url
                else:
                    from core.line.store_selection import default_control_db_path

                    self.db_path = default_control_db_path()

        self._init_db()

    def _init_db(self) -> None:
        if self.database_url:
            self._init_postgres()
        else:
            self._init_sqlite()

    def _init_sqlite(self) -> None:
        if self.db_path != ":memory:" and isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect_sqlite()
        try:
            conn.executescript(self._SCHEMA_SQL)
            conn.commit()
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def _init_postgres(self) -> None:
        import psycopg

        with psycopg.connect(self.database_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS resource_leases (
                        resource_id VARCHAR(256) PRIMARY KEY,
                        resource_type VARCHAR(64) NOT NULL,
                        holder_id VARCHAR(160) NOT NULL,
                        fencing_token INTEGER NOT NULL DEFAULT 1,
                        acquired_at TIMESTAMPTZ NOT NULL,
                        heartbeat_at TIMESTAMPTZ NOT NULL,
                        expires_at TIMESTAMPTZ NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released', 'expired')),
                        metadata JSONB NOT NULL DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_resource_leases_status ON resource_leases (status, expires_at);
                    """
                )

    def _connect_sqlite(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
                self._mem_conn.executescript(self._SCHEMA_SQL)
                self._mem_conn.commit()
            return self._mem_conn
        path_str = str(self.db_path)
        conn = sqlite3.connect(path_str, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def acquire(
        self,
        resource_id: str,
        resource_type: str,
        holder_id: str,
        *,
        ttl_s: int = DEFAULT_LEASE_TTL_S,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ResourceLease:
        """Acquire an exclusive lease for a given resource.

        Raises ResourceBusyError if an active, unexpired lease exists for another holder.
        If a lease was expired (e.g. holder crashed), atomically increments fencing_token
        and reclaims ownership.
        """
        now_dt = now or datetime.now(UTC)
        expires_dt = now_dt + timedelta(seconds=ttl_s)
        now_iso = now_dt.isoformat()
        expires_iso = expires_dt.isoformat()
        meta_dict = metadata or {}
        meta_json = json.dumps(meta_dict, ensure_ascii=False)

        with self._lock:
            if self.database_url:
                return self._acquire_postgres(
                    resource_id, resource_type, holder_id, now_dt, expires_dt, meta_dict
                )
            return self._acquire_sqlite(
                resource_id, resource_type, holder_id, now_iso, expires_iso, meta_dict, meta_json
            )

    def _acquire_sqlite(
        self,
        resource_id: str,
        resource_type: str,
        holder_id: str,
        now_iso: str,
        expires_iso: str,
        meta_dict: dict[str, Any],
        meta_json: str,
    ) -> ResourceLease:
        conn = self._connect_sqlite()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT resource_id, resource_type, holder_id, fencing_token, acquired_at, heartbeat_at, expires_at, status, metadata FROM resource_leases WHERE resource_id = ?",
                (resource_id,),
            )
            row = cur.fetchone()

            if row is not None:
                status = row["status"]
                row_expires = row["expires_at"]
                current_holder = row["holder_id"]
                current_fencing = row["fencing_token"]

                # Active and not expired:
                if status == "active" and row_expires > now_iso:
                    if current_holder == holder_id:
                        # Re-entrant acquisition / refresh:
                        conn.execute(
                            "UPDATE resource_leases SET heartbeat_at = ?, expires_at = ?, metadata = ? WHERE resource_id = ?",
                            (now_iso, expires_iso, meta_json, resource_id),
                        )
                        conn.commit()
                        return ResourceLease(
                            resource_id=resource_id,
                            resource_type=resource_type,
                            holder_id=holder_id,
                            fencing_token=current_fencing,
                            acquired_at=row["acquired_at"],
                            heartbeat_at=now_iso,
                            expires_at=expires_iso,
                            status="active",
                            metadata=meta_dict,
                        )
                    conn.rollback()
                    raise ResourceBusyError(resource_id, current_holder, row_expires)

                # Lease was released or expired: reclaim with incremented fencing_token
                new_fencing = current_fencing + 1
                conn.execute(
                    """UPDATE resource_leases
                       SET resource_type = ?, holder_id = ?, fencing_token = ?, acquired_at = ?, heartbeat_at = ?, expires_at = ?, status = 'active', metadata = ?
                       WHERE resource_id = ?""",
                    (resource_type, holder_id, new_fencing, now_iso, now_iso, expires_iso, meta_json, resource_id),
                )
                conn.commit()
                return ResourceLease(
                    resource_id=resource_id,
                    resource_type=resource_type,
                    holder_id=holder_id,
                    fencing_token=new_fencing,
                    acquired_at=now_iso,
                    heartbeat_at=now_iso,
                    expires_at=expires_iso,
                    status="active",
                    metadata=meta_dict,
                )

            # Insert new lease:
            conn.execute(
                """INSERT INTO resource_leases (resource_id, resource_type, holder_id, fencing_token, acquired_at, heartbeat_at, expires_at, status, metadata)
                   VALUES (?, ?, ?, 1, ?, ?, ?, 'active', ?)""",
                (resource_id, resource_type, holder_id, now_iso, now_iso, expires_iso, meta_json),
            )
            conn.commit()
            return ResourceLease(
                resource_id=resource_id,
                resource_type=resource_type,
                holder_id=holder_id,
                fencing_token=1,
                acquired_at=now_iso,
                heartbeat_at=now_iso,
                expires_at=expires_iso,
                status="active",
                metadata=meta_dict,
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            if self.db_path != ":memory:":
                conn.close()

    def _acquire_postgres(
        self,
        resource_id: str,
        resource_type: str,
        holder_id: str,
        now_dt: datetime,
        expires_dt: datetime,
        meta_dict: dict[str, Any],
    ) -> ResourceLease:
        import psycopg

        now_iso = now_dt.isoformat()
        expires_iso = expires_dt.isoformat()

        with psycopg.connect(self.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT resource_id, resource_type, holder_id, fencing_token, acquired_at, heartbeat_at, expires_at, status, metadata FROM resource_leases WHERE resource_id = %s FOR UPDATE",
                    (resource_id,),
                )
                row = cur.fetchone()

                if row is not None:
                    _rid, _rtype, current_holder, current_fencing, acq_at, _hb, row_expires, status, _meta = row
                    row_expires_iso = row_expires.isoformat() if hasattr(row_expires, "isoformat") else str(row_expires)

                    if status == "active" and row_expires > now_dt:
                        if current_holder == holder_id:
                            cur.execute(
                                "UPDATE resource_leases SET heartbeat_at = %s, expires_at = %s, metadata = %s WHERE resource_id = %s",
                                (now_dt, expires_dt, json.dumps(meta_dict), resource_id),
                            )
                            conn.commit()
                            return ResourceLease(
                                resource_id=resource_id,
                                resource_type=resource_type,
                                holder_id=holder_id,
                                fencing_token=current_fencing,
                                acquired_at=acq_at.isoformat() if hasattr(acq_at, "isoformat") else str(acq_at),
                                heartbeat_at=now_iso,
                                expires_at=expires_iso,
                                status="active",
                                metadata=meta_dict,
                            )
                        raise ResourceBusyError(resource_id, current_holder, row_expires_iso)

                    new_fencing = current_fencing + 1
                    cur.execute(
                        """UPDATE resource_leases
                           SET resource_type = %s, holder_id = %s, fencing_token = %s, acquired_at = %s, heartbeat_at = %s, expires_at = %s, status = 'active', metadata = %s
                           WHERE resource_id = %s""",
                        (resource_type, holder_id, new_fencing, now_dt, now_dt, expires_dt, json.dumps(meta_dict), resource_id),
                    )
                    conn.commit()
                    return ResourceLease(
                        resource_id=resource_id,
                        resource_type=resource_type,
                        holder_id=holder_id,
                        fencing_token=new_fencing,
                        acquired_at=now_iso,
                        heartbeat_at=now_iso,
                        expires_at=expires_iso,
                        status="active",
                        metadata=meta_dict,
                    )

                cur.execute(
                    """INSERT INTO resource_leases (resource_id, resource_type, holder_id, fencing_token, acquired_at, heartbeat_at, expires_at, status, metadata)
                       VALUES (%s, %s, %s, 1, %s, %s, %s, 'active', %s)""",
                    (resource_id, resource_type, holder_id, now_dt, now_dt, expires_dt, json.dumps(meta_dict)),
                )
                conn.commit()
                return ResourceLease(
                    resource_id=resource_id,
                    resource_type=resource_type,
                    holder_id=holder_id,
                    fencing_token=1,
                    acquired_at=now_iso,
                    heartbeat_at=now_iso,
                    expires_at=expires_iso,
                    status="active",
                    metadata=meta_dict,
                )

    def heartbeat(
        self,
        lease: ResourceLease,
        *,
        ttl_s: int = DEFAULT_LEASE_TTL_S,
        now: datetime | None = None,
    ) -> ResourceLease:
        """Renew lease expiry and verify ownership and fencing token."""
        now_dt = now or datetime.now(UTC)
        expires_dt = now_dt + timedelta(seconds=ttl_s)
        now_iso = now_dt.isoformat()
        expires_iso = expires_dt.isoformat()

        with self._lock:
            if self.database_url:
                import psycopg

                with psycopg.connect(self.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT holder_id, fencing_token, status, expires_at FROM resource_leases WHERE resource_id = %s FOR UPDATE",
                            (lease.resource_id,),
                        )
                        row = cur.fetchone()
                        if row is None or row[0] != lease.holder_id or row[1] != lease.fencing_token or row[2] != "active" or row[3] <= now_dt:
                            raise LeaseLostError(lease.resource_id, lease.holder_id, lease.fencing_token)
                        cur.execute(
                            "UPDATE resource_leases SET heartbeat_at = %s, expires_at = %s WHERE resource_id = %s",
                            (now_dt, expires_dt, lease.resource_id),
                        )
                        conn.commit()
            else:
                conn = self._connect_sqlite()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute(
                        "SELECT holder_id, fencing_token, status, expires_at FROM resource_leases WHERE resource_id = ?",
                        (lease.resource_id,),
                    )
                    row = cur.fetchone()
                    if row is None or row["holder_id"] != lease.holder_id or row["fencing_token"] != lease.fencing_token or row["status"] != "active" or row["expires_at"] <= now_iso:
                        conn.rollback()
                        raise LeaseLostError(lease.resource_id, lease.holder_id, lease.fencing_token)
                    conn.execute(
                        "UPDATE resource_leases SET heartbeat_at = ?, expires_at = ? WHERE resource_id = ?",
                        (now_iso, expires_iso, lease.resource_id),
                    )
                    conn.commit()
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    if self.db_path != ":memory:":
                        conn.close()

            lease.heartbeat_at = now_iso
            lease.expires_at = expires_iso
            return lease

    def release(
        self,
        lease: ResourceLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Release the resource lease, allowing other holders to acquire it immediately."""
        now_dt = now or datetime.now(UTC)
        now_iso = now_dt.isoformat()

        with self._lock:
            if self.database_url:
                import psycopg

                with psycopg.connect(self.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT holder_id, fencing_token, status FROM resource_leases WHERE resource_id = %s FOR UPDATE",
                            (lease.resource_id,),
                        )
                        row = cur.fetchone()
                        if row is not None and row[0] == lease.holder_id and row[1] == lease.fencing_token and row[2] == "active":
                            cur.execute(
                                "UPDATE resource_leases SET status = 'released', expires_at = %s WHERE resource_id = %s",
                                (now_dt, lease.resource_id),
                            )
                            conn.commit()
                            return True
                        return False
            else:
                conn = self._connect_sqlite()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    cur = conn.execute(
                        "SELECT holder_id, fencing_token, status FROM resource_leases WHERE resource_id = ?",
                        (lease.resource_id,),
                    )
                    row = cur.fetchone()
                    if row is not None and row["holder_id"] == lease.holder_id and row["fencing_token"] == lease.fencing_token and row["status"] == "active":
                        conn.execute(
                            "UPDATE resource_leases SET status = 'released', expires_at = ? WHERE resource_id = ?",
                            (now_iso, lease.resource_id),
                        )
                        conn.commit()
                        return True
                    conn.rollback()
                    return False
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
                finally:
                    if self.db_path != ":memory:":
                        conn.close()

    def is_held_by(
        self,
        resource_id: str,
        holder_id: str,
        *,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Check if an active, unexpired lease is currently held by holder_id."""
        now_dt = now or datetime.now(UTC)
        now_iso = now_dt.isoformat()

        with self._lock:
            if self.database_url:
                import psycopg

                with psycopg.connect(self.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT holder_id, fencing_token, status, expires_at FROM resource_leases WHERE resource_id = %s",
                            (resource_id,),
                        )
                        row = cur.fetchone()
                        if row is None:
                            return False
                        _holder, token, status, expires_at = row
                        if _holder != holder_id or status != "active" or expires_at <= now_dt:
                            return False
                        if fencing_token is not None and token != fencing_token:
                            return False
                        return True
            else:
                conn = self._connect_sqlite()
                try:
                    cur = conn.execute(
                        "SELECT holder_id, fencing_token, status, expires_at FROM resource_leases WHERE resource_id = ?",
                        (resource_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return False
                    if row["holder_id"] != holder_id or row["status"] != "active" or row["expires_at"] <= now_iso:
                        return False
                    if fencing_token is not None and row["fencing_token"] != fencing_token:
                        return False
                    return True
                finally:
                    if self.db_path != ":memory:":
                        conn.close()

    @contextmanager
    def hold(
        self,
        resource_id: str,
        resource_type: str,
        holder_id: str,
        *,
        ttl_s: int = DEFAULT_LEASE_TTL_S,
        metadata: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> Generator[ResourceLease, None, None]:
        """Context manager to acquire and automatically release a lease."""
        lease = self.acquire(resource_id, resource_type, holder_id, ttl_s=ttl_s, metadata=metadata, now=now)
        try:
            yield lease
        finally:
            try:
                self.release(lease)
            except Exception as exc:
                logger.debug("Error auto-releasing lease %s: %s", resource_id, exc)


_GLOBAL_COORDINATOR: ResourceLeaseCoordinator | None = None
_GLOBAL_LOCK = threading.Lock()


def get_resource_coordinator() -> ResourceLeaseCoordinator:
    """Get or initialize the process-wide ResourceLeaseCoordinator."""
    global _GLOBAL_COORDINATOR
    with _GLOBAL_LOCK:
        if _GLOBAL_COORDINATOR is None:
            _GLOBAL_COORDINATOR = ResourceLeaseCoordinator()
        return _GLOBAL_COORDINATOR
