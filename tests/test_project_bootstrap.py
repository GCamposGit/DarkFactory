from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.adoption.bootstrap import (
    BootstrapBlockedError,
    GitHubProvider,
    LocalBareProvider,
    bootstrap_project,
)
from core.adoption.service import initialize_project
from core.projects.registry import ProjectRegistry
from core.projects.ticket_execution import validate_project_commands


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)


def _project(tmp_path: Path, with_tests: bool = True) -> Path:
    root = initialize_project(tmp_path / "novo", project_name="Novo")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "user.email", "t@local")
    if with_tests:
        (root / "tests").mkdir()
        (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "tests")
    return root


def _boot(root: Path, tmp_path: Path, provider=None, **kw):
    return bootstrap_project(
        root,
        project_id="novo",
        owner="guigc",
        provider=provider or LocalBareProvider(tmp_path / "remotes"),
        registry_file=tmp_path / "reg" / "projects.json",
        validate_commands=kw.pop("validate_commands", ["python -m pytest tests -q -p no:cacheprovider -n0"]),
        **kw,
    )


def test_bootstrap_reaches_executable_project_and_is_idempotent(tmp_path: Path) -> None:
    root = _project(tmp_path)
    first = _boot(root, tmp_path)
    remote = Path(first.remote_url)
    assert _git(remote, "rev-parse", "--verify", "main").returncode == 0
    entry = json.loads((tmp_path / "reg" / "projects.json").read_text(encoding="utf-8"))
    novo = next(item for item in entry if item["id"] == "novo")
    assert novo["commands"]["validate"] and novo["repo_url"] == first.remote_url
    assert novo["default_branch"] == "main"

    project = ProjectRegistry(tmp_path / "reg" / "projects.json").get_project("novo")
    assert project is not None and project.commands.validate_cmds
    report = validate_project_commands(project, root, timeout_s=120)
    assert report.ok

    second = _boot(root, tmp_path)
    assert {"remote", "git_remote", "registry"} <= set(second.reused)
    assert len([p for p in entry if p["id"] == "novo"]) == 1
    assert _git(root, "remote").stdout.split() == ["origin"]


def test_missing_tests_and_credentials_are_structured_blocks(tmp_path: Path) -> None:
    root = _project(tmp_path, with_tests=False)
    with pytest.raises(BootstrapBlockedError) as err:
        _boot(root, tmp_path)
    assert err.value.code == "missing_tests"
    assert not (tmp_path / "reg" / "projects.json").exists()

    root2 = _project(tmp_path / "b")
    with pytest.raises(BootstrapBlockedError) as err2:
        _boot(root2, tmp_path, provider=LocalBareProvider(tmp_path / "r2", credentials_available=False))
    assert err2.value.code == "missing_credentials"
    state = json.loads((root2 / ".git" / "darkfac-bootstrap-state.json").read_text(encoding="utf-8"))
    assert state["last_error"]["error"] == "missing_credentials"


def test_partial_failure_is_resumable_and_keeps_existing_origin(tmp_path: Path) -> None:
    root = _project(tmp_path)
    _git(root, "remote", "add", "origin", str(tmp_path / "other.git"))
    with pytest.raises(BootstrapBlockedError) as err:
        _boot(root, tmp_path)
    assert err.value.code == "origin_conflict"
    assert _git(root, "remote", "get-url", "origin").stdout.strip() == str(tmp_path / "other.git")
    state = json.loads((root / ".git" / "darkfac-bootstrap-state.json").read_text(encoding="utf-8"))
    assert "remote" in state["completed"] and "registry" not in state["completed"]
    _git(root, "remote", "remove", "origin")
    assert _boot(root, tmp_path).project_id == "novo"


def test_github_provider_requires_authorization_and_creates_private_only(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        code = 1 if argv[:3] == ["gh", "repo", "view"] else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    with pytest.raises(BootstrapBlockedError) as err:
        GitHubProvider(authorized=False, allowed_owners=["guigc"], runner=runner).check_credentials()
    assert err.value.code == "remote_creation_not_authorized"
    provider = GitHubProvider(authorized=True, allowed_owners=["guigc"], runner=runner)
    with pytest.raises(BootstrapBlockedError) as err2:
        provider.ensure_repository("stranger", "x")
    assert err2.value.code == "owner_not_authorized"
    repo = provider.ensure_repository("guigc", "novo")
    assert repo.created and ["gh", "repo", "create", "guigc/novo", "--private"] in calls


def test_bootstrap_registers_environment_and_full_command_set(tmp_path: Path) -> None:
    from core.projects.models import EnvironmentKind, ProjectCommands, ProjectEnvironment

    root = _project(tmp_path)
    cmds = ProjectCommands(setup=["python -m venv .venv"], build=["python -m compileall -q src"], smoke=["echo smoke"])
    env = ProjectEnvironment(kind=EnvironmentKind.VENV, path=".venv", setup=["python -m venv .venv"])
    _boot(root, tmp_path, project_commands=cmds, environment=env)
    _boot(root, tmp_path, project_commands=cmds, environment=env)
    projects = ProjectRegistry(tmp_path / "reg" / "projects.json").list_projects()
    assert [p.id for p in projects].count("novo") == 1
    novo = next(p for p in projects if p.id == "novo")
    assert novo.environment is not None and novo.environment.kind is EnvironmentKind.VENV
    assert novo.commands.build == ["python -m compileall -q src"] and novo.commands.smoke == ["echo smoke"]
    assert novo.commands.validate_cmds
