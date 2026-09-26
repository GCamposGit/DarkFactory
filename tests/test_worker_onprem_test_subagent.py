"""
Tests for On-Premises Remote Test Worker and TestSubagentEngine Integration (USR-16).

Validates:
1. Pydantic v2 schemas: TestExecutionInstruction & DistilledTestReport extensions.
2. Remote worker daemon FastAPI endpoints (/health, /execute).
3. TestSubagentEngine health probing (online, offline, timeout).
4. Automatic local fallback and fail-closed policies.
5. DarkHub API endpoints (/api/harness/workers, /api/harness/execute).
"""

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.harness.remote_worker import (
    WorkerHealthStatus,
    create_worker_app,
)
from core.harness.test_subagent import (
    DistilledTestReport,
    FailedTestItem,
    TestExecutionInstruction,
    TestScope,
    TestSubagentEngine,
)
from hub.backend.main import app as hub_app


# ---------------------------------------------------------------------------
# 1. Models Verification
# ---------------------------------------------------------------------------


def test_instruction_and_report_models_defaults_and_serialization() -> None:
    """Ensure all USR-16 fields are present with correct defaults in Pydantic models."""
    instruction = TestExecutionInstruction(target="tests/test_foo.py")
    assert instruction.worker_mode == "local"
    assert instruction.remote_worker_url == "http://100.78.181.90:8080"
    assert instruction.allow_fallback is True
    assert instruction.worker_probe_timeout == 1.0

    dumped = instruction.model_dump()
    assert dumped["worker_mode"] == "local"
    assert dumped["remote_worker_url"] == "http://100.78.181.90:8080"
    assert dumped["allow_fallback"] is True
    assert dumped["worker_probe_timeout"] == 1.0

    report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.45,
        total_discovered=10,
        passed_count=10,
        failed_count=0,
        skipped_count=0,
        failures=[],
        concise_summary="10 passed in 0.45s",
        agent_feedback="All tests passed successfully.",
        raw_log_path="/tmp/test.log",
    )
    assert report.worker_id == "local"
    assert report.execution_mode == "local"

    # Test custom values
    custom_report = DistilledTestReport(
        verdict="FAILED",
        success=False,
        exit_code=1,
        duration_seconds=1.2,
        concise_summary="1 failed in 1.20s",
        agent_feedback="test_foo failed",
        worker_id="onprem-z97-server",
        execution_mode="remote_onprem_offload",
    )
    assert custom_report.worker_id == "onprem-z97-server"
    assert custom_report.execution_mode == "remote_onprem_offload"


# ---------------------------------------------------------------------------
# 2. Remote Worker Daemon Endpoints
# ---------------------------------------------------------------------------


def test_remote_worker_app_health_endpoint() -> None:
    """Test remote worker daemon /health endpoint."""
    worker_app = create_worker_app(node_id="test-desktop-g45ipem")
    client = TestClient(worker_app)

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["node_id"] == "test-desktop-g45ipem"
    assert data["version"] == "1.0.0"
    assert "docker_ready" in data
    assert "project_root" in data


