"""Durable worker-to-Hub owner action synchronization."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.line.human import HumanRequest


def test_sync_event_survives_transport_failure_and_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.owner_actions import sync

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("DARKHUB_CLOUD_URL", "https://hub.example.invalid")
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-key")
    request = HumanRequest(kind="infra", run_id="run-1", blocking_stage="release", guide_md="1. Restore service")
    attempts = []

    def offline(*args, **kwargs):
        raise OSError("private transport detail")

    monkeypatch.setattr(sync.urllib.request, "urlopen", offline)
    sync.publish_request(request)
    assert sync.outbox_path().is_file()

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def online(req, **kwargs):
        attempts.append((req, kwargs))
        return Response()

    monkeypatch.setattr(sync.urllib.request, "urlopen", online)
    assert sync.flush_pending(force=True) == 1
    event = sync.SyncEvent.model_validate_json(attempts[0][0].data)
    assert event.request.run_id == "run-1"
    assert event.closed is False
    assert attempts[0][0].full_url == "https://hub.example.invalid/api/owner-actions/sync"
    assert attempts[0][1]["timeout"] <= 3
    assert sync.flush_pending(force=True) == 0


def test_closed_outbox_state_is_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.owner_actions import sync

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKHUB_CLOUD_URL", raising=False)
    request = HumanRequest(kind="infra", run_id="run-2", blocking_stage="release", guide_md="1. Restore")
    sync.publish_request(request)
    sync.publish_closed("run-2", "release")
    sync.publish_request(request)
    rows = json.loads(sync.outbox_path().read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert next(iter(rows.values()))["event"]["closed"] is True
    sync.publish_closed("close-first", "planning")
    sync.publish_request(request.model_copy(update={"run_id": "close-first", "blocking_stage": "planning"}))
    rows = json.loads(sync.outbox_path().read_text(encoding="utf-8"))
    assert all(row["event"]["closed"] for row in rows.values())


def test_outbox_is_a_root_artifact_file_outside_directory_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.owner_actions import sync
    from core.orchestrator.artifact_retention import sweep_stale_artifacts

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKHUB_CLOUD_URL", raising=False)
    request = HumanRequest(kind="infra", run_id="run-3", blocking_stage="release", guide_md="1. Restore")
    sync.publish_request(request)
    assert sync.outbox_path() == tmp_path / "artifacts" / "owner-action-outbox"
    assert sync.outbox_path().is_file()
    report = sweep_stale_artifacts(root=tmp_path / "artifacts", run_status=lambda _: None, now=10**12)
    assert report.scanned == 0
    assert sync.outbox_path().exists()


def test_worker_polls_outbox_even_when_saturated(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.owner_actions import sync
    from core.orchestrator.cloud_worker import CloudWorker

    calls = []
    monkeypatch.setattr(sync, "flush_pending", lambda: calls.append("flush"))
    worker = object.__new__(CloudWorker)
    worker._draining = False
    worker.max_slots = 1
    worker._active_tasks = {"busy": object()}
    assert worker.poll_and_execute_once() is False
    assert calls == ["flush"]


def test_open_ack_cannot_ack_a_concurrent_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.owner_actions import sync

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKHUB_CLOUD_URL", raising=False)
    request = HumanRequest(kind="infra", run_id="race", blocking_stage="release", guide_md="1. Restore")
    sync.publish_request(request)
    monkeypatch.setenv("DARKHUB_CLOUD_URL", "https://hub.example.invalid")
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-key")

    class Response:
        def __enter__(self):
            sync.publish_closed("race", "release")
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(sync.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    assert sync.flush_pending(force=True) == 0
    row = next(iter(json.loads(sync.outbox_path().read_text(encoding="utf-8")).values()))
    assert row["event"]["closed"] is True
    assert row["sent"] is False
    assert sync.flush_pending(force=True) == 1


def test_long_job_heartbeat_flushes_without_new_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from core.owner_actions import sync
    from core.orchestrator import cloud_worker

    calls, waits = [], []
    monkeypatch.setattr(sync, "flush_pending", lambda: calls.append("flush"))

    class Event:
        def wait(self, seconds):
            waits.append(seconds)
            return len(waits) > 1

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(cloud_worker.threading, "Event", Event)
    monkeypatch.setattr(cloud_worker.threading, "Thread", Thread)
    worker = object.__new__(cloud_worker.CloudWorker)
    worker._store = SimpleNamespace(lease_duration_sec=600)
    worker._start_lease_heartbeat(SimpleNamespace(lease_id="long-job"))
    assert calls == ["flush"]
    assert max(waits) <= 15
    assert sync.RETRY_INTERVAL_SEC <= 60
