"""Connect a greenfield project to a private remote and the executable project registry (USR-274).

The flow is idempotent and resumable: every step records itself in a state file kept inside
``.git`` of the new project, existing resources are detected and reused, and nothing that
already exists is ever deleted or overwritten.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from core.projects.detect import detect_commands
from core.projects.models import ProjectCommands, ProjectDescriptor, ProjectEnvironment, ProjectKind
from core.projects.registry import ProjectRegistry

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "darkfac-bootstrap-state.json"
STEPS = ("preflight", "remote", "git_remote", "push", "registry")

CommandRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class BootstrapBlockedError(RuntimeError):
    """Structured, explicit block: ``code`` is stable, ``details`` is machine readable."""

    def __init__(self, code: str, message: str, **details: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details)

    def as_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, "details": self.details}


class RemoteRepository(BaseModel):
    owner: str
    name: str
    url: str
    private: bool = True
    created: bool = False


class RemoteProvider(Protocol):
    """Abstraction over where the private repository lives."""

    def check_credentials(self) -> None:
        """Raise BootstrapBlockedError(code='missing_credentials') when unusable."""

    def ensure_repository(self, owner: str, name: str) -> RemoteRepository:
        """Return the private repository, creating it only when absent."""


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )


class GitHubProvider:
    """Creates PRIVATE repositories with ``gh`` and only for explicitly authorized owners."""

    def __init__(
        self,
        *,
        authorized: bool,
        allowed_owners: Sequence[str],
        runner: CommandRunner | None = None,
    ) -> None:
        self.authorized = authorized
        self.allowed_owners = tuple(owner.lower() for owner in allowed_owners)
        self._run = runner or _default_runner

    def check_credentials(self) -> None:
        if not self.authorized:
            raise BootstrapBlockedError(
                "remote_creation_not_authorized", "repository creation requires the explicit authorization flag"
            )
        try:
            status = self._run(["gh", "auth", "status"])
        except OSError as exc:
            raise BootstrapBlockedError("missing_credentials", f"gh CLI unavailable: {exc}") from exc
        if status.returncode != 0:
            raise BootstrapBlockedError("missing_credentials", "gh is not authenticated (gh auth status failed)")

    def ensure_repository(self, owner: str, name: str) -> RemoteRepository:
        if not self.authorized:
            raise BootstrapBlockedError(
                "remote_creation_not_authorized", "repository creation requires the explicit authorization flag"
            )
        if owner.lower() not in self.allowed_owners:
            raise BootstrapBlockedError(
                "owner_not_authorized", f"owner '{owner}' is not in the allowed owners", owner=owner
            )
        slug = f"{owner}/{name}"
        view = self._run(["gh", "repo", "view", slug, "--json", "visibility,url"])
        if view.returncode == 0:
            info = json.loads(view.stdout or "{}")
            if str(info.get("visibility", "")).upper() != "PRIVATE":
                raise BootstrapBlockedError(
                    "existing_repository_not_private", f"{slug} exists and is not private", repository=slug
                )
            url = str(info.get("url") or f"https://github.com/{slug}")
            return RemoteRepository(owner=owner, name=name, url=url + ".git" if not url.endswith(".git") else url)
        created = self._run(["gh", "repo", "create", slug, "--private"])
        if created.returncode != 0:
            raise BootstrapBlockedError(
                "remote_creation_failed", (created.stderr or created.stdout).strip() or "gh repo create failed",
                repository=slug,
            )
        return RemoteRepository(owner=owner, name=name, url=f"https://github.com/{slug}.git", created=True)


class LocalBareProvider:
    """Test/offline provider: a bare repository under ``base_dir`` plays the private remote."""

    def __init__(self, base_dir: Path, *, credentials_available: bool = True) -> None:
        self.base_dir = Path(base_dir)
        self.credentials_available = credentials_available

    def check_credentials(self) -> None:
        if not self.credentials_available:
            raise BootstrapBlockedError("missing_credentials", "local provider has no credentials")

    def ensure_repository(self, owner: str, name: str) -> RemoteRepository:
        path = (self.base_dir / owner / f"{name}.git").resolve()
        created = False
        if not path.exists():
            path.mkdir(parents=True)
            proc = _default_runner(["git", "init", "--bare", str(path)])
            if proc.returncode != 0:
                raise BootstrapBlockedError("remote_creation_failed", proc.stderr.strip(), repository=str(path))
            created = True
        return RemoteRepository(owner=owner, name=name, url=str(path), created=created)


class BootstrapState(BaseModel):
    project_id: str
    completed: list[str] = Field(default_factory=list)
    remote_url: str | None = None
    remote_created: bool = False
    last_error: dict[str, object] | None = None


@dataclass(frozen=True)
class BootstrapResult:
    project_id: str
    remote_url: str
    branch: str
    registry_file: str
    validate_commands: tuple[str, ...]
    reused: tuple[str, ...]
    state_file: str


def _git(root: Path, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if check and proc.returncode != 0:
        raise BootstrapBlockedError(
            "git_failed", f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}", command=" ".join(args)
        )
    return proc


def _state_path(root: Path) -> Path:
    return root / ".git" / STATE_FILE_NAME


def _load_state(root: Path, project_id: str) -> BootstrapState:
    path = _state_path(root)
    if path.is_file():
        try:
            state = BootstrapState.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            state = BootstrapState(project_id=project_id)
        if state.project_id == project_id:
            return state
    return BootstrapState(project_id=project_id)


def _save_state(root: Path, state: BootstrapState) -> None:
    path = _state_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _has_tests(root: Path) -> bool:
    for pattern in ("test_*.py", "*_test.py", "*.test.*", "*.spec.*"):
        for found in root.rglob(pattern):
            parts = set(found.relative_to(root).parts)
            if not parts & {".git", "node_modules", ".venv", ".factory", ".agents"}:
                return True
    return False


def _merged_commands(explicit: ProjectCommands | None, validate: Sequence[str]) -> ProjectCommands:
    """Explicit setup/build/smoke (and validate when given) win; otherwise use the validate list."""
    if explicit is None:
        return ProjectCommands(validate=list(validate))
    merged = explicit.model_copy(deep=True)
    if not merged.validate_cmds:
        merged.validate_cmds = list(validate)
    return merged


def bootstrap_project(
    project_root: Path,
    *,
    project_id: str,
    owner: str,
    provider: RemoteProvider,
    registry_file: Path,
    repo_name: str | None = None,
    name: str | None = None,
    prefix: str = "PRJ",
    default_branch: str = "main",
    validate_commands: Sequence[str] | None = None,
    require_tests: bool = True,
    project_commands: ProjectCommands | None = None,
    environment: ProjectEnvironment | None = None,
) -> BootstrapResult:
    """Connect ``project_root`` to a private remote and register it as executable.

    Raises BootstrapBlockedError (structured) on missing tests/commands/credentials or conflicts.
    Replays reuse existing remote, ``origin`` and registry entry; progress lives in the state file.
    """

    root = project_root.expanduser().resolve(strict=True)
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise BootstrapBlockedError("not_a_git_repository", f"{root} is not a Git repository")
    state = _load_state(root, project_id)
    reused: list[str] = []

    def done(step: str) -> None:
        if step not in state.completed:
            state.completed.append(step)
        state.last_error = None
        _save_state(root, state)

    try:
        # Preflight: tests, commands, credentials.
        if require_tests and not _has_tests(root):
            raise BootstrapBlockedError("missing_tests", "project has no tests; refusing to register as executable")
        if not validate_commands and project_commands is not None and project_commands.validate_cmds:
            validate_commands = list(project_commands.validate_cmds)
        commands = list(validate_commands) if validate_commands else list(detect_commands(root).validate_cmds)
        if not commands:
            raise BootstrapBlockedError("missing_validate_commands", "no commands.validate could be determined")
        provider.check_credentials()
        done("preflight")

        # Remote repository (reuse when it exists).
        repo = provider.ensure_repository(owner, repo_name or project_id)
        if repo.created:
            state.remote_created = True
        elif "remote" in state.completed or not repo.created:
            reused.append("remote")
        state.remote_url = repo.url
        done("remote")

        # Local origin + branch (never overwrite a different origin).
        current = _git(root, "remote", "get-url", "origin", check=False)
        if current.returncode == 0:
            if current.stdout.strip() != repo.url:
                raise BootstrapBlockedError(
                    "origin_conflict",
                    "origin already points to a different URL; not overwriting",
                    existing=current.stdout.strip(),
                    expected=repo.url,
                )
            reused.append("git_remote")
        else:
            _git(root, "remote", "add", "origin", repo.url)
        if _git(root, "branch", "--show-current").stdout.strip() != default_branch:
            _git(root, "branch", "-M", default_branch)
        done("git_remote")

        # Push (a no-op when the remote already has the commit).
        _git(root, "push", "-u", "origin", default_branch)
        done("push")

        # Registry (core repo file only: .factory/projects.json or the injected path).
        registry = ProjectRegistry(registry_file)
        wanted = ProjectDescriptor(
            id=project_id,
            name=name or project_id,
            path=str(root),
            kind=ProjectKind.INTERNAL_PRODUCT,
            prefix=prefix,
            repo_url=repo.url,
            default_branch=default_branch,
            commands=_merged_commands(project_commands, commands),
            environment=environment,
        )
        existing = registry.get_project(project_id)
        if existing is not None:
            if existing.repo_url != wanted.repo_url or existing.path != wanted.path:
                raise BootstrapBlockedError(
                    "registry_conflict",
                    "registry already has this project id with different path/repo; not overwriting",
                    project_id=project_id,
                )
            reused.append("registry")
            if existing.commands.validate_cmds:
                commands = list(existing.commands.validate_cmds)
            else:
                existing.commands = wanted.commands
                registry.register_project(existing)
            if existing.environment is None and environment is not None:
                existing.environment = environment
                registry.register_project(existing)
        else:
            registry.register_project(wanted)
        done("registry")
    except BootstrapBlockedError as exc:
        state.last_error = exc.as_dict()
        _save_state(root, state)
        raise

    return BootstrapResult(
        project_id=project_id,
        remote_url=repo.url,
        branch=default_branch,
        registry_file=str(registry.projects_file),
        validate_commands=tuple(commands),
        reused=tuple(reused),
        state_file=str(_state_path(root)),
    )
