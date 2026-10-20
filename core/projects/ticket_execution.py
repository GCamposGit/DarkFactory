"""Project-scoped workspace and validation helpers for run_ticket."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable

from core.line.stage_build import CommandRunResult, run_shell_commands
from core.projects.models import ProjectDescriptor
from core.projects.registry import normalize_repo_url


class ProjectExecutionError(RuntimeError):
    """A project cannot safely receive a ticket run."""


def project_checkout(project: ProjectDescriptor) -> Path:
    """Resolve or clone the project's own checkout, never falling back to DarkFac."""
    path = project.resolve_path()
    if path is None:
        if not project.repo_url:
            raise ProjectExecutionError(
                f"Projeto {project.id} não tem path compatível nem repo_url; configure-o no onboarding."
            )
        path = Path.home() / ".darkfac" / "projects" / project.id
    path = path.expanduser().resolve()
    if not path.exists():
        if not project.repo_url:
            raise ProjectExecutionError(
                f"Projeto {project.id} não tem checkout nem repo_url configurado no onboarding."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", "clone", "--branch", project.default_branch, project.repo_url, str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        if clone.returncode:
            raise ProjectExecutionError(f"clone do projeto {project.id} falhou: {clone.stderr.strip()}")
    if not (path / ".git").exists():
        raise ProjectExecutionError(f"path do projeto {project.id} não é um checkout Git: {path}")
    if project.repo_url:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=path, capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False,
        )
        expected = normalize_repo_url(project.repo_url).rstrip("/").casefold()
        actual = normalize_repo_url(remote.stdout.strip()).rstrip("/").casefold()
        if remote.returncode or actual != expected:
            raise ProjectExecutionError(
                f"origin do checkout {path} não corresponde ao repo_url registrado para {project.id}."
            )
    return path


def _run(project: ProjectDescriptor, commands: Iterable[str], cwd: Path, timeout_s: int, phase: str = "run") -> CommandRunResult:
    """Run commands in the product's own environment when declared (USR-275); else legacy inherit."""
    env = project.environment
    if env is None:
        return run_shell_commands(commands, cwd, timeout_s=timeout_s)
    from core.projects.environment import ProjectEnvironmentError, run_in_environment

    try:
        return run_in_environment(env, commands, cwd, timeout_s=timeout_s, phase=phase)
    except ProjectEnvironmentError as exc:
        raise ProjectExecutionError(f"Projeto {project.id}: {json.dumps(exc.as_dict(), ensure_ascii=True)}") from exc


def validate_project_commands(project: ProjectDescriptor, cwd: Path, *, timeout_s: int = 1800) -> CommandRunResult:
    """Run only explicitly declared setup and validation commands for a project."""
    commands = project.commands
    if not commands.validate_cmds:
        raise ProjectExecutionError(
            f"Projeto {project.id} não declara commands.validate. Execute o Project Adoption Gateway "
            "(skill 07) no onboarding e declare comandos de validação antes de executar tickets."
        )
    return _run(project, commands.validate_cmds, cwd, timeout_s)


def setup_project(project: ProjectDescriptor, cwd: Path, *, timeout_s: int = 1800) -> CommandRunResult:
    """Run project-declared setup commands before starting the development agent."""
    env = project.environment
    if env is None:
        return run_shell_commands(project.commands.setup, cwd, timeout_s=timeout_s)
    from core.projects.environment import ProjectEnvironmentError, setup_environment

    try:
        return setup_environment(env, cwd, project.commands.setup, timeout_s=timeout_s)
    except ProjectEnvironmentError as exc:
        raise ProjectExecutionError(f"Projeto {project.id}: {json.dumps(exc.as_dict(), ensure_ascii=True)}") from exc


def run_project_commands(
    commands: Iterable[str], cwd: Path, *, timeout_s: int = 1800, project: ProjectDescriptor | None = None
) -> CommandRunResult:
    """Run project-declared command lists, stopping at the first failure."""
    if project is None:
        return run_shell_commands(commands, cwd, timeout_s=timeout_s)
    return _run(project, commands, cwd, timeout_s)
