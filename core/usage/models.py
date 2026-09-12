"""Typed contracts for provider quotas and project-wide model telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Return an RFC 3339 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


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


class AccountUsageReport(BaseModel):
    checked_at: str = Field(default_factory=utc_now_iso)
    accounts: List[ProviderAccountUsage]
    connected_count: int
    limited_count: int
    disconnected_count: int


class ModelCallEvent(BaseModel):
    invocation_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=128)
    ticket_id: Optional[str] = Field(default=None, max_length=120)
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
