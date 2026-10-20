"""Pure, immutable security contracts for the DarkFac Wave 0 pilot."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.enterprise.models import EnterpriseProjectConfig


class DataClassification(str, Enum):
    """Ordered data sensitivity; unknown values are validation errors."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class SensitiveEffect(str, Enum):
    """Effects which require explicit profile permission before execution."""

    NETWORK = "network"
    CREDENTIALS = "credentials"
    WRITE = "write"
    PUBLICATION = "publication"
    DEPLOY = "deploy"
    INFERENCE = "inference"


class SecurityProfile(BaseModel):
    """Versioned pilot policy; presence alone grants no effect permissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    revision: int = Field(strict=True, ge=1)
    project_id: Literal["darkfac"]
    maximum_classification: DataClassification
    allowed_effects: frozenset[SensitiveEffect] = frozenset()
    enterprise: EnterpriseProjectConfig | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Accept integer schema identifiers without bool/float/string coercion."""
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_enterprise_scope(self) -> "SecurityProfile":
        """Reject an enterprise policy attached to a different project."""
        if self.enterprise is not None and self.enterprise.project_id != self.project_id:
            raise ValueError("enterprise project must match security profile")
        return self


class EffectDecision(BaseModel):
    """Structured helper verdict, never an execution authorization token."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed: bool
    reason: Literal[
        "profile_missing", "profile_invalid", "project_mismatch", "revision_mismatch",
        "classification_unknown", "effect_unknown", "classification_exceeded",
        "effect_not_allowed", "profile_permission",
    ]
