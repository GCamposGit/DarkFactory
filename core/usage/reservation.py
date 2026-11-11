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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, Field

from core.paths import state_root

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESERVATIONS_PATH = state_root() / "usage" / "reservations.json"


class QuotaReservation(BaseModel):
    """A temporary lease holding a slice of provider quota."""

    reservation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    provider_id: str
    harness: str
    percent: float = Field(ge=0.0, le=100.0, default=2.0)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expires_at: str
    ticket_id: Optional[str] = None
    purpose: str = "stage"


class QuotaReservationManager:
    """Thread-safe and process-safe reservation store for provider quotas."""

    def __init__(self, path: Optional[Path] = None, default_ttl_seconds: int = 900) -> None:
        self.path = Path(path) if path is not None else (state_root() / "usage" / "reservations.json")
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

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        """Exclusive lock so two processes cannot both pass the affordability check."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        handle = open(lock_path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def active_rows(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Reservations that have not expired."""

        current_time = now or datetime.now(timezone.utc)
        with self._lock:
            data = self._load_data()
            return self._prune_expired(data.get("reservations", []), current_time)

    def reserve_if_affordable(
        self,
        *,
        provider_id: str,
        harness: str,
        percent: float,
        headroom: float,
        required_total: float,
        ticket_id: Optional[str] = None,
        purpose: str = "review",
        ttl_seconds: Optional[int] = None,
    ) -> Tuple[bool, str, Optional[str], float]:
        """Reserve ``percent`` only when ``headroom`` still covers ``required_total``.

        Other active reservations for the same provider are subtracted first.
        The same ticket and purpose refresh the existing slice instead of stacking.
        """

        if percent < 0 or required_total < 0 or headroom < 0:
            raise ValueError("quota values cannot be negative")
        if percent > 100:
            raise ValueError("percent cannot exceed 100")
        clean_provider = provider_id.lower().strip()
        clean_harness = harness.lower().strip()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        with self._process_lock():
            with self._lock:
                now = datetime.now(timezone.utc)
                data = self._load_data()
                active = self._prune_expired(data.get("reservations", []), now)
                others: List[Dict[str, Any]] = []
                own: Optional[Dict[str, Any]] = None
                for row in active:
                    same_ticket = str(row.get("ticket_id") or "") == str(ticket_id or "")
                    same_purpose = str(row.get("purpose") or "") == purpose
                    same_harness = str(row.get("harness") or "") == clean_harness
                    if own is None and same_ticket and same_purpose and same_harness:
                        own = row
                    else:
                        others.append(row)
                reserved_others = sum(
                    float(row.get("percent") or 0.0)
                    for row in others
                    if str(row.get("provider_id") or "") == clean_provider
                )
                available = float(headroom) - reserved_others
                if available + 1e-9 < float(required_total):
                    data["reservations"] = others + ([own] if own else [])
                    self._save_data(data)
                    return False, "insufficient_quota", None, available
                if own is not None:
                    own["expires_at"] = (now + timedelta(seconds=ttl)).isoformat()
                    own["percent"] = float(percent)
                    data["reservations"] = others + [own]
                    self._save_data(data)
                    return True, "already_reserved", str(own.get("reservation_id") or ""), available
                reservation = QuotaReservation(
                    provider_id=clean_provider,
                    harness=clean_harness,
                    percent=float(percent),
                    created_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=ttl)).isoformat(),
                    ticket_id=ticket_id,
                    purpose=purpose,
                )
                data["reservations"] = others + [reservation.model_dump()]
                self._save_data(data)
                return True, "reserved", reservation.reservation_id, available

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
