"""Data contracts for Dark Factory Multi-Project Management."""

from __future__ import annotations

from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from core.roadmap.models import RoadmapProjectSummary


class ProjectKind(str, Enum):
    """Categorization of managed projects."""
    CORE = "core"
    CLIENT_PORTFOLIO = "client_portfolio"
    INTERNAL_PRODUCT = "internal_product"
    SAAS_APP = "saas_app"


class ProjectDescriptor(BaseModel):
    """Canonical model for a project managed by Dark Factory."""
    id: str = Field(..., min_length=2, max_length=64, description="Unique project slug identifier")
    name: str = Field(..., min_length=2, max_length=120, description="Human-readable project title")
    description: str = Field(default="", description="High-level project scope or client description")
    path: Optional[str] = Field(default=None, description="Filesystem absolute path to workspace")
    kind: ProjectKind = Field(default=ProjectKind.CLIENT_PORTFOLIO, description="Project architectural kind")
    prefix: str = Field(default="PRJ", max_length=10, description="Ticket ID prefix for demands (e.g. SIT, SC)")
    domain: Optional[str] = Field(default=None, description="Primary public domain if applicable")
    deploy_target: Optional[str] = Field(default=None, description="Primary deployment pipeline/target")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_roadmap_summary(self) -> RoadmapProjectSummary:
        """Convert to the standard RoadmapProjectSummary used across DarkHub."""
        return RoadmapProjectSummary(
            id=self.id,
            name=self.name,
            description=self.description,
        )
