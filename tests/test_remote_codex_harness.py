"""Unit tests for remote worker Codex endpoint and RemoteCodexModelProvider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.execution.providers import RemoteCodexModelProvider, get_model_provider
from core.harness.remote_worker import (
    CodexExecutionRequest,
    CodexExecutionResponse,
    create_worker_app,
)


def test_remote_worker_codex_endpoint_mocked(tmp_path) -> None:
    """Test /harness/codex endpoint using FastAPI TestClient with mocked codex process."""
    app = create_worker_app(project_root=tmp_path, node_id="test-node")
    client = TestClient(app)

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stdout = "def generate_slug(): pass"
    fake_proc.stderr = "model: gpt-5.6-sol\ntokens used: 42"

    with patch("core.harness.remote_worker.find_codex_binary", return_value="fake_codex.cmd"):
        with patch("subprocess.run", return_value=fake_proc):
            response = client.post(
                "/harness/codex",
                json={
                    "prompt": "Create a slugifier function",
                    "system_prompt": "You are a code assistant",
                    "timeout_seconds": 30,
                },
            )

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["text"] == "def generate_slug(): pass"
    assert data["model"] == "gpt-5.6-sol"


def test_remote_codex_provider_generate_success() -> None:
    """Test RemoteCodexModelProvider parses successful response."""
    provider = RemoteCodexModelProvider(base_url="http://fake-node:8080")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "success": True,
        "text": "def test_slug(): assert True",
        "model": "gpt-5.6-sol",
        "tokens_used": 55,
        "duration_seconds": 1.2,
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = provider.generate("Generate slug test", model="codex")

    assert result.text == "def test_slug(): assert True"
    assert result.model == "gpt-5.6-sol"
    assert result.measured_cost == 0.0
    assert result.is_measured is True
    assert result.metadata["backend"] == "remote_codex"


def test_remote_codex_provider_generate_error() -> None:
    """Test RemoteCodexModelProvider raises on failed response."""
    provider = RemoteCodexModelProvider(base_url="http://fake-node:8080")

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({
        "success": False,
        "error": "Session token expired",
    }).encode("utf-8")
    mock_response.__enter__.return_value = mock_response

    with patch("urllib.request.urlopen", return_value=mock_response):
        with pytest.raises(RuntimeError, match="Session token expired"):
            provider.generate("Generate slug test", model="codex")
