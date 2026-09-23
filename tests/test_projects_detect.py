"""Tests for offline project command autodetection and resolution (HF-27-01)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.projects.detect import detect_commands
from core.projects.models import (
    DeployConfig,
    ProjectCommands,
    ProjectDescriptor,
    ProjectKind,
    SmokeCheck,
)
from core.projects.registry import normalize_repo_url, resolve_commands


def test_empty_repo_returns_empty_commands(tmp_path: Path) -> None:
    commands = detect_commands(tmp_path)
    assert commands == ProjectCommands()
    assert commands.setup == []
    assert commands.validate_cmds == []
    assert commands.build == []
    assert commands.smoke == []


def test_node_repo_with_npm_detects_ci_test_and_build(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "vitest run", "build": "vite build"}}),
        encoding="utf-8",
    )

    commands = detect_commands(tmp_path)

    assert commands.setup == ["npm ci"]
    assert commands.validate_cmds == ["npm test"]
    assert commands.build == ["npm run build"]


def test_node_repo_without_test_script_leaves_validate_empty(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"build": "vite build"}}), encoding="utf-8"
    )

    commands = detect_commands(tmp_path)

    assert commands.setup == ["npm ci"]
    assert commands.validate_cmds == []
    assert commands.build == ["npm run build"]


def test_node_repo_with_pnpm_lockfile_uses_pnpm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '6.0'\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["pnpm install --frozen-lockfile"]
    assert commands.validate_cmds == ["pnpm test"]
    assert commands.build == []


def test_node_repo_with_yarn_lockfile_uses_yarn(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": {"test": "jest", "build": "webpack"}}), encoding="utf-8"
    )
    (tmp_path / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["yarn install --frozen-lockfile"]
    assert commands.validate_cmds == ["yarn test"]
    assert commands.build == ["yarn run build"]


def test_python_repo_with_requirements_and_tests_dir(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["python -m pip install -r requirements.txt"]
    assert commands.validate_cmds == ["python -m pytest -q"]
    assert commands.build == []


def test_python_repo_without_tests_leaves_validate_empty(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["python -m pip install -r requirements.txt"]
    assert commands.validate_cmds == []


def test_python_repo_with_pyproject_only(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "test_root.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["python -m pip install -e ."]
    assert commands.validate_cmds == ["python -m pytest -q"]


def test_harness_config_present_uses_quick_validate(tmp_path: Path) -> None:
    (tmp_path / "harness.config.json").write_text("{}", encoding="utf-8")
    # Even with a package.json present, harness.config.json takes precedence.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}), encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == []
    assert commands.validate_cmds == ["python core/harness/runner.py --quick"]
    assert commands.build == []


def test_harness_config_with_requirements_txt_includes_pip_install_setup(tmp_path: Path) -> None:
    (tmp_path / "harness.config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pydantic\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["python -m pip install -r requirements.txt"]
    assert commands.validate_cmds == ["python core/harness/runner.py --quick"]
    assert commands.build == []


def test_harness_config_with_pyproject_only_includes_pip_install_editable(tmp_path: Path) -> None:
    (tmp_path / "harness.config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == ["python -m pip install -e ."]
    assert commands.validate_cmds == ["python core/harness/runner.py --quick"]


def test_harness_config_without_python_manifest_leaves_setup_empty(tmp_path: Path) -> None:
    (tmp_path / "harness.config.json").write_text("{}", encoding="utf-8")

    commands = detect_commands(tmp_path)

    assert commands.setup == []
    assert commands.validate_cmds == ["python core/harness/runner.py --quick"]


def test_resolve_commands_prefers_explicit_over_detected(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}), encoding="utf-8"
    )
    project = ProjectDescriptor(
        id="demo",
        name="Demo Project",
        commands=ProjectCommands(setup=["custom setup"], validate=["custom validate"]),
    )

    resolved = resolve_commands(project, tmp_path)

    # Explicit fields win.
    assert resolved.setup == ["custom setup"]
    assert resolved.validate_cmds == ["custom validate"]
    # Fields left empty fall back to autodetection.
    assert resolved.build == ["npm run build"]
    assert resolved.smoke == []


def test_resolve_commands_falls_back_fully_when_nothing_explicit(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    project = ProjectDescriptor(id="demo", name="Demo Project")

    resolved = resolve_commands(project, tmp_path)

    assert resolved.setup == ["python -m pip install -r requirements.txt"]
    assert resolved.validate_cmds == ["python -m pytest -q"]


def test_project_descriptor_backward_compatible_without_new_fields() -> None:
    legacy_payload = {
        "id": "legacy",
        "name": "Legacy Project",
        "path": "C:\\dev\\Legacy",
        "kind": "internal_product",
        "prefix": "LEG",
        "domain": "legacy.local",
        "deploy_target": "local_service",
    }

    project = ProjectDescriptor.model_validate(legacy_payload)

    assert project.repo_url is None
    assert project.default_branch == "main"
    assert project.commands == ProjectCommands()
    assert project.deploy is None
    assert project.smoke == []
    assert project.exec_affinity == []
    assert project.requires_commercial_acceptance is False


def test_deploy_config_rejects_inline_secret_values() -> None:
    with pytest.raises(Exception):
        DeployConfig(type="dokploy", params={"api_token": "sk-live-123"})


def test_deploy_config_accepts_secret_reference_keys() -> None:
    config = DeployConfig(type="dokploy", params={"api_token_ref": "DOKPLOY_API_TOKEN"})
    assert config.params == {"api_token_ref": "DOKPLOY_API_TOKEN"}


def test_deploy_config_allows_non_secret_keys() -> None:
    config = DeployConfig(type="hostinger_ftp", params={"host": "ftp.example.com", "port": "21"})
    assert config.params["host"] == "ftp.example.com"


def test_smoke_check_defaults() -> None:
    check = SmokeCheck(url="https://example.com/")
    assert check.expect_status == 200
    assert check.expect_text is None


def test_real_registry_projects_json_loads_with_new_fields() -> None:
    """The 4 real projects in .factory/projects.json must carry repo_url,
    deploy and smoke (or a documented reason to leave them null/empty), and
    repo_url must be HTTPS (the VPS clones over HTTPS with a token, not SSH)."""
    from core.paths import project_root

    projects_file = project_root() / ".factory" / "projects.json"
    data = json.loads(projects_file.read_text(encoding="utf-8"))
    projects = [ProjectDescriptor.model_validate(item) for item in data]

    assert len(projects) == 4
    by_id = {p.id: p for p in projects}
    assert by_id["darkfac"].repo_url is not None
    assert by_id["site-ggcampos"].repo_url is not None
    assert by_id["segundo-cerebro"].repo_url is not None
    assert by_id["jarvis"].repo_url is not None
    assert by_id["jarvis"].default_branch == "master"
    for project in projects:
        assert project.repo_url is not None
        assert project.repo_url.startswith("https://"), f"{project.id}: expected HTTPS repo_url"
        assert project.repo_url == normalize_repo_url(project.repo_url)
        if project.deploy is not None:
            assert project.deploy.params == {}


def test_normalize_repo_url_converts_ssh_shorthand_to_https() -> None:
    assert (
        normalize_repo_url("git@github.com:GCamposGit/SegundoCerebro.git")
        == "https://github.com/GCamposGit/SegundoCerebro.git"
    )


def test_normalize_repo_url_leaves_https_unchanged() -> None:
    https_url = "https://github.com/GCamposGit/Site_ggcampos.git"
    assert normalize_repo_url(https_url) == https_url


def test_project_commands_validate_field_uses_validate_json_key() -> None:
    """The Python attribute is `validate_cmds` (to avoid shadowing
    BaseModel.validate), but the public constructor kwarg / JSON key stays
    "validate" per the HF-27-01 contract."""
    commands = ProjectCommands.model_validate({"validate": ["python -m pytest -q"]})
    assert commands.validate_cmds == ["python -m pytest -q"]

    dumped = commands.model_dump(by_alias=True)
    assert dumped["validate"] == ["python -m pytest -q"]
    assert "validate_cmds" not in dumped


def test_project_commands_field_does_not_shadow_basemodel_validate(recwarn) -> None:
    """Regression guard for the pydantic 'Field name "validate" shadows an
    attribute in parent "BaseModel"' UserWarning triggered by declaring a
    field literally named `validate`."""
    import importlib

    import core.projects.models as models_module

    importlib.reload(models_module)
    shadow_warnings = [
        w
        for w in recwarn.list
        if issubclass(w.category, UserWarning) and "shadows an attribute in parent" in str(w.message)
    ]
    assert not shadow_warnings
