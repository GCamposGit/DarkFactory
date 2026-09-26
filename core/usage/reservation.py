"""Atomic quota reservation manager for concurrent tasks in Dark Factory.

Prevents multiple parallel workers from simultaneously observing the same quota headroom
and exhausting provider limits before cooldown or rate limit errors trigger.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESERVATIONS_PATH = REPO_ROOT / ".factory" / "usage" / "reservations.json"


class QuotaReservation(BaseModel):
    """A temporary lease holding a slice of provider quota."""

    reservation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str
    harness: str
    percent: float = Field(ge=0.0, le=100.0, default=2.0)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str
    ticket_id: Optional[str] = None


class QuotaReservationManager:
    """Thread-safe and process-safe reservation store for provider quotas."""

    def __init__(self, path: Optional[Path] = None, default_ttl_seconds: int = 900) -> None:
        self.path = Path(path) if path is not None else DEFAULT_RESERVATIONS_PATH
        self.default_ttl_seconds = default_ttl_seconds
        self._lock = threading.RLock()

    def _parse_time(self, raw: Any) -> Optional[datetime]:
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    def _load_data(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return {"reservations": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "reservations" in raw and isinstance(raw["reservations"], list):
                return raw
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load reservations from %s: %s", self.path, exc)
        return {"reservations": []}

    def _save_data(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _prune_expired(self, items: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
        active = []
        for r in items:
            exp = self._parse_time(r.get("expires_at"))
            if exp and exp > now:
                active.append(r)
        return active

    def reserve(
        self,
        provider_id: str,
        harness: str,
        percent: float = 2.0,
        ttl_seconds: Optional[int] = None,
        ticket_id: Optional[str] = None,
    ) -> str:
        """Create a quota reservation for a job, returning the reservation_id."""
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        res = QuotaReservation(
            provider_id=provider_id.lower().strip(),
            harness=harness.lower().strip(),
            percent=percent,
            created_at=now.isoformat(),
            expires_at=expires_at,
            ticket_id=ticket_id,
        )

        with self._lock:
            data = self._load_data()
            active = self._prune_expired(data.get("reservations", []), now)
            active.append(res.model_dump())
            data["reservations"] = active
            self._save_data(data)

        logger.debug(
            "Created quota reservation %s for %s (%s%%, TTL %ss)",
            res.reservation_id,
            provider_id,
            percent,
            ttl,
        )
        return res.reservation_id

    def release(self, reservation_id: str) -> bool:
        """Release a previously acquired quota reservation."""
        if not reservation_id:
            return False
        with self._lock:
            data = self._load_data()
            now = datetime.now(timezone.utc)
            original = data.get("reservations", [])
            active = self._prune_expired(original, now)
            filtered = [r for r in active if r.get("reservation_id") != reservation_id]
            released = len(filtered) < len(active)
            data["reservations"] = filtered
            self._save_data(data)

        if released:
            logger.debug("Released quota reservation %s", reservation_id)
        return released

    def get_active_reserved_percent(self, provider_id: str, now: Optional[datetime] = None) -> float:
        """Sum of currently active reserved percentage for a provider."""
        current_time = now or datetime.now(timezone.utc)
        clean_provider = provider_id.lower().strip()
        with self._lock:
            data = self._load_data()
            active = self._prune_expired(data.get("reservations", []), current_time)
            total = sum(
                float(r.get("percent", 0.0))
                for r in active
                if r.get("provider_id") == clean_provider
            )
            return round(total, 2)
