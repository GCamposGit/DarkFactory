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
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from core.line.failure_taxonomy import canonical_cause, classify


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
        message = str(error).casefold()
        receipt_error = re.fullmatch(r"recovery_receipt:(receipt_[a-z0-9_]+)", message)
        name = receipt_error.group(1) if receipt_error else "error"
    normalized = canonical_cause(f"{name}: {message}" if isinstance(error, BaseException) else message)
    if normalized != "unknown":
        return normalized
    return re.sub(r"[^a-z0-9_]+", "_", name).strip("_")[:64] or "unknown_error"


def normalize_main_sha(main_sha: Optional[str]) -> str:
    """Stable, case-insensitive form of a main-branch sha; missing/blank becomes ''."""
    return str(main_sha or "").strip().casefold()


@dataclass(frozen=True)
class AdmissionFingerprint:
    project_id: str
    ticket_id: str
    candidate_digest: str
    context_digest: str
    error_class: str
    resource_version: str
    main_sha: str
    sha256: str


def make_fingerprint(
    *, project_id: str, ticket_id: str, candidate_digest: Optional[str] = None,
    baseline_sha: Optional[str] = None, context_digest: Optional[str] = None,
    error: BaseException | str, resource_version: str, main_sha: Optional[str] = None,
) -> AdmissionFingerprint:
    candidate = safe_digest(candidate_digest or baseline_sha or "unknown")
    context = safe_digest(context_digest or "unknown")
    resource = safe_digest(resource_version)
    error_class = normalize_error(error)
    main = normalize_main_sha(main_sha)
    raw = {
        "project_id": project_id, "ticket_id": ticket_id,
        "candidate_digest": candidate, "context_digest": context,
        "error_class": error_class, "resource_version": resource, "main_sha": main,
    }
    return AdmissionFingerprint(
        project_id=project_id, ticket_id=ticket_id, candidate_digest=candidate,
        context_digest=context, error_class=error_class,
        resource_version=resource, main_sha=main, sha256=_canonical_hash(raw),
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
                    db.execute("BEGIN IMMEDIATE")
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
                    if "main_sha" not in columns:
                        db.execute("ALTER TABLE failures ADD COLUMN main_sha TEXT NOT NULL DEFAULT ''")
                    db.execute("""CREATE TABLE IF NOT EXISTS ticket_holds (
                        project_id TEXT NOT NULL, ticket_id TEXT NOT NULL, cause TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
                        occurrences INTEGER NOT NULL, fingerprint TEXT NOT NULL, held_at TEXT NOT NULL,
                        main_sha TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (project_id, ticket_id)
                    )""")
                    hold_columns = {row[1] for row in db.execute("PRAGMA table_info(ticket_holds)")}
                    if "main_sha" not in hold_columns:
                        db.execute("ALTER TABLE ticket_holds ADD COLUMN main_sha TEXT NOT NULL DEFAULT ''")
                    for hold in db.execute("SELECT project_id, ticket_id, cause FROM ticket_holds").fetchall():
                        if classify(hold["cause"]) == "infra":
                            db.execute("DELETE FROM ticket_holds WHERE project_id=? AND ticket_id=?",
                                       (hold["project_id"], hold["ticket_id"]))
                    db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                        scope TEXT PRIMARY KEY, token TEXT NOT NULL, expires_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS ticket_budget (
                        project TEXT NOT NULL, ticket TEXT NOT NULL,
                        candidate_failures INTEGER NOT NULL DEFAULT 0, infra_streak INTEGER NOT NULL DEFAULT 0,
                        next_attempt_at REAL NOT NULL DEFAULT 0, last_cause TEXT NOT NULL DEFAULT '',
                        main_sha TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                        PRIMARY KEY(project, ticket)
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS ticket_attempts (
                        project TEXT NOT NULL, ticket TEXT NOT NULL, run_id TEXT NOT NULL,
                        PRIMARY KEY(project, ticket, run_id)
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
                    db.execute("""CREATE TABLE IF NOT EXISTS rebase_recovery (
                        fingerprint TEXT PRIMARY KEY, project_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
                        candidate_digest TEXT NOT NULL, content_digest TEXT NOT NULL,
                        upstream_sha TEXT NOT NULL DEFAULT '', inferred INTEGER NOT NULL DEFAULT 0,
                        recovery_started INTEGER NOT NULL DEFAULT 0, recovery_result TEXT NOT NULL DEFAULT '',
                        cause TEXT NOT NULL DEFAULT '', resume_command TEXT NOT NULL DEFAULT '',
                        held_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
                    )""")
                    rebase_columns = {row[1] for row in db.execute("PRAGMA table_info(rebase_recovery)")}
                    for name, declaration in (
                        ("recovery_started", "INTEGER NOT NULL DEFAULT 0"),
                        ("recovery_result", "TEXT NOT NULL DEFAULT ''"),
                    ):
                        if name not in rebase_columns:
                            db.execute(f"ALTER TABLE rebase_recovery ADD COLUMN {name} {declaration}")
                    db.commit()
            except (OSError, sqlite3.Error) as exc:
                raise AdmissionStoreError("admission_store_unavailable") from exc

    @staticmethod
    def _scope(project_id: str, ticket_id: str, candidate_digest: str, context_digest: str, resource_version: str) -> str:
        return _canonical_hash([project_id, ticket_id, candidate_digest, context_digest, resource_version])

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
                    error_class, resource_version, recorded_at, next_probe_at, backoff_sec, open, occurrences,
                    main_sha
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET recorded_at=excluded.recorded_at,
                    next_probe_at=excluded.next_probe_at, backoff_sec=excluded.backoff_sec, open=1,
                    occurrences=excluded.occurrences, main_sha=excluded.main_sha""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.error_class,
                     fingerprint.resource_version, now.isoformat(), next_probe.isoformat(), backoff, occurrences,
                     fingerprint.main_sha))
                db.commit()
                return occurrences
        except (sqlite3.Error, OSError) as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def set_ticket_hold(self, fingerprint: AdmissionFingerprint, occurrences: int, *,
                        connection: sqlite3.Connection | None = None) -> bool:
        """Create a terminal hold once. Only an explicit reset or merge releases it."""
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with nullcontext(connection) if connection is not None else self._connection() as db:
                cursor = db.execute("""INSERT OR IGNORE INTO ticket_holds
                    (project_id, ticket_id, cause, candidate_digest, context_digest, occurrences,
                     fingerprint, held_at, main_sha)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (fingerprint.project_id, fingerprint.ticket_id, fingerprint.error_class,
                            fingerprint.candidate_digest, fingerprint.context_digest, int(occurrences),
                            fingerprint.sha256, now, fingerprint.main_sha))
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def ticket_hold(self, *, project_id: str, ticket_id: str, candidate_digest: str = "",
                    context_digest: str = "", main_sha: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Return the terminal hold, independent of candidate, host and main SHA."""
        try:
            with self._connection() as db:
                row = db.execute("SELECT * FROM ticket_holds WHERE project_id=? AND ticket_id=?",
                                 (project_id, ticket_id)).fetchone()
                if row is None:
                    return None
                return dict(row)
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def ticket_holds(self) -> list[dict[str, Any]]:
        """Todos os holds por ticket (ticket, causa, ocorrencias, sha do main) para last_cycle e briefing."""
        try:
            with self._connection() as db:
                rows = db.execute("""SELECT project_id, ticket_id, cause, occurrences, held_at, main_sha
                    FROM ticket_holds ORDER BY ticket_id""").fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def recovery_state(self) -> dict[str, Any]:
        """Return the persistent recovery latch, defaulting to inactive."""
        try:
            with self._connection() as db:
                row = db.execute("SELECT * FROM dispatcher_recovery WHERE singleton=1").fetchone()
                return dict(row) if row else {"active": 0, "cause": "", "fingerprint": "", "successes": 0}
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def budget(self, project: str, ticket: str, *, main_sha: str = "") -> dict[str, Any]:
        """Read durable counters; a known new main clears infrastructure delay only."""
        main_sha = normalize_main_sha(main_sha)
        try:
            with self._connection() as db:
                if main_sha:
                    db.execute("""UPDATE ticket_budget SET infra_streak=0, next_attempt_at=0, main_sha=?, updated_at=?
                        WHERE project=? AND ticket=? AND main_sha<>?""",
                               (main_sha, self.clock().astimezone(timezone.utc).isoformat(), project, ticket, main_sha))
                row = db.execute("SELECT * FROM ticket_budget WHERE project=? AND ticket=?",
                                 (project, ticket)).fetchone()
                return dict(row) if row else {"candidate_failures": 0, "infra_streak": 0,
                                              "next_attempt_at": 0, "last_cause": ""}
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def retry_reason(self, project: str, ticket: str, *, main_sha: str = "", now: float | None = None) -> str:
        """Admission based on ticket counters, never on an unstable fingerprint."""
        budget = self.budget(project, ticket, main_sha=main_sha)
        hold = self.ticket_hold(project_id=project, ticket_id=ticket)
        if hold:
            return f"ticket_hold:{hold['cause']}"
        epoch = self.clock().timestamp() if now is None else now
        return "backoff_infra" if float(budget["next_attempt_at"]) > epoch else ""

    def record_attempt(self, project: str, ticket: str, cause: str, *, run_id: str,
                       main_sha: str = "", detail: str = "", log_path: str = "",
                       phase: str = "unknown", next_step: str = "", notify: Any = None) -> dict[str, Any]:
        """Charge a failed run exactly once across launcher, dispatcher and resumes."""
        cause = canonical_cause(cause)
        category = classify(cause)
        main_sha = normalize_main_sha(main_sha)
        now = self.clock().astimezone(timezone.utc)
        attempt_count = 0
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute("INSERT OR IGNORE INTO ticket_attempts VALUES (?, ?, ?)",
                                   (project, ticket, run_id)).rowcount
                row = db.execute("SELECT * FROM ticket_budget WHERE project=? AND ticket=?",
                                 (project, ticket)).fetchone()
                candidate = int(row["candidate_failures"]) if row else 0
                streak = int(row["infra_streak"]) if row else 0
                if row and main_sha and row["main_sha"] != main_sha:
                    streak = 0
                if fresh:
                    candidate += int(category in {"candidate", "unknown"})
                    streak = streak + 1 if category == "infra" else 0
                    delay = min(600 * 2 ** min(max(streak - 1, 0), 4), 7200) if streak else 0
                    db.execute("""INSERT INTO ticket_budget VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project, ticket) DO UPDATE SET candidate_failures=excluded.candidate_failures,
                        infra_streak=excluded.infra_streak, next_attempt_at=excluded.next_attempt_at,
                        last_cause=excluded.last_cause, main_sha=excluded.main_sha, updated_at=excluded.updated_at""",
                               (project, ticket, candidate, streak, now.timestamp() + delay if delay else 0,
                                cause, main_sha or (row["main_sha"] if row else ""), now.isoformat()))
                new_hold = False
                if fresh and (category == "owner" or (category in {"candidate", "unknown"} and candidate >= 3)):
                    fp = make_fingerprint(project_id=project, ticket_id=ticket, error=cause,
                                          resource_version="ticket_budget", main_sha=main_sha)
                    new_hold = self.set_ticket_hold(fp, candidate if category != "owner" else 1, connection=db)
                attempt_count = int(db.execute(
                    "SELECT COUNT(*) FROM ticket_attempts WHERE project=? AND ticket=?", (project, ticket),
                ).fetchone()[0])
                db.commit()
            budget = self.budget(project, ticket)
            hold = self.ticket_hold(project_id=project, ticket_id=ticket)
            transition = "blocked" if hold else "retrying"
            if not next_step:
                if hold:
                    next_step = "Review the latest evidence, correct the cause, then release the ticket hold."
                elif category == "infra":
                    next_step = "The dispatcher will retry after its persisted backoff; inspect host and network evidence if it repeats."
                else:
                    next_step = "Inspect the latest evidence and correct the failure before the bounded retry is exhausted."
            from core.line.failure_alerts import record_actionable_failure, notify_deterministic_failure, notify_ticket_hold
            record_actionable_failure(
                ticket, cause, detail, phase=phase, attempts=attempt_count,
                next_step=next_step, transition=transition,
            )
            if fresh and new_hold and notify is not None:
                notify(ticket, cause, detail, log_path)
            elif category != "infra" and notify is None:
                if fresh and new_hold:
                    notify_ticket_hold(ticket, cause, detail, log_path)
                notify_deterministic_failure(
                    ticket, cause, detail, log_path, phase=phase, attempts=attempt_count,
                    next_step=next_step, transition=transition,
                )
            return budget
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def reset_budget(self, project: str, ticket: str) -> None:
        """A proven merge releases the hold and clears counters, retaining run deduplication."""
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM ticket_budget WHERE project=? AND ticket=?", (project, ticket))
                db.execute("DELETE FROM ticket_holds WHERE project_id=? AND ticket_id=?", (project, ticket))
                db.execute("DELETE FROM rebase_recovery WHERE project_id=? AND ticket_id=?", (project, ticket))
                db.commit()
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

    def rebase_inference_spent(self, fingerprint: AdmissionFingerprint) -> bool:
        """True when this fingerprint already used its single development inference."""
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT inferred FROM rebase_recovery WHERE fingerprint=?",
                    (fingerprint.sha256,),
                ).fetchone()
                return bool(row and int(row["inferred"]))
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def rebase_recovery_state(self, fingerprint: AdmissionFingerprint) -> dict[str, Any] | None:
        """Return durable recovery progress for one exact conflict fingerprint."""
        try:
            with self._connection() as db:
                row = db.execute(
                    "SELECT * FROM rebase_recovery WHERE fingerprint=?", (fingerprint.sha256,),
                ).fetchone()
                return dict(row) if row is not None else None
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def begin_rebase_recovery(self, fingerprint: AdmissionFingerprint) -> bool:
        """Claim the one deterministic repair attempt for this conflict fingerprint."""
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("""INSERT INTO rebase_recovery (
                    fingerprint, project_id, ticket_id, candidate_digest, content_digest, upstream_sha,
                    inferred, recovery_started, recovery_result, cause, resume_command, held_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, '', '', '', '', ?)
                ON CONFLICT(fingerprint) DO UPDATE SET recovery_started=1, updated_at=excluded.updated_at
                WHERE recovery_started=0""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.main_sha, now))
                db.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def reserve_rebase_inference(self, fingerprint: AdmissionFingerprint) -> bool:
        """Atomically spend the single inference before calling a development harness."""
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("""INSERT INTO rebase_recovery (
                    fingerprint, project_id, ticket_id, candidate_digest, content_digest, upstream_sha,
                    inferred, recovery_started, recovery_result, cause, resume_command, held_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, '', '', '', '', ?)
                ON CONFLICT(fingerprint) DO UPDATE SET inferred=1, updated_at=excluded.updated_at
                WHERE inferred=0""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.main_sha, now))
                db.commit()
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def record_rebase_inference(self, fingerprint: AdmissionFingerprint) -> None:
        """Compatibility wrapper; reserve before inference so a crash cannot repeat it."""
        self.reserve_rebase_inference(fingerprint)

    def record_rebase_recovery_result(
        self, fingerprint: AdmissionFingerprint, *, result: str,
    ) -> dict[str, Any]:
        """Persist the outcome of the bounded deterministic or harness recovery."""
        allowed = {"resolved", "route_wait", "interrupted", "technical_hold"}
        if result not in allowed:
            raise ValueError(f"invalid rebase recovery result: {result}")
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO rebase_recovery (
                    fingerprint, project_id, ticket_id, candidate_digest, content_digest, upstream_sha,
                    inferred, recovery_started, recovery_result, cause, resume_command, held_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?, '', '', '', ?)
                ON CONFLICT(fingerprint) DO UPDATE SET recovery_started=1,
                    recovery_result=excluded.recovery_result, updated_at=excluded.updated_at""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.main_sha,
                     result, now))
                db.commit()
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc
        state = self.rebase_recovery_state(fingerprint)
        if state is None:
            raise AdmissionStoreError("admission_store_unavailable")
        return state

    def record_rebase_technical_hold(
        self, fingerprint: AdmissionFingerprint, *, cause: str, resume_command: str,
    ) -> dict[str, Any]:
        """Store a fingerprint hold. Does not create a ticket hold or ask the owner."""
        stored_cause = canonical_cause(cause)
        command = " ".join(str(resume_command).split())[:500]
        now = self.clock().astimezone(timezone.utc).isoformat()
        try:
            with self._connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""INSERT INTO rebase_recovery (
                    fingerprint, project_id, ticket_id, candidate_digest, content_digest, upstream_sha,
                    inferred, recovery_started, recovery_result, cause, resume_command, held_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1, 'technical_hold', ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET recovery_started=1,
                    recovery_result='technical_hold', cause=excluded.cause,
                    resume_command=excluded.resume_command, held_at=excluded.held_at,
                    updated_at=excluded.updated_at""",
                    (fingerprint.sha256, fingerprint.project_id, fingerprint.ticket_id,
                     fingerprint.candidate_digest, fingerprint.context_digest, fingerprint.main_sha,
                     stored_cause, command, now, now))
                db.commit()
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc
        rows = self.rebase_recovery_rows(project_id=fingerprint.project_id, ticket_id=fingerprint.ticket_id)
        found = next((row for row in rows if row["fingerprint"] == fingerprint.sha256), None)
        if found is None:
            raise AdmissionStoreError("admission_store_unavailable")
        return found

    def rebase_recovery_rows(self, *, project_id: str, ticket_id: str) -> list[dict[str, Any]]:
        """Hashes, cause and resume command for one ticket. No conflict body is stored."""
        try:
            with self._connection() as db:
                rows = db.execute("""SELECT fingerprint, project_id, ticket_id, candidate_digest,
                    content_digest, upstream_sha, inferred, recovery_started, recovery_result,
                    cause, resume_command, held_at, updated_at
                    FROM rebase_recovery WHERE project_id=? AND ticket_id=? ORDER BY updated_at""",
                    (project_id, ticket_id)).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc

    def snapshot(self) -> list[dict[str, Any]]:
        """Safe diagnostic rows; contains hashes/classes only, never error text."""
        try:
            with self._connection() as db:
                rows = db.execute(
                    """SELECT fingerprint, project_id, ticket_id, candidate_digest,
                    context_digest, error_class, resource_version, recorded_at, next_probe_at,
                    backoff_sec, open, main_sha FROM failures ORDER BY recorded_at"""
                ).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            raise AdmissionStoreError("admission_store_unavailable") from exc


__all__ = [
    "AdmissionFingerprint", "AdmissionStore", "AdmissionStoreError", "make_fingerprint",
    "normalize_error", "normalize_main_sha", "safe_digest",
]
