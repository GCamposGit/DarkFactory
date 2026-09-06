from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.orchestrator import state
from core.orchestrator.models import MergeEvidence
from core.orchestrator.state import StateTransitionError, TaskStatus


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.json"
    monkeypatch.setattr(state, "STATE_FILE", path)
    return path


def test_illegal_transition_is_rejected_without_persisting(state_file: Path) -> None:
    with pytest.raises(StateTransitionError, match="UNSET -> IMPLEMENTING"):
        state.update_task_status("DF-01", TaskStatus.IMPLEMENTING)

    assert not state_file.exists()


@pytest.mark.parametrize(
    "reserved_key",
    ["id", "status", "created_at", "updated_at", "history", "version", "merge_evidence"],
)
def test_metadata_cannot_overwrite_control_fields(
    state_file: Path, reserved_key: str
) -> None:
    with pytest.raises(StateTransitionError, match="reserved control fields"):
        state.update_task_status(
            "DF-01", TaskStatus.TRIAGED, metadata={reserved_key: "forged"}
        )

    assert not state_file.exists()


def test_merged_requires_commit_linked_evidence(state_file: Path) -> None:
    transitions = [
        TaskStatus.TRIAGED,
        TaskStatus.PLANNED,
        TaskStatus.IMPLEMENTING,
        TaskStatus.VALIDATING,
        TaskStatus.REVIEWING,
        TaskStatus.READY_TO_MERGE,
    ]
    for next_status in transitions:
        state.update_task_status("DF-01", next_status)

    with pytest.raises(StateTransitionError, match="merge evidence"):
        state.update_task_status("DF-01", TaskStatus.MERGED)

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["tasks"]["DF-01"]["status"] == "READY_TO_MERGE"

    evidence = MergeEvidence(
        provider="local_git",
        candidate_sha="a" * 40,
        merge_commit_sha="b" * 40,
        verification_id="verification-01",
    )
    record = state.update_task_status(
        "DF-01", TaskStatus.MERGED, merge_evidence=evidence
    )

    assert record.status is TaskStatus.MERGED
    assert record.merge_evidence == evidence
    assert record.created_at is not None
    assert record.updated_at is not None
    assert all(item.timestamp is not None for item in record.history)


def test_dispatch_priority_is_preserved(state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "ensure_daily_benchmark", lambda: None)
    state.update_task_status("planned", TaskStatus.TRIAGED)
    state.update_task_status("planned", TaskStatus.PLANNED)
    state.update_task_status("fix", TaskStatus.TRIAGED)
    state.update_task_status("fix", TaskStatus.PLANNED)
    state.update_task_status("fix", TaskStatus.IMPLEMENTING)
    state.update_task_status("fix", TaskStatus.NEEDS_FIX)

    selected = state.get_next_dispatchable_task()

    assert selected is not None
    assert selected["id"] == "fix"

