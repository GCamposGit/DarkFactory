"""Data models and contracts for Dark Factory Notification and Alert System.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 5 & 10)
- HYBRID_AUTONOMY_REQUIREMENTS (Scenarios G1, G6, G8)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AlertSeverity(str, Enum):
    """Severity levels for operational alerts."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCategory(str, Enum):
    """Categorization of operational alerts."""

    TOKEN_QUOTA = "token_quota"
    EXECUTION_BUDGET = "execution_budget"
    SYSTEM_HEALTH = "system_health"
    WORKFLOW_GATE = "workflow_gate"


class NotificationChannel(str, Enum):
    """Delivery channels supported by the notification system."""

    TELEGRAM = "telegram"
    DARKHUB = "darkhub"
    ALL = "all"


class NotificationEvent(BaseModel):
    """Typed immutable record of an operational notification/alert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notification_id: str = Field(min_length=1)
    category: AlertCategory
    severity: AlertSeverity
    title: str = Field(min_length=1)
    message: str = Field(min_length=1)
    provider_id: Optional[str] = None
    remaining_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    delivered_channels: List[str] = Field(default_factory=list)
    acknowledged: bool = False
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TokenAlertThresholds(BaseModel):
    """Configurable thresholds for token and quota warnings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_threshold_percent: float = Field(default=25.0, ge=1.0, le=99.0)
    critical_threshold_percent: float = Field(default=10.0, ge=0.1, le=50.0)
    cooldown_minutes: int = Field(default=30, ge=1, le=1440)
    edge_triggered: bool = Field(default=True, description="Notify once on threshold crossing (25% and 10%)")

