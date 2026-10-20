"""Evidence-bearing threat inventory contracts, without scans or campaigns."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from core.security.models import DataClassification

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ThreatCase(BaseModel):
    """An abuse case linked to a control and executable test or justified N/A."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    threat_id: NonBlank
    abuse: NonBlank
    impact: NonBlank
    control_refs: tuple[NonBlank, ...] = ()
    test_refs: tuple[NonBlank, ...] = ()
    not_applicable_reason: NonBlank | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> "ThreatCase":
        """Reject uncovered threats and ambiguous applicable/N-A records."""
        covered = bool(self.control_refs and self.test_refs)
        if self.not_applicable_reason is not None:
            if self.control_refs or self.test_refs:
                raise ValueError("N-A cannot also claim controls or tests")
        elif not covered:
            raise ValueError("applicable threat requires controls and tests")
        return self


class ThreatAsset(BaseModel):
    """An asset with explicitly recorded known or unknown classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    asset_id: NonBlank
    description: NonBlank
    classification: DataClassification | None
    consumers: tuple[NonBlank, ...] = Field(min_length=1)
    evidence: tuple[NonBlank, ...] = ()
    unknowns: tuple[NonBlank, ...] = ()

    @model_validator(mode="after")
    def require_evidence_or_unknown(self) -> "ThreatAsset":
        """Do not interpret missing evidence as a verified inventory."""
        if not self.evidence and not self.unknowns:
            raise ValueError("asset requires evidence or an explicit unknown")
        if self.classification is None and not self.unknowns:
            raise ValueError("unknown classification requires explicit unknowns")
        return self


class ThreatBoundary(BaseModel):
    """A documented trust crossing, never a grant to cross it."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    boundary_id: NonBlank
    source: NonBlank
    destination: NonBlank
    asset_ids: tuple[NonBlank, ...] = Field(min_length=1)
    threats: tuple[ThreatCase, ...] = Field(min_length=1)
    evidence: tuple[NonBlank, ...] = ()
    unknowns: tuple[NonBlank, ...] = ()


class ThreatModel(BaseModel):
    """DarkFac inventory with referential integrity and evidence gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    project_id: Literal["darkfac"]
    module: NonBlank
    revision: int = Field(strict=True, ge=1)
    assets: tuple[ThreatAsset, ...] = Field(min_length=1)
    boundaries: tuple[ThreatBoundary, ...] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int:
        """Accept integer schema identifiers without bool/float/string coercion."""
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def validate_inventory(self) -> "ThreatModel":
        """Require unique identifiers, registered assets and explicit evidence."""
        asset_ids = {asset.asset_id for asset in self.assets}
        if len(asset_ids) != len(self.assets):
            raise ValueError("duplicate asset identifier")
        if len({boundary.boundary_id for boundary in self.boundaries}) != len(self.boundaries):
            raise ValueError("duplicate boundary identifier")
        for boundary in self.boundaries:
            if len({case.threat_id for case in boundary.threats}) != len(boundary.threats):
                raise ValueError("duplicate threat identifier within boundary")
            if not set(boundary.asset_ids).issubset(asset_ids):
                raise ValueError("boundary references unknown asset")
            if not boundary.evidence and not boundary.unknowns:
                raise ValueError("boundary requires evidence or an explicit unknown")
        return self
