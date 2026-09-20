"""Unit tests for multi-harness endpoints in remote_worker.py (HF-07-02)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.harness.remote_worker import create_worker_app


def test_remote_worker_health_includes_harnesses(tmp_path) -> None:
    """Test /health endpoint reports detected harnesses on the node."""
    app = create_worker_app(project_root=tmp_path, node_id="test-multi-node")
    client = TestClient(app)

    with patch("core.harness.remote_worker.find_codex_binary", return_value="fake_codex.cmd"):
        with patch("core.harness.remote_worker.find_grok_binary", return_value="fake_grok.exe"):
            response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["node_id"] == "test-multi-node"
    assert "codex" in data["available_harnesses"]
    assert "grok" in data["available_harnesses"]


def test_remote_worker_execute_grok_mocked(tmp_path) -> None:
    """Test unified /harness/execute endpoint dispatching to Grok."""
    app = create_worker_app(project_root=tmp_path, node_id="test-multi-node")
    client = TestClient(app)

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "Grok analysis complete: architecture valid."
    fake_proc.stderr = ""

    with patch("core.harness.remote_worker.find_grok_binary", return_value="fake_grok.exe"):
        with patch("subprocess.run", return_value=fake_proc):
            response = client.post(
                "/harness/execute",
                json={
                    "harness": "grok",
                    "prompt": "Evaluate system architecture",
                    "system_prompt": "You are a software architect",
                    "timeout_seconds": 30,
                },
            )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["harness"] == "grok"
    assert "Grok analysis complete" in data["text"]


def test_remote_worker_dedicated_grok_endpoint(tmp_path) -> None:
    """Test dedicated /harness/grok endpoint."""
    app = create_worker_app(project_root=tmp_path, node_id="test-multi-node")
    client = TestClient(app)

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "Grok dedicated response."
    fake_proc.stderr = ""

    with patch("core.harness.remote_worker.find_grok_binary", return_value="fake_grok.exe"):
        with patch("subprocess.run", return_value=fake_proc):
            response = client.post(
                "/harness/grok",
                json={"prompt": "Ping grok", "timeout_seconds": 15},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["harness"] == "grok"
    assert data["text"] == "Grok dedicated response."


def test_remote_worker_dedicated_antigravity_endpoint(tmp_path) -> None:
    """Test dedicated /harness/antigravity endpoint."""
    app = create_worker_app(project_root=tmp_path, node_id="test-multi-node")
    client = TestClient(app)

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "Antigravity conversation synthesized."
    fake_proc.stderr = ""

    with patch("core.harness.remote_worker.find_antigravity_binary", return_value="fake_agentapi.bat"):
        with patch("subprocess.run", return_value=fake_proc):
            response = client.post(
                "/harness/antigravity",
                json={"prompt": "Summarize run", "model": "flash", "timeout_seconds": 15},
            )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["harness"] == "antigravity"
    assert data["text"] == "Antigravity conversation synthesized."


def test_remote_worker_unsupported_harness(tmp_path) -> None:
    """Test unknown harness identifier returns clean error without crashing."""
    app = create_worker_app(project_root=tmp_path, node_id="test-multi-node")
    client = TestClient(app)

    response = client.post(
        "/harness/execute",
        json={"harness": "unknown_ai", "prompt": "Hello"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert "Unsupported harness identifier" in data["error"]
