"""SQLite-backed shared state used by line coordination.

The database path may point to storage shared by multiple launcher instances.
Every mutation is performed in an explicit SQLite transaction so separate
processes, harnesses and hosts observe the same serialized state.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_SHARED_STATE_ENV = "DARKFAC_SHARED_STATE_PATH"


class SharedState:
    """Small transactional store shared by coordination clients."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        configured_path = path or os.environ.get(DEFAULT_SHARED_STATE_ENV)
        if configured_path is None:
            from core.paths import state_root

            # Same default as core.line.coordination.resolve_state_path, so every client on a
            # host opens the same registry regardless of the current directory.
            configured_path = state_root() / "state" / "shared.sqlite3"

        self.path = Path(configured_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS line_sessions (
                    session_id TEXT PRIMARY KEY,
                    harness TEXT NOT NULL,
                    model TEXT,
                    host TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_line_sessions_expiration
                    ON line_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS coordination_lease (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    lease_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    model TEXT,
                    host TEXT NOT NULL,
                    decided_at REAL NOT NULL,
                    origin TEXT NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coordination_status (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    absence_reason TEXT,
                    changed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS coordination_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    lease_id TEXT,
                    actor_session_id TEXT,
                    cause TEXT,
                    created_at REAL NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_coordination_events_created
                    ON coordination_events(created_at, event_id);
                """
            )

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        """Open an atomic transaction.

        ``BEGIN IMMEDIATE`` is used by lease operations so concurrent clients
        cannot both observe and acquire an empty coordinator slot.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def connection(self) -> sqlite3.Connection:
        """Return a caller-owned connection for read-only diagnostics."""

        return self._connect()
