"""Typed contracts for the deterministic orchestrator state ledger."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskStatus(str, Enum):
    UNSET = "UNSET"
    TRIAGED = "TRIAGED"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    NEEDS_FIX = "NEEDS_FIX"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    FAILED = "FAILED"


class MergeEvidence(BaseModel):
    """Evidence emitted by the system that performed the merge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["github", "gitlab", "local_git", "manual"]
    candidate_sha: str
    merge_commit_sha: str
    verification_id: str = Field(min_length=1)

    @field_validator("candidate_sha", "merge_commit_sha")
    @classmethod
    def validate_git_sha(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not 7 <= len(normalized) <= 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("must be a 7-64 character hexadecimal Git SHA")
        return normalized


class StateHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    timestamp: datetime


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    id: str = Field(min_length=1)
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    history: list[StateHistoryEntry] = Field(default_factory=list)
    merge_evidence: MergeEvidence | None = None


class StateLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    tasks: dict[str, TaskRecord] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)
