"""Durable atomic store for User Demands."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock, get_ident
from typing import Any

from core.demands.models import UserTicket
from core.demands.id_allocator import _file_lock, reserve_ticket_id
from core.roadmap.models import DeliveryStatus, utc_now

logger = logging.getLogger(__name__)

DEFAULT_DEMANDS_PATH = Path(".factory/demands/demands.json")
_STATUS_PROGRESS = {
    DeliveryStatus.DISCOVERED: 0,
    DeliveryStatus.ACCEPTED: 1,
    DeliveryStatus.PLANNED: 2,
    DeliveryStatus.IMPLEMENTING: 3,
    DeliveryStatus.VALIDATING: 4,
    DeliveryStatus.COMPLETED: 5,
    DeliveryStatus.CANCELLED: 5,
}


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
        self.migration_gate_required = (
            os.environ.get("DARKFAC_DEMANDS_MIGRATION_GATE") == "required"
            and bool(self.shared_volume_name)
        )
        if self.shared_volume_name and not self.path.parent.is_mount():
            raise RuntimeError(
                f"Demand volume {self.shared_volume_name} is not mounted at {self.path.parent}"
            )
        if self.shared_volume_name:
            self._write_lock_path = self.path.with_name("demands-write.lock")
        else:
            canonical_path = str(self.path.resolve())
            if os.name == "nt":
                canonical_path = canonical_path.casefold()
            lock_id = sha256(canonical_path.encode("utf-8")).hexdigest()[:24]
            self._write_lock_path = Path(tempfile.gettempdir()) / "darkfac-demand-locks" / f"{lock_id}.lock"
        self._lock = RLock()
        self._ensure_storage()

    def _assert_migration_ready(self) -> None:
        """Cloud line cannot consume the seed before the Hub imports its old ledger."""
        if not self.migration_gate_required:
            return
        try:
            manifest = self.migration_manifest()
        except ValueError as exc:
            raise RuntimeError("Shared demand migration is pending") from exc
        if manifest is None:
            raise RuntimeError("Shared demand migration is pending")

    def migration_manifest(self) -> dict[str, str] | None:
        """Read and verify the durable first-rollout receipt against the live ledger."""
        marker = self.path.parent / ".shared_demands_migration.json"
        if not marker.exists():
            return None
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            versions = payload.get("versions") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict) or payload.get("version") != 1
                or payload.get("state") != "complete" or not isinstance(versions, dict)
                or not versions or payload.get("ticket_count") != len(versions)
                or any(not isinstance(k, str) or not k or not isinstance(v, str) for k, v in versions.items())
            ):
                raise ValueError("Invalid demand migration marker")
            canonical = json.dumps(versions, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if sha256(canonical).hexdigest() != payload.get("manifest_sha256"):
                raise ValueError("Demand migration marker hash mismatch")
            rows = self._read_raw(strict=True)
            by_id = {row.get("id"): row for row in rows if isinstance(row, dict)}
            if len(by_id) != len(rows):
                raise ValueError("Demand ledger has duplicate or invalid rows")
            for ticket_id, stamp in versions.items():
                expected = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if expected.tzinfo is None:
                    raise ValueError("Demand migration timestamp lacks timezone")
                row = by_id.get(ticket_id)
                if row is None or self._updated_at(row) < expected.astimezone(timezone.utc):
                    raise ValueError(f"Migrated demand {ticket_id} is missing or stale")
            return versions
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("Demand migration marker or ledger is invalid") from exc

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
                continue
            # A committed seed can advance delivery status, but cannot replace
            # runtime edits or regress a ticket because of clock skew.
            current_row = merged[positions[ticket_id]]
            current = UserTicket.model_validate(current_row)
            incoming = UserTicket.model_validate(row)
            if _STATUS_PROGRESS[incoming.status] > _STATUS_PROGRESS[current.status]:
                if incoming.status == DeliveryStatus.COMPLETED and not (incoming.delivery_evidence or "").strip():
                    raise ValueError(f"Completed seed demand {ticket_id} has no delivery evidence")
                advanced = dict(current_row)
                advanced["status"] = incoming.status.value
                advanced["delivery_evidence"] = incoming.delivery_evidence or current.delivery_evidence
                later = max(cls._updated_at(current_row), cls._updated_at(row)) + timedelta(microseconds=1)
                advanced["updated_at"] = later.isoformat().replace("+00:00", "Z")
                merged[positions[ticket_id]] = advanced
                changed = True
        return merged if changed else None

    def _read_raw(self, *, strict: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists():
                if strict:
                    raise ValueError(f"Demand ledger is missing: {self.path}")
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
        self._assert_migration_ready()
        with self._lock:
            raw_items = self._read_raw(strict=True)
            tickets: list[UserTicket] = []
            seen_ids: set[str] = set()
            for raw in raw_items:
                try:
                    ticket = UserTicket.model_validate(raw)
                    if ticket.id in seen_ids:
                        raise ValueError(f"Duplicate demand ticket ID: {ticket.id}")
                    seen_ids.add(ticket.id)
                    if project_id and ticket.project_id != project_id:
                        continue
                    if status and ticket.status != status:
                        continue
                    tickets.append(ticket)
                except Exception as exc:
                    if self.seed_path is not None or self.shared_volume_name:
                        raise ValueError(f"Invalid demand row in {self.path}") from exc
                    logger.warning(f"Skipping corrupted demand row in {self.path}: {exc}")
            return sorted(tickets, key=lambda t: t.id)

    def get_ticket(self, ticket_id: str) -> UserTicket | None:
        with self._lock:
            tickets = self.list_tickets()
            for ticket in tickets:
                if ticket.id == ticket_id:
                    return ticket
            return None

    def save_ticket(
        self, ticket: UserTicket, *, expected_updated_at: datetime | None = None,
    ) -> UserTicket:
        self._assert_migration_ready()
        with self._lock, _file_lock(self._write_lock_path):
            return self._save_ticket_locked(ticket, expected_updated_at=expected_updated_at)

    def _save_ticket_locked(
        self, ticket: UserTicket, *, expected_updated_at: datetime | None = None,
    ) -> UserTicket:
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
                current = UserTicket.model_validate(raw)
                if expected_updated_at is not None and current.updated_at != expected_updated_at:
                    raise ValueError(f"Stale demand ticket {ticket.id}: reload before saving")
                incoming_time = self._updated_at(ticket_dump)
                current_time = self._updated_at(raw)
                if incoming_time < current_time or (incoming_time == current_time and ticket_dump != raw):
                    raise ValueError(f"Stale demand ticket {ticket.id}: reload before saving")
                if _STATUS_PROGRESS[ticket.status] < _STATUS_PROGRESS[current.status]:
                    raise ValueError(f"Stale demand status for {ticket.id}: reload before saving")
                raw_items[i] = ticket_dump
                updated = True
                break
        if not updated:
            raw_items.append(ticket_dump)
        self._write_raw(raw_items)
        return ticket

    def next_ticket_id(self, project_id: str = "darkfac") -> str:
        self._assert_migration_ready()
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
        self._assert_migration_ready()
        with self._lock, _file_lock(self._write_lock_path):
            ticket = next(
                (UserTicket.model_validate(raw) for raw in self._read_raw(strict=True)
                 if raw.get("id") == ticket_id),
                None,
            )
            if not ticket:
                raise KeyError(f"Ticket '{ticket_id}' not found")
            now = max(utc_now(), self._updated_at(json.loads(ticket.model_dump_json())) + timedelta(microseconds=1))
            updates: dict[str, Any] = {
                "status": status,
                "updated_at": now,
            }
            if delivery_evidence is not None:
                updates["delivery_evidence"] = delivery_evidence
            updated = ticket.model_copy(update=updates)
            self._save_ticket_locked(updated)
            return updated
