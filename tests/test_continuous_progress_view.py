"""Deterministic tests for HF-13-02: Painel de progresso e estagnação no DarkHub.

Normative implementation of:
- docs/handoffs/continuous-autonomy/HF-13-02.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- ADR-HF-001
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core.workflow.control_contracts import IntakeCommand
from core.workflow.control_store import SQLiteControlStore
from hub.backend.main import app
from hub.backend.models import ProgressProjection
from hub.backend.service import DarkHubService


@pytest.fixture
def temp_service(tmp_path: Path) -> DarkHubService:
    store_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=store_path)
    service = DarkHubService(
        data_dir=tmp_path / "data",
        project_root=tmp_path,
        control_store=store,
    )
    return service


class TestContinuousProgressView:
    def test_progress_projection_clean_state(self, temp_service: DarkHubService) -> None:
        """Querying progress on a project without active jobs returns a zeroed, non-stalled projection."""
        progress = temp_service.get_progress_projection("darkfac")
        assert isinstance(progress, ProgressProjection)
        assert progress.project_id == "darkfac"
        assert progress.ready_count == 0
        assert progress.running_count == 0
        assert progress.blocked_count == 0
        assert progress.oldest_eligible_age == 0.0
        assert progress.stalled is False
        assert progress.healthy is True
        assert len(progress.alerts) == 0

    def test_progress_projection_counts_and_age(self, temp_service: DarkHubService) -> None:
        """Accurately calculates ready, running, and blocked counts along with oldest_eligible_age."""
        store = temp_service.control_store
        t0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

        # Ingest an autonomous demand
        cmd = IntakeCommand(
            project_id="darkfac",
            channel="cli",
            external_id="ext-prog-01",
            mode="autonomous",
            policy_ref="policy-v1",
            payload={
                "title": "Progress Task 1",
                "problem": "Test progress tracking",
                "journey": "Intake to validation",
                "non_goals": ["None"],
                "criteria": ["Tracked in projection"],
            },
        )
        receipt = store.accept(cmd, t0)
        assert receipt.run_id is not None

        # 10 seconds later: job is pending
        t1 = t0 + timedelta(seconds=10)
        progress1 = temp_service.get_progress_projection("darkfac", now=t1)
        assert progress1.ready_count == 1
        assert progress1.running_count == 0
        assert 9.0 <= progress1.oldest_eligible_age <= 11.0
        assert progress1.stalled is False

    def test_stagnation_detection_when_ready_with_free_slot_over_30s(self, temp_service: DarkHubService) -> None:
        """Invariant: Ready job waiting >30s with available worker capacity flags stalled=True."""
        store = temp_service.control_store
        t0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

        cmd = IntakeCommand(
            project_id="darkfac",
            channel="hub",
            external_id="ext-stalled-01",
            mode="autonomous",
            policy_ref="policy-v1",
            payload={
                "title": "Stalled Task",
                "problem": "Worker starvation simulation",
                "journey": "Stalled on queue",
                "non_goals": ["None"],
                "criteria": ["Detect stalled >30s"],
            },
        )
        store.accept(cmd, t0)

        # 35 seconds later with 0 running jobs and free capacity
        t_stalled = t0 + timedelta(seconds=35)
        progress = temp_service.get_progress_projection("darkfac", now=t_stalled, max_capacity=2)

        assert progress.ready_count == 1
        assert progress.running_count == 0
        assert progress.oldest_eligible_age >= 35.0
        assert progress.stalled is True

        # Check deduplicated alert
        alert_ids = [a.incident_id for a in progress.alerts]
        assert "stalled:darkfac" in alert_ids
        assert alert_ids.count("stalled:darkfac") == 1

    def test_no_stagnation_when_worker_slots_are_full(self, temp_service: DarkHubService) -> None:
        """When all capacity slots are busy, ready jobs waiting >30s are queued, NOT stalled."""
        store = temp_service.control_store
        t0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

        cmd = IntakeCommand(
            project_id="darkfac",
            channel="hub",
            external_id="ext-busy-01",
            mode="autonomous",
            policy_ref="policy-v1",
            payload={
                "title": "Queued Task",
                "problem": "Busy worker pool",
                "journey": "Waiting for busy slot",
                "non_goals": ["None"],
                "criteria": ["Queued without stalled flag"],
            },
        )
        store.accept(cmd, t0)

        t_busy = t0 + timedelta(seconds=45)
        # Max capacity = 1, running = 1 (no free slot)
        progress = temp_service.get_progress_projection("darkfac", now=t_busy, max_capacity=1, running_override=1)

        assert progress.stalled is False
        assert not any("stalled" in a.incident_id for a in progress.alerts)

    def test_alerts_deduplicated_by_incident_id(self, temp_service: DarkHubService) -> None:
        """Multiple triggers of the same condition produce at most one alert per incident_id."""
        store = temp_service.control_store
        t0 = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)

        # Create two stalled jobs for the same project
        for i in (1, 2):
            cmd = IntakeCommand(
                project_id="darkfac",
                channel="hub",
                external_id=f"ext-multi-{i}",
                mode="autonomous",
                policy_ref="policy-v1",
                payload={
                    "title": f"Stalled Task {i}",
                    "problem": "Multi job test",
                    "journey": "Both ready",
                    "non_goals": ["None"],
                    "criteria": ["Pass"],
                },
            )
            store.accept(cmd, t0)

        t_eval = t0 + timedelta(seconds=40)
        progress = temp_service.get_progress_projection("darkfac", now=t_eval, max_capacity=4)

        assert progress.stalled is True
        incident_counts = {}
        for a in progress.alerts:
            incident_counts[a.incident_id] = incident_counts.get(a.incident_id, 0) + 1

        for inc_id, count in incident_counts.items():
            assert count == 1, f"Alert {inc_id} duplicated {count} times"

    def test_api_roadmap_progress_endpoint(self, temp_service: DarkHubService, monkeypatch: pytest.MonkeyPatch) -> None:
        """The GET /{project_id}/roadmap/progress route returns valid 200 with ProgressProjection payload."""
        from hub.backend.api import get_hub_service

        monkeypatch.setattr("hub.backend.api.get_hub_service", lambda: temp_service)

        client = TestClient(app)
        response = client.get("/darkfac/roadmap/progress")
        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == "darkfac"
        assert "oldest_eligible_age" in data
        assert "stalled" in data
        assert "ready_count" in data
        assert "running_count" in data
        assert "blocked_count" in data
        assert "alerts" in data