def test_remote_worker_app_execute_endpoint() -> None:
    """Test remote worker daemon /execute endpoint delegating to headless engine."""
    worker_app = create_worker_app(node_id="desktop-g45ipem")
    client = TestClient(worker_app)

    fake_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.5,
        total_discovered=5,
        passed_count=5,
        failed_count=0,
        skipped_count=0,
        failures=[],
        concise_summary="5 passed in 0.50s",
        agent_feedback="All tests passed successfully.",
    )

    with patch("core.harness.remote_worker.TestSubagentEngine.execute", return_value=fake_report):
        response = client.post(
            "/execute",
            json={
                "target": "tests/test_sample.py",
                "scope": "file",
                "worker_mode": "auto",
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["verdict"] == "PASSED"
        assert result["worker_id"] == "desktop-g45ipem"
        assert result["execution_mode"] == "remote_onprem_offload"


# ---------------------------------------------------------------------------
# 3. TestSubagentEngine Probing & Failover
# ---------------------------------------------------------------------------


def test_engine_probe_remote_worker_success() -> None:
    """Engine probe returns parsed dict when worker is online."""
    engine = TestSubagentEngine()
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.read.return_value = json.dumps({
        "status": "ok",
        "node_id": "onprem-z97-server",
        "docker_ready": True,
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = engine.probe_remote_worker("http://100.78.181.90:8080", timeout=0.5)
        assert result is not None
        assert result["status"] == "ok"
        assert result["node_id"] == "onprem-z97-server"


def test_engine_probe_remote_worker_offline() -> None:
    """Engine probe returns None when worker is offline or raises error."""
    engine = TestSubagentEngine()

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        result = engine.probe_remote_worker("http://100.78.181.90:8080", timeout=0.5)
        assert result is None


def test_engine_execute_local_mode() -> None:
    """Explicit local mode bypasses probing and runs locally."""
    engine = TestSubagentEngine()
    fake_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.2,
        concise_summary="all passed",
        agent_feedback="ok",
    )

    with patch.object(engine, "execute_local", return_value=fake_report) as mock_local:
        with patch.object(engine, "probe_remote_worker") as mock_probe:
            instruction = TestExecutionInstruction(target="tests/foo.py", worker_mode="local")
            report = engine.execute(instruction)
            assert report.verdict == "PASSED"
            mock_probe.assert_not_called()
            mock_local.assert_called_once_with(instruction, execution_mode="local")


def test_engine_execute_remote_success() -> None:
    """When remote worker is healthy, engine offloads execution remotely."""
    engine = TestSubagentEngine()
    fake_remote_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.8,
        concise_summary="passed on server",
        agent_feedback="ok",
        worker_id="onprem-z97-server",
        execution_mode="remote_onprem_offload",
    )

    instruction = TestExecutionInstruction(
        target="tests/foo.py",
        worker_mode="auto",
        remote_worker_url="http://100.78.181.90:8080",
    )

    with patch.object(engine, "probe_remote_worker", return_value={"status": "ok"}):
        with patch.object(engine, "execute_remote", return_value=fake_remote_report) as mock_remote:
            report = engine.execute(instruction)
            assert report.worker_id == "onprem-z97-server"
            assert report.execution_mode == "remote_onprem_offload"
            mock_remote.assert_called_once()


def test_engine_execute_auto_failover_to_local_when_remote_offline() -> None:
    """Auto mode smoothly falls back to local execution when remote is offline."""
    engine = TestSubagentEngine()
    fake_local_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.3,
        concise_summary="passed locally",
        agent_feedback="ok",
        worker_id="local",
        execution_mode="local_fallback",
    )

    instruction = TestExecutionInstruction(
        target="tests/foo.py",
        worker_mode="auto",
        remote_worker_url="http://100.78.181.90:8080",
        allow_fallback=True,
    )

    with patch.object(engine, "probe_remote_worker", return_value=None):
        with patch.object(engine, "execute_local", return_value=fake_local_report) as mock_local:
            report = engine.execute(instruction)
            assert report.execution_mode == "local_fallback"
            mock_local.assert_called_once_with(instruction, execution_mode="local_fallback")


def test_engine_execute_remote_fail_closed_when_no_fallback() -> None:
    """Strict remote mode with allow_fallback=False raises RuntimeError when remote is offline."""
    engine = TestSubagentEngine()
    instruction = TestExecutionInstruction(
        target="tests/foo.py",
        worker_mode="remote",
        remote_worker_url="http://100.78.181.90:8080",
        allow_fallback=False,
    )

    with patch.object(engine, "probe_remote_worker", return_value=None):
        with pytest.raises(RuntimeError, match="Remote test worker .* is unavailable"):
            engine.execute(instruction)


def test_engine_execute_remote_network_error_triggers_fallback() -> None:
    """If remote was healthy at probe time but HTTP POST fails, falls back if allowed."""
    engine = TestSubagentEngine()
    fake_fallback_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.5,
        concise_summary="fallback ok",
        agent_feedback="ok",
        worker_id="local",
        execution_mode="local_fallback",
    )

    instruction = TestExecutionInstruction(
        target="tests/foo.py",
        worker_mode="auto",
        remote_worker_url="http://100.78.181.90:8080",
        allow_fallback=True,
    )

    with patch.object(engine, "probe_remote_worker", return_value={"status": "ok"}):
        with patch.object(engine, "execute_remote", side_effect=urllib.error.URLError("Connection reset")):
            with patch.object(engine, "execute_local", return_value=fake_fallback_report) as mock_local:
                report = engine.execute(instruction)
                assert report.execution_mode == "local_fallback"
                mock_local.assert_called_once_with(instruction, execution_mode="local_fallback")


