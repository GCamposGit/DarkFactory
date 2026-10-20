from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.projects.environment import (
    ProjectEnvironmentError,
    build_product_env,
    mask_secrets,
)
from core.projects.models import ProjectDescriptor, ProjectEnvironment
from core.projects.ticket_execution import (
    ProjectExecutionError,
    run_project_commands,
    setup_project,
    validate_project_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET = "sk-test-secret-value-123"


def _product(tmp_path: Path) -> tuple[ProjectDescriptor, Path]:
    root = tmp_path / "product"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n"
        "        self.assertEqual(1 + 1, 2)\n",
        encoding="utf-8",
    )
    py = f'"{sys.executable}"'
    project = ProjectDescriptor(
        id="standalone-demo",
        name="Standalone Demo",
        path=str(root),
        environment={
            "kind": "venv",
            "path": ".venv",
            "setup": [f"{py} -m venv --without-pip .venv"],
            "env_allow": ["PRODUCT_API_KEY"],
        },
        commands={
            "validate": ["python -m unittest discover -s tests -q"],
            "build": ['python -c "import os; print(os.environ.get(\'PRODUCT_API_KEY\', \'\'))"'],
            "smoke": ['python -c "import core"'],
        },
    )
    return project, root


def test_setup_test_build_run_in_own_environment_outside_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("DARKFAC_SECRET_TOKEN", "factory-only-token-xyz")
    monkeypatch.setenv("PRODUCT_API_KEY", SECRET)
    project, root = _product(tmp_path)
    assert REPO_ROOT not in root.resolve().parents

    setup = setup_project(project, root)
    assert setup.ok, setup.combined_output
    assert (root / ".venv").is_dir()

    validation = validate_project_commands(project, root)
    assert validation.ok, validation.combined_output

    build = run_project_commands(project.commands.build, root, project=project)
    assert build.ok, build.combined_output
    # allowlisted reference resolved by name, but its value is masked in the output
    assert SECRET not in build.combined_output
    assert "***" in build.combined_output

    # counter-proof: core is not importable from the product environment,
    # even though the host PYTHONPATH points at the factory checkout
    smoke = run_project_commands(project.commands.smoke, root, project=project)
    assert not smoke.ok
    assert "ModuleNotFoundError" in smoke.combined_output


def test_product_env_strips_factory_variables_and_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = {
        "PATH": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "VIRTUAL_ENV": str(tmp_path / "core-venv"),
        "DARKFAC_HOME": "x",
        "OPENAI_API_KEY": "leak-1",
        "PRODUCT_API_KEY": SECRET,
        "TEMP": str(tmp_path),
    }
    env = ProjectEnvironment(kind="system", env_allow=["PRODUCT_API_KEY"])
    child, secrets = build_product_env(env, tmp_path, host_env=host)
    assert "PYTHONPATH" not in child and "DARKFAC_HOME" not in child
    assert "OPENAI_API_KEY" not in child
    assert "VIRTUAL_ENV" not in child
    assert child["PRODUCT_API_KEY"] == SECRET
    assert secrets == [SECRET]
    assert mask_secrets(f"key={SECRET}", secrets) == "key=***"


def test_env_allow_rejects_factory_reserved_names() -> None:
    for name in ("PYTHONPATH", "DARKFAC_TOKEN", "VIRTUAL_ENV", "bad name"):
        with pytest.raises(ValueError):
            ProjectEnvironment(env_allow=[name])


def test_missing_declared_environment_is_a_structured_error(tmp_path: Path) -> None:
    project, root = _product(tmp_path)
    with pytest.raises(ProjectExecutionError) as excinfo:
        validate_project_commands(project, root)
    assert '"error": "environment_missing"' in str(excinfo.value)
    with pytest.raises(ProjectEnvironmentError) as direct:
        build_product_env(project.environment, root, require_ready=True)
    assert direct.value.code == "environment_missing"
    assert direct.value.as_dict()["details"]["kind"] == "venv"


def test_venv_path_must_stay_inside_product(tmp_path: Path) -> None:
    env = ProjectEnvironment(kind="venv", path="../escape")
    with pytest.raises(ProjectEnvironmentError) as excinfo:
        build_product_env(env, tmp_path)
    assert excinfo.value.code == "environment_path_outside_project"


def test_project_without_environment_keeps_legacy_behavior(tmp_path: Path) -> None:
    project = ProjectDescriptor(
        id="legacy-demo", name="Legacy Demo", commands={"validate": ['python -c "print(1)"']}
    )
    assert project.environment is None
    result = validate_project_commands(project, tmp_path)
    assert result.ok
