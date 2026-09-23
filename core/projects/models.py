"""Data contracts for Dark Factory Multi-Project Management."""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.roadmap.models import RoadmapProjectSummary

# Matches param keys that look like secrets. A key is only rejected when it
# looks secret-like AND does not end in "_ref" (a reference to a secret held
# elsewhere, e.g. an env var name or vault path — never the value itself).
_SECRET_LIKE_KEY = re.compile(r"(password|token|secret)", re.IGNORECASE)


class ProjectKind(str, Enum):
    """Categorization of managed projects."""
    CORE = "core"
    CLIENT_PORTFOLIO = "client_portfolio"
    INTERNAL_PRODUCT = "internal_product"
    SAAS_APP = "saas_app"


class DeployTargetType(str, Enum):
    """Supported deployment adapters for the production line (HF-27)."""
    DOKPLOY = "dokploy"
    HOSTINGER_FTP = "hostinger_ftp"
    LOCAL_SERVICE = "local_service"
    NONE = "none"


class ProjectCommands(BaseModel):
    """Explicit setup/validate/build/smoke commands for a project.

    Any empty list is autodetected at run time via `core.projects.detect`
    (see `core.projects.registry.resolve_commands`). A non-empty explicit
    list always wins over autodetection.
    """
    # `validate` is the field's public/JSON name (per HF-27-01 spec), but a
    # field literally named "validate" shadows BaseModel's own deprecated
    # `validate` classmethod (pydantic emits a UserWarning at class creation
    # time). The attribute is renamed to `validate_cmds` with alias="validate"
    # so the JSON/dict key and constructor keyword stay "validate" while the
    # Python attribute is collision-free; populate_by_name also accepts
    # "validate_cmds" as an input key.
    model_config = ConfigDict(populate_by_name=True)

    setup: list[str] = Field(default_factory=list, description="Commands to install dependencies")
    validate_cmds: list[str] = Field(
        default_factory=list, alias="validate", description="Commands to run the test suite"
    )
    build: list[str] = Field(default_factory=list, description="Commands to produce a deployable artifact")
    smoke: list[str] = Field(default_factory=list, description="Local smoke/sanity commands run before deploy")


class DeployConfig(BaseModel):
    """Deployment adapter selection and non-secret parameters."""
    type: DeployTargetType = Field(..., description="Deployment adapter identifier")
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Adapter parameters. No secret values allowed; use a '*_ref' key to reference one held elsewhere.",
    )

    @field_validator("params")
    @classmethod
    def _reject_inline_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if _SECRET_LIKE_KEY.search(key) and not key.endswith("_ref"):
                raise ValueError(
                    f"deploy.params key '{key}' looks like a secret; store it externally and "
                    "reference it with a key ending in '_ref' instead"
                )
        return value


class SmokeCheck(BaseModel):
    """A single post-deploy HTTP smoke check."""
    url: str = Field(..., description="URL to request after deploy")
    expect_status: int = Field(default=200, description="Expected HTTP status code")
    expect_text: Optional[str] = Field(default=None, description="Optional substring expected in the response body")


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

    # HF-27-01: executable registry fields. All optional/defaulted so existing
    # JSON without them still loads unchanged.
    repo_url: Optional[str] = Field(
        default=None, description="Git remote URL for clone, e.g. https://github.com/<owner>/<repo>.git"
    )
    default_branch: str = Field(default="main", description="Default branch used for df/<run_id> workspaces")
    commands: ProjectCommands = Field(
        default_factory=ProjectCommands,
        description="Explicit setup/validate/build/smoke commands; empty entries fall back to autodetection",
    )
    deploy: Optional[DeployConfig] = Field(
        default=None, description="Deployment adapter configuration; params must not carry secret values"
    )
    smoke: list[SmokeCheck] = Field(default_factory=list, description="HTTP smoke checks run after deploy")
    exec_affinity: list[str] = Field(
        default_factory=list,
        description="Extra required capabilities for job routing (e.g. 'target:local_service', 'gpu')",
    )
    requires_commercial_acceptance: bool = Field(
        default=False, description="Blocks automatic deploy until commercial/business acceptance is given"
    )

    def to_roadmap_summary(self) -> RoadmapProjectSummary:
        """Convert to the standard RoadmapProjectSummary used across DarkHub."""
        return RoadmapProjectSummary(
            id=self.id,
            name=self.name,
            description=self.description,
        )
