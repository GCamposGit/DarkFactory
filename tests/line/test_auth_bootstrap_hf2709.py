"""Tests for core.line.auth_bootstrap (HF-27-09).

Every test uses the fake-CLI fixtures from tests/line/conftest.py or a
monkeypatched `find_codex_binary`, so no real Claude/Codex CLI, no network
call and no Telegram bot token is ever touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.line import agent_cli, auth_bootstrap
from tests.line.conftest import set_fake_response, write_python_shim


def test_probe_claude_ok(make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {"stdout": json.dumps({"result": "ok", "is_error": False}), "returncode": 0},
    )

    result = auth_bootstrap.probe_claude()
    assert result.ok is True
    assert result.harness == "claude"


def test_probe_claude_auth_expired(make_fake_cli, monkeypatch):
    cmd_path, env_var = make_fake_cli("claude")
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: cmd_path)
    set_fake_response(
        monkeypatch,
        env_var,
        {
            "stdout": json.dumps({"result": "please login again, token expired", "is_error": True}),
            "returncode": 1,
        },
    )

    result = auth_bootstrap.probe_claude()
    assert result.ok is False
    assert result.detail == "auth_expired"


def test_probe_claude_not_installed(monkeypatch):
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: None)
    result = auth_bootstrap.probe_claude()
    assert result.ok is False
    assert result.detail == "not_installed"


def test_probe_codex_status_ok(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_codex_status.py"
    script.write_text(
        "import sys\nsys.stdout.write('Logged in as darkfac@example.com')\nsys.exit(0)\n",
        encoding="utf-8",
    )
    shim = write_python_shim(tmp_path / "fake_codex_status", script)
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: str(shim))

    result = auth_bootstrap.probe_codex_status()
    assert result.ok is True
    assert result.harness == "codex"


def test_probe_codex_status_not_logged_in(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_codex_status_fail.py"
    script.write_text(
        "import sys\nsys.stdout.write('Not logged in')\nsys.exit(1)\n",
        encoding="utf-8",
    )
    shim = write_python_shim(tmp_path / "fake_codex_status_fail", script)
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: str(shim))

    result = auth_bootstrap.probe_codex_status()
    assert result.ok is False


def test_probe_codex_status_not_installed(monkeypatch):
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: None)
    result = auth_bootstrap.probe_codex_status()
    assert result.ok is False
    assert result.detail == "not_installed"


def test_probe_codex_status_timeout(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_codex_status_slow.py"
    script.write_text(
        "import time, sys\ntime.sleep(2)\nsys.exit(0)\n",
        encoding="utf-8",
    )
    shim = write_python_shim(tmp_path / "fake_codex_status_slow", script)
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: str(shim))

    result = auth_bootstrap.probe_codex_status(timeout_s=0.2)
    assert result.ok is False
    assert result.detail == "timeout"


def test_probe_harness_auth_unknown_harness_assumed_ok():
    # No registered probe for grok/antigravity yet: never drop what we cannot evaluate.
    assert auth_bootstrap.probe_harness_auth("grok") is True
    assert auth_bootstrap.probe_harness_auth("antigravity") is True


def test_probe_harness_auth_dispatches_registered_probe(monkeypatch):
    monkeypatch.setitem(
        auth_bootstrap._PROBES,
        "claude",
        lambda: auth_bootstrap.HarnessProbeResult(harness="claude", ok=False, detail="forced"),
    )
    assert auth_bootstrap.probe_harness_auth("claude") is False


def test_probe_harness_auth_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setitem(auth_bootstrap._PROBES, "claude", _boom)
    assert auth_bootstrap.probe_harness_auth("claude") is False


def test_filter_capabilities_drops_failed_harness():
    caps = ["git", "gh", "harness:claude", "harness:codex"]
    kept = auth_bootstrap.filter_capabilities_by_auth(
        caps, prober=lambda h: h != "codex"
    )
    assert kept == ["git", "gh", "harness:claude"]


def test_filter_capabilities_keeps_non_harness_untouched():
    caps = ["git", "gh", "node", "python"]
    kept = auth_bootstrap.filter_capabilities_by_auth(caps, prober=lambda h: False)
    assert kept == caps


def test_filter_capabilities_prober_exception_drops_capability():
    def _boom(_harness: str) -> bool:
        raise RuntimeError("probe crashed")

    kept = auth_bootstrap.filter_capabilities_by_auth(["harness:claude"], prober=_boom)
    assert kept == []


def test_bootstrap_codex_login_parses_url_and_code_and_notifies(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_codex_device_auth.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.write('Open https://auth.openai.com/device and enter code AB12-CD34')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    shim = write_python_shim(tmp_path / "fake_codex_device_auth", script)
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: str(shim))
    monkeypatch.setattr(
        auth_bootstrap,
        "probe_codex_status",
        lambda: auth_bootstrap.HarnessProbeResult(harness="codex", ok=True, detail="ok"),
    )

    sent_messages: list[str] = []
    result = auth_bootstrap.bootstrap_codex_login(notifier=lambda msg: sent_messages.append(msg) or True)

    assert result.ok is True
    assert len(sent_messages) == 1
    assert "https://auth.openai.com/device" in sent_messages[0]
    assert "AB12-CD34" in sent_messages[0]


def test_bootstrap_codex_login_missing_binary(monkeypatch):
    monkeypatch.setattr(auth_bootstrap, "find_codex_binary", lambda: None)
    result = auth_bootstrap.bootstrap_codex_login(notifier=lambda msg: True)
    assert result.ok is False
    assert result.detail == "not_installed"


def test_bootstrap_claude_login_sends_instructions_and_probes(monkeypatch):
    monkeypatch.setattr(
        auth_bootstrap,
        "probe_claude",
        lambda: auth_bootstrap.HarnessProbeResult(harness="claude", ok=False, detail="not_installed"),
    )
    sent_messages: list[str] = []
    result = auth_bootstrap.bootstrap_claude_login(notifier=lambda msg: sent_messages.append(msg) or True)

    assert result.ok is False
    assert len(sent_messages) == 1
    assert "claude setup-token" in sent_messages[0]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in sent_messages[0]


def test_notify_human_without_telegram_config_returns_false(monkeypatch):
    # Force an unconfigured Telegram gateway (no chat ids) regardless of any
    # local .env on the machine running the suite, so this never dials out.
    from core.integrations.telegram import TelegramConfig

    monkeypatch.setattr(
        "core.integrations.telegram.load_telegram_config",
        lambda *args, **kwargs: TelegramConfig(),
    )
    result = auth_bootstrap._notify_human("test message")
    assert result is False


def test_cli_main_prints_json_and_exit_code(monkeypatch, capsys):
    monkeypatch.setitem(
        auth_bootstrap._BOOTSTRAP,
        "codex",
        lambda: auth_bootstrap.HarnessProbeResult(harness="codex", ok=True, detail="ok"),
    )
    exit_code = auth_bootstrap.main(["codex"])
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert payload["harness"] == "codex"
    assert payload["ok"] is True


def test_cli_main_nonzero_exit_on_failed_probe(monkeypatch, capsys):
    monkeypatch.setitem(
        auth_bootstrap._BOOTSTRAP,
        "claude",
        lambda: auth_bootstrap.HarnessProbeResult(harness="claude", ok=False, detail="not_installed"),
    )
    exit_code = auth_bootstrap.main(["claude"])
    assert exit_code == 1


def test_cli_main_rejects_unknown_harness():
    with pytest.raises(SystemExit):
        auth_bootstrap.main(["unknown-harness"])
