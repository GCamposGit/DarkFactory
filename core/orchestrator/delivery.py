"""Fail-closed delivery policy and idempotent merge queue for DF-20."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.integrations.github import PullRequestSnapshot


class DeliveryRisk(str, Enum):
    """Autonomy classes from the Dark Factory policy."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class DeliveryStatus(str, Enum):
    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    MANUAL_REVIEW = "manual_review"


class QueueStatus(str, Enum):
    QUEUED = "queued"


class DeliveryPolicyError(RuntimeError):
    """Base error for delivery policy or queue failures."""


class DeliveryBlockedError(DeliveryPolicyError):
    """Raised when a blocked decision is submitted to the merge queue."""

    def __init__(self, decision: "DeliveryDecision") -> None:
        self.decision = decision
        super().__init__(decision.reason)


class IdempotencyConflictError(DeliveryPolicyError):
    """The same idempotency key was reused for a different delivery request."""


class DeliveryStoreError(DeliveryPolicyError):
    """The durable merge queue cannot be loaded or persisted safely."""


_SHA_CHARS = frozenset("0123456789abcdef")


def _normalize_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not 7 <= len(normalized) <= 64 or any(char not in _SHA_CHARS for char in normalized):
        raise ValueError("must be a 7-64 character hexadecimal Git SHA")
    return normalized


class DeliveryRequest(BaseModel):
    """Immutable intent to evaluate one exact pull-request head."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=3)
    pull_request_number: int = Field(gt=0)
    base_sha: str
    candidate_sha: str
    risk_class: DeliveryRisk
    required_checks: tuple[str, ...] = ("trusted-pr-policy", "pr-validation")
    idempotency_key: str = Field(min_length=1)

    _normalize_base_sha = field_validator("base_sha")(_normalize_sha)
    _normalize_candidate_sha = field_validator("candidate_sha")(_normalize_sha)

    @field_validator("task_id", "repository", "idempotency_key")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if value.count("/") != 1 or any(not part for part in value.split("/")):
            raise ValueError("repository must use the owner/name form")
        return value

    @field_validator("required_checks")
    @classmethod
    def normalize_checks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for check in value:
            item = check.strip()
            if item and item not in normalized:
                normalized.append(item)
        if not normalized:
            raise ValueError("required_checks must contain at least one check")
        return tuple(normalized)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class DeliveryDecision(BaseModel):
    """Auditable result of evaluating a request against one PR snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DeliveryStatus
    reason: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=64, max_length=64)
    candidate_sha: str
    observed_head_sha: str
    passed_checks: tuple[str, ...] = ()
    missing_checks: tuple[str, ...] = ()
    stale_checks: tuple[str, ...] = ()
    failed_checks: tuple[str, ...] = ()
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _normalize_candidate_sha = field_validator("candidate_sha", "observed_head_sha")(_normalize_sha)

    @property
    def eligible(self) -> bool:
        return self.status is DeliveryStatus.ELIGIBLE


class DeliveryPolicy:
    """Evaluate whether a PR may enter the automated merge queue."""

    def __init__(self, *, autonomous_risk_classes: set[DeliveryRisk] | None = None) -> None:
        self.autonomous_risk_classes = frozenset(
            autonomous_risk_classes or {DeliveryRisk.A, DeliveryRisk.B}
        )

    def evaluate(
        self,
        request: DeliveryRequest,
        snapshot: PullRequestSnapshot,
    ) -> DeliveryDecision:
        """Return a decision; no blocked or stale state is treated as success."""
        checks_by_name = {check.name: check for check in snapshot.checks}
        missing = tuple(name for name in request.required_checks if name not in checks_by_name)
        stale = tuple(
            name
            for name in request.required_checks
            if name in checks_by_name and checks_by_name[name].head_sha != request.candidate_sha
        )
        failed = tuple(
            name
            for name in request.required_checks
            if name in checks_by_name
            and name not in stale
            and not checks_by_name[name].passed
        )
        passed = tuple(
            name
            for name in request.required_checks
            if name in checks_by_name and name not in stale and checks_by_name[name].passed
        )

        blockers: list[str] = []
        if snapshot.repository != request.repository:
            blockers.append("repository_mismatch")
        if snapshot.number != request.pull_request_number:
            blockers.append("pull_request_mismatch")
        if snapshot.base_sha != request.base_sha:
            blockers.append("base_sha_mismatch")
        if snapshot.head_sha != request.candidate_sha:
            blockers.append("candidate_sha_mismatch")
        if snapshot.state.lower() != "open":
            blockers.append("pull_request_not_open")
        if snapshot.draft:
            blockers.append("draft_pull_request")
        if snapshot.mergeable is not True:
            blockers.append("mergeability_not_confirmed")
        if missing:
            blockers.append(f"missing_checks={','.join(missing)}")
        if stale:
            blockers.append(f"stale_checks={','.join(stale)}")
        if failed:
            blockers.append(f"failed_checks={','.join(failed)}")

        if blockers:
            status = DeliveryStatus.BLOCKED
            reason = "Delivery blocked: " + "; ".join(blockers)
        elif request.risk_class not in self.autonomous_risk_classes:
            status = DeliveryStatus.MANUAL_REVIEW
            reason = f"Risk class {request.risk_class.value} requires manual review"
        else:
            status = DeliveryStatus.ELIGIBLE
            reason = "All required checks passed on the exact candidate SHA"

        return DeliveryDecision(
            status=status,
            reason=reason,
            request_fingerprint=request.fingerprint(),
            candidate_sha=request.candidate_sha,
            observed_head_sha=snapshot.head_sha,
            passed_checks=passed,
            missing_checks=missing,
            stale_checks=stale,
            failed_checks=failed,
        )