# ---------------------------------------------------------------------------
# 4. DarkHub API Endpoints
# ---------------------------------------------------------------------------


def test_hub_api_list_test_workers() -> None:
    """DarkHub /api/harness/workers returns worker nodes and live status."""
    client = TestClient(hub_app)

    with patch("core.harness.test_subagent.TestSubagentEngine.probe_remote_worker") as mock_probe:
        def fake_probe(url: str, timeout: float = 0.8):
            if "100.78.181.90" in url:
                return {"status": "ok", "node_id": "onprem-z97-server", "docker_ready": True}
            return None

        mock_probe.side_effect = fake_probe

        response = client.get("/api/harness/workers")
        assert response.status_code == 200
        workers = response.json()
        assert isinstance(workers, list)
        assert len(workers) >= 2

        onprem = next((w for w in workers if w["id"] == "onprem-z97-server"), None)
        assert onprem is not None
        assert onprem["status"] == "online"
        assert onprem["healthy"] is True
        assert onprem["details"]["node_id"] == "onprem-z97-server"


def test_hub_api_execute_test_suite() -> None:
    """DarkHub /api/harness/execute delegates to TestSubagentEngine."""
    client = TestClient(hub_app)

    fake_report = DistilledTestReport(
        verdict="PASSED",
        success=True,
        exit_code=0,
        duration_seconds=0.7,
        total_discovered=12,
        passed_count=12,
        failed_count=0,
        skipped_count=0,
        failures=[],
        concise_summary="12 passed in 0.70s",
        agent_feedback="All tests passed successfully.",
        worker_id="local",
        execution_mode="local",
    )

    with patch("core.harness.test_subagent.TestSubagentEngine.execute", return_value=fake_report):
        payload = {
            "target": "tests/test_worker_onprem_test_subagent.py",
            "scope": "file",
            "worker_mode": "auto",
        }
        response = client.post("/api/harness/execute", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["verdict"] == "PASSED"
        assert data["success"] is True
        assert data["total_discovered"] == 12


# ---------------------------------------------------------------------------
# 5. Remote System Management Endpoints (/system/exec, /system/update)
# ---------------------------------------------------------------------------


def test_remote_worker_app_system_exec_endpoint() -> None:
    """Test remote worker daemon /system/exec endpoint executing commands."""
    worker_app = create_worker_app(node_id="test-desktop-g45ipem")
    client = TestClient(worker_app)

    # Simple echo command
    response = client.post(
        "/system/exec",
        json={"command": "python -c \"print('REMOTE_OK')\"", "timeout_seconds": 10},
    )
    assert response.status_code == 200
    res_data = response.json()
    assert res_data["success"] is True
    assert res_data["exit_code"] == 0
    assert "REMOTE_OK" in res_data["stdout"]


def test_remote_worker_app_system_update_endpoint() -> None:
    """Test remote worker daemon /system/update endpoint."""
    worker_app = create_worker_app(node_id="test-desktop-g45ipem")
    client = TestClient(worker_app)

    with patch("subprocess.run") as mock_subp:
        mock_subp.return_value = MagicMock(returncode=0, stdout="Already up to date.", stderr="")
        response = client.post("/system/update", json={"branch": "codex/usr-16-onprem-worker"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Already up to date" in data["output"]


def test_remote_worker_app_system_restart_endpoint() -> None:
    """Test remote worker daemon /system/restart endpoint returns restarting message."""
    worker_app = create_worker_app(node_id="test-desktop-g45ipem")
    client = TestClient(worker_app)

    with patch("core.harness.remote_worker.trigger_daemon_restart") as mock_restart:
        response = client.post("/system/restart")
        assert response.status_code == 200
        assert response.json()["status"] == "restarting"
        mock_restart.assert_called_once()


