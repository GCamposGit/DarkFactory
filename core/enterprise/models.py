"""Domain contracts for Enterprise Profile On-Demand (HF-24).

Defines Pydantic v2 models for data residency, cryptographic audit chain,
role-based access, SLA metrics, and enterprise deployment verification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field


class DataResidencyMode(str, Enum):
    """Permitted geographic and isolation scope for data and inference."""
    LOCAL_ONLY = "local_only"
    EU_ONLY = "eu_only"
    HYBRID_ENCRYPTED = "hybrid_encrypted"
    GLOBAL_ALLOWED = "global_allowed"


class EnterpriseRole(str, Enum):
    """Actor roles governing access and authorization in enterprise scope."""
    OWNER = "owner"
    ENTERPRISE_ADMIN = "enterprise_admin"
    SECURITY_AUDITOR = "security_auditor"
    OPERATOR = "operator"


class EnterpriseProjectConfig(BaseModel):
    """Enterprise policy configuration attached to a specific managed project."""
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, description="Target project identifier")
    enabled: bool = Field(default=True, description="Whether enterprise hardening is active")
    residency_mode: DataResidencyMode = Field(
        default=DataResidencyMode.LOCAL_ONLY,
        description="Geographic/infrastructure residency boundary",
    )
    max_rpo_minutes: int = Field(default=60, ge=5, le=1440, description="Max acceptable data loss window in minutes")
    max_rto_minutes: int = Field(default=30, ge=1, le=720, description="Max acceptable recovery time in minutes")
    pii_sanitization_strict: bool = Field(default=True, description="Mask PII before any inference or external dispatch")
    require_owner_signoff: bool = Field(default=True, description="Strict human owner signoff required before production deploy")
    allowed_providers: list[str] = Field(
        default_factory=lambda: ["ollama_local"],
        description="Explicit allowlist of inference providers",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    """Cryptographically chained immutable audit event record."""
    model_config = ConfigDict(extra="forbid")

    event_id: str
    sequence: int
    actor_id: str
    project_id: str
    action: str
    resource: str
    payload_hash: str
    timestamp: str
    prev_hash: str
    event_hash: str


class AuditChainVerificationResult(BaseModel):
    """Verification verdict on cryptographic audit log integrity."""
    model_config = ConfigDict(extra="forbid")

    is_valid: bool
    total_events: int
    tampered_event_id: Optional[str] = None
    error_message: str = ""


class SLACheckResult(BaseModel):
    """SLA measurement for backup age (RPO) and recovery drill latency (RTO)."""
    model_config = ConfigDict(extra="forbid")

    project_id: str
    is_compliant: bool
    last_backup_age_minutes: float
    measured_rto_minutes: float
    violations: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EnterpriseDeployDecision(BaseModel):
    """Comprehensive policy evaluation decision for enterprise production deployment."""
    model_config = ConfigDict(extra="forbid")

    project_id: str
    target_environment: str
    approved: bool
    owner_signoff_verified: bool
    audit_chain_verified: bool
    residency_verified: bool
    sla_verified: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
