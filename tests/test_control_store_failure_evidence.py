"""A stage that does not succeed keeps its `evidence_refs` in the control store.

USR-62 pilot (07/10): `finish()` stored `evidence_refs` only for `success`, so a `failed`/`retry` stage
(`validate_exhausted`, `clean_validate_failed:<sha>`) reached the live board with no clue about the failing
tests. The refs now survive every outcome; an empty list never overwrites a stored value.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.workflow.control_contracts import IntakeCommand, RuntimeOwner, StageResult
from core.workflow.control_store import SQLiteControlStore

_T0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(
        db_path=tmp_path / "control.db", runtime_owner=RuntimeOwner.HF05_SQLITE.value, lease_duration_sec=45
    )


def _accept(store: SQLiteControlStore, external_id: str) -> None:
    store.accept(
        IntakeCommand(
            channel="cli",
            external_id=external_id,
            project_id="darkfac",
            payload={
                "title": "Evidence",
                "problem": "p",
                "journey": "j",
                "non_goals": ["n"],
                "criteria": ["c"],
            },
            mode="autonomous",
            policy_ref="policy-v1",
        ),
        _T0,
    )


def _stored_refs(tmp_path: Path) -> list[list[str]]:
    with sqlite3.connect(tmp_path / "control.db") as conn:
        rows = conn.execute("SELECT evidence_refs FROM jobs ORDER BY created_at, rowid").fetchall()
    return [json.loads(row[0]) for row in rows]


def test_failed_stage_persists_its_evidence_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _accept(store, "ev-failed")
    claim = store.claim("w1", ["economy", "coding"], _T0)
    assert claim is not None

    refs = ["validate_failed:tests/test_x.py::test_y", "validate_exit:1"]
    store.finish(claim, StageResult(outcome="failed", cause_code="validate_exhausted", evidence_refs=refs), _T0 + timedelta(seconds=5))

    assert _stored_refs(tmp_path)[0] == refs


def test_retry_stage_persists_its_evidence_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _accept(store, "ev-retry")
    claim = store.claim("w1", ["economy", "coding"], _T0)
    assert claim is not None

    refs = ["clean_validate_step:validate", "validate_failed:tests/test_x.py::test_y"]
    store.finish(claim, StageResult(outcome="retry", cause_code="retry:development", evidence_refs=refs), _T0 + timedelta(seconds=5))

    assert _stored_refs(tmp_path)[0] == refs


def test_waiting_human_stage_persists_its_evidence_refs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _accept(store, "ev-waiting")
    claim = store.claim("w1", ["economy", "coding"], _T0)
    assert claim is not None

    refs = ["grill_deadline:2026-10-08T12:00:00+00:00"]
    store.finish(claim, StageResult(outcome="waiting_human", cause_code="grill_pending", evidence_refs=refs), _T0 + timedelta(seconds=5))

    assert _stored_refs(tmp_path)[0] == refs


def test_a_result_without_refs_leaves_the_stored_refs_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _accept(store, "ev-empty")
    claim = store.claim("w1", ["economy", "coding"], _T0)
    assert claim is not None
    store.finish(claim, StageResult(outcome="retry", cause_code="r1", evidence_refs=["first"]), _T0 + timedelta(seconds=5))

    claim2 = store.claim("w2", ["economy", "coding"], _T0 + timedelta(seconds=6))
    assert claim2 is not None
    store.finish(claim2, StageResult(outcome="failed", cause_code="boom"), _T0 + timedelta(seconds=10))

    assert _stored_refs(tmp_path)[0] == ["first"]
