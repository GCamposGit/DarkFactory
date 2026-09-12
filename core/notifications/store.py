"""Persistent store and retention manager for notifications and alerts."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.notifications.models import AlertSeverity, NotificationEvent

logger = logging.getLogger("darkfac.notifications.store")


def get_default_retention_days() -> int:
    """Reads retention period from environment or defaults to 30 days."""
    try:
        val = os.environ.get("DARKFAC_NOTIFICATIONS_RETENTION_DAYS", "30").strip()
        return max(1, int(val))
    except (ValueError, TypeError):
        return 30


class NotificationStore:
    """Thread-safe append-only notification repository with automated retention pruning."""

    def __init__(
        self,
        store_path: Optional[Path] = None,
        retention_days: Optional[int] = None,
    ) -> None:
        self.store_path = store_path or Path(
            os.environ.get(
                "DARKFAC_NOTIFICATIONS_PATH",
                str(Path.cwd() / ".factory" / "notifications" / "notifications.jsonl"),
            )
        )
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days if retention_days is not None else get_default_retention_days()
        self._lock = threading.Lock()

    def add_notification(self, event: NotificationEvent) -> NotificationEvent:
        """Appends a new notification event to the store."""
        with self._lock:
            with self.store_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.model_dump(mode="json")) + "\n")
        return event

    def list_notifications(
        self,
        limit: int = 50,
        severity: Optional[AlertSeverity] = None,
        unread_only: bool = False,
    ) -> List[NotificationEvent]:
        """Returns recent notifications, optionally filtered by severity or read status."""
        if not self.store_path.exists():
            return []

        results: List[NotificationEvent] = []
        with self._lock:
            lines = self.store_path.read_text(encoding="utf-8").strip().splitlines()

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                event = NotificationEvent.model_validate(data)
                if severity is not None and event.severity != severity:
                    continue
                if unread_only and event.acknowledged:
                    continue
                results.append(event)
                if len(results) >= limit:
                    break
            except Exception as exc:
                logger.warning("Error parsing notification line: %s", exc)

        return results

    def get_notification(self, notification_id: str) -> Optional[NotificationEvent]:
        """Finds a specific notification by ID."""
        for event in self.list_notifications(limit=500):
            if event.notification_id == notification_id:
                return event
        return None

    def mark_acknowledged(self, notification_id: str) -> bool:
        """Marks a notification as acknowledged/read in the store."""
        if not self.store_path.exists():
            return False

        updated = False
        with self._lock:
            lines = self.store_path.read_text(encoding="utf-8").strip().splitlines()
            new_lines: List[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if data.get("notification_id") == notification_id:
                        data["acknowledged"] = True
                        updated = True
                    new_lines.append(json.dumps(data))
                except Exception:
                    new_lines.append(line)

            if updated:
                temp_file = self.store_path.with_suffix(".tmp")
                temp_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                temp_file.replace(self.store_path)

        return updated

    def prune_expired(self) -> int:
        """Prunes records older than the configured retention days. Returns pruned count."""
        if not self.store_path.exists():
            return 0

        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        pruned_count = 0

        with self._lock:
            lines = self.store_path.read_text(encoding="utf-8").strip().splitlines()
            retained_lines: List[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    ts_str = data.get("timestamp")
                    if ts_str:
                        # Parse ISO datetime
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if ts < cutoff:
                            pruned_count += 1
                            continue
                    retained_lines.append(line)
                except Exception:
                    retained_lines.append(line)

            if pruned_count > 0:
                temp_file = self.store_path.with_suffix(".tmp")
                temp_file.write_text(
                    ("\n".join(retained_lines) + "\n") if retained_lines else "",
                    encoding="utf-8",
                )
                temp_file.replace(self.store_path)
                logger.info("Pruned %d expired notifications (older than %d days)", pruned_count, self.retention_days)

        return pruned_count
