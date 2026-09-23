"""Tests for the HF-27-09 `ready_age_sec` claim() filter.

`claim(worker, capabilities, now, ready_age_sec=...)` lets a lower-priority
worker (Desktop/Notebook) skip jobs that are not old enough yet, so a
higher-priority worker (VPS) gets first refusal. `ready_age_sec=0.0` (the
default) must reproduce the exact pre-HF-27-09 behaviour on both the SQLite
and the mock-mode Postgres backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.workflow.control_contracts import IntakeCommand, RuntimeOwner
from core.workflow.control_store import ControlStore, SQLiteControlStore, _is_ready_enough


@pytest.fixture(params=["sqlite", "postgres_mock"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> ControlStore:
    if request.param == "sqlite":
        db_file = tmp_path / f"control_{request.node.name}.db"
        return SQLiteControlStore(
            db_path=db_file,
            runtime_owner=RuntimeOwner.HF05_SQLITE.value,
            lease_duration_sec=45,
        )
    return PostgresControlStore(
        mock_mode=True,
        runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
        lease_duration_sec=45,
    )


def _accept(store: ControlStore, now: datetime, external_id: str) -> str:
    cmd = IntakeCommand(
        channel="cli",
        external_id=external_id,
        project_id="darkfac",
        payload={
            "title": "HF-27-09 ready_age test",
            "problem": "Validate ready_age_sec claim filtering",
            "journey": "Unit test",
            "non_goals": ["No manual deployment"],
            "criteria": ["Test passes deterministically"],
        },
        mode="autonomous",
        policy_ref="policy-v1",
    )
    receipt = store.accept(cmd, now)
    return receipt.run_id


def test_ready_age_zero_preserves_default_behaviour(store: ControlStore) -> None:
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    _accept(store, now, "ext-ready-age-default")

    claim = store.claim(worker="w1", capabilities=["economy", "coding"], now=now, ready_age_sec=0.0)
    assert claim is not None

    # Same-call default (no kwarg) must behave identically.
    claim_none = store.claim(worker="w2", capabilities=["economy", "coding"], now=now)
    assert claim_none is None  # already claimed above


def test_ready_age_filter_skips_fresh_job(store: ControlStore) -> None:
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    _accept(store, now, "ext-ready-age-fresh")

    # Job was just created (age 0s); a worker requiring 30s of ready_age
    # must not claim it yet.
    claim = store.claim(worker="w-secondary", capabilities=["economy", "coding"], now=now, ready_age_sec=30.0)
    assert claim is None


def test_ready_age_filter_allows_old_enough_job(store: ControlStore) -> None:
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    _accept(store, now, "ext-ready-age-old")

    later = now + timedelta(seconds=31)
    claim = store.claim(worker="w-secondary", capabilities=["economy", "coding"], now=later, ready_age_sec=30.0)
    assert claim is not None


def test_higher_priority_worker_claims_first(store: ControlStore) -> None:
    """Simulates VPS (ready_age=0) racing a Desktop worker (ready_age=30) on a fresh job."""
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    _accept(store, now, "ext-ready-age-priority")

    # Desktop polls first but the job is too fresh for it.
    desktop_claim = store.claim(worker="desktop", capabilities=["economy", "coding"], now=now, ready_age_sec=30.0)
    assert desktop_claim is None

    # VPS polls immediately after and claims it.
    vps_claim = store.claim(worker="vps", capabilities=["economy", "coding"], now=now, ready_age_sec=0.0)
    assert vps_claim is not None
    assert vps_claim.owner == "vps"


def test_is_ready_enough_helper_handles_malformed_timestamp() -> None:
    now = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
    # A malformed timestamp must be treated as "ready" (soft hint, not a gate).
    assert _is_ready_enough("not-a-timestamp", now, 30.0) is True


def test_is_ready_enough_helper_computes_age() -> None:
    now = datetime(2026, 9, 23, 12, 0, 30, tzinfo=UTC)
    created = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC).isoformat()
    assert _is_ready_enough(created, now, 30.0) is True
    assert _is_ready_enough(created, now, 31.0) is False
