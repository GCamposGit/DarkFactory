#!/usr/bin/env python3
"""Deterministic lifecycle transitions for autonomous factory tasks."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.benchmarks.fetcher import ensure_daily_benchmark
from core.orchestrator.models import MergeEvidence, StateHistoryEntry, StateLedger, TaskRecord, TaskStatus

STATE_FILE = Path(".factory/state.json")
CONTROL_FIELDS = frozenset(
    {"id", "status", "created_at", "updated_at", "history", "version", "merge_evidence"}
)
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.UNSET: frozenset({TaskStatus.TRIAGED}),
    TaskStatus.TRIAGED: frozenset({TaskStatus.PLANNED, TaskStatus.FAILED}),
    TaskStatus.PLANNED: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.FAILED}),
    TaskStatus.IMPLEMENTING: frozenset({TaskStatus.VALIDATING, TaskStatus.NEEDS_FIX, TaskStatus.FAILED}),
    TaskStatus.VALIDATING: frozenset({TaskStatus.REVIEWING, TaskStatus.NEEDS_FIX, TaskStatus.FAILED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.READY_TO_MERGE, TaskStatus.NEEDS_FIX, TaskStatus.FAILED}),
    TaskStatus.NEEDS_FIX: frozenset({TaskStatus.IMPLEMENTING, TaskStatus.FAILED}),
    TaskStatus.READY_TO_MERGE: frozenset({TaskStatus.MERGED, TaskStatus.NEEDS_FIX, TaskStatus.FAILED}),
    TaskStatus.MERGED: frozenset(),
    TaskStatus.FAILED: frozenset(),
}


class StateTransitionError(ValueError):
    """A requested transition violates the state-machine contract."""


class StatePersistenceError(RuntimeError):
    """The durable state ledger cannot be read or validated."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _migrate_legacy_ledger(raw: Mapping[str, Any]) -> StateLedger:
    """Read the pre-DF-01 shape without preserving unsafe control metadata."""
    now = _utc_now()
    migrated: dict[str, TaskRecord] = {}
    raw_tasks = raw.get("tasks", {})
    if not isinstance(raw_tasks, dict):
        raise StatePersistenceError("State ledger tasks must be an object")
    for task_id, value in raw_tasks.items():
        if not isinstance(value, dict):
            raise StatePersistenceError(f"Task {task_id!r} must be an object")
        status = TaskStatus(value.get("status", TaskStatus.UNSET.value))
        metadata = {
            key: item
            for key, item in value.items()
            if key not in CONTROL_FIELDS and key not in {"updated_at", "created_at"}
        }
        history = [
            StateHistoryEntry(status=TaskStatus(item["status"]), timestamp=now)
            for item in value.get("history", [])
            if isinstance(item, dict) and "status" in item
        ]
        migrated[str(task_id)] = TaskRecord(
            id=str(value.get("id", task_id)),
            status=status,
            created_at=now,
            updated_at=now,
            metadata=metadata,
            history=history,
        )
    return StateLedger(tasks=migrated, history=list(raw.get("history", [])))


def _load_ledger() -> StateLedger:
    path = Path(STATE_FILE)
    if not path.exists():
        return StateLedger()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatePersistenceError(f"Failed to read state ledger {path}: {exc}") from exc
    try:
        return StateLedger.model_validate(raw)
    except ValidationError:
        try:
            return _migrate_legacy_ledger(raw)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise StatePersistenceError(f"Invalid state ledger {path}: {exc}") from exc


def load_state() -> dict[str, Any]:
    """Load and validate the state ledger in a JSON-compatible shape."""
    return _load_ledger().model_dump(mode="json")


def save_state(state: Mapping[str, Any] | StateLedger) -> None:
    """Validate and atomically persist the state ledger."""
    try:
        ledger = state if isinstance(state, StateLedger) else StateLedger.model_validate(state)
    except ValidationError as exc:
        raise StatePersistenceError(f"Refusing to persist invalid state: {exc}") from exc
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


def update_task_status(
    task_id: str,
    status: TaskStatus,
    metadata: Mapping[str, Any] | None = None,
    *,
    merge_evidence: MergeEvidence | Mapping[str, Any] | None = None,
) -> TaskRecord:
    """Apply one legal transition and persist only after full validation."""
    requested_metadata = dict(metadata or {})
    reserved = sorted(CONTROL_FIELDS.intersection(requested_metadata))
    if reserved:
        raise StateTransitionError(f"Metadata contains reserved control fields: {', '.join(reserved)}")
    ledger = _load_ledger()
    existing = ledger.tasks.get(task_id)
    current_status = existing.status if existing else TaskStatus.UNSET
    if status is current_status and existing is not None:
        return existing
    if status not in ALLOWED_TRANSITIONS[current_status]:
        raise StateTransitionError(f"Illegal task transition {current_status.value} -> {status.value}")
    evidence: MergeEvidence | None = None
    if merge_evidence is not None:
        try:
            evidence = MergeEvidence.model_validate(merge_evidence)
        except ValidationError as exc:
            raise StateTransitionError(f"Invalid merge evidence: {exc}") from exc
    if status is TaskStatus.MERGED and evidence is None:
        raise StateTransitionError("MERGED requires commit-linked merge evidence")
    if status is not TaskStatus.MERGED and evidence is not None:
        raise StateTransitionError("Merge evidence is only valid for MERGED transitions")
    now = _utc_now()
    history = list(existing.history) if existing else []
    history.append(StateHistoryEntry(status=status, timestamp=now))
    merged_metadata = dict(existing.metadata) if existing else {}
    merged_metadata.update(requested_metadata)
    record = TaskRecord(
        version=(existing.version if existing else 1),
        id=task_id,
        status=status,
        created_at=(existing.created_at if existing else now),
        updated_at=now,
        metadata=merged_metadata,
        history=history,
        merge_evidence=evidence or (existing.merge_evidence if existing else None),
    )
    ledger.tasks[task_id] = record
    ledger.history.append(
        {"task_id": task_id, "from": current_status.value, "to": status.value, "timestamp": now.isoformat()}
    )
    save_state(ledger)
    print(f"[STATE] Task '{task_id}' -> {status.value}")
    return record


def get_next_dispatchable_task() -> dict[str, Any] | None:
    """Return the highest-priority in-flight task."""
    try:
        ensure_daily_benchmark()
    except Exception as exc:
        print(f"[WARN] Daily benchmark check skipped: {exc}")
    ledger = _load_ledger()
    priorities = (
        {TaskStatus.NEEDS_FIX},
        {TaskStatus.VALIDATING, TaskStatus.REVIEWING},
        {TaskStatus.PLANNED},
        {TaskStatus.TRIAGED},
    )
    for statuses in priorities:
        for task in ledger.tasks.values():
            if task.status in statuses:
                return task.model_dump(mode="json")
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "next":
        selected = get_next_dispatchable_task()
        print(json.dumps(selected, indent=2) if selected else "No tasks pending.")
    else:
        print(json.dumps(load_state(), indent=2))
