"""Contract-level tests for HF-02-02.

These tests deliberately use only the project dependencies; DBOS is not
imported while the common laboratory contracts are collected.
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


def make_config(tmp_path: Path, *, runtime: RuntimeKind = RuntimeKind.NATIVE_SQLITE) -> LabConfig:
    return LabConfig(
        lab_id="contract-test",
        root_dir=tmp_path,
        runtime=runtime,
        runtime_version="native-core",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_contract",
        database_url_env=("DARKFAC_HF02_DATABASE_URL" if runtime is RuntimeKind.DBOS_POSTGRES else None),
        effect_base_url="http://127.0.0.1:18402",
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

    for invalid_root in (True, {"path": str(tmp_path)}, None):
        json_payload = json.loads(config.model_dump_json())
        json_payload["root_dir"] = invalid_root
        with pytest.raises(ValidationError):
            LabConfig.model_validate_json(json.dumps(json_payload))


def test_root_enum_and_unknown_fields_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_config(tmp_path / "missing")
    with pytest.raises(ValidationError):
        make_config(tmp_path, runtime="unsupported")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LabConfig(
            **{
                **make_config(tmp_path).model_dump(),
                "unexpected": True,
            }
        )


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
    result = ScenarioResult(
        lab_id="contract-test",
        scenario_id=scenario.scenario_id,
        runtime=RuntimeKind.NATIVE_SQLITE,
        repeat_index=1,
        status="pass",
        environment_ref="HF-02-01",
        validation_mode=ValidationMode.REAL_LAB,
        target_differences=[],
        assertions={"terminal": True},
        duration_ms=1.5,
        effect_count=1,
        actual_step_invocations=4,
    )
    comparison = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="HF-02-01",
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


def test_scenario_result_validation_mode_roundtrip_and_rejections() -> None:
    base_data = {
        "lab_id": "lab-1",
        "scenario_id": "R02",
        "runtime": RuntimeKind.NATIVE_SQLITE,
        "repeat_index": 1,
        "status": "pass",
        "assertions": {"step": True},
        "duration_ms": 10.0,
        "effect_count": 1,
        "actual_step_invocations": 2,
    }

    # 1. Roundtrip de cada modo suportado
    for mode in (ValidationMode.REAL_LAB, ValidationMode.TARGET_ENVIRONMENT, ValidationMode.MOCK_ONLY):
        res = ScenarioResult(
            **base_data,
            environment_ref="env-lab",
            validation_mode=mode,
            target_differences=["diff-1"],
        )
        assert res.validation_mode is mode
        restored = ScenarioResult.model_validate_json(res.model_dump_json())
        assert restored == res
        assert restored.validation_mode is mode
        assert restored.target_differences == ["diff-1"]

    # 2. Rejeição de modo desconhecido/inválido
    for invalid_mode in ("simulation", "invalid_mode", 123, True):
        with pytest.raises(ValidationError):
            ScenarioResult(**base_data, environment_ref="env-lab", validation_mode=invalid_mode)

    # 3. Dado antigo sem origem não recebe default de alvo (ausência de validation_mode ou environment_ref falha)
    with pytest.raises(ValidationError):
        ScenarioResult(**base_data, environment_ref="env-lab")  # falta validation_mode
    with pytest.raises(ValidationError):
        ScenarioResult(**base_data, validation_mode=ValidationMode.TARGET_ENVIRONMENT)  # falta environment_ref

    # 4. Target differences com string vazia ou em branco é rejeitado
    with pytest.raises(ValidationError):
        ScenarioResult(
            **base_data,
            environment_ref="env-lab",
            validation_mode=ValidationMode.REAL_LAB,
            target_differences=["   "],
        )

    # 5. Agregação em RuntimeComparison preserva target_differences e não promove mock/mixed a evidência operacional
    res_mock = ScenarioResult(
        **base_data,
        environment_ref="env-mock",
        validation_mode=ValidationMode.MOCK_ONLY,
        target_differences=["sqlite_in_memory_instead_of_disk"],
    )
    res_target = ScenarioResult(
        **{
            **base_data,
            "scenario_id": "R03",
            "environment_ref": "env-target",
            "validation_mode": ValidationMode.TARGET_ENVIRONMENT,
            "target_differences": ["network_latency_10ms"],
        }
    )
    comparison_mixed = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="HF-02-01",
        code_sha="local",
        results=[res_mock, res_target],
    )
    assert comparison_mixed.all_target_differences == [
        "sqlite_in_memory_instead_of_disk",
        "network_latency_10ms",
    ]
    assert comparison_mixed.has_operational_evidence is False

    comparison_target_only = RuntimeComparison(
        baseline_snapshot_hash="sha256:test",
        environment_ref="HF-02-01",
        code_sha="local",
        results=[res_target],
    )
    assert comparison_target_only.has_operational_evidence is True
