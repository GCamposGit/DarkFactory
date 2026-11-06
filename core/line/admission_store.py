"""Durable failure fingerprints and dispatch reservations (USR-468 T2).

Only hashes and normalized error classes are persisted. SQLite transactions serialize
competing dispatchers, and an unavailable store is an admission refusal.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def safe_digest(value: Any) -> str:
    """One-way digest for candidate, context, or resource identity inputs."""
    return _canonical_hash(value)


def normalize_error(error: BaseException | str) -> str:
    """Stable, non-sensitive error category; exception text is never retained."""
    if isinstance(error, BaseException):
        name = type(error).__name__.casefold()
        message = str(error).casefold()
    else:
        name = "error"
        message = str(error).casefold()
    categories = (
        ("quota", ("quota", "rate limit", "rate_limit", "429", "no_route_available")),
        ("context", ("context", "scanner", "scan failed", "input too long")),
        ("resource", ("worker", "host", "health", "capacity", "remote_job_lost")),
        ("checkout", ("worktree", "checkout", "git")),
    )
    for category, needles in categories:
        if any(needle in message for needle in needles):
            return category
    return re.sub(r"[^a-z0-9_]+", "_", name).strip("_")[:64] or "unknown_error"


@dataclass(frozen=True)
class AdmissionFingerprint:
    project_id: str
    ticket_id: str
    candidate_digest: str
    context_digest: str
    error_class: str
    resource_version: str
    sha256: str


def make_fingerprint(
    *, project_id: str, ticket_id: str, candidate_digest: Optional[str] = None,
    baseline_sha: Optional[str] = None, context_digest: Optional[str] = None,
    error: BaseException | str, resource_version: str,
) -> AdmissionFingerprint:
    candidate = safe_digest(candidate_digest or baseline_sha or "unknown")
    context = safe_digest(context_digest or "unknown")
    resource = safe_digest(resource_version)
    error_class = normalize_error(error)
    raw = {
        "project_id": project_id, "ticket_id": ticket_id,
        "candidate_digest": candidate, "context_digest": context,
        "error_class": error_class, "resource_version": resource,
    }
    return AdmissionFingerprint(
        project_id=project_id, ticket_id=ticket_id, candidate_digest=candidate,
        context_digest=context, error_class=error_class,
        resource_version=resource, sha256=_canonical_hash(raw),
    )


class AdmissionStoreError(RuntimeError):
    """Store could not safely make an admission decision."""


class AdmissionStore:
    """SQLite-backed breaker and exclusive attempt reservations."""

    def __init__(self, path: Path | str, *, clock: Any = None, reservation_ttl_sec: int = 3600) -> None:
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.reservation_ttl_sec = max(1, int(reservation_ttl_sec))
        self._init_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._init_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._connection() as db:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("""CREATE TABLE IF NOT EXISTS failures (
                        fingerprint TEXT PRIMARY KEY, project_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
                        error_class TEXT NOT NULL, resource_version TEXT NOT NULL,
                        recorded_at TEXT NOT NULL, next_probe_at TEXT NOT NULL,
                        backoff_sec INTEGER NOT NULL, open INTEGER NOT NULL DEFAULT 1,
                        occurrences INTEGER NOT NULL DEFAULT 1
                    )""")
                    columns = {row[1] for row in db.execute("PRAGMA table_info(failures)")}
                    if "occurrences" not in columns:
                        db.execute("ALTER TABLE failures ADD COLUMN occurrences INTEGER NOT NULL DEFAULT 1")
                    db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                        scope TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS dispatcher_recovery (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1), active INTEGER NOT NULL,
                        cause TEXT NOT NULL, fingerprint TEXT NOT NULL, updated_at TEXT NOT NULL,
                        successes INTEGER NOT NULL DEFAULT 0, receipt_cause TEXT NOT NULL DEFAULT '',
                        receipt_digest TEXT NOT NULL DEFAULT '', recovered_candidate_digest TEXT NOT NULL DEFAULT ''
                    )""")
                    recovery_columns = {row[1] for row in db.execute("PRAGMA table_info(dispatcher_recovery)")}
                    for name in ("receipt_cause", "receipt_digest", "recovered_candidate_digest"):
                        if name not in recovery_columns:
                            db.execute(f"ALTER TABLE dispatcher_recovery ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            except (OSError, sqlite3.Error) as exc:
                raise AdmissionStoreError("admission_store_unavailable") from exc

    @staticmethod
    def _scope(project_id: str, ticket_id: str, candidate_digest: str, context_digest: str, resource_version: str) -> str:
        return _canonical_hash([project_id, ticket_id, candidate_digest, context_digest, resource_version])

    def is_failure_open(
        self, *, project_id: str, ticket_id: str, candidate_digest: str,
        context_digest: str, resource_version: str, error_class: Optional[str] = None,
    ) -> bool:
        """Return true for an identical failure scope; elapsed backoff alone never opens it."""
        try:
            with self._connection() as db:
                query = """SELECT 1 FROM failures WHERE project_id=? AND ticket_id=?
                    AND candidate_digest=? AND context_digest=? AND resource_version=? AND open=1"""
                params: tuple[Any, ...] = (project_id, ticket_id, candidate_digest, context_digest, resource_version)
                if error_class is not None:
                    query += " AND error_class=?"
                    params += (error_class,)
                row = db.execute(query + " LIMIT 1", params).fetchone()
                return row is not None
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def record_failure(self, fingerprint: AdmissionFingerprint, *, initial_backoff_sec: int = 600) -> int:
        """Persist a failure atomically, increasing its probe delay on repeated observations."""
        now = self.clock().astimezone(timezone.utc)
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT backoff_sec, occurrences FROM failures WHERE fingerprint=?", (fingerprint.sha256,)).fetchone()
                backoff = max(1, int(initial_backoff_sec)) if row is None else min(int(row["backoff_sec"]) * 2, 86400)
                occurrences = 1 if row is None else int(row["occurrences"]) + 1
                # First observation is recorded and immediately becomes an open breaker.
                next_probe = now + timedelta(seconds=backoff)
                db.execute("""INSERT INTO failures (
                    fingerprint, project_id, ticket_id, candidate_digest, context_digest,
                    error_class, resource_version, recorded_at, next_probe_at, backoff_sec, open, occurrences
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET recorded_at=excluded.recorded_at,
                    next_probe_at=excluded.next_probe_at, backoff_sec=excluded.backoff_sec, open=1,
                    occurrences=excluded.occurrences""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.error_class,
                     fingerprint.resource_version, now.isoformat(), next_probe.isoformat(), backoff, occurrences))
                db.commit()
                return occurrences
        except (sqlite3.Error, OSError) as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def recovery_state(self) -> dict[str, Any]:
        """Return the persistent recovery latch, defaulting to inactive."""
        try:
            with self._connection() as db:
                row = db.execute("SELECT * FROM dispatcher_recovery WHERE singleton=1").fetchone()
                return dict(row) if row else {"active": 0, "cause": "", "fingerprint": "", "successes": 0}
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def set_recovery(self, *, active: bool, cause: str = "", fingerprint: str = "", success: bool = False) -> dict[str, Any]:
        """Persist recovery activation or clear it only after an evidenced delivery."""
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT active, successes FROM dispatcher_recovery WHERE singleton=1").fetchone()
                was_active = bool(old and int(old["active"]))
                successes = (int(old["successes"]) if old else 0) + int(success)
                db.execute("""INSERT INTO dispatcher_recovery(singleton, active, cause, fingerprint, updated_at, successes)
                    VALUES(1, ?, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET active=excluded.active, cause=excluded.cause,
                    fingerprint=excluded.fingerprint, updated_at=excluded.updated_at, successes=excluded.successes""",
                    (int(active), cause, fingerprint, now, successes))
                if active and not was_active:
                    db.execute("UPDATE dispatcher_recovery SET receipt_cause='' WHERE singleton=1")
                db.commit()
            return self.recovery_state()
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def record_recovery_receipt_cause(self, cause: str) -> dict[str, Any]:
        """Persist why a candidate's attempted recovery receipt was refused."""
        safe_cause = re.sub(r"[^a-z0-9_]+", "_", str(cause).casefold()).strip("_")[:96] or "receipt_invalid"
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute(
                    "UPDATE dispatcher_recovery SET receipt_cause=?, updated_at=? WHERE singleton=1 AND active=1",
                    (safe_cause, now),
                )
            return self.recovery_state()
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def complete_recovery(self, *, receipt_digest: str, candidate_digest: str) -> dict[str, Any]:
        """Atomically clear recovery once per valid production receipt.

        Replaying the same receipt after a restart is idempotent and does not
        increment the recovery success counter again.
        """
        digest = str(receipt_digest).strip()
        candidate = str(candidate_digest).strip()
        if not digest or not candidate:
            raise ValueError("receipt_digest and candidate_digest are required")
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT active, receipt_digest, successes FROM dispatcher_recovery WHERE singleton=1").fetchone()
                if row is None:
                    db.rollback()
                    return self.recovery_state()
                if not int(row["active"]):
                    # An already completed receipt is an idempotent replay.
                    db.rollback()
                    return self.recovery_state()
                if row["receipt_digest"] == digest:
                    # A previously consumed production proof cannot close a later recovery episode.
                    db.rollback()
                    return self.recovery_state()
                db.execute("""UPDATE dispatcher_recovery SET active=0, updated_at=?, successes=successes+1,
                    receipt_cause='', receipt_digest=?, recovered_candidate_digest=? WHERE singleton=1 AND active=1""",
                    (now, digest, candidate))
                db.commit()
            return self.recovery_state()
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def reserve(self, *, project_id: str, ticket_id: str, candidate_digest: str,
                context_digest: str, resource_version: str) -> Optional[str]:
        """Acquire one durable attempt reservation, or return None if another process owns it."""
        scope = self._scope(project_id, ticket_id, candidate_digest, context_digest, resource_version)
        now = self.clock().astimezone(timezone.utc)
        token = uuid.uuid4().hex
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM reservations WHERE expires_at<=?", (now.isoformat(),))
                row = db.execute("SELECT token FROM reservations WHERE scope=?", (scope,)).fetchone()
                if row:
                    db.rollback()
                    return None
                expiry = now + timedelta(seconds=self.reservation_ttl_sec)
                db.execute("INSERT INTO reservations VALUES (?, ?, ?)", (scope, token, expiry.isoformat()))
                db.commit()
                return token
        except (sqlite3.Error, OSError) as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def reserve_recovery(self, *, lease_duration_sec: int = 90) -> Optional[str]:
        """Reserve the single recovery delivery slot across tickets and dispatcher restarts."""
        scope = self._scope("__recovery__", "global", "1", "1", "1")
        now = self.clock().astimezone(timezone.utc)
        token = uuid.uuid4().hex
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM reservations WHERE expires_at<=?", (now.isoformat(),))
                if db.execute("SELECT 1 FROM reservations WHERE scope=?", (scope,)).fetchone():
                    db.rollback()
                    return None
                expiry = now + timedelta(seconds=max(1, int(lease_duration_sec)))
                db.execute("INSERT INTO reservations VALUES (?, ?, ?)", (scope, token, expiry.isoformat()))
                db.commit()
                return token
        except (sqlite3.Error, OSError) as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def heartbeat_recovery(self, token: str, *, lease_duration_sec: int = 90) -> bool:
        """Extend only the currently fenced global recovery reservation."""
        scope = self._scope("__recovery__", "global", "1", "1", "1")
        expiry = self.clock().astimezone(timezone.utc) + timedelta(seconds=max(1, int(lease_duration_sec)))
        try:
            with self._connection() as db:
                cursor = db.execute("UPDATE reservations SET expires_at=? WHERE scope=? AND token=?",
                                    (expiry.isoformat(), scope, token))
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def release_recovery(self, token: str) -> None:
        self.release(project_id="__recovery__", ticket_id="global", candidate_digest="1",
                     context_digest="1", resource_version="1", token=token)

    def release(self, *, project_id: str, ticket_id: str, candidate_digest: str,
                context_digest: str, resource_version: str, token: str) -> None:
        scope = self._scope(project_id, ticket_id, candidate_digest, context_digest, resource_version)
        try:
            with self._connection() as db:
                db.execute("DELETE FROM reservations WHERE scope=? AND token=?", (scope, token))
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def snapshot(self) -> list[dict[str, Any]]:
        """Safe diagnostic rows; contains hashes/classes only, never error text."""
        try:
            with self._connection() as db:
                rows = db.execute(
                    """SELECT fingerprint, project_id, ticket_id, candidate_digest,
                    context_digest, error_class, resource_version, recorded_at, next_probe_at,
                    backoff_sec, open FROM failures ORDER BY recorded_at"""
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc


__all__ = ["AdmissionFingerprint", "AdmissionStore", "AdmissionStoreError", "make_fingerprint", "normalize_error", "safe_digest"]
