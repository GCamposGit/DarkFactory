"""Durable atomic store for User Demands."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, get_ident
from typing import Any

from core.demands.models import UserTicket
from core.demands.id_allocator import _file_lock, reserve_ticket_id
from core.roadmap.models import DeliveryStatus, utc_now

logger = logging.getLogger(__name__)

DEFAULT_DEMANDS_PATH = Path(".factory/demands/demands.json")


class DemandsStore:
    """Thread-safe, atomic persistence for user demands in JSON format."""

    def __init__(
        self, path: Path | str | None = None, *, seed_path: Path | str | None = None,
    ) -> None:
        configured_path = os.environ.get("DARKFAC_DEMANDS_PATH")
        if path is not None:
            self.path = Path(path)
        else:
            self.path = Path(configured_path) if configured_path else DEFAULT_DEMANDS_PATH
        configured_seed = os.environ.get("DARKFAC_DEMANDS_SEED_PATH")
        use_environment_seed = path is None or (
            configured_path is not None and self.path == Path(configured_path)
        )
        self.seed_path = (
            Path(seed_path) if seed_path is not None
            else Path(configured_seed) if configured_seed and use_environment_seed
            else None
        )
        self.shared_volume_name = (
            os.environ.get("DARKFAC_DEMANDS_SHARED_VOLUME") if use_environment_seed else None
        )
        if self.shared_volume_name and not self.path.parent.is_mount():
            raise RuntimeError(
                f"Demand volume {self.shared_volume_name} is not mounted at {self.path.parent}"
            )
        self._write_lock_path = self.path.with_name("demands-write.lock")
        self._lock = RLock()
        self._ensure_storage()

    def _ensure_storage(self) -> None:
        with self._lock, _file_lock(self._write_lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            seed = self._read_seed()
            if not self.path.exists():
                self._write_raw(seed or [])
            elif seed is not None:
                merged = self._merge_seed(self._read_raw(strict=True), seed)
                if merged is not None:
                    self._write_raw(merged)

    def _read_seed(self) -> list[dict[str, Any]] | None:
        if self.seed_path is None or self.seed_path.resolve() == self.path.resolve():
            return None
        if not self.seed_path.is_file():
            raise FileNotFoundError(f"Demand seed is missing: {self.seed_path}")
        try:
            raw = json.loads(self.seed_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"Demand seed is unreadable: {self.seed_path}") from exc
        if not isinstance(raw, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in raw
        ):
            raise ValueError(f"Demand seed has invalid rows: {self.seed_path}")
        ids = [row["id"] for row in raw]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Demand seed has duplicate IDs: {self.seed_path}")
        return raw

    @staticmethod
    def _updated_at(row: dict[str, Any]) -> datetime:
        try:
            raw = str(row.get("updated_at", "")).replace("Z", "+00:00")
            timestamp = datetime.fromisoformat(raw)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def _merge_seed(
        cls, live: list[dict[str, Any]], seed: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        merged = list(live)
        positions: dict[str, int] = {}
        for index, row in enumerate(merged):
            ticket_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(ticket_id, str) or ticket_id in positions:
                raise ValueError("Live demand ledger has invalid or duplicate IDs")
            positions[ticket_id] = index
        changed = False
        for row in seed:
            ticket_id = row["id"]
            if ticket_id not in positions:
                positions[ticket_id] = len(merged)
                merged.append(row)
                changed = True
            elif cls._updated_at(row) > cls._updated_at(merged[positions[ticket_id]]):
                merged[positions[ticket_id]] = row
                changed = True
        return merged if changed else None

    def _read_raw(self, *, strict: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                content = self.path.read_text(encoding="utf-8").strip()
                if not content:
                    if strict:
                        raise ValueError("Demand ledger is empty")
                    return []
                payload = json.loads(content)
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict) and isinstance(payload.get("demands"), list):
                    return payload["demands"]
                if strict:
                    raise ValueError("Demand ledger must contain a list")
                return []
            except Exception as exc:
                if strict:
                    raise ValueError(f"Cannot read demand ledger: {self.path}") from exc
                logger.error(f"Failed to read demands from {self.path}: {exc}")
                return []

    def _write_raw(self, items: list[dict[str, Any]]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{get_ident()}.tmp"
            )
            content = json.dumps(items, indent=2, ensure_ascii=False)
            try:
                tmp_path.write_text(content, encoding="utf-8")
                tmp_path.replace(self.path)
            finally:
                tmp_path.unlink(missing_ok=True)

    def list_tickets(
        self,
        project_id: str | None = None,
        status: DeliveryStatus | None = None,
    ) -> list[UserTicket]:
        with self._lock:
            raw_items = self._read_raw(strict=self.seed_path is not None)
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
        with self._lock, _file_lock(self._write_lock_path):
            return self._save_ticket_locked(ticket)

    def _save_ticket_locked(self, ticket: UserTicket) -> UserTicket:
        """Persist one ticket while the cross-process ledger lock is held."""
        if ticket.status == DeliveryStatus.COMPLETED and not (ticket.delivery_evidence or "").strip():
            raise ValueError(f"Completed ticket {ticket.id} requires delivery_evidence")
        if ticket.status == DeliveryStatus.COMPLETED and ticket.is_live_deploy:
            evidence = (ticket.delivery_evidence or "").strip()
            if not any(marker in evidence for marker in ("live_converged", "live_verified", "live_provisional")):
                raise ValueError(
                    f"Completed ticket {ticket.id} with live deployment requirements requires live convergence proof in delivery_evidence"
                )
        raw_items = self._read_raw(strict=True)
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
            from core.projects.registry import get_project_registry

            prefix = get_project_registry().get_ticket_prefix(project_id)
            return reserve_ticket_id(self.path, prefix)

    def update_status(
        self,
        ticket_id: str,
        status: DeliveryStatus,
        notes: str | None = None,
        delivery_evidence: str | None = None,
    ) -> UserTicket:
        with self._lock, _file_lock(self._write_lock_path):
            ticket = next(
                (UserTicket.model_validate(raw) for raw in self._read_raw(strict=True)
                 if raw.get("id") == ticket_id),
                None,
            )
            if not ticket:
                raise KeyError(f"Ticket '{ticket_id}' not found")
            now = utc_now()
            updates: dict[str, Any] = {
                "status": status,
                "updated_at": now,
            }
            if delivery_evidence is not None:
                updates["delivery_evidence"] = delivery_evidence
            updated = ticket.model_copy(update=updates)
            self._save_ticket_locked(updated)
            return updated
