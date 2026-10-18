"""USR-271: Grok read mode must not wait on an unavailable optional MCP server.

Everything is faked (`_run_bounded`, `find_grok_binary`, the config file and the socket probe): no real
Grok call and no real sleeps.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.line import agent_cli
from core.line.agent_cli import (
    AgentRequest,
    BoundedProcessResult,
    check_grok_optional_mcp_servers,
    filter_grok_config_text,
    run_agent,
)

_CONFIG = """disabled_mcp_servers = ["off_one"]

[mcp_servers.up]
url = "http://127.0.0.1:18788/mcp"
enabled = true

[mcp_servers.up.headers]
Authorization = "Bearer x"

[mcp_servers.down]
url = "http://127.0.0.1:18790/mcp"
enabled = true

[mcp_servers.down.headers]
Authorization = "Bearer y"

[mcp_servers.off_one]
url = "http://127.0.0.1:1/mcp"

[mcp_servers.flagged_off]
url = "http://127.0.0.1:2/mcp"
enabled = false

[ui]
simple_mode = true
"""


@pytest.fixture()
def grok_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "grokhome"
    home.mkdir()
    (home / "config.toml").write_text(_CONFIG, encoding="utf-8")
    (home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(home))
    monkeypatch.delenv("DARKFAC_STRICT_MCP_CONFIG", raising=False)
    monkeypatch.setattr(agent_cli, "find_grok_binary", lambda: "grok")

    def fake_connect(addr: tuple[str, int], timeout: float = 0) -> Any:
        if addr[1] == 18790:
            raise ConnectionRefusedError("refused")
        import contextlib

        return contextlib.nullcontext()

    monkeypatch.setattr(agent_cli.socket, "create_connection", fake_connect)
    return home


def _req() -> AgentRequest:
    return AgentRequest(prompt="review", mode="read", harness="grok", timeout_s=240, cwd=Path("."))


def test_probe_only_flags_enabled_unreachable_servers(grok_home: Path) -> None:
    names, reasons = check_grok_optional_mcp_servers()
    assert names == ["down"]
    assert "down" in reasons[0]


def test_filter_removes_only_dropped_tables() -> None:
    out = filter_grok_config_text(_CONFIG, ["down"])
    assert "mcp_servers.down" not in out and "18790" not in out and "Bearer y" not in out
    assert "[mcp_servers.up.headers]" in out and "[ui]" in out and "simple_mode" in out


def test_read_launches_with_isolated_home_without_down_server(grok_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> BoundedProcessResult:
        home = Path(kw["env"]["GROK_HOME"])
        seen["home"] = home
        seen["config"] = (home / "config.toml").read_text(encoding="utf-8")
        seen["auth"] = (home / "auth.json").exists()
        return BoundedProcessResult(stdout="APPROVED", returncode=0, duration_s=1.0)

    monkeypatch.setattr(agent_cli, "_run_bounded", fake_run)
    res = run_agent(_req())
    assert res.ok and res.text == "APPROVED"
    assert seen["home"] != grok_home and "18790" not in seen["config"] and "18788" in seen["config"]
    assert seen["auth"] is True
    assert not seen["home"].exists()  # throwaway home cleaned up
    assert "18790" in (grok_home / "config.toml").read_text(encoding="utf-8")  # user config untouched


def test_healthy_mcp_keeps_user_home(grok_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_cli.socket, "create_connection", lambda *a, **k: __import__("contextlib").nullcontext())
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kw: Any) -> BoundedProcessResult:
        seen["env"] = kw.get("env")
        return BoundedProcessResult(stdout="ok", returncode=0)

    monkeypatch.setattr(agent_cli, "_run_bounded", fake_run)
    assert run_agent(_req()).ok
    assert seen["env"] is None


def test_mcp_init_failure_is_infra_error_not_approval(grok_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = "init failed segundocerebro_fibra http://127.0.0.1:18790/mcp ConnectionRefused"
    monkeypatch.setattr(
        agent_cli, "_run_bounded", lambda argv, **kw: BoundedProcessResult(stdout="", stderr=err, returncode=1)
    )
    res = run_agent(_req())
    assert not res.ok
    assert res.error_kind == "harness_infra_error"
    assert res.text.startswith("mcp_unavailable")


def test_timeout_with_mcp_noise_is_infra_error(grok_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    err = "init failed x http://127.0.0.1:18790/mcp ConnectionRefused"
    monkeypatch.setattr(
        agent_cli,
        "_run_bounded",
        lambda argv, **kw: BoundedProcessResult(stderr=err, returncode=-1, timed_out=True, duration_s=240.0),
    )
    res = run_agent(_req())
    assert not res.ok and res.error_kind == "harness_infra_error"
    assert "mcp_unavailable" in res.text and "timed out after 240" in res.text


def test_plain_timeout_stays_timeout(grok_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_cli.socket, "create_connection", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(
        agent_cli, "_run_bounded", lambda argv, **kw: BoundedProcessResult(timed_out=True, duration_s=240.0)
    )
    res = run_agent(_req())
    assert not res.ok and res.error_kind == "timeout"
