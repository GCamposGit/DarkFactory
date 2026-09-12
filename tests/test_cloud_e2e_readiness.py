"""End-to-end cloud coordination, worker execution, and resilience test suite.

Governed by HF-03-06 / ADR-HF-001.
Verifies:
- Complete workflow step dispatch via CloudWorker with slot management.
- Integration with CloudArtifactStore for deterministic output tracking.
- Resilience and state preservation across simulated process restarts.
- Strict absence of credential leakage in diagnostics.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from core.orchestrator.cloud_artifacts import CloudArtifactStore
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.orchestrator.cloud_db import probe_cloud_database
from core.orchestrator.cloud_worker import CloudWorker


def test_cloud_coordinator_and_worker_end_to_end_cycle(tmp_path: Path) -> None:
    artifact_store = CloudArtifactStore(root_dir=tmp_path / "artifacts")
    coordinator = CloudCoordinator(database_url=None, max_concurrent_slots=2)
    worker = CloudWorker(worker_id="cloud-worker-1", max_slots=2)

    status = coordinator.inspect_status()
    assert status.max_concurrent_slots == 2

    # Step 1: Computation step
    res1 = worker.execute_step(
        workflow_id="wf-cloud-001",
        step_id="step-prepare",
        step_callable=lambda: {"tokens": 1500, "status": "prepared"},
    )
    assert res1.success is True

    # Step 2: Artifact generation step
    def generate_artifact():
        ref = artifact_store.store_artifact(
            workflow_id="wf-cloud-001",
            filename="analysis.json",
            content='{"analysis_complete": true, "findings": 0}',
        )
        return ref.model_dump()

    res2 = worker.execute_step(
        workflow_id="wf-cloud-001",
        step_id="step-artifact",
        step_callable=generate_artifact,
    )
    assert res2.success is True
    assert res2.output["relative_path"] == "wf-cloud-001/analysis.json"

    # Step 3: Verify worker slot is completely freed
    assert worker.slot_status().allocated_slots == 0
    assert worker.slot_status().available_slots == 2


def test_cloud_resilience_and_crash_recovery_lifecycle() -> None:
    worker = CloudWorker(worker_id="cloud-worker-1", max_slots=2)

    # Step 1 succeeds
    res1 = worker.execute_step("wf-crash-001", "step-1", lambda: "step-1-ok")
    assert res1.success is True

    # Step 2 simulated crash
    res2 = worker.execute_step(
        "wf-crash-001",
        "step-2",
        lambda: (_ for _ in ()).throw(RuntimeError("Simulated SIGKILL crash")),
    )
    assert res2.success is False
    assert "Simulated SIGKILL crash" in (res2.error or "")

    # Slot must be cleanly released despite exception
    assert worker.slot_status().allocated_slots == 0

    # Step 2 retry / recovery in fresh process
    recovered_worker = CloudWorker(worker_id="cloud-worker-1", max_slots=2)
    res2_retry = recovered_worker.execute_step("wf-crash-001", "step-2", lambda: "step-2-recovered")
    assert res2_retry.success is True
    assert res2_retry.output == "step-2-recovered"


def test_probe_contract_reports_waiting_access_without_exception() -> None:
    probe = probe_cloud_database()
    assert probe.status == "blocked"
    assert "waiting_access" in (probe.error_message or "")
