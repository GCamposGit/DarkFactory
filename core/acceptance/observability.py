"""Structured observability, SLA latency tracking, and audit ledger for HF-15.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 5 & 11 / HF-15)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 4, line 60 & Section 7, Scenarios G1-G8)
  Target metrics:
  - Dispatch latency <= 30 seconds
  - Reconciliation latency <= 60 seconds
  - Zero false positives
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from core.acceptance.models import (
    AcceptanceTelemetryEvent,
    HF15MetricsSummary,
    ScenarioStatus,
)

logger = logging.getLogger("darkfac.acceptance.observability")

SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_\-]{20,})"),
    re.compile(r"(ghp_[a-zA-Z0-9]{20,})"),
    re.compile(r"(\d{8,12}:[a-zA-Z0-9_\-]{30,})"),  # Telegram bot token
    re.compile(r"(eyJ[a-zA-Z0-9_\-]+\.eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)"),  # JWT
]


def sanitize_payload(data: Any) -> Any:
    """Recursively redacts secrets and auth tokens from telemetry payloads."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if any(term in k.lower() for term in ("token", "key", "secret", "password", "auth")):
                sanitized[k] = "[REDACTED_SECRET]"
            else:
                sanitized[k] = sanitize_payload(v)
        return sanitized
    if isinstance(data, list):
        return [sanitize_payload(item) for item in data]
    if isinstance(data, str):
        masked = data
        for pattern in SECRET_PATTERNS:
            masked = pattern.sub("[REDACTED_SECRET]", masked)
        return masked
    return data


class HF15ObservabilityTracker:
    """Thread-safe append-only telemetry logger and metrics aggregator."""

    def __init__(self, ledger_path: Optional[Path] = None) -> None:
        self.ledger_path = ledger_path or (
            Path.cwd() / ".factory" / "hf15" / "workspace" / "telemetry" / "observability_ledger.jsonl"
        )
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._scenario_statuses: Dict[str, ScenarioStatus] = {}
        self._dispatch_latencies: List[float] = []
        self._reconciliation_latencies: List[float] = []
        self._rto_durations: List[float] = []
        self._max_slots: int = 0
        self._budget_spent: float = 0.0

    def record_event(
        self,
        scenario_id: str,
        event_type: str,
        duration_ms: float = 0.0,
        details: Optional[Dict[str, Any]] = None,
    ) -> AcceptanceTelemetryEvent:
        """Records a single audit event to the append-only ledger."""
        safe_details = sanitize_payload(details or {})
        event = AcceptanceTelemetryEvent(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            scenario_id=scenario_id,
            event_type=event_type,
            duration_ms=round(duration_ms, 2),
            details=safe_details,
            timestamp=datetime.now(UTC),
        )

        with self._lock:
            # Update metrics caches based on event_type
            if event_type == "dispatch_completed":
                self._dispatch_latencies.append(duration_ms)
            elif event_type == "reconciliation_completed":
                self._reconciliation_latencies.append(duration_ms)
            elif event_type == "rollback_completed" and "rto_seconds" in safe_details:
                self._rto_durations.append(float(safe_details["rto_seconds"]))
            elif event_type == "slots_allocated" and "active_slots" in safe_details:
                self._max_slots = max(self._max_slots, int(safe_details["active_slots"]))
            elif event_type == "budget_consumed" and "cost_usd" in safe_details:
                self._budget_spent += float(safe_details["cost_usd"])

            if event_type in {"scenario_passed", "scenario_failed", "scenario_rolled_back"}:
                status_map = {
                    "scenario_passed": ScenarioStatus.PASSED,
                    "scenario_failed": ScenarioStatus.FAILED,
                    "scenario_rolled_back": ScenarioStatus.ROLLED_BACK,
                }
                self._scenario_statuses[scenario_id] = status_map[event_type]

            # Write to jsonl
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event.model_dump(mode="json")) + "\n")

        return event

    @contextlib.contextmanager
    def measure_latency(
        self,
        scenario_id: str,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> Iterator[None]:
        """Context manager to measure and log operation duration in milliseconds."""
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            self.record_event(
                scenario_id=scenario_id,
                event_type=event_type,
                duration_ms=elapsed_ms,
                details=details,
            )

    def get_metrics_summary(self) -> HF15MetricsSummary:
        """Computes aggregate performance metrics and verifies compliance with SLAs."""
        with self._lock:
            passed = sum(1 for s in self._scenario_statuses.values() if s == ScenarioStatus.PASSED)
            failed = sum(1 for s in self._scenario_statuses.values() if s == ScenarioStatus.FAILED)
            rolled_back = sum(
                1 for s in self._scenario_statuses.values() if s == ScenarioStatus.ROLLED_BACK
            )

            avg_dispatch = (
                sum(self._dispatch_latencies) / len(self._dispatch_latencies)
                if self._dispatch_latencies
                else 0.0
            )
            avg_reconciliation = (
                sum(self._reconciliation_latencies) / len(self._reconciliation_latencies)
                if self._reconciliation_latencies
                else 0.0
            )
            avg_rto = (
                sum(self._rto_durations) / len(self._rto_durations)
                if self._rto_durations
                else 0.0
            )

            # SLAs: dispatch <= 30s (30,000ms), reconciliation <= 60s (60,000ms), zero unrecovered failures
            sla_dispatch = avg_dispatch <= 30000.0
            sla_reconciliation = avg_reconciliation <= 60000.0
            sla_no_failures = failed == 0

            all_slas = sla_dispatch and sla_reconciliation and sla_no_failures

            return HF15MetricsSummary(
                total_scenarios=8,
                passed_scenarios=passed,
                failed_scenarios=failed,
                rolled_back_scenarios=rolled_back,
                avg_dispatch_latency_ms=round(avg_dispatch, 2),
                avg_reconciliation_latency_ms=round(avg_reconciliation, 2),
                avg_rto_seconds=round(avg_rto, 4),
                max_active_slots_used=self._max_slots,
                total_budget_spent_usd=round(self._budget_spent, 4),
                all_slas_met=all_slas,
            )

    def get_recent_events(
        self,
        limit: int = 50,
        scenario_id: Optional[str] = None,
    ) -> List[AcceptanceTelemetryEvent]:
        """Reads recent events from the ledger, optionally filtered by scenario."""
        if not self.ledger_path.exists():
            return []

        events: List[AcceptanceTelemetryEvent] = []
        with self._lock:
            lines = self.ledger_path.read_text(encoding="utf-8").strip().splitlines()

        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                evt = AcceptanceTelemetryEvent.model_validate(raw)
                if scenario_id is None or evt.scenario_id == scenario_id:
                    events.append(evt)
                if len(events) >= limit:
                    break
            except Exception as exc:
                logger.warning("Error parsing ledger line: %s", exc)

        return events
