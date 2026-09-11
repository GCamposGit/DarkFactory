"""Contracts for the isolated HF-02 runtime comparison laboratory.

Importing this package is intentionally dependency-light: optional DBOS and
PostgreSQL adapters are not imported during common-suite collection.
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
