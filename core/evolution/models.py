"""Domain contracts for Factory Self-Evolution (HF-25).

Defines typed Pydantic v2 models for evolution triggers, targets, proposals,
holdout evaluation results, and rollback snapshots.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class EvolutionTrigger(str, Enum):
    """Event or observation that triggered a proposed self-evolution."""
    FAIL_REPEATED = "fail_repeated"
    RULES_DRIFT = "rules_drift"
    RCA_DISCOVERY = "rca_discovery"
    BENCHMARK_SHIFT = "benchmark_shift"
    MANUAL_PROPOSAL = "manual_proposal"


class EvolutionTarget(str, Enum):
    """Category of artifact that is targeted for evolutionary mutation."""
    SKILL_INSTRUCTION = "skill_instruction"
    CONTEXT_RULE = "context_rule"
    ARCHETYPE_TEMPLATE = "archetype_template"
    ROUTING_CONFIG = "routing_config"


class EvolutionStatus(str, Enum):
    """Lifecycle status of an evolution proposal."""
    PROPOSED = "proposed"
    EVALUATING = "evaluating"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


class SecurityViolationError(Exception):
    """Raised when an evolution candidate attempts to modify verifiers or protected governance files."""


class EvolutionProposal(BaseModel):
    """Specifies a proposed evolutionary patch to a skill, rule, or template."""
    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, description="Unique identifier for the proposal")
    target_kind: EvolutionTarget
    target_path: str = Field(min_length=1, description="Relative path in repo to the file being updated")
    trigger: EvolutionTrigger
    patch_content: str = Field(min_length=1, description="Target complete new text or patch chunk")
    rationale: str = Field(min_length=1, description="Root-cause evidence or necessity justification")
    status: EvolutionStatus = EvolutionStatus.PROPOSED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    promoted_at: datetime | None = None
    retired_at: datetime | None = None
    eval_version: str = Field(default="v1.0", description="Evaluation harness version used for verification")
    metadata: dict[str, Any] = Field(default_factory=dict)


class HoldoutEvaluationResult(BaseModel):
    """Structured report produced by testing a candidate in the isolated sandbox."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    proposal_id: str
    passed: bool
    discovered_steps: int = 0
    passed_steps: int = 0
    tampering_detected: bool = False
    protected_files_touched: list[str] = Field(default_factory=list)
    evidence_hash: str = ""
    log_summary: str = ""
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RollbackSnapshot(BaseModel):
    """Snapshot of target file contents before promotion, enabling deterministic atomic rollback."""
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    proposal_id: str
    target_path: str
    previous_content: str
    previous_hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EvolutionReport(BaseModel):
    """Consolidated state report of the self-evolution subsystem."""
    model_config = ConfigDict(extra="forbid")

    total_proposals: int
    active_promotions: int
    rejected_count: int
    proposals: list[EvolutionProposal] = Field(default_factory=list)
    snapshots: list[RollbackSnapshot] = Field(default_factory=list)
