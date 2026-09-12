"""
Core Telemetry Engine for DarkFac.
Provides structured, real-time observability for all AI model executions.
"""

from core.telemetry.models import (
    TelemetryRecord,
    TelemetryRecordCreate,
    TelemetryFilters,
    TelemetryStats,
    TelemetryQueryResult,
)
from core.telemetry.store import TelemetryStore
from core.telemetry.hardware import get_hardware_context, HardwareContext

__all__ = [
    "TelemetryRecord",
    "TelemetryRecordCreate",
    "TelemetryFilters",
    "TelemetryStats",
    "TelemetryQueryResult",
    "TelemetryStore",
    "get_hardware_context",
    "HardwareContext",
]
