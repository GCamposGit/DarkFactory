"""Deterministic unit tests for HF-08-01: Atomic Transactional Demand Intake.

Governed by:
- docs/handoffs/continuous-autonomy/HF-08-01.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/demands/autonomous_intake.py
- core/demands/integrated_service.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.integrated_service import IntegratedIntakeService
from core.demands.models import DemandInput
from core.demands.store import DemandsStore
from core.workflow.control_contracts import (
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
)
from core.workflow.control_store import SQLiteControlStore
from core.workflow.runtime import WorkflowRuntime

BASE_TIME = datetime(2026, 9, 18, 14, 0, 0, tzinfo=UTC)


def _sample_payload(title: str = "Automated Backup") -> dict:
    return {
        "title": title,
        "problem": "Manual backup is prone to human error",
        "journey": "User configures schedule; system executes backups",
        "non_goals": ["Manual tape operations"],
        "criteria": ["Scheduled execution", "Integrity check"],
    }


def test_intake_atomic_commit_creates_demand_run_and_grill_job() -> None:
    """Validate that accept() atomically creates demand, run, initial grill job, and outbox."""
    store = SQLiteControlStore(db_path=":memory:")
    service = AutonomousIntakeService(store=store)

    cmd = IntakeCommand(
        project_id="proj-atomic",
        channel="cli",
        external_id="ext-atomic-1",
        payload=_sample_payload(),
        mode="autonomous",
        policy_ref="policy-v1",
    )

    receipt = service.accept(cmd, now=BASE_TIME)
    assert receipt.mode == "autonomous"
    assert receipt.demand_id.startswith("dem-")
    assert receipt.run_id is not None and receipt.run_id.startswith("run-")
    assert receipt.initial_job_id is not None and receipt.initial_job_id.startswith("job-")

    # Verify initial grill job can be claimed
    claim = store.claim(worker="grill-worker-1", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim is not None
    assert claim.job_key.stage == "grill"
    assert claim.job_key.run_id == receipt.run_id


def test_intake_idempotent_replay_returns_identical_receipt() -> None:
    """Validate that resubmitting identical payload returns identical receipt with same IDs."""
    store = SQLiteControlStore(db_path=":memory:")
    service = AutonomousIntakeService(store=store)

    cmd = IntakeCommand(
        project_id="proj-idem",
        channel="web",
        external_id="ext-idem-1",
        payload=_sample_payload(),
        mode="autonomous",
        policy_ref="policy-v1",
    )

    receipt_1 = service.accept(cmd, now=BASE_TIME)
    receipt_2 = service.accept(cmd, now=BASE_TIME)

    assert receipt_1.demand_id == receipt_2.demand_id
    assert receipt_1.demand_version == receipt_2.demand_version
    assert receipt_1.run_id == receipt_2.run_id
    assert receipt_1.initial_job_id == receipt_2.initial_job_id
    assert receipt_1.committed_at == receipt_2.committed_at


def test_intake_conflicting_payload_raises_idempotency_conflict() -> None:
    """Validate that submitting differing payload for same external_id fails closed."""
    store = SQLiteControlStore(db_path=":memory:")
    service = AutonomousIntakeService(store=store)

    cmd_original = IntakeCommand(
        project_id="proj-conflict",
        channel="api",
        external_id="ext-conf-1",
        payload=_sample_payload("Original Title"),
        mode="autonomous",
        policy_ref="policy-v1",
    )
    service.accept(cmd_original, now=BASE_TIME)

    cmd_conflicting = IntakeCommand(
        project_id="proj-conflict",
        channel="api",
        external_id="ext-conf-1",
        payload=_sample_payload("Altered Conflict Title"),
        mode="autonomous",
        policy_ref="policy-v1",
    )

    with pytest.raises(IdempotencyConflict):
        service.accept(cmd_conflicting, now=BASE_TIME)


def test_intake_documentary_mode_returns_null_execution_ids() -> None:
    """Validate that documentary mode records demand without fictitious run or job IDs."""
    store = SQLiteControlStore(db_path=":memory:")
    service = AutonomousIntakeService(store=store)

    cmd = IntakeCommand(
        project_id="proj-doc",
        channel="docs",
        external_id="ext-doc-1",
        payload=_sample_payload("Documentary Record"),
        mode="documentary",
        policy_ref="policy-v1",
    )

    receipt = service.accept(cmd, now=BASE_TIME)
    assert receipt.mode == "documentary"
    assert receipt.demand_id.startswith("dem-")
    assert receipt.run_id is None
    assert receipt.initial_job_id is None

    # No jobs should be available for claim
    claim = store.claim(worker="worker-1", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim is None


def test_intake_post_commit_projection_failure_does_not_duplicate_demand(tmp_path) -> None:
    """Validate that projection failure preserves committed demand without duplication."""
    store = SQLiteControlStore(db_path=":memory:")
    mock_demands_store = MagicMock(spec=DemandsStore)
    mock_demands_store.save_ticket.side_effect = IOError("Disk write simulation error")

    service = AutonomousIntakeService(store=store, demands_store=mock_demands_store)

    cmd = IntakeCommand(
        project_id="proj-proj-fail",
        channel="cli",
        external_id="ext-fail-1",
        payload=_sample_payload(),
        mode="autonomous",
        policy_ref="policy-v1",
    )

    # Accept completes successfully despite projection failure
    receipt = service.accept(cmd, now=BASE_TIME)
    assert receipt.demand_id is not None
    assert mock_demands_store.save_ticket.called

    # Replay returns exact same receipt without creating duplicate
    receipt_replay = service.accept(cmd, now=BASE_TIME)
    assert receipt_replay.demand_id == receipt.demand_id


def test_integrated_service_mode_enforcement(tmp_path) -> None:
    """Validate mode handling and store requirement in IntegratedIntakeService."""
    test_store = DemandsStore(path=tmp_path / "test_demands.json")
    intake = IntegratedIntakeService(store=test_store)
    demand = DemandInput(
        project_id="proj-int",
        title="Test Feature",
        problem_statement="Problem description",
        core_journey="User flow",
        non_goals=["non goal"],
        acceptance_criteria=["criterion"],
    )

    # Autonomous mode without store or runtime must raise ValueError
    with pytest.raises(ValueError, match="Autonomous mode requires"):
        intake.receive_demand(demand, mode="autonomous")

    # Documentary mode succeeds without runtime and returns null run_id
    doc_res = intake.receive_demand(demand, mode="documentary", force_heuristic=True)
    assert doc_res["mode"] == "documentary"
    assert doc_res["run_id"] is None
    assert doc_res["run_record"] is None

    # Autonomous mode with WorkflowRuntime succeeds and populates run_id
    runtime = WorkflowRuntime(database_path=tmp_path / "runtime.db")
    auto_res = intake.receive_demand(demand, mode="autonomous", runtime=runtime, force_heuristic=True)
    assert auto_res["mode"] == "autonomous"
    assert auto_res["run_id"] is not None
    assert auto_res["run_record"] is not None
