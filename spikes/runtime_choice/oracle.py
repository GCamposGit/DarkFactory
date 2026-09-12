"""Independent oracle for evaluating scenario execution outcomes in HF-02.

Consults the external effect store and observation history directly.  Never
trusts an adapter or driver boolean without independent corroborating evidence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from spikes.runtime_choice.contracts import (
    DriverEventKind,
    FaultPoint,
    ResultStatus,
    RuntimeStatus,
    ScenarioResult,
    ScenarioSpec,
    TerminalExpectation,
    ValidationMode,
)
from spikes.runtime_choice.controller import ExecutionTrace
from spikes.runtime_choice.effect_store import NativeEffectStore

LOGGER = logging.getLogger(__name__)


class ScenarioOracle:
    """Verifies scenario results using independent state from NativeEffectStore."""

    def __init__(self, store: NativeEffectStore) -> None:
        self.store = store

    def evaluate(
        self,
        trace: ExecutionTrace,
        spec: ScenarioSpec,
        *,
        environment_ref: str = "environment.json",
        validation_mode: ValidationMode = ValidationMode.REAL_LAB,
        target_differences: list[str] | None = None,
    ) -> ScenarioResult:
        """Evaluate an execution trace against the frozen ScenarioSpec."""
        assertions: dict[str, bool] = {}
        target_diffs = list(target_differences or [])

        # 1. Independent external effect verification
        actual_effect_count = self.store.effect_count(workflow_id=trace.workflow_id)
        assertions["effect_count_matches_spec"] = actual_effect_count == spec.expected_effect_count

        # 2. Independent step observation verification
        actual_observations = self.store.observations(trace.workflow_id)
        actual_step_invocations = sum(1 for obs in actual_observations if obs.kind == "step_started")
        if spec.expected_step_invocations > 0:
            assertions["step_invocations_matches_spec"] = actual_step_invocations == spec.expected_step_invocations

        # 3. Terminal status evaluation
        last_event = trace.events[-1] if trace.events else None
        last_kind = last_event.kind if last_event else None
        last_runtime_status = last_event.runtime_status if last_event else None

        # Check for unsupported capabilities first
        is_unsupported = any(
            event.kind is DriverEventKind.UNSUPPORTED or event.runtime_status is RuntimeStatus.UNSUPPORTED
            for event in trace.events
        )

        # Check for lying driver: driver claims succeeded, but effect count does not match expected
        if last_runtime_status is RuntimeStatus.SUCCEEDED and not assertions["effect_count_matches_spec"]:
            assertions["driver_truthfulness"] = False
        else:
            assertions["driver_truthfulness"] = True

        # Check distinct process PIDs for crash recovery (R02)
        if spec.fault_point is FaultPoint.CRASH_AFTER_CHECKPOINT:
            assertions["distinct_process_restart"] = (
                len(trace.process_pids) >= 2 and len(set(trace.process_pids)) >= 2
            )
            assertions["recovery_duration_measured"] = (
                trace.recovery_ms is not None and trace.recovery_ms > 0
            )

        # Check expected block reason
        if spec.expected_block_reason is not None:
            reason_observed = False
            expected_norm = spec.expected_block_reason.lower().replace("-", "_")
            candidates = {expected_norm, expected_norm.replace("storage", "store"), expected_norm.replace("store", "storage")}
            for candidate in candidates:
                for event in trace.events:
                    if event.code and candidate in event.code.lower().replace("-", "_"):
                        reason_observed = True
                        break
                if trace.error_code and candidate in trace.error_code.lower().replace("-", "_"):
                    reason_observed = True
                    break
            assertions["expected_block_reason_verified"] = reason_observed

        # Expected terminal checks
        if spec.expected_terminal is TerminalExpectation.SUCCEEDED:
            assertions["terminal_matches"] = (
                last_runtime_status is RuntimeStatus.SUCCEEDED
                and last_kind is DriverEventKind.COMPLETED
            )
        elif spec.expected_terminal is TerminalExpectation.CANCELLED:
            assertions["terminal_matches"] = (
                last_runtime_status is RuntimeStatus.CANCELLED
                or last_kind is DriverEventKind.CANCELLED
            )
        elif spec.expected_terminal is TerminalExpectation.ERROR:
            assertions["terminal_matches"] = (
                last_runtime_status is RuntimeStatus.ERROR
                or last_kind is DriverEventKind.ERROR
                or trace.error_code is not None
            )
        elif spec.expected_terminal is TerminalExpectation.WAITING:
            assertions["terminal_matches"] = (
                last_runtime_status is RuntimeStatus.WAITING
                or last_kind is DriverEventKind.WAITING
            )
        else:
            assertions["terminal_matches"] = True

        # Determine final status
        if is_unsupported:
            status = ResultStatus.UNSUPPORTED
        elif not assertions.get("driver_truthfulness", True):
            status = ResultStatus.FAIL
        elif all(assertions.values()):
            status = ResultStatus.PASS
        else:
            status = ResultStatus.FAIL

        # Determine error code
        result_error_code = trace.error_code
        if not result_error_code and last_event and last_event.code:
            result_error_code = last_event.code

        return ScenarioResult(
            lab_id=trace.lab_id,
            scenario_id=spec.scenario_id,
            runtime=trace.runtime,
            repeat_index=trace.repeat_index,
            status=status,
            environment_ref=environment_ref,
            validation_mode=validation_mode,
            target_differences=target_diffs,
            assertions=assertions,
            duration_ms=trace.duration_ms,
            recovery_ms=trace.recovery_ms,
            rss_peak_mib=trace.rss_peak_mib,
            effect_count=actual_effect_count,
            actual_step_invocations=actual_step_invocations,
            artifact_refs=trace.artifact_refs,
            error_code=result_error_code,
        )


__all__ = ["ScenarioOracle"]
