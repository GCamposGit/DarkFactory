"""Pydantic contracts for the operational roadmap projection."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return an aware UTC timestamp so snapshots are portable across hosts."""

    return datetime.now(timezone.utc)


class RoadmapItemType(str, Enum):
    EPIC = "epic"
    FEATURE = "feature"
    INFRASTRUCTURE = "infrastructure"
    RESEARCH = "research"
    QUALITY = "quality"
    SECURITY = "security"
    DOCUMENTATION = "documentation"
    OPERATIONS = "operations"


class LifecycleStage(str, Enum):
    FOUNDATIONS = "foundations"
    EXECUTION = "execution"
    NEXT_STEPS = "next_steps"
    FUTURE = "future"


class DeliveryStatus(str, Enum):
    DISCOVERED = "discovered"
    ACCEPTED = "accepted"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PlanningHorizon(str, Enum):
    NOW = "now"
    NEXT = "next"
    LATER = "later"
    EXPLORATORY = "exploratory"
    UNSCHEDULED = "unscheduled"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class FreshnessStatus(str, Enum):
    CURRENT = "current"
    AGING = "aging"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class DependencyType(str, Enum):
    REQUIRES = "requires"
    BLOCKS = "blocks"
    UNLOCKS = "unlocks"
    PARENT_OF = "parent_of"
    RELATED_TO = "related_to"


class RoadmapFlag(str, Enum):
    BLOCKED = "blocked"
    AT_RISK = "at_risk"
    CONFLICTING = "conflicting"
    OBSOLETE = "obsolete"


class ConfirmationState(str, Enum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class RoadmapSourceRef(BaseModel):
    """Navigable provenance for a planning or execution fact."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(..., min_length=1)
    source_kind: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    locator: str = Field(..., min_length=1)
    revision: str | None = None
    produced_by: str | None = None


class RoadmapEvidenceRef(BaseModel):
    """Evidence that can support a completion or operational state."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(..., min_length=1)
    evidence_kind: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    locator: str = Field(..., min_length=1)
    observed_at: datetime | None = None
    verified: bool = True


class RoadmapDependency(BaseModel):
    """Typed relation to another roadmap item.

    The relation is declared by the item containing it. For example, an item
    with ``requires(RM-01)`` can only advance after RM-01 is complete.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(..., min_length=1)
    type: DependencyType
    label: str | None = None


class RoadmapItem(BaseModel):
    """A single immutable projection of an operational roadmap entity."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    project_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    description: str = ""
    state_rationale: str = ""
    item_type: RoadmapItemType
    lifecycle_stage: LifecycleStage
    delivery_status: DeliveryStatus
    horizon: PlanningHorizon
    confidence: ConfidenceLevel

    parent_id: str | None = None
    planned_start: datetime | None = None
    target_date: datetime | None = None

    dependencies: list[RoadmapDependency] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    source_refs: list[RoadmapSourceRef] = Field(default_factory=list)
    evidence_refs: list[RoadmapEvidenceRef] = Field(default_factory=list)

    source_revision: str = ""
    observed_at: datetime = Field(default_factory=utc_now)
    last_verified_at: datetime = Field(default_factory=utc_now)
    freshness_status: FreshnessStatus = FreshnessStatus.CURRENT

    operational_flags: list[RoadmapFlag] = Field(default_factory=list)
    blocking_dependency_ids: list[str] = Field(default_factory=list)
    downstream_item_ids: list[str] = Field(default_factory=list)
    confirmation_state: ConfirmationState = ConfirmationState.CONFIRMED


class RoadmapCandidate(RoadmapItem):
    """Internal source record with precedence metadata."""

    id: str = ""
    source_id: str = Field(..., min_length=1)
    source_priority: int = 100


class RoadmapConsistencyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1)
    severity: str = Field(..., pattern="^(warning|error)$")
    message: str = Field(..., min_length=1)
    item_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class RoadmapSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    label: str
    source_kind: str
    locator: str
    status: str = Field(default="available", pattern="^(available|unavailable|stale)$")
    revision: str | None = None
    content_hash: str | None = None
    observed_at: datetime = Field(default_factory=utc_now)
    error: str | None = None


class RoadmapStats(BaseModel):
    total_items: int = 0
    confirmed_items: int = 0
    blocked_items: int = 0
    conflict_items: int = 0
    stale_items: int = 0
    by_horizon: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)


class RoadmapSnapshot(BaseModel):
    """Immutable-in-practice snapshot returned by all interfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    snapshot_hash: str
    project_id: str
    observed_at: datetime
    items: list[RoadmapItem] = Field(default_factory=list)
    stats: RoadmapStats = Field(default_factory=RoadmapStats)
    sources_consulted: list[RoadmapSourceState] = Field(default_factory=list)
    sources_unavailable: list[str] = Field(default_factory=list)
    issues: list[RoadmapConsistencyIssue] = Field(default_factory=list)
    derivation_policy: str
    source_fingerprint: str


class RoadmapHealth(BaseModel):
    project_id: str
    snapshot_id: str
    snapshot_hash: str
    observed_at: datetime
    total_items: int
    confirmed_items: int
    blocked_items: int
    conflicts: int
    warnings: int
    stale: bool
    sources_consulted: list[RoadmapSourceState] = Field(default_factory=list)
    sources_unavailable: list[str] = Field(default_factory=list)
    policy: str


class RoadmapProjectSummary(BaseModel):
    id: str
    name: str
    description: str
    roadmap_available: bool = True


class RoadmapSourceDocument(BaseModel):
    source_id: str
    label: str
    locator: str
    content: str
    content_type: str = "text/plain"


def model_sort_key(item: RoadmapItem) -> tuple[Any, ...]:
    """Stable ordering used by the compiler and every consumer."""

    stage_order = {
        LifecycleStage.FOUNDATIONS: 0,
        LifecycleStage.EXECUTION: 1,
        LifecycleStage.NEXT_STEPS: 2,
        LifecycleStage.FUTURE: 3,
    }
    horizon_order = {
        PlanningHorizon.NOW: 0,
        PlanningHorizon.NEXT: 1,
        PlanningHorizon.LATER: 2,
        PlanningHorizon.EXPLORATORY: 3,
        PlanningHorizon.UNSCHEDULED: 4,
    }
    return (stage_order[item.lifecycle_stage], horizon_order[item.horizon], item.id)
