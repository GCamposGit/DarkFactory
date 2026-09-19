"""Weighted Fair Scheduler and Hybrid Slot Capacity Manager (HF-23).

Governed by Universal Engineering Standards and Gate G1 Grill decisions.
Implements:
1. Strict Slot Bounds: 1 Heavy slot (GPU/full test suites) and up to 4 Light slots.
2. Weighted Fair Queueing (WFQ): Proportional dispatch favoring production projects
   (Atrium > Jarvis > DarkFac).
3. Starvation Guard: Guarantees that low-weight projects are not starved by promoting
   them after max_starvation_ticks.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    JobSlotKind,
    ProjectWeightConfig,
    SlotAllocation,
    SlotCapacityConfig,
    utc_now_iso,
)

import json
from pathlib import Path

DEFAULT_SCHEDULER_FILE = Path(".factory/portfolio/scheduler_state.json")

logger = logging.getLogger("darkfac.portfolio.scheduler")


class SlotSaturationError(Exception):
    """Raised when a worker attempts to claim a slot that has reached maximum capacity."""
    pass


class PortfolioScheduler:
    """Manages slot allocation capacity and weighted project queue scheduling."""

    def __init__(
        self,
        capacity_config: Optional[SlotCapacityConfig] = None,
        weight_config: Optional[ProjectWeightConfig] = None,
        storage_file: Optional[Path] = None,
        composed_mode: bool = False,
    ) -> None:
        self.capacity = capacity_config or SlotCapacityConfig()
        self.weights = weight_config or ProjectWeightConfig()
        self.storage_file = storage_file or DEFAULT_SCHEDULER_FILE
        self.composed_mode = composed_mode
        if not self.composed_mode:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)

        self._active_slots: Dict[str, SlotAllocation] = {}
        self._queues: Dict[str, List[Dict[str, Any]]] = {}
        self._starvation_ticks: Dict[str, int] = {}
        self._deficit_credits: Dict[str, float] = {}
        if not self.composed_mode:
            self._load()

    def _load(self) -> None:
        """Load state from persistent JSON file."""
        if self.storage_file.is_file():
            try:
                data = json.loads(self.storage_file.read_text(encoding="utf-8"))
                self._queues = data.get("queues", {})
                self._starvation_ticks = data.get("starvation_ticks", {})
                self._deficit_credits = data.get("deficit_credits", {})
                raw_slots = data.get("active_slots", {})
                self._active_slots = {
                    k: SlotAllocation.model_validate(v) for k, v in raw_slots.items()
                }
            except Exception as exc:
                logger.warning("Failed to load scheduler state: %s", exc)

    def _save(self) -> None:
        """Save state to persistent JSON file."""
        try:
            data = {
                "queues": self._queues,
                "starvation_ticks": self._starvation_ticks,
                "deficit_credits": self._deficit_credits,
                "active_slots": {k: v.model_dump() for k, v in self._active_slots.items()},
            }
            self.storage_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save scheduler state: %s", exc)

    def _reconcile_expired_slots(self, now: Optional[datetime] = None) -> None:
        """Purge slots whose leases have expired."""
        current_time = now or datetime.now(timezone.utc)
        expired_ids = []
        for slot_id, alloc in self._active_slots.items():
            try:
                exp = datetime.fromisoformat(alloc.expires_at)
                if current_time >= exp:
                    expired_ids.append(slot_id)
            except Exception:
                expired_ids.append(slot_id)

        for s_id in expired_ids:
            logger.info("Evicting expired slot allocation '%s'", s_id)
            del self._active_slots[s_id]

    def get_active_counts(self) -> Tuple[int, int]:
        """Return (active_heavy_slots, active_light_slots)."""
        self._reconcile_expired_slots()
        heavy = sum(1 for s in self._active_slots.values() if s.kind == JobSlotKind.HEAVY)
        light = sum(1 for s in self._active_slots.values() if s.kind == JobSlotKind.LIGHT)
        return heavy, light

    def can_acquire_slot(self, kind: JobSlotKind) -> bool:
        """Check if capacity is available for the given slot kind."""
        heavy, light = self.get_active_counts()
        if kind == JobSlotKind.HEAVY:
            return heavy < self.capacity.max_heavy_slots
        elif kind == JobSlotKind.LIGHT:
            return light < self.capacity.max_light_slots
        return False

    def acquire_slot(
        self,
        job_id: str,
        kind: JobSlotKind,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> Optional[SlotAllocation]:
        """Attempt to claim a capacity slot. Returns SlotAllocation if successful, None if saturated."""
        if self.composed_mode:
            raise RuntimeError("Composed mode forbids JSON slot allocations; delegate claims to ControlStore.")
        self._reconcile_expired_slots()
        if not self.can_acquire_slot(kind):
            logger.warning(
                "Slot capacity saturated for kind '%s' (Heavy: %d/%d, Light: %d/%d)",
                kind.value,
                *self.get_active_counts(),
                self.capacity.max_heavy_slots,
                self.capacity.max_light_slots,
            )
            return None

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        slot_id = f"slot_{kind.value}_{uuid.uuid4().hex[:8]}"

        alloc = SlotAllocation(
            slot_id=slot_id,
            job_id=job_id,
            worker_id=worker_id,
            kind=kind,
            acquired_at=now.isoformat(),
            expires_at=expires_at,
        )
        self._active_slots[slot_id] = alloc
        self._save()
        logger.info("Allocated %s slot '%s' to worker '%s' for job '%s'", kind.value, slot_id, worker_id, job_id)
        return alloc

    def release_slot(self, slot_id: str) -> bool:
        """Release an active slot allocation."""
        if slot_id in self._active_slots:
            del self._active_slots[slot_id]
            self._save()
            logger.info("Released slot '%s'", slot_id)
            return True
        return False

    # --------------------------------------------------------------------------
    # Queue Management with Weighted Fairness & Starvation Protection
    # --------------------------------------------------------------------------

    def enqueue_job(
        self,
        job_id: str,
        project_id: str,
        kind: JobSlotKind = JobSlotKind.LIGHT,
        priority: int = 0,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Insert a job into the project's queue."""
        if project_id not in self._queues:
            self._queues[project_id] = []
            self._starvation_ticks[project_id] = 0
            self._deficit_credits[project_id] = self.weights.weights.get(project_id, self.weights.default_weight)

        item = {
            "job_id": job_id,
            "project_id": project_id,
            "kind": kind,
            "priority": priority,
            "enqueued_at": utc_now_iso(),
            "payload": payload or {},
        }
        self._queues[project_id].append(item)
        self._save()
        logger.info("Enqueued job '%s' (%s) for project '%s'", job_id, kind.value, project_id)

    def dequeue_next(self) -> Optional[Dict[str, Any]]:
        """Select the next eligible job applying Weighted Fair Queueing and Starvation Protection.

        Only dequeues a job if slot capacity currently permits its kind.
        """
        self._reconcile_expired_slots()

        # Find projects that have queued jobs
        candidate_projects = [p for p, q in self._queues.items() if len(q) > 0]
        if not candidate_projects:
            return None

        # Filter candidate projects whose head job can acquire a slot
        dispatchable_projects = []
        for p in candidate_projects:
            head_job = self._queues[p][0]
            if self.can_acquire_slot(head_job["kind"]):
                dispatchable_projects.append(p)

        if not dispatchable_projects:
            logger.debug("No jobs can be dispatched due to slot capacity constraints.")
            return None

        # 1. Starvation Guard Check
        starved_projects = [
            p for p in dispatchable_projects
            if self._starvation_ticks.get(p, 0) >= self.weights.max_starvation_ticks
        ]
        if starved_projects:
            # Pick the project with the highest starvation ticks
            selected_project = max(starved_projects, key=lambda p: self._starvation_ticks.get(p, 0))
            logger.info("Starvation guard triggered for project '%s' (ticks=%d)", selected_project, self._starvation_ticks[selected_project])
            return self._pop_and_update_metrics(selected_project)

        # 2. Weighted Deficit Round-Robin Selection
        # Add credits based on configured weights
        for p in dispatchable_projects:
            weight = self.weights.weights.get(p, self.weights.default_weight)
            self._deficit_credits[p] = self._deficit_credits.get(p, 0.0) + weight

        # Choose the project with the highest accumulated deficit credits
        selected_project = max(dispatchable_projects, key=lambda p: self._deficit_credits.get(p, 0.0))
        # Deduct standard cost quantum (e.g. 1.0)
        self._deficit_credits[selected_project] = max(0.0, self._deficit_credits[selected_project] - 1.0)

        return self._pop_and_update_metrics(selected_project)

    def _pop_and_update_metrics(self, selected_project: str) -> Dict[str, Any]:
        """Pop head job from selected project and update starvation ticks for all projects."""
        job = self._queues[selected_project].pop(0)

        # Reset starvation for selected project
        self._starvation_ticks[selected_project] = 0

        # Increment starvation ticks for other projects that still have queued jobs
        for p, q in self._queues.items():
            if p != selected_project and len(q) > 0:
                self._starvation_ticks[p] = self._starvation_ticks.get(p, 0) + 1

        self._save()
        logger.info("Dispatched job '%s' from project '%s' (remaining queue: %d)", job["job_id"], selected_project, len(self._queues[selected_project]))
        return job

    def get_queue_status(self) -> Dict[str, int]:
        """Return counts of queued jobs per project."""
        return {p: len(q) for p, q in self._queues.items()}

    def get_starvation_status(self) -> Dict[str, int]:
        """Return starvation ticks per project."""
        return dict(self._starvation_ticks)
