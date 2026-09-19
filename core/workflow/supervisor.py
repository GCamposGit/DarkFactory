"""Continuous Portfolio Supervisor for the HF-05 workflow boundary.

Normative implementation of CONTRACTS.md and ticket HF-05-05.
Features:
- Stable paginated scanning of portfolio projects and jobs (resilient to dynamic insertion).
- Continuous state and lease repair (recovering expired claims in <= 60s under simulated clock).
- Outbox event reconciliation and materialization.
- Ready-age tracking and worker/consumer absence detection.
- Interruptible sleep up to 15s with 30s tick cycle.
- Zero LLM idling (purely deterministic state-machine loop).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.workflow.control_store import ControlStore
from core.workflow.reconciliation import (
    PortfolioReconciliationReport,
    reconcile_portfolio_state,
)

logger = logging.getLogger(__name__)


class SupervisorConfig(BaseModel):
    """Configuration parameters for the Portfolio Supervisor."""

    model_config = ConfigDict(extra="forbid")

    tick_interval_sec: float = 30.0
    max_sleep_sec: float = 15.0
    ready_age_threshold_sec: float = 60.0
    page_size: int = 50
    reconciliation_sla_sec: float = 60.0


class SupervisorTickReport(BaseModel):
    """Detailed deterministic execution report for a single supervisor tick."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    cycle_id: str
    projects_scanned: list[str] = Field(default_factory=list)
    active_runs_count: int = 0
    pending_jobs_count: int = 0
    running_jobs_count: int = 0
    repaired_leases_count: int = 0
    outbox_repaired_count: int = 0
    consumer_absent: bool = False
    starved_projects: list[str] = Field(default_factory=list)
    max_ready_age_sec: float = 0.0
    next_wakeup: datetime
    sla_met: bool = True


class PortfolioSupervisor:
    """Continuous portfolio supervisor scanning state, outbox, ready-age, and leases."""

    def __init__(
        self,
        store: ControlStore,
        config: SupervisorConfig | None = None,
        *,
        registry_provider: Callable[..., Sequence[str]] | None = None,
        outbox_dispatcher: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        self.store = store
        self.config = config or SupervisorConfig()
        self.registry_provider = registry_provider
        self.outbox_dispatcher = outbox_dispatcher
        self._last_tick: datetime | None = None
        self._is_running = False

    def scan_projects(self, limit: int | None = None) -> list[str]:
        """Stable paginated scan of all active projects in the portfolio."""
        page_size = limit or self.config.page_size
        all_projects: list[str] = []
        cursor: str | None = None

        while True:
            if hasattr(self.store, "list_active_projects"):
                page_projects, cursor = self.store.list_active_projects(cursor=cursor, limit=page_size)
            elif self.registry_provider is not None:
                all_projects = list(self.registry_provider())
                break
            else:
                # Fallback: single pass if list_active_projects not supported
                break

            all_projects.extend(page_projects)
            if not cursor or len(page_projects) < page_size:
                break

        return all_projects

    def tick(self, now: datetime | None = None) -> SupervisorTickReport:
        """Execute a single atomic supervisor cycle: scan, repair, metrics, wakeup."""
        effective_now = now or datetime.now(UTC)
        self._last_tick = effective_now

        # 1. Stable paginated scan of portfolio projects
        projects_scanned = self.scan_projects()

        # 2. Outbox and state repair (lease sweep and recovery)
        recon_report: PortfolioReconciliationReport = reconcile_portfolio_state(
            self.store,
            effective_now,
            limit=self.config.page_size,
        )

        # 3. Outbox processing / repair
        outbox_repaired = 0
        if hasattr(self.store, "get_pending_outbox"):
            pending_outbox = self.store.get_pending_outbox(limit=self.config.page_size)
            for evt in pending_outbox:
                if self.outbox_dispatcher:
                    try:
                        success = self.outbox_dispatcher(evt)
                        if success:
                            self.store.materialize(evt, effective_now)
                            outbox_repaired += 1
                    except Exception as exc:
                        logger.warning(f"Error dispatching outbox event {evt.get('outbox_id')}: {exc}")

        # 4. Ready-age and claim metrics
        ready_metrics: dict[str, Any] = {}
        if hasattr(self.store, "get_ready_age_metrics"):
            ready_metrics = self.store.get_ready_age_metrics(effective_now)

        active_runs = ready_metrics.get("active_runs", len(projects_scanned))
        pending_jobs = ready_metrics.get("pending_count", 0)
        running_jobs = ready_metrics.get("running_count", 0)
        max_ready_age = ready_metrics.get("max_ready_age_sec", 0.0)
        starved_projects = ready_metrics.get("starved_projects", [])

        # 5. Worker / consumer absence detection
        # Consumer is flagged as absent if:
        # - there are pending jobs and zero active/running jobs
        # - or pending jobs exceed the ready-age threshold
        consumer_absent = False
        if pending_jobs > 0 and (running_jobs == 0 or max_ready_age >= self.config.ready_age_threshold_sec):
            consumer_absent = True

        # 6. Calculate next wakeup
        next_wakeup_dt = self.next_wakeup(effective_now)

        return SupervisorTickReport(
            timestamp=effective_now,
            cycle_id=recon_report.cycle_id,
            projects_scanned=projects_scanned,
            active_runs_count=active_runs,
            pending_jobs_count=pending_jobs,
            running_jobs_count=running_jobs,
            repaired_leases_count=recon_report.repaired_keys_count,
            outbox_repaired_count=outbox_repaired,
            consumer_absent=consumer_absent,
            starved_projects=starved_projects,
            max_ready_age_sec=max_ready_age,
            next_wakeup=next_wakeup_dt,
            sla_met=recon_report.sla_met,
        )

    def next_wakeup(self, now: datetime | None = None) -> datetime:
        """Compute the next scheduled execution timestamp."""
        effective_now = now or datetime.now(UTC)
        # Bounded sleep: tick_interval_sec with maximum ceiling max_sleep_sec for interruptibility
        sleep_sec = min(self.config.tick_interval_sec, self.config.max_sleep_sec)
        return effective_now + timedelta(seconds=sleep_sec)

    def run(self, stop_event: threading.Event | None = None) -> None:
        """Run the supervisor continuous loop until stop_event is set."""
        event = stop_event or threading.Event()
        self._is_running = True

        try:
            while not event.is_set():
                now = datetime.now(UTC)
                self.tick(now)

                wakeup = self.next_wakeup(now)
                sleep_sec = max(0.1, min(self.config.max_sleep_sec, (wakeup - now).total_seconds()))

                # Interruptible wait
                if event.wait(timeout=sleep_sec):
                    break
        finally:
            self._is_running = False


# Canonical alias as requested by HF-05-05 specification
Supervisor = PortfolioSupervisor
