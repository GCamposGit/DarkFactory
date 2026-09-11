"""Independent, durable effect oracle for the HF-02 laboratory.

The store is deliberately outside the process running an adapter.  A driver
cannot make a successful effect disappear by returning a successful boolean.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from pydantic import Field, field_validator

from spikes.runtime_choice.contracts import (
    FaultPoint,
    ScenarioSpec,
    StrictLabModel,
    _ensure_json_value,
    _non_blank,
    _safe_id,
)


LOGGER = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 16 * 1024
CATALOG_PATH = Path(__file__).with_name("scenarios.json")


class EffectStoreError(RuntimeError):
    """Base error for the independent effect oracle."""


class EffectConflictError(EffectStoreError):
    """An operation key was reused with different effect identity."""


class ApprovalConflictError(EffectStoreError):
    """A decision id was reused with a different release digest or choice."""


class ApprovalSubjectNotBoundError(EffectStoreError):
    """An approval was submitted before its workflow subject was registered."""


class ApprovalSubjectConflictError(EffectStoreError):
    """A workflow subject was bound to a different release digest."""


class EffectRequest(StrictLabModel):
    """Validated payload accepted by ``POST /effects``."""

    operation_key: str
    workflow_id: str
    release_digest: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    fault_point: FaultPoint | None = None

    @field_validator("operation_key", "workflow_id", "release_digest")
    @classmethod
    def _safe_strings(cls, value: str, info: Any) -> str:
        return _safe_id(value, field_name=info.field_name)

    @field_validator("payload_hash")
    @classmethod
    def _payload_hash_is_sha256(cls, value: str) -> str:
        normalized = _non_blank(value, field_name="payload_hash").lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("payload_hash must be a SHA-256 hex digest")
        return normalized

    @field_validator("fault_point", mode="before")
    @classmethod
    def _fault_point_from_json(cls, value: object) -> FaultPoint | None:
        if value is None or isinstance(value, FaultPoint):
            return value
        return FaultPoint(value)


class EffectReceipt(StrictLabModel):
    """Stable receipt returned for an idempotent operation key."""

    operation_key: str
    receipt_id: str
    workflow_id: str
    release_digest: str
    payload_hash: str
    created_at: datetime

    @field_validator("operation_key", "receipt_id", "workflow_id", "release_digest")
    @classmethod
    def _receipt_strings_are_safe(cls, value: str, info: Any) -> str:
        return _safe_id(value, field_name=info.field_name)


class EffectCommit(StrictLabModel):
    """Store result plus the simulated lost-response signal."""

    receipt: EffectReceipt
    created: bool
    disconnect_after_commit: bool = False


class ObservationRequest(StrictLabModel):
    """An independently persisted invocation/barrier observation."""

    workflow_id: str
    kind: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("workflow_id", "kind")
    @classmethod
    def _observation_strings_are_safe(cls, value: str, info: Any) -> str:
        return _safe_id(value, field_name=info.field_name)

    @field_validator("step_id")
    @classmethod
    def _step_id_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _safe_id(value, field_name="step_id")

    @field_validator("details")
    @classmethod
    def _details_are_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _ensure_json_value(value, path="details")
        return value


class ObservationRecord(StrictLabModel):
    sequence: int = Field(ge=1)
    workflow_id: str
    kind: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalRequest(StrictLabModel):
    """Application-level approval filter used by the scenarios."""

    decision_id: str
    workflow_id: str
    release_digest: str
    choice: str

    @field_validator("decision_id", "workflow_id", "release_digest", "choice")
    @classmethod
    def _approval_strings_are_safe(cls, value: str, info: Any) -> str:
        return _safe_id(value, field_name=info.field_name)


class ApprovalRecord(ApprovalRequest):
    created_at: datetime


class NativeEffectStore:
    """SQLite store owned by the laboratory effect service."""

    def __init__(self, lab_root: Path | str) -> None:
        self.lab_root = Path(lab_root).resolve()
        self.effects_dir = self.lab_root / "effects"
        self.effects_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.effects_dir / "effects.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS effects(
                    operation_key TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL UNIQUE,
                    workflow_id TEXT NOT NULL,
                    release_digest TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS disconnect_faults(
                    operation_key TEXT PRIMARY KEY,
                    armed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations(
                    workflow_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    step_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workflow_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS approvals(
                    decision_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    release_digest TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_subjects(
                    workflow_id TEXT PRIMARY KEY,
                    payload_digest TEXT NOT NULL,
                    bound_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> EffectReceipt:
        return EffectReceipt(
            operation_key=row["operation_key"],
            receipt_id=row["receipt_id"],
            workflow_id=row["workflow_id"],
            release_digest=row["release_digest"],
            payload_hash=row["payload_hash"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def arm_disconnect_once(self, operation_key: str) -> None:
        key = _safe_id(operation_key, field_name="operation_key")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO disconnect_faults(operation_key, armed_at) VALUES (?, ?)",
                (key, self._iso(self._now())),
            )

    def apply_effect(
        self,
        request: EffectRequest | dict[str, Any],
        *,
        disconnect_after_commit: bool = False,
    ) -> EffectCommit:
        normalized = request if isinstance(request, EffectRequest) else EffectRequest.model_validate(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM effects WHERE operation_key = ?", (normalized.operation_key,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["workflow_id"] != normalized.workflow_id
                    or existing["release_digest"] != normalized.release_digest
                    or existing["payload_hash"] != normalized.payload_hash
                ):
                    raise EffectConflictError(
                        f"operation key {normalized.operation_key!r} conflicts with its receipt"
                    )
                return EffectCommit(receipt=self._receipt_from_row(existing), created=False)

            now = self._now()
            receipt_id = f"receipt-{hashlib.sha256(normalized.operation_key.encode()).hexdigest()[:24]}"
            connection.execute(
                """
                INSERT INTO effects(
                    operation_key, receipt_id, workflow_id, release_digest,
                    payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.operation_key,
                    receipt_id,
                    normalized.workflow_id,
                    normalized.release_digest,
                    normalized.payload_hash,
                    self._iso(now),
                ),
            )

            if disconnect_after_commit or normalized.fault_point is FaultPoint.COMMIT_THEN_DISCONNECT_ONCE:
                connection.execute(
                    "INSERT OR IGNORE INTO disconnect_faults(operation_key, armed_at) VALUES (?, ?)",
                    (normalized.operation_key, self._iso(now)),
                )
            fault = connection.execute(
                "DELETE FROM disconnect_faults WHERE operation_key = ?",
                (normalized.operation_key,),
            ).rowcount
            connection.commit()
            row = connection.execute(
                "SELECT * FROM effects WHERE operation_key = ?", (normalized.operation_key,)
            ).fetchone()
            assert row is not None
            return EffectCommit(
                receipt=self._receipt_from_row(row),
                created=True,
                disconnect_after_commit=bool(fault),
            )

    def get_effect(self, operation_key: str) -> EffectReceipt | None:
        key = _safe_id(operation_key, field_name="operation_key")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM effects WHERE operation_key = ?", (key,)).fetchone()
        return self._receipt_from_row(row) if row is not None else None

    def effect_count(self, *, workflow_id: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM effects"
        params: tuple[str, ...] = ()
        if workflow_id is not None:
            query += " WHERE workflow_id = ?"
            params = (_safe_id(workflow_id, field_name="workflow_id"),)
        with self._connect() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def record_observation(
        self,
        request: ObservationRequest | dict[str, Any],
    ) -> ObservationRecord:
        normalized = request if isinstance(request, ObservationRequest) else ObservationRequest.model_validate(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM observations WHERE workflow_id = ?",
                (normalized.workflow_id,),
            ).fetchone()
            sequence = int(row[0])
            now = self._now()
            connection.execute(
                """
                INSERT INTO observations(
                    workflow_id, sequence, kind, step_id, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized.workflow_id,
                    sequence,
                    normalized.kind,
                    normalized.step_id,
                    json.dumps(normalized.details, sort_keys=True, separators=(",", ":")),
                    self._iso(now),
                ),
            )
            connection.commit()
        return ObservationRecord(
            sequence=sequence,
            workflow_id=normalized.workflow_id,
            kind=normalized.kind,
            step_id=normalized.step_id,
            details=normalized.details,
            created_at=now,
        )

    def observations(self, workflow_id: str) -> list[ObservationRecord]:
        normalized = _safe_id(workflow_id, field_name="workflow_id")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM observations WHERE workflow_id = ? ORDER BY sequence",
                (normalized,),
            ).fetchall()
        return [
            ObservationRecord(
                sequence=row["sequence"],
                workflow_id=row["workflow_id"],
                kind=row["kind"],
                step_id=row["step_id"],
                details=json.loads(row["details_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def record_approval(
        self,
        request: ApprovalRequest | dict[str, Any],
    ) -> ApprovalRecord:
        normalized = request if isinstance(request, ApprovalRequest) else ApprovalRequest.model_validate(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            subject = connection.execute(
                "SELECT payload_digest FROM approval_subjects WHERE workflow_id = ?",
                (normalized.workflow_id,),
            ).fetchone()
            if subject is None:
                raise ApprovalSubjectNotBoundError(
                    f"workflow {normalized.workflow_id!r} has no approval subject"
                )
            if subject["payload_digest"] != normalized.release_digest:
                raise ApprovalSubjectConflictError(
                    f"workflow {normalized.workflow_id!r} is bound to another release"
                )
            existing = connection.execute(
                "SELECT * FROM approvals WHERE decision_id = ?", (normalized.decision_id,)
            ).fetchone()
            if existing is not None:
                if tuple(existing[field] for field in ("workflow_id", "release_digest", "choice")) != (
                    normalized.workflow_id,
                    normalized.release_digest,
                    normalized.choice,
                ):
                    raise ApprovalConflictError(
                        f"decision id {normalized.decision_id!r} conflicts with its receipt"
                    )
                return ApprovalRecord(
                    decision_id=existing["decision_id"],
                    workflow_id=existing["workflow_id"],
                    release_digest=existing["release_digest"],
                    choice=existing["choice"],
                    created_at=datetime.fromisoformat(existing["created_at"]),
                )
            now = self._now()
            connection.execute(
                "INSERT INTO approvals(decision_id, workflow_id, release_digest, choice, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    normalized.decision_id,
                    normalized.workflow_id,
                    normalized.release_digest,
                    normalized.choice,
                    self._iso(now),
                ),
            )
            connection.commit()
        return ApprovalRecord(**normalized.model_dump(), created_at=now)

    def bind_approval_subject(self, workflow_id: str, payload_digest: str) -> None:
        """Bind a workflow to one release before any approval can be delivered.

        This method is intentionally available on the controller-owned store,
        not as an HTTP endpoint.  The candidate can submit an approval, but it
        cannot redefine the subject against which that approval is checked.
        """

        normalized_workflow = _safe_id(workflow_id, field_name="workflow_id")
        normalized_digest = _safe_id(payload_digest, field_name="payload_digest")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_digest FROM approval_subjects WHERE workflow_id = ?",
                (normalized_workflow,),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != normalized_digest:
                    raise ApprovalSubjectConflictError(
                        f"workflow {normalized_workflow!r} is already bound to another release"
                    )
                return
            connection.execute(
                "INSERT INTO approval_subjects(workflow_id, payload_digest, bound_at) VALUES (?, ?, ?)",
                (normalized_workflow, normalized_digest, self._iso(self._now())),
            )
            connection.commit()


EXPECTED_SCENARIO_CATALOG_SHA256 = "daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8"


def load_scenario_catalog(
    path: Path | str = CATALOG_PATH,
    *,
    expected_digest: str | None = EXPECTED_SCENARIO_CATALOG_SHA256,
) -> list[ScenarioSpec]:
    """Load the frozen catalogue and reject duplicate or missing IDs.

    Verifies the exact SHA-256 digest against the approved hash to prevent
    unauthorized mutations of expected scenario outcomes.
    """

    catalog_path = Path(path)
    actual_hash = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    if expected_digest is not None and actual_hash != expected_digest:
        raise ValueError(
            f"scenario catalogue digest mismatch: expected {expected_digest}, got {actual_hash}"
        )
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("scenario catalogue must be a JSON array")
    scenarios = [ScenarioSpec.model_validate(item) for item in raw]
    ids = [scenario.scenario_id for scenario in scenarios]
    if len(ids) != len(set(ids)) or ids != [f"R{index:02d}" for index in range(1, 13)]:
        raise ValueError("scenario catalogue must contain R01-R12 exactly once and in order")
    return scenarios


def scenario_catalog_hash(path: Path | str = CATALOG_PATH) -> str:
    """Return the SHA-256 of the exact UTF-8 catalogue bytes."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


__all__ = [
    "ApprovalConflictError",
    "ApprovalRecord",
    "ApprovalRequest",
    "ApprovalSubjectConflictError",
    "ApprovalSubjectNotBoundError",
    "EffectCommit",
    "EffectConflictError",
    "EffectReceipt",
    "EffectRequest",
    "EffectStoreError",
    "EXPECTED_SCENARIO_CATALOG_SHA256",
    "MAX_REQUEST_BYTES",
    "NativeEffectStore",
    "ObservationRecord",
    "ObservationRequest",
    "load_scenario_catalog",
    "scenario_catalog_hash",
]
