"""Bounded, observable backlog-feeder cycle bound to the cloud coordinator loop (USR-304).

`BacklogFeeder` (USR-283) knew how to reconcile the ledger with the line but nothing called it.
`FeederCycle` is the operational binding: the long-running `CloudCoordinator` supervision loop
calls `maybe_run()` on every tick; the cycle only does work once per interval (default 15 min,
which is also the "VPS cron every 15 min" of USR-221 -- there is no second scheduler).

Per cycle it:
- reads the ledger (`.factory/demands/demands.json`, or `DARKFAC_DEMANDS_PATH`) through `DemandsStore`;
- evaluates every open ticket (Gate G1, dependencies, project, retries -- all unchanged);
- counts runs already in flight and only admits `slots - in_flight` new tickets, so a completed
  run frees its slot for the next ticket on the following cycle;
- optionally refuses to admit anything when no harness is above the quota floor;
- emits one structured log line and keeps the last report (`last_report`) for status endpoints.

Idempotency is delegated to `submit_ticket_to_line` (external id `ticket:<id>`): replays and
several coordinators never duplicate a run.
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from core.demands.store import DemandsStore
from core.line.backlog_feeder import BacklogFeeder

logger = logging.getLogger("darkfac.feeder_cycle")

ENABLED_ENV = "DARKFAC_FEEDER_ENABLED"
INTERVAL_ENV = "DARKFAC_FEEDER_INTERVAL_SEC"
SLOTS_ENV = "DARKFAC_FEEDER_MAX_ACTIVE"
QUOTA_GATE_ENV = "DARKFAC_FEEDER_QUOTA_GATE"
DEFAULT_INTERVAL_SEC = 900.0

QuotaGate = Callable[[], tuple[bool, str]]


class FeederCycleReport(BaseModel):
    """Outcome of one feeder cycle (log/metric/event payload)."""

    ran_at: str
    evaluated: int = 0
    eligible: int = 0
    in_flight: int = 0
    free_slots: int = 0
    submitted: int = 0
    replayed: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    submitted_ids: list[str] = Field(default_factory=list)
    idle_reason: str | None = None
    error: str | None = None


def _truthy(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def interval_from_env() -> float:
    try:
        value = float(os.environ.get(INTERVAL_ENV, "").strip() or DEFAULT_INTERVAL_SEC)
    except ValueError:
        return DEFAULT_INTERVAL_SEC
    return value if value > 0 else DEFAULT_INTERVAL_SEC


def default_quota_gate() -> tuple[bool, str]:
    """True when some write-capable harness is above the 15% floor (fail-closed on errors)."""
    try:
        from core.line.routing import pick

        chosen = pick("development", ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"])
    except Exception as exc:
        return False, f"quota_floor: probe failed ({exc})"
    if chosen is None:
        return False, "quota_floor: no harness above the critical floor"
    return True, ""


class FeederCycle:
    """Interval-bounded wrapper around `BacklogFeeder.reconcile_and_feed`."""

    def __init__(
        self,
        feeder: BacklogFeeder,
        *,
        interval_sec: float | None = None,
        max_active: int = 3,
        quota_gate: QuotaGate | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.feeder = feeder
        self.interval_sec = interval_sec if interval_sec is not None else interval_from_env()
        self.max_active = max_active
        self.quota_gate = quota_gate
        self._clock = clock
        self._last_run: float | None = None
        self.last_report: FeederCycleReport | None = None
        self.cycles_run = 0

    @classmethod
    def from_env(cls, control_store: Any, *, max_active: int = 3) -> FeederCycle | None:
        """Build the production cycle, or None when disabled via `DARKFAC_FEEDER_ENABLED=0`."""
        if not _truthy(os.environ.get(ENABLED_ENV), True):
            return None
        if os.environ.get("DARKFAC_DEMANDS_PATH"):
            demands = DemandsStore()
        else:
            from core.paths import project_root

            demands = DemandsStore(Path(project_root()) / ".factory" / "demands" / "demands.json")
        feeder = BacklogFeeder(demands_store=demands, control_store=control_store)
        try:
            slots = int(os.environ.get(SLOTS_ENV, "").strip() or max_active)
        except ValueError:
            slots = max_active
        gate = default_quota_gate if _truthy(os.environ.get(QUOTA_GATE_ENV), False) else None
        return cls(feeder, max_active=slots, quota_gate=gate)

    def due(self) -> bool:
        return self._last_run is None or (self._clock() - self._last_run) >= self.interval_sec

    def maybe_run(self, *, now: datetime | None = None) -> FeederCycleReport | None:
        """Run a cycle if the interval elapsed; never raises."""
        if not self.due():
            return None
        self._last_run = self._clock()
        return self.run_once(now=now)

    def run_once(self, *, now: datetime | None = None) -> FeederCycleReport:
        current = now or datetime.now(UTC)
        report = FeederCycleReport(ran_at=current.isoformat())
        self.cycles_run += 1
        try:
            candidates = self.feeder.list_candidates(now=now)
            report.evaluated = len(candidates)
            report.eligible = sum(1 for c in candidates if c.eligible)
            report.in_flight = sum(1 for c in candidates if c.reason == "in_flight")
            skipped = Counter(c.reason or "unknown" for c in candidates if not c.eligible)
            report.skipped = dict(skipped)
            report.free_slots = max(0, self.max_active - report.in_flight)

            if report.eligible == 0:
                report.idle_reason = "no_eligible_ticket"
            elif report.free_slots == 0:
                report.idle_reason = "no_free_slot"
            elif self.quota_gate is not None and not (gate := self.quota_gate())[0]:
                report.idle_reason = gate[1] or "quota_floor"
                report.skipped["quota_floor"] = report.eligible
            else:
                fed = self.feeder.reconcile_and_feed(limit=report.free_slots, now=now)
                report.submitted = fed.submitted_count
                report.replayed = fed.replayed_count
                report.submitted_ids = [s.ticket_id for s in fed.submissions if s.ok and not s.replayed]
                refused = [s for s in fed.submissions if not s.ok]
                if refused:
                    report.skipped["submit_refused"] = len(refused)
        except Exception as exc:  # fail closed: the loop must survive store outages
            report.error = f"{type(exc).__name__}: {exc}"
            report.idle_reason = "store_error"
            logger.warning("Feeder cycle failed: %s", report.error)
        self.last_report = report
        logger.info("feeder_cycle %s", report.model_dump_json())
        return report


__all__ = ["FeederCycle", "FeederCycleReport", "default_quota_gate", "interval_from_env"]
