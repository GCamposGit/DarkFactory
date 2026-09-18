"""Domain contracts for Cross-Project Reusable Catalog (HF-25).

Defines Pydantic v2 models for reusable software components, files,
metadata manifests, and cross-project synchronization results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class ComponentKind(str, Enum):
    """Classification of reusable components across the project portfolio."""
    UI_COMPONENT = "ui_component"
    INTEGRATION_HOOK = "integration_hook"
    SCRIPT_UTILITY = "script_utility"
    SCHEMA_CONTRACT = "schema_contract"


class ComponentFile(BaseModel):
    """File belonging to a reusable component with path, hash, and content."""
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Relative path inside the project repository")
    content: str = Field(default="", description="Text content or template string")
    sha256: str = Field(default="", description="Hex SHA-256 digest of content")


class ComponentDescriptor(BaseModel):
    """Metadata manifest describing a reusable cross-project component."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Unique slug identifier (e.g. 'atrium-seo-validator')")
    name: str = Field(min_length=1, description="Human-readable title")
    version: str = Field(default="1.0.0", description="SemVer version")
    kind: ComponentKind
    source_project_id: str = Field(description="Project ID where the component was originally developed")
    description: str = Field(min_length=1, description="Functional summary of the component")
    compatible_archetypes: list[str] = Field(
        default_factory=list,
        description="Archetype IDs this component can be installed into (e.g. personal_presence, internal_tool)",
    )
    files: list[ComponentFile] = Field(default_factory=list, description="Files comprising this component")
    dependencies: list[str] = Field(default_factory=list, description="External package dependencies")
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


class SyncResult(BaseModel):
    """Result of synchronizing or injecting a component into a target project."""
    model_config = ConfigDict(extra="forbid")

    component_id: str
    target_project_id: str
    target_path: str
    success: bool
    files_synced: list[str] = Field(default_factory=list)
    files_skipped: list[str] = Field(default_factory=list)
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
