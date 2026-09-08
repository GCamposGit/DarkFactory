"""Typed contracts for adopting projects into the Dark Factory."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field


class ProjectKind(str, Enum):
    """Whether the target already contains product code."""

    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"


class FileAction(str, Enum):
    """A non-destructive action proposed for one target path."""

    CREATE = "create"
    ADOPT = "adopt"
    UPDATE = "update"
    REMOVE = "remove"
    UNCHANGED = "unchanged"
    PRESERVE = "preserve"
    CONFLICT = "conflict"


class GitSnapshot(BaseModel):
    """Read-only Git state captured during preflight."""

    model_config = ConfigDict(frozen=True)

    repository: bool
    root: str | None = None
    head: str | None = None
    branch: str | None = None
    dirty_paths: tuple[str, ...] = ()

    @computed_field
    @property
    def clean(self) -> bool:
        return self.repository and not self.dirty_paths


class StackProfile(BaseModel):
    """Portable commands inferred from canonical project files."""

    model_config = ConfigDict(frozen=True)

    ecosystems: tuple[str, ...] = ()
    configuration_files: tuple[str, ...] = ()
    validation_commands: tuple[str, ...] = ()


class ProjectInspection(BaseModel):
    """Facts about a target repository without reading private data."""

    model_config = ConfigDict(frozen=True)

    project_root: str
    project_name: str
    kind: ProjectKind
    git: GitSnapshot
    stack: StackProfile
    governance_files: tuple[str, ...] = ()
    existing_lock: bool = False


class PlannedFile(BaseModel):
    """One managed or preserved path in an adoption plan."""

    model_config = ConfigDict(frozen=True)

    path: str
    action: FileAction
    desired_sha256: str | None = None
    current_sha256: str | None = None
    reason: str = ""


class AdoptionPlan(BaseModel):
    """Complete, reviewable plan produced before any target write."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    inspection: ProjectInspection
    source_commit: str
    source_repository: str
    autonomy_level: int = Field(default=2, ge=0, le=5)
    files: tuple[PlannedFile, ...]
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @computed_field
    @property
    def ready(self) -> bool:
        return not self.blockers and all(item.action != FileAction.CONFLICT for item in self.files)


class AdoptionLock(BaseModel):
    """Consumer-verifiable provenance and ownership manifest."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = 1
    source_repository: str
    source_commit: str
    autonomy_level: int = Field(ge=0, le=5)
    installation_mode: str = "namespaced_snapshot"
    runtime_root: str = ".factory/runtime"
    managed_files: dict[str, str]
    seeded_project_files: dict[str, str] = Field(default_factory=dict)
    agents_block_sha256: str
    validation_commands: tuple[str, ...]


class AdoptionResult(BaseModel):
    """Result of a completed transaction."""

    model_config = ConfigDict(frozen=True)

    project_root: str
    source_commit: str
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    adopted: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    lock_path: str


class VerificationReport(BaseModel):
    """Drift and readiness status for an adopted project."""

    model_config = ConfigDict(frozen=True)

    project_root: str
    lock_valid: bool
    managed_files_checked: int = 0
    problems: tuple[str, ...] = ()

    @computed_field
    @property
    def ready(self) -> bool:
        return self.lock_valid and not self.problems and self.managed_files_checked > 0


class TaskPreparation(BaseModel):
    """A durable handoff for a task-specific branch and worktree."""

    model_config = ConfigDict(frozen=True)

    ticket_id: str
    title: str
    project_root: str
    branch: str
    worktree: str
    base_sha: str
    owner: str
    allowed_paths: tuple[str, ...]
    validate_commands: tuple[str, ...]
    manifest_path: str


def portable_path(path: Path, root: Path) -> str:
    """Return a stable POSIX path relative to ``root``."""

    return path.relative_to(root).as_posix()
