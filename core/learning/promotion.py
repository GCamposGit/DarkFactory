"""Learning candidate contract and promotion engine for DarkFac (DF-19).

Enforces strict evaluation gates and empirical supporting run requirements before
any learned rule or preference candidate can be promoted to ACTIVE status.
Provides clean atomic rollback for active candidates.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from core.learning.models import PolicyOrigin, PolicyStatus

DEFAULT_CANDIDATES_PATH = Path(".factory") / "learning" / "candidates.json"


class PromotionDeniedError(Exception):
    """Raised when a learning candidate fails deterministic promotion criteria."""


class LearningCandidate(BaseModel):
    """Domain contract for a candidate rule, convention, or RCA patch.

    Conforms to Section 3 (Contratos mínimos) of DEVELOPMENT_PLAN_2026-09-05.md:
    rule_id, origin, scope, supporting_runs, eval_version, before_after, status, rollback_ref
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    origin: PolicyOrigin
    scope: str = Field(min_length=1)
    supporting_runs: list[str] = Field(default_factory=list)
    eval_version: str = Field(min_length=1)
    before_after: dict[str, Any] = Field(default_factory=dict)
    status: PolicyStatus = PolicyStatus.PROPOSED
    rollback_ref: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rule_content: str = Field(default="", description="The concrete preventative or behavioral rule text")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    promoted_at: datetime | None = None
    retired_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LearningPromotionEngine:
    """Manages the evaluation, promotion, persistence, and rollback of learning candidates."""

    def __init__(self, storage_path: Path | str | None = None) -> None:
        self.storage_path = Path(storage_path) if storage_path else DEFAULT_CANDIDATES_PATH
        self._candidates: dict[str, LearningCandidate] = {}
        self.load()

    def register_candidate(
        self,
        rule_id: str,
        origin: PolicyOrigin | str,
        scope: str,
        supporting_runs: list[str] | None = None,
        eval_version: str = "",
        before_after: dict[str, Any] | None = None,
        confidence: float = 1.0,
        rule_content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> LearningCandidate:
        """Register a new candidate in PROPOSED status."""
        normalized_id = rule_id.strip()
        if not normalized_id:
            raise ValueError("rule_id must not be blank")

        norm_origin = PolicyOrigin(origin) if isinstance(origin, str) else origin
        candidate = LearningCandidate(
            rule_id=normalized_id,
            origin=norm_origin,
            scope=scope.strip(),
            supporting_runs=list(supporting_runs or []),
            eval_version=eval_version.strip() or "unassigned",
            before_after=dict(before_after or {}),
            status=PolicyStatus.PROPOSED,
            confidence=confidence,
            rule_content=rule_content.strip(),
            metadata=dict(metadata or {}),
        )
        self._candidates[normalized_id] = candidate
        self.save()
        return candidate

    def record_eval_pass(
        self,
        rule_id: str,
        eval_version: str,
        test_output: str = "",
    ) -> LearningCandidate:
        """Record successful evaluation against a specific eval suite version."""
        candidate = self.get_candidate(rule_id)
        normalized_version = eval_version.strip()
        if not normalized_version:
            raise ValueError("eval_version must not be blank")
        if candidate.status is PolicyStatus.RETIRED:
            raise PromotionDeniedError(f"Retired candidate {rule_id!r} cannot be evaluated again")
        candidate.eval_version = normalized_version
        candidate.status = PolicyStatus.EVALUATED
        history = candidate.metadata.setdefault("eval_history", [])
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eval_version": normalized_version,
            "passed": True,
            "output_snippet": test_output[:500],
        })
        self.save()
        return candidate

    def record_eval_failure(
        self,
        rule_id: str,
        eval_version: str,
        error_message: str = "",
    ) -> LearningCandidate:
        """Record evaluation failure, keeping candidate in unpromoted status."""
        candidate = self.get_candidate(rule_id)
        normalized_version = eval_version.strip()
        if not normalized_version:
            raise ValueError("eval_version must not be blank")
        if candidate.status is PolicyStatus.RETIRED:
            raise PromotionDeniedError(f"Retired candidate {rule_id!r} cannot be evaluated again")
        was_active = candidate.status is PolicyStatus.ACTIVE
        candidate.eval_version = normalized_version
        candidate.promoted_at = None
        if was_active:
            candidate.status = PolicyStatus.RETIRED
            candidate.retired_at = datetime.now(timezone.utc)
            candidate.rollback_ref = (
                f"eval_failure_{rule_id}_{candidate.retired_at.strftime('%Y%m%d%H%M%S')}"
            )
        else:
            candidate.status = PolicyStatus.PROPOSED
        history = candidate.metadata.setdefault("eval_history", [])
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "eval_version": normalized_version,
            "passed": False,
            "error": error_message[:500],
        })
        if was_active:
            candidate.metadata["retirement_reason"] = "evaluation failed"
        self.save()
        return candidate

    def promote_candidate(
        self,
        rule_id: str,
        verified_eval_version: str,
    ) -> LearningCandidate:
        """Promote candidate to ACTIVE status after verifying all fail-closed gates."""
        candidate = self.get_candidate(rule_id)
        requested_version = verified_eval_version.strip()

        if candidate.status != PolicyStatus.EVALUATED:
            raise PromotionDeniedError(
                f"Candidate {rule_id!r} must be in EVALUATED status before promotion, got {candidate.status.value!r}"
            )

        if not candidate.supporting_runs or not all(run.strip() for run in candidate.supporting_runs):
            raise PromotionDeniedError(
                f"Candidate {rule_id!r} must have at least one supporting run to prove empirical validity"
            )

        if not requested_version or candidate.eval_version != requested_version:
            raise PromotionDeniedError(
                f"Candidate {rule_id!r} eval_version mismatch: evaluated on {candidate.eval_version!r}, "
                f"promotion requested for {verified_eval_version!r}"
            )

        eval_history = candidate.metadata.get("eval_history", [])
        if not any(
            isinstance(record, dict)
            and record.get("passed") is True
            and record.get("eval_version") == requested_version
            for record in eval_history
        ):
            raise PromotionDeniedError(
                f"Candidate {rule_id!r} has no recorded passing evaluation for {requested_version!r}"
            )

        candidate.status = PolicyStatus.ACTIVE
        candidate.promoted_at = datetime.now(timezone.utc)
        self.save()
        return candidate

    def rollback_candidate(
        self,
        rule_id: str,
        reason: str,
        rollback_ref: str | None = None,
    ) -> LearningCandidate:
        """Roll back an active rule to RETIRED status with explicit rollback reference."""
        candidate = self.get_candidate(rule_id)
        if candidate.status is not PolicyStatus.ACTIVE:
            raise PromotionDeniedError(
                f"Only ACTIVE candidates can be rolled back, got {candidate.status.value!r}"
            )
        if not reason.strip():
            raise ValueError("rollback reason must not be blank")

        original = candidate.model_copy(deep=True)
        retired_at = datetime.now(timezone.utc)
        candidate.status = PolicyStatus.RETIRED
        candidate.retired_at = retired_at
        candidate.rollback_ref = rollback_ref or f"revert_{rule_id}_{retired_at.strftime('%Y%m%d%H%M%S')}"
        candidate.metadata["retirement_reason"] = reason.strip()
        try:
            self.save()
        except Exception:
            self._candidates[rule_id] = original
            raise
        return candidate

    def get_candidate(self, rule_id: str) -> LearningCandidate:
        """Fetch candidate by id or raise KeyError."""
        if rule_id not in self._candidates:
            raise KeyError(f"Learning candidate {rule_id!r} not found")
        return self._candidates[rule_id]

    def get_active_candidates(self, scope: str | None = None) -> list[LearningCandidate]:
        """Return only active candidates, optionally filtered by scope."""
        active = [c for c in self._candidates.values() if c.status == PolicyStatus.ACTIVE]
        if not scope:
            return active

        normalized_scope = scope.strip().lower()
        matched: list[LearningCandidate] = []
        for c in active:
            c_scope = c.scope.strip().lower()
            if c_scope == normalized_scope or c_scope in normalized_scope or normalized_scope in c_scope:
                matched.append(c)
        return matched

    def list_candidates(self, status: PolicyStatus | None = None) -> list[LearningCandidate]:
        """List all candidates, optionally filtered by status."""
        if status is None:
            return list(self._candidates.values())
        return [c for c in self._candidates.values() if c.status == status]

    def load(self) -> None:
        """Load candidates from disk if storage file exists."""
        if not self.storage_path.exists():
            return
        try:
            with self.storage_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            candidates = {}
            for item in raw.get("candidates", []):
                candidate = LearningCandidate.model_validate(item)
                candidates[candidate.rule_id] = candidate
            self._candidates = candidates
        except Exception:
            # If file cannot be read or is empty, keep memory dictionary clean
            pass

    def save(self) -> None:
        """Persist candidates to disk atomically."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1",
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "candidates": [c.model_dump(mode="json") for c in self._candidates.values()],
        }
        target = self.storage_path
        temp_file = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        try:
            with temp_file.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            temp_file.replace(target)
        except Exception:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise


__all__ = [
    "DEFAULT_CANDIDATES_PATH",
    "LearningCandidate",
    "LearningPromotionEngine",
    "PromotionDeniedError",
]
