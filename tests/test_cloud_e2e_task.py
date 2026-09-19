"""Unit test for autonomous cloud e2e task runner.

Verifies end-to-end task execution in local/mock mode:
- Intake ingestion
- Job claiming with monotonic fencing token
- Step execution with CloudWorker
- Volume artifact storage with SHA-256 verification
- Stage result persistence
- Decoupled ContinuousObserver audit against 13 negative rules
"""

from __future__ import annotations

import pytest
from scripts.cloud_e2e_task import run_e2e_autonomous_task


def test_autonomous_cloud_e2e_task_execution() -> None:
    """Validate full autonomous lifecycle execution in local/mock mode."""
    summary = run_e2e_autonomous_task()

    assert summary["status"] == "SUCCESS"
    assert "run-" in summary["workflow"]["run_id"]
    assert "dem-" in summary["workflow"]["demand_id"]
    assert summary["job_execution"]["fencing_token"] >= 1
    assert summary["job_execution"]["worker_id"] == "cloud-worker-1"
    assert summary["artifact"]["integrity_verified"] is True
    assert len(summary["artifact"]["sha256"]) == 64
    assert summary["acceptance_audit"]["passed"] is True
    assert summary["acceptance_audit"]["rules_evaluated"] == 13
    assert summary["acceptance_audit"]["violations"] == []
