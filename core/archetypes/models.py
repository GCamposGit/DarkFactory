"""Data models and schemas for Dark Factory Archetypes subsystem (HF-20)."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class ArchetypeKind(str, Enum):
    """Supported archetype categories according to HYBRID_WORKFLOW_PLAN (HF-20)."""
    PERSONAL_PRESENCE = "personal_presence"
    INTERNAL_TOOL = "internal_tool"
    SECOND_BRAIN = "second_brain"


class StackDescriptor(BaseModel):
    """Describes the technical stack, runtime, and deployment targets of an archetype."""
    framework: str = Field(..., description="Primary web/app framework (e.g. Astro 5, FastAPI)")
    styling: str = Field(..., description="Styling or UI system (e.g. Tailwind CSS 4, DaisyUI)")
    content_format: str = Field(..., description="Content format (e.g. MDX + Zod Collections, SQLite)")
    runtime: str = Field("node", description="Primary runtime environment (e.g. node, python)")
    deployment_targets: List[str] = Field(default_factory=list, description="Supported deployment targets")


class ContentSchemaField(BaseModel):
    """Describes a single field in a content schema for LLM/Second-Brain ingestion."""
    name: str
    field_type: str = "string"
    required: bool = True
    description: str = ""


class ContentSchemaDescriptor(BaseModel):
    """Describes a decoupled content collection or schema for the archetype."""
    collection_name: str
    description: str
    path: str
    fields: List[ContentSchemaField] = Field(default_factory=list)
    example: dict[str, Any] = Field(default_factory=dict)


class ArchetypeManifest(BaseModel):
    """Manifest describing an archetype blueprint in the catalog."""
    id: str
    kind: ArchetypeKind
    title: str
    description: str
    version: str = "1.0.0"
    stack: StackDescriptor
    content_schemas: List[ContentSchemaDescriptor] = Field(default_factory=list)
    required_inputs: List[str] = Field(default_factory=list)
    default_files: List[str] = Field(default_factory=list)


class ScaffoldRequest(BaseModel):
    """Parameters for one-shot project generation from an archetype."""
    archetype_id: str
    project_name: str
    target_dir: str
    author_name: str = "Executive Leader"
    author_title: str = "Executive Portfolio & Showcase"
    author_bio: str = "Strategy, Investments and AI Transformation."
    domain: str = "example.com"
    deploy_target: str = "hostinger_ftp"
    description: str = ""


class ScaffoldResult(BaseModel):
    """Result of an archetype scaffolding operation."""
    success: bool
    project_name: str
    target_dir: str
    archetype_id: str
    files_created: List[str] = Field(default_factory=list)
    manifest: Optional[ArchetypeManifest] = None
    next_steps: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
