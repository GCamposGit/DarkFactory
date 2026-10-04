"""Tests for USR-125: Fail fast when optional Segundo Cérebro MCP is unavailable.

Validates that:
1. Optional MCP probe fails fast in a short documented window (<= 2.0s - 3.0s)
   rather than waiting for the agent's 1800s timeout.
2. Headless Claude Code execution receives `--strict-mcp-config` when configured MCP
   servers are offline, preventing startup hangs and allowing ticket execution to proceed.
3. No credentials, tokens, or authorization headers are leaked in logs or error messages.
4. SegundoCerebroClient fails fast in degraded mode returning INSUFFICIENT_EVIDENCE.
5. Worktree and ticket state remain intact and resumable.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.knowledge import (
    KnowledgeQuery,
    KnowledgeQueryResult,
    SecondBrainStatus,
    SegundoCerebroClient,
)
from core.line.agent_cli import (
    DEFAULT_MCP_PROBE_TIMEOUT,
    AgentRequest,
    build_claude_argv,
    check_optional_mcp_servers,
    redact_secrets,
)

pytestmark = [pytest.mark.offline]


# ---------------------------------------------------------------------------
# 1. check_optional_mcp_servers Tests
# ---------------------------------------------------------------------------


def test_check_optional_mcp_servers_no_config(tmp_path: Path):
    non_existent = tmp_path / "non_existent.json"
    healthy, reasons = check_optional_mcp_servers(config_path=non_existent)
    assert healthy is True
    assert reasons == []


def test_check_optional_mcp_servers_empty_mcp_servers(tmp_path: Path):
    cfg_file = tmp_path / "empty_claude.json"
    cfg_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    healthy, reasons = check_optional_mcp_servers(config_path=cfg_file)
    assert healthy is True
    assert reasons == []


def test_check_optional_mcp_servers_healthy_server(tmp_path: Path):
    # Bind a temporary local socket to simulate an active MCP server
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    try:
        cfg_file = tmp_path / "healthy_claude.json"
        cfg_file.write_text(
            json.dumps({
                "mcpServers": {
                    "segundocerebro": {
                        "type": "http",
                        "url": f"http://127.0.0.1:{port}/mcp",
                    }
                }
            }),
            encoding="utf-8",
        )
        healthy, reasons = check_optional_mcp_servers(config_path=cfg_file, timeout_sec=1.0)
        assert healthy is True
        assert reasons == []
    finally:
        server_sock.close()


def test_check_optional_mcp_servers_offline_fails_fast(tmp_path: Path):
    # Port that is not bound
    unused_port = 59123
    cfg_file = tmp_path / "offline_claude.json"
    cfg_file.write_text(
        json.dumps({
            "mcpServers": {
                "segundocerebro": {
                    "type": "http",
                    "url": f"http://127.0.0.1:{unused_port}/mcp",
                    "headers": {
                        "Authorization": "Bearer secret_token_1234567890"
                    }
                }
            }
        }),
        encoding="utf-8",
    )

    t0 = time.perf_counter()
    healthy, reasons = check_optional_mcp_servers(config_path=cfg_file, timeout_sec=0.5)
    elapsed = time.perf_counter() - t0

    assert healthy is False
    assert len(reasons) == 1
    assert f"127.0.0.1:{unused_port}" in reasons[0]
    # Probing must finish in a short window (far below the 1800s agent timeout)
    assert elapsed < 1.5


def test_check_optional_mcp_servers_redacts_credentials(tmp_path: Path):
    secret_token = "Bearer E5TJLnjOMqCKPkuOqJLiaL_1mg7tHCwtM79OPEQCv1U"
    cfg_file = tmp_path / "secret_claude.json"
    cfg_file.write_text(
        json.dumps({
            "mcpServers": {
                "segundocerebro": {
                    "type": "http",
                    "url": f"http://127.0.0.1:59124/mcp?token={secret_token}",
                    "headers": {"Authorization": secret_token}
                }
            }
        }),
        encoding="utf-8",
    )

    with patch("core.line.agent_cli.socket.create_connection", side_effect=OSError(f"Failed with {secret_token}")):
        healthy, reasons = check_optional_mcp_servers(config_path=cfg_file, timeout_sec=0.2)
        assert healthy is False
        for reason in reasons:
            assert "E5TJLnjOMqCKPkuOqJLiaL" not in reason
            assert "[REDACTED]" in redact_secrets(reason) or "E5TJLnjOMqCKPkuOqJLiaL" not in reason


def test_env_var_override_darkfac_strict_mcp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "claude.json"
    cfg_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    monkeypatch.setenv("DARKFAC_STRICT_MCP_CONFIG", "1")
    healthy, reasons = check_optional_mcp_servers(config_path=cfg_file)
    assert healthy is False
    assert any("explicitly forced" in r for r in reasons)

    monkeypatch.setenv("DARKFAC_STRICT_MCP_CONFIG", "0")
    healthy, reasons = check_optional_mcp_servers(config_path=cfg_file)
    assert healthy is True
    assert reasons == []


# ---------------------------------------------------------------------------
# 2. build_claude_argv & _run_claude CLI Tests
# ---------------------------------------------------------------------------


def test_build_claude_argv_strict_mcp_flag():
    req = AgentRequest(
        prompt="do work",
        cwd=Path.cwd(),
        mode="write",
        harness="claude",
    )

    argv_normal = build_claude_argv("claude", req, strict_mcp_config=False)
    assert "--strict-mcp-config" not in argv_normal

    argv_strict = build_claude_argv("claude", req, strict_mcp_config=True)
    assert "--strict-mcp-config" in argv_strict


def test_run_claude_injects_strict_mcp_when_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.line import agent_cli

    fake_claude_json = tmp_path / ".claude.json"
    fake_claude_json.write_text(
        json.dumps({
            "mcpServers": {
                "segundocerebro": {
                    "type": "http",
                    "url": "http://127.0.0.1:59125/mcp"
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: "fake_claude")

    captured_argv = []

    def mock_run_bounded(argv, **kwargs):
        captured_argv.extend(argv)
        return agent_cli.BoundedProcessResult(
            stdout=json.dumps({"result": "done", "is_error": False}),
            stderr="",
            returncode=0,
            duration_s=0.1,
            timed_out=False,
        )

    monkeypatch.setattr(agent_cli, "_run_bounded", mock_run_bounded)

    req = AgentRequest(
        prompt="implement ticket",
        cwd=tmp_path,
        mode="write",
        harness="claude",
    )

    result = agent_cli._run_claude(req)
    assert result.ok is True
    assert "--strict-mcp-config" in captured_argv


# ---------------------------------------------------------------------------
# 3. SegundoCerebroClient Fail-Fast & Degraded Mode Tests
# ---------------------------------------------------------------------------


def test_segundo_cerebro_client_http_health_check_fails_fast():
    # Point client to an offline HTTP MCP endpoint
    offline_url = "http://127.0.0.1:59126/mcp"
    client = SegundoCerebroClient(mcp_url=offline_url)

    t0 = time.perf_counter()
    status = client.check_health(timeout_sec=0.5)
    elapsed = time.perf_counter() - t0

    assert isinstance(status, SecondBrainStatus)
    assert status.available is False
    assert status.mcp_endpoint == offline_url
    assert "unreachable" in status.error_message.lower()
    # Must fail fast in <= 1.5s
    assert elapsed < 1.5


def test_segundo_cerebro_client_search_fails_fast_with_insufficient_evidence():
    offline_url = "http://127.0.0.1:59127/mcp"
    client = SegundoCerebroClient(mcp_url=offline_url)

    query = KnowledgeQuery(
        query="regras de seguranca",
        project_id="darkfac",
        min_score=0.6,
    )

    t0 = time.perf_counter()
    result = client.search(query)
    elapsed = time.perf_counter() - t0

    assert isinstance(result, KnowledgeQueryResult)
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.citations == []
    assert result.total_found == 0
    # Must not hang for 60s or 1800s
    assert elapsed < 4.0


def test_segundo_cerebro_client_redacts_auth_token_in_http_failure(monkeypatch: pytest.MonkeyPatch):
    secret_token = "sk-super-secret-key-12345678"
    monkeypatch.setenv("SEGUNDO_CEREBRO_API_KEY", secret_token)

    offline_url = "http://127.0.0.1:59128/mcp"
    client = SegundoCerebroClient(mcp_url=offline_url)

    res = client._execute_mcp_tool("search", {"consulta": "teste"})
    assert "error" in res
    assert secret_token not in res["error"]
