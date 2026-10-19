"""USR-234: persistent "refresh quotas" requests handed from the Hub to the metering host.

The Hub (VPS) never probes provider CLIs; a button press only records a request in a JSON file on
the Hub volume. The host that owns the CLIs polls the pending requests, measures, syncs and closes
each request with the new ``checked_at`` (or a failure reason).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

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
    updated_at: Optional[str] = None
    checked_at: Optional[str] = None
    failure_reason: Optional[str] = None


class RefreshRequestUpdate(BaseModel):
    status: RefreshStatus
    checked_at: Optional[str] = Field(default=None, max_length=64)
    failure_reason: Optional[str] = Field(default=None, max_length=500)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RefreshRequestStore:
    """JSON-file store; atomic replace, process-local lock."""

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

    def create(self, target_node: Optional[str] = None) -> RefreshRequest:
        now = self._clock().isoformat()
        request = RefreshRequest(
            id=uuid.uuid4().hex[:12],
            requested_at=now,
            updated_at=now,
            target_node=(target_node or "").strip() or DEFAULT_TARGET_NODE,
        )
        with self._lock:
            requests = self._load()
            requests.append(request)
            self._save(requests)
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

    def update(self, request_id: str, change: RefreshRequestUpdate) -> Optional[RefreshRequest]:
        with self._lock:
            requests = self._load()
            for request in requests:
                if request.id != request_id:
                    continue
                if request.status in (RefreshStatus.DONE, RefreshStatus.FAILED):
                    return request
                request.status = change.status
                request.updated_at = self._clock().isoformat()
                if change.status == RefreshStatus.DONE:
                    request.checked_at = change.checked_at or request.updated_at
                    request.failure_reason = None
                elif change.status == RefreshStatus.FAILED:
                    request.failure_reason = change.failure_reason or "falha desconhecida na medicao"
                self._save(requests)
                return request
        return None
