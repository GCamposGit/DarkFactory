"""Persistent refresh requests settled by the next snapshot publication, without host polling."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional

from pydantic import BaseModel, Field
from core.common.time import utc_now as _utc_now

DEFAULT_TARGET_NODE = "predator-neo-16"
MAX_REQUESTS_KEPT = 50


class RefreshStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class RefreshRequest(BaseModel):
    id: str
    status: RefreshStatus = RefreshStatus.PENDING
    requested_at: str
    target_node: str = DEFAULT_TARGET_NODE
    provider_id: str = ""
    updated_at: Optional[str] = None
    checked_at: Optional[str] = None
    failure_reason: Optional[str] = None


class RefreshRequestUpdate(BaseModel):
    status: RefreshStatus
    checked_at: Optional[str] = Field(default=None, max_length=64)
    failure_reason: Optional[str] = Field(default=None, max_length=500)


class RefreshRequestStore:
    """Persistent request ledger; the sidecar SQLite index serializes writers across processes."""

    def __init__(self, path: Path, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def _load(self) -> List[RefreshRequest]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [RefreshRequest.model_validate(item) for item in raw.get("requests", [])]
        except (OSError, ValueError, AttributeError):
            return []

    def _save(self, requests: List[RefreshRequest]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        body = {"requests": [r.model_dump(mode="json") for r in requests[-MAX_REQUESTS_KEPT:]]}
        tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _connect(self) -> sqlite3.Connection:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock.sqlite3")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(lock_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("CREATE TABLE IF NOT EXISTS refresh_lock (id INTEGER PRIMARY KEY CHECK (id = 1))")
        connection.execute("INSERT OR IGNORE INTO refresh_lock(id) VALUES (1)")
        return connection

    @contextmanager
    def _locked_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def create(self, target_node: Optional[str] = None, provider_id: str = "") -> RefreshRequest:
        now = self._clock().isoformat()
        request = RefreshRequest(
            id=uuid.uuid4().hex[:12],
            requested_at=now,
            updated_at=now,
            target_node=(target_node or "").strip() or DEFAULT_TARGET_NODE,
            provider_id=provider_id.strip().lower(),
        )
        with self._lock:
            with self._locked_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                requests = self._load()
                existing = next((r for r in reversed(requests) if
                    r.provider_id == request.provider_id and r.target_node == request.target_node and
                    r.status in (RefreshStatus.PENDING, RefreshStatus.IN_PROGRESS)), None)
                if existing:
                    connection.commit()
                    return existing
                requests.append(request)
                self._save(requests)
                connection.commit()
        return request

    def fail_provider_request(self, provider_id: str, reason: str) -> Optional[RefreshRequest]:
        """Mark the latest completed sample request as persistently unusable."""
        with self._lock:
            with self._locked_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                requests = self._load()
                request = next((r for r in reversed(requests) if
                    r.provider_id == provider_id.strip().lower() and r.status == RefreshStatus.DONE), None)
                if request is None:
                    connection.commit()
                    return None
                request.status = RefreshStatus.FAILED
                request.failure_reason = reason[:500]
                request.updated_at = self._clock().isoformat()
                self._save(requests)
                connection.commit()
                return request

    def get(self, request_id: str) -> Optional[RefreshRequest]:
        with self._lock:
            return next((r for r in self._load() if r.id == request_id), None)

    def pending(self, target_node: Optional[str] = None) -> List[RefreshRequest]:
        with self._lock:
            return [
                r
                for r in self._load()
                if r.status in (RefreshStatus.PENDING, RefreshStatus.IN_PROGRESS)
                and (not target_node or r.target_node == target_node)
            ]

    def latest_for_provider(self, provider_id: str, target_node: Optional[str] = None) -> Optional[RefreshRequest]:
        provider = provider_id.strip().lower()
        with self._lock:
            return next((r for r in reversed(self._load()) if r.provider_id == provider and
                         (not target_node or r.target_node == target_node)), None)

    def update(self, request_id: str, change: RefreshRequestUpdate) -> Optional[RefreshRequest]:
        with self._lock:
            with self._locked_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                requests = self._load()
                for request in requests:
                    if request.id != request_id:
                        continue
                    if request.status in (RefreshStatus.DONE, RefreshStatus.FAILED):
                        connection.commit()
                        return request
                    request.status = change.status
                    request.updated_at = self._clock().isoformat()
                    if change.status == RefreshStatus.DONE:
                        request.checked_at = change.checked_at or request.updated_at
                        request.failure_reason = None
                    elif change.status == RefreshStatus.FAILED:
                        request.failure_reason = change.failure_reason or "falha desconhecida na medicao"
                    self._save(requests)
                    connection.commit()
                    return request
                connection.commit()
        return None

    def complete_from_snapshots(self, node_id: str, accounts: list[dict[str, Any]]) -> None:
        """Settle pending refreshes on publication, without a separate host polling loop.

        A publication of old files must never be reported as a new measurement.
        """
        for request in self.pending(node_id):
            matching = [a for a in accounts if not request.provider_id or
                        str(a.get("provider_id", "")).strip().lower() == request.provider_id]
            if not matching:
                continue
            try:
                stamps = [datetime.fromisoformat(str(a.get("checked_at", "")).replace("Z", "+00:00"))
                          for a in matching]
                normalized = [stamp.replace(tzinfo=stamp.tzinfo or timezone.utc) for stamp in stamps]
                measured_at = max(normalized) if request.provider_id else min(normalized)
                requested = datetime.fromisoformat(request.requested_at.replace("Z", "+00:00"))
                requested = requested.replace(tzinfo=requested.tzinfo or timezone.utc)
            except (ValueError, TypeError):
                continue
            if measured_at > requested:
                self.update(request.id, RefreshRequestUpdate(status=RefreshStatus.DONE, checked_at=measured_at.isoformat()))
