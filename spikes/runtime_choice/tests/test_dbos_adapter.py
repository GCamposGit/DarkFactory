"""Tests for HF-02-05 isolated DBOS adapter."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from spikes.runtime_choice.contracts import (
    DriverAction,
    DriverCommand,
    DriverEventKind,
    LabConfig,
    RuntimeKind,
    RuntimeStatus,
    WorkflowVersion,
)
from spikes.runtime_choice.dbos_adapter import DBOSAdapter, DBOSAdapterError
from spikes.runtime_choice.driver import run_jsonl


def make_dbos_config(tmp_path: Path, *, db_env: str = "DARKFAC_HF02_DATABASE_URL") -> LabConfig:
    return LabConfig(
        lab_id="dbos-test",
        root_dir=tmp_path,
        runtime=RuntimeKind.DBOS_POSTGRES,
        runtime_version="dbos-2.31.1",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_dbostest",
        database_url_env=db_env,
        effect_base_url="http://127.0.0.1:18402",
        lease_seconds=2,
    )


def test_dbos_adapter_rejects_non_dbos_runtime(tmp_path: Path) -> None:
    config = LabConfig(
        lab_id="native-cfg",
        root_dir=tmp_path,
        runtime=RuntimeKind.NATIVE_SQLITE,
        runtime_version="native-core",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_native",
        effect_base_url="http://127.0.0.1:18402",
    )
    with pytest.raises(ValueError, match="DBOSAdapter requires runtime=dbos_postgres"):
        DBOSAdapter(config)


def test_dbos_adapter_fails_cleanly_when_database_url_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    config = make_dbos_config(tmp_path)
    with pytest.raises(DBOSAdapterError) as exc:
        DBOSAdapter(config)
    assert exc.value.code == "STORE_UNAVAILABLE"


def test_dbos_adapter_fails_with_adapter_unavailable_when_dbos_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://user:secret@localhost:5432/darkfac_hf02_dbostest")
    config = make_dbos_config(tmp_path)
    with pytest.raises(DBOSAdapterError) as exc:
        DBOSAdapter(config)
    assert exc.value.code in {"DBOS_ADAPTER_UNAVAILABLE", "STORE_UNAVAILABLE"}


def test_driver_emits_clean_store_unavailable_for_dbos_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    config = make_dbos_config(tmp_path)
    config_path = tmp_path / "lab-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    from spikes.runtime_choice.driver import DriverConfigurationError, DriverSession

    with pytest.raises(DriverConfigurationError) as exc:
        DriverSession(config)
    assert exc.value.code == "STORE_UNAVAILABLE"

    # Also test CLI invocation over subprocess
    import subprocess
    import sys
    res = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert res.returncode == 2
    assert res.stderr.strip() == "STORE_UNAVAILABLE"
