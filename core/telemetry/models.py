"""
Pydantic v2 typed models and contracts for AI model telemetry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


def utc_now_iso() -> str:
    """Return an RFC 3339 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


class ExecutionMode(str, Enum):
    UI = "ui"
    HEADLESS = "headless"


class TelemetryRecordCreate(BaseModel):
    """Payload to record a new AI model execution."""

    id: Optional[str] = None
    timestamp: Optional[str] = None
    ticket_id: Optional[str] = Field(default=None, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    tier: str = Field(default="local", max_length=50)
    harness: str = Field(default="core.execution", max_length=80)
    execution_mode: ExecutionMode = ExecutionMode.HEADLESS
    device_name: Optional[str] = Field(default=None, max_length=120)
    os_platform: Optional[str] = Field(default=None, max_length=80)
    accelerator: Optional[str] = Field(default=None, max_length=120)
    input_tokens: int = Field(default=0, ge=0)
    processing_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: Optional[int] = Field(default=None, ge=0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    success: bool = True
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def compute_defaults(self) -> TelemetryRecordCreate:
        if not self.id:
            self.id = uuid4().hex
        if not self.timestamp:
            self.timestamp = utc_now_iso()
        if self.total_tokens is None:
            self.total_tokens = self.input_tokens + self.processing_tokens + self.output_tokens
        return self


class TelemetryRecord(BaseModel):
    """Persisted AI model execution record."""

    id: str
    timestamp: str
    ticket_id: Optional[str] = None
    provider: str
    model: str
    tier: str
    harness: str
    execution_mode: ExecutionMode
    device_name: str
    os_platform: str
    accelerator: str
    input_tokens: int
    processing_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    cost_usd: float
    success: bool
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TelemetryFilters(BaseModel):
    """Filters for querying telemetry records."""

    ticket_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    execution_mode: Optional[ExecutionMode] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    success: Optional[bool] = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class BreakdownItem(BaseModel):
    name: str
    count: int
    total_tokens: int
    total_cost_usd: float


class TelemetryStats(BaseModel):
    """Aggregated analytical statistics across telemetry runs."""

    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    success_rate_percent: float = 0.0
    total_input_tokens: int = 0
    total_processing_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    by_model: List[BreakdownItem] = Field(default_factory=list)
    by_provider: List[BreakdownItem] = Field(default_factory=list)
    by_ticket: List[BreakdownItem] = Field(default_factory=list)
    by_execution_mode: List[BreakdownItem] = Field(default_factory=list)
    by_hardware: List[BreakdownItem] = Field(default_factory=list)


class TelemetryQueryResult(BaseModel):
    """Paginated result set for telemetry runs query."""

    runs: List[TelemetryRecord]
    total_count: int
    limit: int
    offset: int
