"""Typed contracts for provider quotas and project-wide model telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from core.common.time import utc_now_iso


STALE_AFTER_SECONDS = 600


def snapshot_age_seconds(checked_at: Optional[str], now: Optional[datetime] = None) -> Optional[int]:
    """Whole seconds between ``checked_at`` and ``now`` (None when unparseable); never negative."""
    if not checked_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0, int((reference - parsed).total_seconds()))


def is_stale(age_seconds: Optional[int]) -> bool:
    """Strictly older than 600 s is stale; exactly 600 s is still fresh."""
    return age_seconds is not None and age_seconds > STALE_AFTER_SECONDS


class AccountConnectionStatus(str, Enum):
    CONNECTED = "connected"
    LIMITED = "limited"
    DISCONNECTED = "disconnected"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


class ProviderFamily(str, Enum):
    FRONTIER = "frontier"
    CHINESE = "chinese"
    GATEWAY = "gateway"
    LOCAL = "local"


class ModelModality(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    EMBEDDING = "embedding"
    MULTIMODAL = "multimodal"
    OTHER = "other"


class ModelTier(str, Enum):
    LOCAL = "local"
    TIER_2 = "tier_2"
    FRONTIER = "frontier"
    PROCEDURAL = "procedural"
    UNKNOWN = "unknown"


class QuotaWindow(BaseModel):
    quota_id: str
    label: str
    used_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    remaining_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    window_duration_minutes: Optional[int] = Field(default=None, ge=1)
    resets_at: Optional[str] = None
    metric: str = "subscription"


class ProbeFailure(BaseModel):
    """Sanitized reason the latest quota probe did not update a sample."""

    category: Literal["configuration", "transport", "authentication", "protocol", "invalid_response", "unavailable"]
    code: str
    occurred_at: str = Field(default_factory=utc_now_iso)
    message: str


class ProviderAccountUsage(BaseModel):
    provider_id: str
    provider_name: str
    family: ProviderFamily
    status: AccountConnectionStatus
    adapter: str
    plan: Optional[str] = None
    account_label: Optional[str] = None
    quota_supported: bool = False
    windows: List[QuotaWindow] = Field(default_factory=list)
    message: str
    dashboard_url: Optional[str] = None
    checked_at: str = Field(default_factory=utc_now_iso)
    raw_fields: dict[str, Any] = Field(default_factory=dict)
    period_start: Optional[str] = None
    plausibility_flags: List[str] = Field(default_factory=list)
    # Freshness of a synced snapshot (USR-233): node that sent it, age and the stale flag.
    source_node: Optional[str] = None
    age_seconds: Optional[int] = None
    stale: bool = False
    probe_failure: Optional[ProbeFailure] = None

    def with_freshness(self, now: Optional[datetime] = None) -> "ProviderAccountUsage":
        """Fill ``age_seconds``/``stale`` from ``checked_at`` (status and windows stay untouched)."""
        age = snapshot_age_seconds(self.checked_at, now)
        self.age_seconds = age
        self.stale = is_stale(age)
        return self


class AccountUsageReport(BaseModel):
    checked_at: str = Field(default_factory=utc_now_iso)
    accounts: List[ProviderAccountUsage]
    connected_count: int
    limited_count: int
    disconnected_count: int


class ModelCallEvent(BaseModel):
    invocation_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=128)
    ticket_id: Optional[str] = Field(default=None, max_length=120)
    project_id: str = Field(default="darkfac", max_length=80)
    execution_mode: Optional[str] = Field(default=None, max_length=50)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    tier: ModelTier = ModelTier.UNKNOWN
    harness: str = Field(default="darkhub", min_length=1, max_length=80)
    modality: ModelModality = ModelModality.TEXT
    success: bool = True
    input_tokens: Optional[int] = Field(default=None, ge=0)
    processing_tokens: Optional[int] = Field(default=0, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    cost_usd: Optional[float] = Field(default=None, ge=0.0)
    latency_ms: Optional[float] = Field(default=None, ge=0.0)
    source: str = Field(default="runtime", max_length=160)
    # Effective reasoning effort reported by the CLI, or the one the request declared (USR-201).
    reasoning_effort: Optional[str] = Field(default=None, max_length=32)
    timestamp: str = Field(default_factory=utc_now_iso)


class ModelUsageAggregate(BaseModel):
    provider: str
    model: str
    tier: ModelTier
    harness: str
    modality: ModelModality
    call_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    first_seen_at: str
    last_seen_at: str


class ModelUsageReport(BaseModel):
    generated_at: str = Field(default_factory=utc_now_iso)
    total_calls: int
    successful_calls: int
    failed_calls: int
    total_tokens: int
    total_cost_usd: float
    aggregates: List[ModelUsageAggregate]
    recent_events: List[ModelCallEvent]
