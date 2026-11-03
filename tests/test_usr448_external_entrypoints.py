"""USR-448 T2: the external-entrypoints registry must catch a dead-code scan before it ships.

Covers: real registry loads and every listed module imports / script file exists; a missing
module or script raises the structured error (not a raw ImportError/json exception); malformed
JSON and a missing required key also raise the structured error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.infra import external_entrypoints as mod

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "external_entrypoints.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _valid_entry(**overrides) -> dict:
    entry = {
        "id": "sample",
        "kind": "module",
        "target": "core.infra.external_entrypoints",
        "trigger_type": "scheduled_task",
        "trigger_name": "Sample-Task",
        "host": "Notebook",
        "installed_by": [],
        "reason": "test fixture",
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------- real registry


def test_real_registry_file_loads_and_validates() -> None:
    registry = mod.load_registry()
    assert registry.version == 1
    assert len(registry.entrypoints) >= 1


def test_real_registry_contains_hub_availability_probe() -> None:
    registry = mod.load_registry()
    ids = {entry.id for entry in registry.entrypoints}
    assert "hub-availability-probe" in ids
    probe = next(e for e in registry.entrypoints if e.id == "hub-availability-probe")
    assert probe.kind == "module"
    assert probe.target == "core.infra.availability_probe"


def test_every_entry_in_the_real_registry_resolves() -> None:
    entries = mod.verify_all()
    assert len(entries) >= 1


@pytest.mark.parametrize("entry_id", [
    "hub-availability-probe",
    "usage-cloud-sync",
    "fleet-dispatcher",
    "onprem-test-worker",
    "onprem-fleet-worker-launcher",
])
def test_known_scheduler_entrypoints_are_registered(entry_id: str) -> None:
    registry = mod.load_registry()
    ids = {entry.id for entry in registry.entrypoints}
    assert entry_id in ids


# --------------------------------------------------------------------------- verify_entrypoint


def test_verify_entrypoint_passes_for_existing_module() -> None:
    entry = mod.ExternalEntrypoint.model_validate(_valid_entry(kind="module", target="core.infra.availability_probe"))
    mod.verify_entrypoint(entry)


def test_verify_entrypoint_passes_for_existing_script() -> None:
    entry = mod.ExternalEntrypoint.model_validate(
        _valid_entry(kind="script", target="scripts/sync_usage_to_cloud.py")
    )
    mod.verify_entrypoint(entry)


def test_verify_entrypoint_raises_structured_error_for_missing_module() -> None:
    entry = mod.ExternalEntrypoint.model_validate(_valid_entry(kind="module", target="core.infra.this_module_does_not_exist"))
    with pytest.raises(mod.ExternalEntrypointsRegistryError, match="this_module_does_not_exist"):
        mod.verify_entrypoint(entry)


def test_verify_entrypoint_raises_structured_error_for_missing_script() -> None:
    entry = mod.ExternalEntrypoint.model_validate(_valid_entry(kind="script", target="scripts/this_script_does_not_exist.py"))
    with pytest.raises(mod.ExternalEntrypointsRegistryError, match="this_script_does_not_exist"):
        mod.verify_entrypoint(entry)


# --------------------------------------------------------------------------- load_registry: malformed input


def test_load_registry_missing_file_raises_structured_error(tmp_path: Path) -> None:
    with pytest.raises(mod.ExternalEntrypointsRegistryError):
        mod.load_registry(tmp_path / "missing.json")


def test_load_registry_broken_json_raises_structured_error(tmp_path: Path) -> None:
    path = tmp_path / "external_entrypoints.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(mod.ExternalEntrypointsRegistryError):
        mod.load_registry(path)


def test_load_registry_missing_required_key_raises_structured_error(tmp_path: Path) -> None:
    entry = _valid_entry()
    del entry["target"]
    path = _write(tmp_path, {"version": 1, "entrypoints": [entry]})
    with pytest.raises(mod.ExternalEntrypointsRegistryError):
        mod.load_registry(path)


def test_load_registry_unknown_kind_raises_structured_error(tmp_path: Path) -> None:
    path = _write(tmp_path, {"version": 1, "entrypoints": [_valid_entry(kind="binary")]})
    with pytest.raises(mod.ExternalEntrypointsRegistryError):
        mod.load_registry(path)


# --------------------------------------------------------------------------- verify_all: end-to-end failure


def test_verify_all_fails_when_a_registered_module_disappears(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"version": 1, "entrypoints": [_valid_entry(id="gone", kind="module", target="core.infra.not_a_real_module")]},
    )
    with pytest.raises(mod.ExternalEntrypointsRegistryError, match="gone"):
        mod.verify_all(path)


def test_verify_all_fails_when_a_registered_script_disappears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(
        tmp_path,
        {"version": 1, "entrypoints": [_valid_entry(id="gone-script", kind="script", target="scripts/nope.py")]},
    )
    with pytest.raises(mod.ExternalEntrypointsRegistryError, match="gone-script"):
        mod.verify_all(path, repo_root=REPO_ROOT)
