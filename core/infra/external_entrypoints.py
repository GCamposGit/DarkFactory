"""USR-448 T2: registry of modules/scripts triggered by a scheduler or service outside git.

`docs/manual/arquitetura.md` records that the USR-377 S2 dead-code cleanup removed
`core/infra/availability_probe.py` because nothing *inside* the repo imported it -- a Windows
Scheduled Task did, invisibly to any import graph. This registry (`.factory/config/external_entrypoints.json`)
and its paired test (`tests/test_usr448_external_entrypoints.py`) give a future dead-code scan a
ground truth: an entry here has a real external caller even though grep never finds one, so it must
not be deleted without first removing it from the registry.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / ".factory" / "config" / "external_entrypoints.json"

TriggerType = Literal["scheduled_task", "startup_folder", "systemd_service"]


class ExternalEntrypointsRegistryError(Exception):
    """Raised when the registry file is missing, malformed, or fails schema validation."""


class ExternalEntrypoint(BaseModel):
    id: str = Field(min_length=1)
    kind: Literal["module", "script"]
    target: str = Field(min_length=1, description="Dotted module path (kind=module) or repo-relative path (kind=script)")
    trigger_type: TriggerType
    trigger_name: str = Field(min_length=1, description="Name of the scheduled task, service or startup launcher")
    host: str = Field(min_length=1, description="Host(s) where the trigger is installed")
    installed_by: list[str] = Field(default_factory=list, description="Repo scripts that register the trigger, if any")
    reason: str = Field(min_length=1, description="Why this entrypoint exists")


class ExternalEntrypointsRegistry(BaseModel):
    version: int
    entrypoints: list[ExternalEntrypoint]


def load_registry(path: Path | None = None) -> ExternalEntrypointsRegistry:
    """Load and schema-validate the registry; wraps every failure into a structured error."""
    registry_path = path or REGISTRY_PATH
    try:
        raw_text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExternalEntrypointsRegistryError(f"Nao foi possivel ler {registry_path}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ExternalEntrypointsRegistryError(f"JSON invalido em {registry_path}: {exc}") from exc
    try:
        return ExternalEntrypointsRegistry.model_validate(raw)
    except ValidationError as exc:
        raise ExternalEntrypointsRegistryError(f"Registro malformado em {registry_path}: {exc}") from exc


def verify_entrypoint(entry: ExternalEntrypoint, repo_root: Path | None = None) -> None:
    """Confirm a single entry's module imports or its script exists; raises on the first problem."""
    root = repo_root or REPO_ROOT
    if entry.kind == "module":
        try:
            importlib.import_module(entry.target)
        except ImportError as exc:
            raise ExternalEntrypointsRegistryError(
                f"Entrypoint '{entry.id}' aponta para o modulo inexistente '{entry.target}' "
                f"(acionado por {entry.trigger_type} '{entry.trigger_name}' em {entry.host})"
            ) from exc
    else:
        if not (root / entry.target).is_file():
            raise ExternalEntrypointsRegistryError(
                f"Entrypoint '{entry.id}' aponta para o script inexistente '{entry.target}' "
                f"(acionado por {entry.trigger_type} '{entry.trigger_name}' em {entry.host})"
            )


def verify_all(path: Path | None = None, repo_root: Path | None = None) -> list[ExternalEntrypoint]:
    """Load the registry and verify every entry resolves; returns the validated entries."""
    registry = load_registry(path)
    for entry in registry.entrypoints:
        verify_entrypoint(entry, repo_root)
    return registry.entrypoints
