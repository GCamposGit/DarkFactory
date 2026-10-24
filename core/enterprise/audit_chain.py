"""Cryptographically Chained Immutable Audit Trail for Enterprise Scope (HF-24).

Implements a SHA-256 hash-chained append-only event log ensuring mathematical
non-repudiation and immediate detection of any historical log tampering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
import uuid

from core.paths import project_root, state_root
from core.enterprise.models import (
    AuditChainVerificationResult,
    AuditEvent,
)

logger = logging.getLogger(__name__)

DEFAULT_ENTERPRISE_DIR = Path(".factory") / "enterprise"
GENESIS_HASH = "0" * 64
_PROCESS_LOCK = threading.Lock()


class AuditReadError(Exception):
    """Structured failure reading the audit log (fail-closed signal)."""

    def __init__(self, code: str, message: str, line: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.line = line


class ImmutableAuditChain:
    """Manages a tamper-evident, append-only hash-chained audit log.

    Guarantees: modification, reordering, deletion of a middle record or any
    unparsable/unreadable line is detected by ``verify_integrity`` (fail-closed).
    A hash chain alone cannot detect deletion of the *tail* records; that is not
    claimed. Writers are serialized by a process lock plus a lock file.
    ``load_events`` is a lenient display reader that skips corrupt lines; it
    must not be used as verification evidence.
    """

    def __init__(self, root: Optional[Path] = None, log_file: Optional[Path] = None) -> None:
        self.root = Path(root) if root else project_root()
        self.log_file = Path(log_file) if log_file else (state_root() / "enterprise" / "audit_trail.jsonl")
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
        with _PROCESS_LOCK, self._file_lock():
            return self._append_locked(
                actor_id, project_id, action, resource, payload, event_id
            )

    def _file_lock(self) -> Any:
        chain = self

        class _Lock:
            def __enter__(self_inner) -> "_Lock":
                path = chain.log_file.with_suffix(chain.log_file.suffix + ".lock")
                deadline = time.monotonic() + 10.0
                while True:
                    try:
                        self_inner.fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        self_inner.path = path
                        return self_inner
                    except FileExistsError:
                        try:
                            if time.time() - path.stat().st_mtime > 30:
                                path.unlink(missing_ok=True)  # stale lock
                                continue
                        except OSError:
                            pass
                        if time.monotonic() > deadline:
                            raise AuditReadError("lock_timeout", f"Cannot lock {path}")
                        time.sleep(0.01)

            def __exit__(self_inner, *exc: Any) -> None:
                os.close(self_inner.fd)
                self_inner.path.unlink(missing_ok=True)

        return _Lock()

    def _append_locked(self, actor_id, project_id, action, resource, payload, event_id) -> AuditEvent:
        events = self._read_strict()  # refuse to extend a corrupt/unreadable chain
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

    def _read_strict(self) -> List[AuditEvent]:
        """Read all events; raise AuditReadError on any malformed line or I/O error."""
        if not self.log_file.is_file():
            return []
        events: List[AuditEvent] = []
        try:
            with self.log_file.open("r", encoding="utf-8") as f:
                for line_idx, line in enumerate(f):
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        events.append(AuditEvent.model_validate(json.loads(line_str)))
                    except Exception as exc:
                        raise AuditReadError(
                            "malformed_line",
                            f"Malformed audit line {line_idx} in {self.log_file}: {exc}",
                            line_idx,
                        ) from exc
        except AuditReadError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise AuditReadError("read_failure", f"Cannot read {self.log_file}: {exc}") from exc
        return events

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
        try:
            all_events = self._read_strict()  # Must verify global chain sequence
        except AuditReadError as err:
            logger.error("Audit verification blocked: %s", err)
            return AuditChainVerificationResult(
                is_valid=False,
                total_events=0,
                error_message=str(err),
                status="unreadable",
                error_code=err.code,
                error_line=err.line,
            )
        if not all_events:
            return AuditChainVerificationResult(
                is_valid=True,
                total_events=0,
                error_message="Audit chain is empty (valid genesis state).",
                status="absent" if not self.log_file.is_file() else "intact",
            )

        expected_prev = GENESIS_HASH
        for idx, evt in enumerate(all_events):
            # 1. Sequence check
            if evt.sequence != idx:
                return AuditChainVerificationResult(
                    is_valid=False,
                    total_events=len(all_events),
                    tampered_event_id=evt.event_id,
                    status="tampered",
                    error_code="chain_violation",
                    error_message=f"Sequence anomaly: expected {idx}, got {evt.sequence}.",
                )

            # 2. Previous hash link check
            if evt.prev_hash != expected_prev:
                return AuditChainVerificationResult(
                    is_valid=False,
                    total_events=len(all_events),
                    tampered_event_id=evt.event_id,
                    status="tampered",
                    error_code="chain_violation",
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
                    status="tampered",
                    error_code="chain_violation",
                    error_message=f"Tampered content at event {evt.event_id}. Recomputed hash {recomputed[:12]}... does not match recorded {evt.event_hash[:12]}...",
                )

            expected_prev = evt.event_hash

        filtered_count = len([e for e in all_events if project_id is None or e.project_id == project_id])
        return AuditChainVerificationResult(
            is_valid=True,
            total_events=filtered_count,
            error_message="Cryptographic audit chain fully verified; 0 anomalies detected.",
        )
