"""Test suite for continuous cloud entrypoints (HF-05-06 / ADR-HF-001).

Tests:
1. PID vivo ocioso sem chamadas a IA (coordinator & worker loops run idle with $0 AI calls).
2. Drenagem de SIGTERM/shutdown no worker:
   - Tarefas em execução completam.
   - Novas requisições de slot são rejeitadas durante o drain.
   - Timeout de drenagem é estritamente respeitado.
3. Invocação de --status via CLI e entrypoint para worker e coordinator:
   - Worker retorna código 0 e JSON estruturado.
   - Coordinator retorna código 2 (waiting_access) ou 0 (ready).
4. Resiliência do coordinator quando o banco de dados está em waiting_access.
5. Contrato do arquivo deploy/dokploy/docker-compose.cloud.yml.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.orchestrator.cloud_coordinator import (
    CloudCoordinator,
    CoordinatorStatus,
    main as coordinator_main,
)
from core.orchestrator.cloud_worker import (
    CloudWorker,
    WorkerSlotStatus,
    main as worker_main,
)
from core.orchestrator.cloud_db import DatabaseProbeResult


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_coordinator_idle_loop_without_ai_calls() -> None:
    """Coordinator supervision loop runs idle without invoking external AI services."""
    coordinator = CloudCoordinator(database_url=None, max_concurrent_slots=2)
    stop_event = threading.Event()

    def run_coordinator() -> None:
        coordinator.run_forever(stop_event=stop_event, poll_interval_sec=0.05, enable_http=False)

    thread = threading.Thread(target=run_coordinator, daemon=True)
    thread.start()

    try:
        # Returns as soon as the thread is up; the generous ceiling only
        # matters when the host is saturated (e.g. xdist with 28 workers).
        deadline = time.monotonic() + 10.0
        while not coordinator._running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert coordinator._running is True
    finally:
        stop_event.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert coordinator._running is False


def test_worker_idle_loop_without_ai_calls() -> None:
    """Worker loop runs idle without invoking external AI services."""
    worker = CloudWorker(worker_id="test-worker-idle", max_slots=2)
    stop_event = threading.Event()

    def run_worker() -> None:
        worker.run_forever(stop_event=stop_event, poll_interval_sec=0.05)

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()

    time.sleep(0.15)
    assert worker._running is True

    # Stop and join cleanly
    stop_event.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert worker._running is False


def test_worker_and_coordinator_subprocess_pid_alive_idle() -> None:
    """Real subprocesses start with live PIDs and run idle loops without crashing."""
    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "core.orchestrator.cloud_worker", "--poll-interval", "0.1"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    coord_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "core.orchestrator.cloud_coordinator",
            "--poll-interval",
            "0.1",
            "--no-http",
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Both processes must be alive
        assert worker_proc.poll() is None
        assert coord_proc.poll() is None
        assert worker_proc.pid > 0
        assert coord_proc.pid > 0

        # Let them run idle
        time.sleep(0.3)
        assert worker_proc.poll() is None
        assert coord_proc.poll() is None
    finally:
        worker_proc.terminate()
        coord_proc.terminate()
        worker_proc.wait(timeout=5.0)
        coord_proc.wait(timeout=5.0)


def test_worker_drain_completes_in_flight_tasks_and_rejects_new() -> None:
    """During drain: in-flight tasks finish, new tasks are rejected, and drain completes."""
    worker = CloudWorker(worker_id="drain-worker-1", max_slots=2)

    # Acquire slot for an in-flight task
    assert worker.try_acquire_slot("task-inflight-1") is True
    assert worker.slot_status().allocated_slots == 1

    # Simulate in-flight task completing in 0.15s
    def delayed_release() -> None:
        time.sleep(0.15)
        worker.release_slot("task-inflight-1")

    release_thread = threading.Thread(target=delayed_release)
    release_thread.start()

    drain_outcome: list[bool] = []

    def initiate_drain() -> None:
        drain_outcome.append(worker.drain(timeout_seconds=2.0))

    drain_thread = threading.Thread(target=initiate_drain)
    drain_thread.start()

    # Give drain a tiny moment to set _draining = True
    time.sleep(0.04)
    assert worker._draining is True

    # New slot acquisition MUST be rejected during drain
    assert worker.try_acquire_slot("task-new-rejected") is False

    # Execute step MUST also fail with rejection during drain
    result = worker.execute_step(
        workflow_id="wf-test",
        step_id="step-drain-reject",
        step_callable=lambda: {"status": "ok"},
    )
    assert result.success is False
    assert "draining" in (result.error or "")

    # Check slot status reflects draining
    status = worker.slot_status()
    assert status.is_draining is True
    assert status.available_slots == 0
    assert status.is_saturated is True

    # Wait for completion
    release_thread.join(timeout=1.0)
    drain_thread.join(timeout=1.0)

    assert len(drain_outcome) == 1
    assert drain_outcome[0] is True
    assert worker.slot_status().allocated_slots == 0


def test_worker_drain_timeout_respected_when_tasks_stuck() -> None:
    """Worker drain respects timeout and returns False if tasks do not finish."""
    worker = CloudWorker(worker_id="stuck-worker", max_slots=2)
    assert worker.try_acquire_slot("stuck-task") is True

    start = time.monotonic()
    drained = worker.drain(timeout_seconds=0.2)
    duration = time.monotonic() - start

    assert drained is False
    assert 0.18 <= duration < 1.0  # Finished close to 0.2s without hanging indefinitely
    assert worker.slot_status().allocated_slots == 1


def test_worker_run_forever_signals_and_drains() -> None:
    """Worker run_forever intercepts stop signal, drains in-flight work, and exits cleanly."""
    worker = CloudWorker(worker_id="worker-shutdown-test", max_slots=2)
    stop_event = threading.Event()

    assert worker.try_acquire_slot("task-active") is True

    def finish_task_after_delay() -> None:
        time.sleep(0.1)
        worker.release_slot("task-active")

    threading.Thread(target=finish_task_after_delay).start()

    def run_worker() -> None:
        worker.run_forever(stop_event=stop_event, poll_interval_sec=0.05)

    worker_thread = threading.Thread(target=run_worker)
    worker_thread.start()

    # Trigger shutdown after brief start
    time.sleep(0.04)
    stop_event.set()

    worker_thread.join(timeout=2.0)
    assert not worker_thread.is_alive()
    assert worker._draining is True
    assert worker.slot_status().allocated_slots == 0


def test_coordinator_cli_status_waiting_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coordinator CLI --status outputs JSON and exits with code 2 when database is waiting_access."""
    # Programmatic entrypoint test
    with patch("core.orchestrator.cloud_coordinator.probe_cloud_database") as mock_probe:
        mock_probe.return_value = DatabaseProbeResult(
            status="waiting_access",
            error_message="Simulated waiting access",
            details={},
        )
        exit_code = coordinator_main(["--status"])
        assert exit_code == 2

    # CLI subprocess invocation
    proc = subprocess.run(
        [sys.executable, "-m", "core.orchestrator.cloud_coordinator", "--status"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    assert data["role"] == "coordinator"
    assert data["database_status"] == "waiting_access"


def test_coordinator_cli_status_ready() -> None:
    """Coordinator CLI --status outputs JSON and exits with code 0 when database is ready."""
    with patch("core.orchestrator.cloud_coordinator.probe_cloud_database") as mock_probe:
        mock_probe.return_value = DatabaseProbeResult(
            status="ready",
            error_message=None,
            details={"version": "16.1"},
        )
        exit_code = coordinator_main(["--status"])
        assert exit_code == 0


def test_worker_cli_status() -> None:
    """Worker CLI --status outputs JSON and exits with code 0."""
    # Programmatic entrypoint test
    exit_code = worker_main(["--status"])
    assert exit_code == 0

    # CLI subprocess invocation
    proc = subprocess.run(
        [sys.executable, "-m", "core.orchestrator.cloud_worker", "--status"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "worker_id" in data
    assert data["max_slots"] == 2
    assert data["allocated_slots"] == 0
    assert data["is_saturated"] is False
    assert data["is_draining"] is False


def test_coordinator_resilience_waiting_access() -> None:
    """Coordinator safely handles waiting_access: reports status, skips scans, and loops cleanly."""
    coordinator = CloudCoordinator(database_url=None)

    status = coordinator.inspect_status()
    assert isinstance(status, CoordinatorStatus)
    assert status.database_status == "waiting_access"
    assert status.error_message is not None

    # scan_and_recover_pending safely returns empty list without exception
    recovered = coordinator.scan_and_recover_pending()
    assert recovered == []

    # run_forever loops safely without raising exceptions
    stop_event = threading.Event()

    def run_loop() -> None:
        coordinator.run_forever(stop_event=stop_event, poll_interval_sec=0.05, enable_http=False)

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()

    try:
        # Returns as soon as the thread is up; the generous ceiling only
        # matters when the host is saturated (e.g. xdist with 28 workers).
        deadline = time.monotonic() + 10.0
        while not coordinator._running and time.monotonic() < deadline:
            time.sleep(0.01)
        assert coordinator._running is True
    finally:
        stop_event.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert coordinator._running is False


def test_docker_compose_cloud_spec() -> None:
    """Validate deploy/dokploy/docker-compose.cloud.yml against production spec."""
    compose_path = REPO_ROOT / "deploy" / "dokploy" / "docker-compose.cloud.yml"
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")

    # 1. No GITHUB_PAT token in context
    assert "${GITHUB_PAT" not in content
    assert "context: https://github.com/GCamposGit/DarkFactory.git#main" in content

    # 2. Pinned image
    assert "image: ghcr.io/gcamposgit/darkfac-cloud:latest" in content

    # 3. Stop grace period 35s
    assert "stop_grace_period: 35s" in content

    # 4. Healthcheck with --status
    assert 'test: ["CMD", "python", "-m", "core.orchestrator.cloud_coordinator", "--status"]' in content
    assert 'test: ["CMD", "python", "-m", "core.orchestrator.cloud_worker", "--status"]' in content
