"""Contracts and adapters for the isolated HF-02 runtime comparison lab.

Importing this package deliberately has no DBOS or PostgreSQL side effects.
"""

from spikes.runtime_choice.contracts import (
    AdapterCapabilities,
    CliExitCode,
    DecisionStatus,
    DriverAction,
    DriverCommand,
    DriverEvent,
    DriverEventKind,
    FaultPoint,
    LabConfig,
    ResultStatus,
    RuntimeComparison,
    RuntimeKind,
    RuntimeStatus,
    ScenarioCapability,
    ScenarioResult,
    ScenarioSpec,
    TerminalExpectation,
    ValidationMode,
    WorkflowVersion,
)

__all__ = [
    "AdapterCapabilities",
    "CliExitCode",
    "DecisionStatus",
    "DriverAction",
    "DriverCommand",
    "DriverEvent",
    "DriverEventKind",
    "FaultPoint",
    "LabConfig",
    "ResultStatus",
    "RuntimeComparison",
    "RuntimeKind",
    "RuntimeStatus",
    "ScenarioCapability",
    "ScenarioResult",
    "ScenarioSpec",
    "TerminalExpectation",
    "ValidationMode",
    "WorkflowVersion",
]