class MergeQueueEntry(BaseModel):
    """Durable queue record; replay returns the same logical entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    request_fingerprint: str = Field(min_length=64, max_length=64)
    task_id: str = Field(min_length=1)
    repository: str = Field(min_length=3)
    pull_request_number: int = Field(gt=0)
    candidate_sha: str
    status: QueueStatus = QueueStatus.QUEUED
    replayed: bool = False
    replay_count: int = Field(default=0, ge=0)
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    _normalize_candidate_sha = field_validator("candidate_sha")(_normalize_sha)


class MergeQueue:
    """Idempotent merge queue with optional atomic JSON persistence."""

    def __init__(self, storage_path: Path | str | None = None) -> None:
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._entries: dict[str, MergeQueueEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.storage_path is None or not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
            entries = raw.get("entries", [])
            if not isinstance(entries, list):
                raise ValueError("entries must be a list")
            loaded: dict[str, MergeQueueEntry] = {}
            for value in entries:
                item = MergeQueueEntry.model_validate(value)
                if item.idempotency_key in loaded:
                    raise ValueError("duplicate idempotency key")
                loaded[item.idempotency_key] = item
            self._entries = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise DeliveryStoreError("merge queue storage is invalid") from exc

    def _save(self) -> None:
        if self.storage_path is None:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1",
            "entries": [entry.model_dump(mode="json") for entry in self._entries.values()],
        }
        temporary = self.storage_path.with_name(f"{self.storage_path.name}.tmp.{os.getpid()}")
        try:
            temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.storage_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise DeliveryStoreError("merge queue persistence failed") from exc

    def enqueue(self, request: DeliveryRequest, decision: DeliveryDecision) -> MergeQueueEntry:
        """Queue only eligible decisions and make retries idempotent."""
        fingerprint = request.fingerprint()
        if decision.request_fingerprint != fingerprint:
            raise DeliveryPolicyError("decision does not belong to the delivery request")
        if not decision.eligible:
            raise DeliveryBlockedError(decision)

        existing = self._entries.get(request.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key {request.idempotency_key!r} was reused for a different request"
                )
            replay = existing.model_copy(update={
                "replayed": True,
                "replay_count": existing.replay_count + 1,
            })
            self._entries[request.idempotency_key] = replay
            try:
                self._save()
            except Exception:
                self._entries[request.idempotency_key] = existing
                raise
            return replay

        queue_id = "mq_" + hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:20]
        entry = MergeQueueEntry(
            queue_id=queue_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            task_id=request.task_id,
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            candidate_sha=request.candidate_sha,
        )
        self._entries[request.idempotency_key] = entry
        try:
            self._save()
        except Exception:
            self._entries.pop(request.idempotency_key, None)
            raise
        return entry

    def get(self, idempotency_key: str) -> MergeQueueEntry | None:
        return self._entries.get(idempotency_key)

    def entries(self) -> tuple[MergeQueueEntry, ...]:
        return tuple(self._entries.values())


__all__ = [
    "DeliveryBlockedError",
    "DeliveryDecision",
    "DeliveryPolicy",
    "DeliveryPolicyError",
    "DeliveryRequest",
    "DeliveryRisk",
    "DeliveryStatus",
    "DeliveryStoreError",
    "IdempotencyConflictError",
    "MergeQueue",
    "MergeQueueEntry",
    "QueueStatus",
]
