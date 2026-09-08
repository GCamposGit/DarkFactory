"""Durable atomic store for User Demands."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from threading import RLock
from typing import Any

from core.demands.models import UserTicket
from core.roadmap.models import DeliveryStatus, utc_now

logger = logging.getLogger(__name__)

DEFAULT_DEMANDS_PATH = Path(".factory/demands/demands.json")


class DemandsStore:
    """Thread-safe, atomic persistence for user demands in JSON format."""

    def __init__(self, path: Path | str = DEFAULT_DEMANDS_PATH) -> None:
        self.path = Path(path)
        self._lock = RLock()
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self._write_raw([])

    def _read_raw(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                content = self.path.read_text(encoding="utf-8").strip()
                if not content:
                    return []
                payload = json.loads(content)
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict) and "demands" in payload:
                    return payload["demands"]
                return []
            except Exception as exc:
                logger.error(f"Failed to read demands from {self.path}: {exc}")
                return []

    def _write_raw(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            content = json.dumps(items, indent=2, ensure_ascii=False)
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(self.path)

    def list_tickets(
        self,
        project_id: str | None = None,
        status: DeliveryStatus | None = None,
    ) -> list[UserTicket]:
        with self._lock:
            raw_items = self._read_raw()
            tickets: list[UserTicket] = []
            for raw in raw_items:
                try:
                    ticket = UserTicket.model_validate(raw)
                    if project_id and ticket.project_id != project_id:
                        continue
                    if status and ticket.status != status:
                        continue
                    tickets.append(ticket)
                except Exception as exc:
                    logger.warning(f"Skipping corrupted demand row in {self.path}: {exc}")
            return sorted(tickets, key=lambda t: t.id)

    def get_ticket(self, ticket_id: str) -> UserTicket | None:
        with self._lock:
            tickets = self.list_tickets()
            for ticket in tickets:
                if ticket.id == ticket_id:
                    return ticket
            return None

    def save_ticket(self, ticket: UserTicket) -> UserTicket:
        with self._lock:
            raw_items = self._read_raw()
            updated = False
            ticket_dump = json.loads(ticket.model_dump_json())
            for i, raw in enumerate(raw_items):
                if raw.get("id") == ticket.id:
                    raw_items[i] = ticket_dump
                    updated = True
                    break
            if not updated:
                raw_items.append(ticket_dump)
            self._write_raw(raw_items)
            return ticket

    def next_ticket_id(self, project_id: str = "darkfac") -> str:
        with self._lock:
            tickets = self.list_tickets(project_id=project_id)
            existing_numbers: list[int] = []
            for t in tickets:
                match = re.search(r"USR-(\d+)", t.id)
                if match:
                    existing_numbers.append(int(match.group(1)))
            next_num = max(existing_numbers, default=0) + 1
            return f"USR-{next_num:02d}"

    def update_status(
        self,
        ticket_id: str,
        status: DeliveryStatus,
        notes: str | None = None,
    ) -> UserTicket:
        with self._lock:
            ticket = self.get_ticket(ticket_id)
            if not ticket:
                raise KeyError(f"Ticket '{ticket_id}' not found")
            now = utc_now()
            updated = ticket.model_copy(update={
                "status": status,
                "updated_at": now,
            })
            self.save_ticket(updated)
            return updated
