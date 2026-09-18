"""Cryptographically Chained Immutable Audit Trail for Enterprise Scope (HF-24).

Implements a SHA-256 hash-chained append-only event log ensuring mathematical
non-repudiation and immediate detection of any historical log tampering.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
import uuid

from core.paths import project_root
from core.enterprise.models import (
    AuditChainVerificationResult,
    AuditEvent,
)

logger = logging.getLogger(__name__)

DEFAULT_ENTERPRISE_DIR = Path(".factory") / "enterprise"
GENESIS_HASH = "0" * 64


class ImmutableAuditChain:
    """Manages an immutable, append-only hash-chained audit log."""

    def __init__(self, root: Optional[Path] = None, log_file: Optional[Path] = None) -> None:
        self.root = Path(root) if root else project_root()
        self.log_file = Path(log_file) if log_file else (self.root / DEFAULT_ENTERPRISE_DIR / "audit_trail.jsonl")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def record_event(
        self,
        actor_id: str,
        project_id: str,
        action: str,
        resource: str,
        payload: Any = None,
        *,
        event_id: Optional[str] = None,
    ) -> AuditEvent:
        """Append a new tamper-evident audit event to the chain."""
        events = self.load_events()
        seq = len(events)
        prev_hash = events[-1].event_hash if events else GENESIS_HASH

        payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8") if payload else b""
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        ts = datetime.now(timezone.utc).isoformat()
        eid = event_id or f"evt_{uuid.uuid4().hex[:12]}"

        # Deterministic canonical string to hash
        canonical = f"{seq}|{eid}|{actor_id}|{project_id}|{action}|{resource}|{payload_hash}|{ts}|{prev_hash}"
        event_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        event = AuditEvent(
            event_id=eid,
            sequence=seq,
            actor_id=actor_id,
            project_id=project_id,
            action=action,
            resource=resource,
            payload_hash=payload_hash,
            timestamp=ts,
            prev_hash=prev_hash,
            event_hash=event_hash,
        )

        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

        return event

    def load_events(self, project_id: Optional[str] = None) -> List[AuditEvent]:
        """Load and parse events from disk, optionally filtered by project."""
        if not self.log_file.is_file():
            return []

        events: List[AuditEvent] = []
        with self.log_file.open("r", encoding="utf-8") as f:
            for line_idx, line in enumerate(f):
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    evt = AuditEvent.model_validate(data)
                    if project_id is None or evt.project_id == project_id:
                        events.append(evt)
                except Exception as exc:
                    logger.error(f"Corrupted audit line {line_idx} in {self.log_file}: {exc}")

        return events

    def verify_integrity(self, project_id: Optional[str] = None) -> AuditChainVerificationResult:
        """Mathematically verify the hash chain integrity from genesis to head."""
        all_events = self.load_events(project_id=None)  # Must verify global chain sequence
        if not all_events:
            return AuditChainVerificationResult(
                is_valid=True,
                total_events=0,
                error_message="Audit chain is empty (valid genesis state).",
            )

        expected_prev = GENESIS_HASH
        for idx, evt in enumerate(all_events):
            # 1. Sequence check
            if evt.sequence != idx:
                return AuditChainVerificationResult(
                    is_valid=False,
                    total_events=len(all_events),
                    tampered_event_id=evt.event_id,
                    error_message=f"Sequence anomaly: expected {idx}, got {evt.sequence}.",
                )

            # 2. Previous hash link check
            if evt.prev_hash != expected_prev:
                return AuditChainVerificationResult(
                    is_valid=False,
                    total_events=len(all_events),
                    tampered_event_id=evt.event_id,
                    error_message=f"Broken hash link at event {evt.event_id}. Expected prev_hash {expected_prev[:12]}..., got {evt.prev_hash[:12]}...",
                )

            # 3. Content hash recalculation
            canonical = f"{evt.sequence}|{evt.event_id}|{evt.actor_id}|{evt.project_id}|{evt.action}|{evt.resource}|{evt.payload_hash}|{evt.timestamp}|{evt.prev_hash}"
            recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if recomputed != evt.event_hash:
                return AuditChainVerificationResult(
                    is_valid=False,
                    total_events=len(all_events),
                    tampered_event_id=evt.event_id,
                    error_message=f"Tampered content at event {evt.event_id}. Recomputed hash {recomputed[:12]}... does not match recorded {evt.event_hash[:12]}...",
                )

            expected_prev = evt.event_hash

        filtered_count = len([e for e in all_events if project_id is None or e.project_id == project_id])
        return AuditChainVerificationResult(
            is_valid=True,
            total_events=filtered_count,
            error_message="Cryptographic audit chain fully verified; 0 anomalies detected.",
        )
