"""Project CLI registration preserves executable project contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.projects import cli
from core.projects.models import ProjectDescriptor
from core.projects.registry import ProjectRegistry


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectRegistry:
    result = ProjectRegistry(tmp_path / "projects.json")
    monkeypatch.setattr(cli, "get_project_registry", lambda: result)
    return result


def _configured_project(tmp_path: Path) -> ProjectDescriptor:
    return ProjectDescriptor(
        id="pilot", name="Pilot", path=str(tmp_path), prefix="PIL", description="Keep me",
        repo_url="https://example.invalid/pilot.git", default_branch="trunk",
        commands={"setup": ["install"], "validate": ["test"], "build": ["build"], "smoke": ["smoke"]},
        deploy={"type": "dokploy", "params": {"application_id": "pilot", "token_ref": "TOKEN_ENV"}},
        smoke=[{"url": "https://example.invalid/health", "expect_text": "ready"}],
    )


def test_legacy_registration_update_preserves_contracts(registry: ProjectRegistry, tmp_path: Path) -> None:
    original = _configured_project(tmp_path)
    registry.register_project(original)
    assert cli.main(["register", "--id", "pilot", "--name", "Renamed", "--path", str(tmp_path),
                     "--kind", "client_portfolio"]) == 0
    expected = original.model_dump()
    expected["name"] = "Renamed"
    assert registry.get_project("pilot").model_dump() == expected


def test_descriptor_import_export_uses_validate_alias(
    registry: ProjectRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = _configured_project(tmp_path)
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(original.model_dump_json(by_alias=True), encoding="utf-8")
    assert cli.main(["register", "--descriptor-file", str(descriptor)]) == 0
    assert ProjectRegistry(registry.projects_file).get_project("pilot") == original
    capsys.readouterr()
    assert cli.main(["get", "--id", "pilot", "--json"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported == original.model_dump(mode="json", by_alias=True)
    assert "validate" in exported["commands"]
    assert "validate_cmds" not in exported["commands"]
    descriptor.write_text(json.dumps(exported), encoding="utf-8")
    assert cli.main(["register", "--descriptor-file", str(descriptor)]) == 0
    capsys.readouterr()
    assert cli.main(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert next(project for project in listed if project["id"] == "pilot") == exported


def test_partial_descriptor_merges_nested_fields_and_explicit_flags_win(
    registry: ProjectRegistry, tmp_path: Path
) -> None:
    registry.register_project(_configured_project(tmp_path))
    descriptor = tmp_path / "partial.json"
    descriptor.write_text(json.dumps({
        "id": "pilot", "name": "From JSON", "commands": {"validate": ["new-test"]},
        "deploy": {"params": {"application_id": "new-app"}},
    }), encoding="utf-8")
    assert cli.main(["register", "--id", "pilot", "--descriptor-file", str(descriptor),
                     "--name", "From CLI", "--prefix", "new"]) == 0
    project = registry.get_project("pilot")
    assert project.name == "From CLI"
    assert project.prefix == "NEW"
    assert project.description == "Keep me"
    assert project.default_branch == "trunk"
    assert project.commands.validate_cmds == ["new-test"]
    assert project.commands.setup == ["install"]
    assert project.commands.build == ["build"]
    assert project.commands.smoke == ["smoke"]
    assert project.deploy.params == {"application_id": "new-app", "token_ref": "TOKEN_ENV"}
    assert project.smoke[0].expect_text == "ready"


@pytest.mark.parametrize("payload", [
    "not-json-private-value", "[]", '{"id":"different","name":"private-value"}',
    '{"id":"pilot","deploy":{"type":"private-value"}}',
    '{"id":"pilot","commands":{"validate":42}}',
    '{"id":"pilot","commands":{"validate":[],"validate_cmds":[]}}',
    '{"id":["pilot"]}',
])
def test_invalid_descriptor_does_not_mutate_or_echo_input(
    registry: ProjectRegistry, tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    original = _configured_project(tmp_path)
    registry.register_project(original)
    before = registry.projects_file.read_bytes()
    descriptor = tmp_path / "invalid.json"
    descriptor.write_text(payload, encoding="utf-8")
    assert cli.main(["register", "--id", "pilot", "--descriptor-file", str(descriptor)]) == 2
    captured = capsys.readouterr()
    assert "private-value" not in captured.out + captured.err
    assert registry.projects_file.read_bytes() == before
    assert registry.get_project("pilot") == original


def test_new_legacy_registration_keeps_legacy_defaults(registry: ProjectRegistry, tmp_path: Path) -> None:
    assert cli.main(["register", "--id", "pilot", "--name", "Pilot", "--path", str(tmp_path),
                     "--kind", "client_portfolio"]) == 0
    project = registry.get_project("pilot")
    assert project.prefix == "USR"
    assert project.description == ""
    assert project.default_branch == "main"
    assert project.commands.validate_cmds == []


def test_legacy_validate_alias_and_explicit_clears(registry: ProjectRegistry, tmp_path: Path) -> None:
    registry.register_project(_configured_project(tmp_path))
    descriptor = tmp_path / "partial.json"
    descriptor.write_text(json.dumps({
        "id": "pilot", "commands": {"validate_cmds": ["replacement"]},
        "deploy": None, "smoke": [],
    }), encoding="utf-8")
    assert cli.main(["register", "--descriptor-file", str(descriptor)]) == 0
    project = registry.get_project("pilot")
    assert project.commands.validate_cmds == ["replacement"]
    assert project.commands.setup == ["install"]
    assert project.deploy is None
    assert project.smoke == []


def test_missing_descriptor_file_does_not_create_registry(
    registry: ProjectRegistry, tmp_path: Path
) -> None:
    assert cli.main(["register", "--descriptor-file", str(tmp_path / "absent.json")]) == 2
    assert not registry.projects_file.exists()
