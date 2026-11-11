"""Deterministic scheduling with priority for already-built candidates.

This module keeps resource allocation for a scheduling decision atomic: quota
and host capacity are copied before selection and consumed only after both
requirements have been checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Tuple


class WorkKind(str, Enum):
    """Kinds of work understood by the candidate-aware scheduler."""

    READY_CANDIDATE = "ready_candidate"
    REVIEW = "review"
    CORRECTION = "correction"
    RESUME = "resume"
    NEW_BUILD = "new_build"


@dataclass(frozen=True)
class CandidateSchedulingConfig:
    """Configuration controlling admission of new builds."""

    max_unmerged_candidates: Optional[int] = 3

    def __post_init__(self) -> None:
        limit = self.max_unmerged_candidates
        if limit is not None and limit < 0:
            raise ValueError("max_unmerged_candidates cannot be negative")

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, object],
    ) -> "CandidateSchedulingConfig":
        raw_limit = values.get(
            "max_unmerged_candidates",
            values.get("MAX_UNMERGED_CANDIDATES", 3),
        )
        if raw_limit is None:
            return cls(max_unmerged_candidates=None)

        if isinstance(raw_limit, str):
            normalized = raw_limit.strip().lower()
            if normalized in {"", "none", "unlimited"}:
                return cls(max_unmerged_candidates=None)
            raw_limit = normalized

        return cls(max_unmerged_candidates=int(raw_limit))

    @classmethod
    def from_environment(
        cls,
        environment: Optional[Mapping[str, str]] = None,
    ) -> "CandidateSchedulingConfig":
        source = os.environ if environment is None else environment
        raw_limit = source.get(
            "ORCHESTRATOR_MAX_UNMERGED_CANDIDATES",
            source.get("MAX_UNMERGED_CANDIDATES", "3"),
        )
        return cls.from_mapping(
            {"max_unmerged_candidates": raw_limit},
        )


@dataclass(frozen=True)
class WorkItem:
    """A schedulable unit and its resource requirements.

    ``queue_rank`` carries the pre-existing scheduler rank. The candidate-aware
    selector does not reorder work within the existing-work and new-build
    groups beyond that rank and the stable fallback fields.
    """

    work_id: str
    kind: WorkKind
    queue_rank: int = 0
    created_at: float = 0.0
    quota_pool: Optional[str] = None
    quota_required: float = 0.0
    host_pool: Optional[str] = None
    host_required: int = 1
    eligible: bool = True

    def __post_init__(self) -> None:
        if not self.work_id:
            raise ValueError("work_id is required")
        if self.quota_required < 0:
            raise ValueError("quota_required cannot be negative")
        if self.host_required < 0:
            raise ValueError("host_required cannot be negative")
        if self.quota_required and not self.quota_pool:
            raise ValueError("quota_pool is required when quota is consumed")
        if self.host_required and not self.host_pool:
            raise ValueError("host_pool is required when a host is consumed")

    @property
    def is_new_build(self) -> bool:
        return self.kind is WorkKind.NEW_BUILD

    @property
    def is_ready_candidate(self) -> bool:
        return self.kind is WorkKind.READY_CANDIDATE


@dataclass(frozen=True)
class SchedulingDecision:
    """Result of one scheduling pass."""

    selected: Tuple[WorkItem, ...]
    deferred: Mapping[str, str]
    remaining_quota: Mapping[str, float]
    remaining_hosts: Mapping[str, int]
    new_builds_paused: bool = False

    def reason_for(self, work_id: str) -> Optional[str]:
        return self.deferred.get(work_id)


@dataclass
class CandidatePriorityScheduler:
    """Select work while protecting ready candidates from new builds."""

    config: CandidateSchedulingConfig = field(
        default_factory=CandidateSchedulingConfig,
    )

    def select(
        self,
        work: Iterable[WorkItem],
        quota_available: Mapping[str, float],
        hosts_available: Mapping[str, int],
        unmerged_candidate_count: Optional[int] = None,
    ) -> SchedulingDecision:
        items = tuple(work)
        candidate_count = self._candidate_count(
            items,
            unmerged_candidate_count,
        )
        new_builds_paused = self._new_builds_paused(candidate_count)

        quota = {
            name: float(available)
            for name, available in quota_available.items()
        }
        hosts = {
            name: int(available)
            for name, available in hosts_available.items()
        }
        selected = []
        deferred = {}

        for item in sorted(items, key=self._selection_key):
            if not item.eligible:
                deferred[item.work_id] = "ineligible"
                continue

            if item.is_new_build and new_builds_paused:
                deferred[item.work_id] = "candidate_limit"
                continue

            missing_resource = self._missing_resource(item, quota, hosts)
            if missing_resource is not None:
                deferred[item.work_id] = missing_resource
                continue

            self._reserve(item, quota, hosts)
            selected.append(item)

        return SchedulingDecision(
            selected=tuple(selected),
            deferred=MappingProxyType(deferred),
            remaining_quota=MappingProxyType(quota),
            remaining_hosts=MappingProxyType(hosts),
            new_builds_paused=new_builds_paused,
        )

    def admits_new_build(self, unmerged_candidate_count: int) -> bool:
        """Return whether a new build may be admitted at the current count."""

        if unmerged_candidate_count < 0:
            raise ValueError("unmerged_candidate_count cannot be negative")
        return not self._new_builds_paused(unmerged_candidate_count)

    def _candidate_count(
        self,
        items: Tuple[WorkItem, ...],
        supplied_count: Optional[int],
    ) -> int:
        if supplied_count is not None:
            if supplied_count < 0:
                raise ValueError(
                    "unmerged_candidate_count cannot be negative",
                )
            return supplied_count

        return sum(
            1
            for item in items
            if item.is_ready_candidate and item.eligible
        )

    def _new_builds_paused(self, candidate_count: int) -> bool:
        limit = self.config.max_unmerged_candidates
        return limit is not None and candidate_count >= limit

    @staticmethod
    def _selection_key(item: WorkItem) -> tuple:
        # Existing work always precedes a new BUILD. Within each group, retain
        # the scheduler's established rank and deterministic fallback order.
        admission_group = 1 if item.is_new_build else 0
        return (
            admission_group,
            item.queue_rank,
            item.created_at,
            item.work_id,
        )

    @staticmethod
    def _missing_resource(
        item: WorkItem,
        quota: Mapping[str, float],
        hosts: Mapping[str, int],
    ) -> Optional[str]:
        if item.quota_required:
            available_quota = quota.get(item.quota_pool or "", 0.0)
            if available_quota < item.quota_required:
                return "quota"

        if item.host_required:
            available_hosts = hosts.get(item.host_pool or "", 0)
            if available_hosts < item.host_required:
                return "host"

        return None

    @staticmethod
    def _reserve(
        item: WorkItem,
        quota: dict,
        hosts: dict,
    ) -> None:
        if item.quota_required:
            quota_pool = item.quota_pool or ""
            quota[quota_pool] -= item.quota_required

        if item.host_required:
            host_pool = item.host_pool or ""
            hosts[host_pool] -= item.host_required


def prioritize_work(
    work: Iterable[WorkItem],
    quota_available: Mapping[str, float],
    hosts_available: Mapping[str, int],
    *,
    max_unmerged_candidates: Optional[int] = 3,
    unmerged_candidate_count: Optional[int] = None,
) -> SchedulingDecision:
    """Convenience entry point for a single scheduling decision."""

    scheduler = CandidatePriorityScheduler(
        CandidateSchedulingConfig(
            max_unmerged_candidates=max_unmerged_candidates,
        ),
    )
    return scheduler.select(
        work,
        quota_available,
        hosts_available,
        unmerged_candidate_count=unmerged_candidate_count,
    )
