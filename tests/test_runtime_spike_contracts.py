"""Public-contract tests for HF-02-02.

The suite intentionally imports no DBOS/PostgreSQL module.  It exercises both
Python construction and the actual JSON transport route used by a future
driver.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from spikes.runtime_choice.contracts import (
    AdapterCapabilities,
    CliExitCode,
    DriverAction,
    DriverCommand,
    LabConfig,
    RuntimeComparison,
    RuntimeKind,
    ScenarioResult,
    ScenarioSpec,
    ValidationMode,
    WorkflowVersion,
)


def make_config(
    tmp_path: Path,
    *,
    runtime: RuntimeKind = RuntimeKind.NATIVE_SQLITE,
    runtime_version: str = "native-core",
) -> LabConfig:
    return LabConfig(
        lab_id="contract-test",
        root_dir=tmp_path,
        runtime=runtime,
        runtime_version=runtime_version,
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_contract",
        database_url_env=("DARKFAC_HF02_DATABASE_URL" if runtime is RuntimeKind.DBOS_POSTGRES else None),
        effect_base_url="http://127.0.0.1:18402",
    )


def make_result(tmp_path: Path, *, repeat_index: int = 1, mode: str = "real_lab") -> ScenarioResult:
    return ScenarioResult(
        lab_id="contract-test",
        scenario_id="R01",
        runtime=RuntimeKind.NATIVE_SQLITE,
        repeat_index=repeat_index,
        status="pass",
        environment_ref="environment.json",
        validation_mode=mode,
        target_differences=[],
        assertions={"terminal": True},
        duration_ms=1.5,
        effect_count=1,
        actual_step_invocations=4,
        artifact_refs=["results/R01.json"],
    )


def test_lab_config_is_strict_and_serializes_no_dsn(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert config.model_dump()["root_dir"] == tmp_path.resolve()
    serialized = config.model_dump_json()
    assert "postgresql://" not in serialized
    assert "password=" not in serialized
    assert config.database_url_env is None

    with pytest.raises(ValidationError):
        LabConfig(**{**config.model_dump(), "database_url_env": "postgresql://user:secret@host/db"})


def test_lab_config_json_round_trip_preserves_exact_object(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    restored = LabConfig.model_validate_json(config.model_dump_json())

    assert restored == config
    assert restored.root_dir == tmp_path.resolve()
    assert LabConfig(**{**config.model_dump(), "root_dir": str(tmp_path)}).root_dir == tmp_path.resolve()

    json_payload = json.loads(config.model_dump_json())
    json_payload["lease_seconds"] = True
    with pytest.raises(ValidationError):
        LabConfig.model_validate_json(json.dumps(json_payload))

    json_payload = json.loads(config.model_dump_json())
    json_payload["unexpected"] = True
    with pytest.raises(ValidationError):
        LabConfig.model_validate_json(json.dumps(json_payload))


def test_root_enum_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_config(tmp_path / "missing")
    with pytest.raises(ValidationError):
        make_config(tmp_path, runtime="unsupported")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LabConfig(**{**make_config(tmp_path).model_dump(), "unexpected": True})


def test_commands_enforce_action_fields_and_closed_enums() -> None:
    command = DriverCommand(
        command_id="command-1",
        action=DriverAction.START,
        workflow_id="workflow-1",
        scenario_id="R01",
        workflow_version=WorkflowVersion.V1,
        payload={"approval_pre_resolved": True},
    )
    assert command.action is DriverAction.START

    with pytest.raises(ValidationError):
        DriverCommand(command_id="command-2", action="start", workflow_id="workflow-1")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DriverCommand(command_id="command-3", action="approve")
    with pytest.raises(ValidationError):
        DriverCommand(command_id="command-4", action="shutdown", workflow_id="workflow-1")


def test_scenario_ids_results_and_comparison_are_closed(tmp_path: Path) -> None:
    scenario = ScenarioSpec(
        scenario_id="R01",
        required=True,
        capability="durable_steps",
        input_payload={"value": 1},
        expected_terminal="succeeded",
        expected_effect_count=1,
        expected_step_invocations=4,
    )
    result = make_result(tmp_path)
    comparison = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="environment.json",
        code_sha="local",
        results=[result],
        capability_matrix={"native_sqlite": AdapterCapabilities(durable_steps=True)},
        eligibility={"native_sqlite": False},
        operational_metrics={"rss_peak_mib": None},
    )

    assert scenario.scenario_id == "R01"
    assert comparison.decision_status.value == "pending_architect_review"
    assert CliExitCode.ENVIRONMENT_BLOCKED == 2
    with pytest.raises(ValidationError):
        ScenarioSpec(
            scenario_id="R99",
            required=True,
            capability="durable_steps",
            expected_terminal="succeeded",
            expected_effect_count=0,
            expected_step_invocations=0,
        )


@pytest.mark.parametrize("mode", ["real_lab", "target_environment", "mock_only"])
def test_result_origin_modes_round_trip(tmp_path: Path, mode: str) -> None:
    result = make_result(tmp_path, mode=mode)
    restored = ScenarioResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.validation_mode is ValidationMode(mode)


def test_result_requires_origin_and_rejects_unknown_mode(tmp_path: Path) -> None:
    data = make_result(tmp_path).model_dump()
    data.pop("validation_mode")
    with pytest.raises(ValidationError):
        ScenarioResult.model_validate(data)

    with pytest.raises(ValidationError):
        make_result(tmp_path, mode="simulation")


def test_secret_fields_and_unsanitized_references_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_config(tmp_path, runtime_version="postgresql://u:p@host/db")
    with pytest.raises(ValidationError):
        ScenarioResult.model_validate(
            {**make_result(tmp_path).model_dump(), "environment_ref": "https://u:p@example.invalid/env"}
        )
    with pytest.raises(ValidationError):
        ScenarioSpec(
            scenario_id="R01",
            required=True,
            capability="durable_steps",
            input_payload={"password": "synthetic-secret"},
            expected_terminal="succeeded",
            expected_effect_count=0,
            expected_step_invocations=0,
        )


def test_duplicate_result_identity_is_rejected(tmp_path: Path) -> None:
    result = make_result(tmp_path)
    with pytest.raises(ValidationError):
        RuntimeComparison(
            baseline_snapshot_hash="sha256:test",
            environment_ref="environment.json",
            code_sha="local",
            results=[result, result],
        )


def test_comparison_preserves_mixed_result_origins(tmp_path: Path) -> None:
    first = make_result(tmp_path)
    second = ScenarioResult.model_validate(
        {
            **first.model_dump(),
            "scenario_id": "R02",
            "environment_ref": "target-environment.json",
            "validation_mode": "mock_only",
        }
    )
    comparison = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="comparison-manifest.json",
        code_sha="local",
        results=[first, second],
    )
    assert {result.environment_ref for result in comparison.results} == {
        "environment.json",
        "target-environment.json",
    }


def test_unsupported_and_blocked_are_not_pass(tmp_path: Path) -> None:
    for status in ("unsupported", "blocked"):
        result = ScenarioResult(
            **{
                **make_result(tmp_path).model_dump(),
                "status": status,
                "target_differences": ["capability not provided"],
            }
        )
        assert result.status.value == status
        assert result.status.value != "pass"


def test_comparison_aggregates_differences_and_operational_evidence(tmp_path: Path) -> None:
    mock_res = make_result(tmp_path, mode="mock_only")
    target_res = ScenarioResult.model_validate(
        {
            **make_result(tmp_path).model_dump(),
            "scenario_id": "R02",
            "validation_mode": "target_environment",
            "target_differences": ["diff-network"],
        }
    )
    mixed = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="comparison-manifest.json",
        code_sha="local",
        results=[mock_res, target_res],
    )
    assert mixed.all_target_differences == ["diff-network"]
    assert mixed.has_operational_evidence is False

    target_only = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="comparison-manifest.json",
        code_sha="local",
        results=[target_res],
    )
    assert target_only.has_operational_evidence is True
