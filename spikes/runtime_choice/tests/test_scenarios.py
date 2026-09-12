"""Tests for HF-02-06 scenario controller, oracle, and fault injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spikes.runtime_choice.contracts import (
    DriverEvent,
    DriverEventKind,
    FaultPoint,
    LabConfig,
    ResultStatus,
    RuntimeKind,
    RuntimeStatus,
    ScenarioCapability,
    ScenarioSpec,
    TerminalExpectation,
    ValidationMode,
    WorkflowVersion,
)
from spikes.runtime_choice.controller import ExecutionTrace, ScenarioController
from spikes.runtime_choice.effect_server import EffectServer
from spikes.runtime_choice.effect_store import (
    load_scenario_catalog,
)
from spikes.runtime_choice.oracle import ScenarioOracle


def make_lab_config(tmp_path: Path, base_url: str) -> LabConfig:
    return LabConfig(
        lab_id="scen-test",
        root_dir=tmp_path,
        runtime=RuntimeKind.NATIVE_SQLITE,
        runtime_version="native-core",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_scentest",
        effect_base_url=base_url,
        lease_seconds=1.0,
        scenario_timeout_seconds=15.0,
    )


def test_scenario_controller_runs_r01_cleanly(tmp_path: Path) -> None:
    catalog = load_scenario_catalog()
    r01_spec = next(s for s in catalog if s.scenario_id == "R01")

    with EffectServer(tmp_path) as server:
        config = make_lab_config(tmp_path, server.base_url)
        controller = ScenarioController(config, effect_server=server)
        trace = controller.run_scenario(r01_spec)

        oracle = ScenarioOracle(server.store)
        result = oracle.evaluate(trace, r01_spec)

        assert result.status is ResultStatus.PASS
        assert result.effect_count == 1
        assert result.actual_step_invocations == 4
        assert result.assertions["effect_count_matches_spec"] is True
        assert result.assertions["driver_truthfulness"] is True
        assert result.assertions["terminal_matches"] is True
        assert len(trace.process_pids) == 1
        controller.shutdown()


def test_scenario_controller_handles_r02_crash_and_restart(tmp_path: Path) -> None:
    catalog = load_scenario_catalog()
    r02_spec = next(s for s in catalog if s.scenario_id == "R02")

    with EffectServer(tmp_path) as server:
        config = make_lab_config(tmp_path, server.base_url)
        controller = ScenarioController(config, effect_server=server)
        trace = controller.run_scenario(r02_spec)

        oracle = ScenarioOracle(server.store)
        result = oracle.evaluate(trace, r02_spec)

        assert result.status is ResultStatus.PASS
        assert result.effect_count == 1
        assert result.assertions["distinct_process_restart"] is True
        assert len(trace.process_pids) == 2
        assert trace.process_pids[0] != trace.process_pids[1]
        assert result.recovery_ms is not None
        assert result.recovery_ms > 0
        controller.shutdown()


def test_oracle_identifies_lying_driver_without_effects(tmp_path: Path) -> None:
    catalog = load_scenario_catalog()
    r01_spec = next(s for s in catalog if s.scenario_id == "R01")

    with EffectServer(tmp_path) as server:
        fake_events = [
            DriverEvent(
                event_id="ev-1",
                workflow_id="wf-fake",
                kind=DriverEventKind.COMPLETED,
                runtime_status=RuntimeStatus.SUCCEEDED,
            )
        ]
        fake_trace = ExecutionTrace(
            lab_id="scen-test",
            scenario_id="R01",
            runtime=RuntimeKind.NATIVE_SQLITE,
            repeat_index=1,
            workflow_id="wf-fake",
            events=fake_events,
            process_pids=[1234],
            duration_ms=50.0,
        )

        oracle = ScenarioOracle(server.store)
        result = oracle.evaluate(fake_trace, r01_spec)

        assert result.status is ResultStatus.FAIL
        assert result.assertions["effect_count_matches_spec"] is False
        assert result.assertions["driver_truthfulness"] is False


def test_scenario_controller_handles_r09_storage_unavailable(tmp_path: Path) -> None:
    catalog = load_scenario_catalog()
    r09_spec = next(s for s in catalog if s.scenario_id == "R09")

    with EffectServer(tmp_path) as server:
        config = make_lab_config(tmp_path, server.base_url)
        controller = ScenarioController(config, effect_server=server)
        trace = controller.run_scenario(r09_spec)

        oracle = ScenarioOracle(server.store)
        result = oracle.evaluate(trace, r09_spec)

        assert result.status is ResultStatus.PASS
        assert result.effect_count == 0
        assert result.assertions["terminal_matches"] is True
        assert result.assertions["expected_block_reason_verified"] is True
        controller.shutdown()
