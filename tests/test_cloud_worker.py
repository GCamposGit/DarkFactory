"""Test suite for HF-03-04: Isolated Cloud Worker and Bounded Concurrency.

Tests:
- Slot status reporting and initial availability.
- Backpressure rejection when max slots is reached.
- Step execution outcome, timing, and slot release.
- Exception containment without worker crash.
"""

from __future__ import annotations

import pytest
from core.orchestrator.cloud_worker import CloudWorker, StepExecutionResult, WorkerSlotStatus


def test_worker_slot_status_reporting() -> None:
    worker = CloudWorker(worker_id="worker-test", max_slots=2)
    status = worker.slot_status()
    assert isinstance(status, WorkerSlotStatus)
    assert status.worker_id == "worker-test"
    assert status.max_slots == 2
    assert status.allocated_slots == 0
    assert status.available_slots == 2
    assert status.is_saturated is False


def test_worker_enforces_backpressure_when_saturated() -> None:
    worker = CloudWorker(worker_id="worker-test", max_slots=2)
    assert worker.try_acquire_slot("task-1") is True
    assert worker.try_acquire_slot("task-2") is True
    # Third slot must be rejected under backpressure
    assert worker.try_acquire_slot("task-3") is False

    status = worker.slot_status()
    assert status.allocated_slots == 2
    assert status.available_slots == 0
    assert status.is_saturated is True

    # After releasing task-1, new task can be acquired
    worker.release_slot("task-1")
    assert worker.try_acquire_slot("task-3") is True


def test_worker_executes_step_and_releases_slot() -> None:
    worker = CloudWorker(worker_id="worker-test", max_slots=2)
    result = worker.execute_step(
        workflow_id="wf-100",
        step_id="step-1",
        step_callable=lambda: {"result": "success", "count": 42},
    )
    assert isinstance(result, StepExecutionResult)
    assert result.success is True
    assert result.duration_ms >= 0.0
    assert result.output == {"result": "success", "count": 42}
    assert result.error is None

    # Slot must be released after execution
    assert worker.slot_status().allocated_slots == 0


def test_worker_contains_step_exceptions() -> None:
    worker = CloudWorker(worker_id="worker-test", max_slots=2)

    def failing_step():
        raise ValueError("Simulated computation fault")

    result = worker.execute_step(
        workflow_id="wf-101",
        step_id="step-err",
        step_callable=failing_step,
    )
    assert result.success is False
    assert "Simulated computation fault" in (result.error or "")
    assert worker.slot_status().allocated_slots == 0
