"""USR-336: headless harness children get no console window; Codex blocks by explicit opt-out."""
from __future__ import annotations

import logging
import subprocess
import sys

import pytest

from core.line import agent_cli, routing

NO_WINDOW = 0x08000000


def _fake_popen(captured: dict):
    class P:
        def __init__(self, argv, **kw):
            captured.update(kw)
            self.pid = 1
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            return "", ""

        def poll(self):
            return 0

    return P


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_run_bounded_creationflags(monkeypatch, platform):
    monkeypatch.setattr(agent_cli.sys, "platform", platform)
    if platform == "win32":
        monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", NO_WINDOW, raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 1, raising=False)
        monkeypatch.setattr(subprocess, "SW_HIDE", 0, raising=False)

        class SI:
            dwFlags = 0
            wShowWindow = 1

        monkeypatch.setattr(subprocess, "STARTUPINFO", SI, raising=False)
    captured: dict = {}
    monkeypatch.setattr(agent_cli.subprocess, "Popen", _fake_popen(captured))
    agent_cli._run_bounded(["codex", "exec"], timeout_s=5)
    if platform == "win32":
        assert captured["creationflags"] & NO_WINDOW
        assert captured["startupinfo"].wShowWindow == 0
    else:
        assert "creationflags" not in captured and "startupinfo" not in captured
        assert captured["start_new_session"] is True


def test_hidden_helpers_noop_off_windows(monkeypatch):
    monkeypatch.setattr(agent_cli.sys, "platform", "linux")
    assert agent_cli.hidden_creationflags(5) == 0
    assert agent_cli.hidden_popen_kwargs() == {}


def test_codex_allowed_by_default_on_windows(monkeypatch):
    monkeypatch.delenv("DARKFAC_CODEX_HEADLESS_ALLOWED", raising=False)
    monkeypatch.setattr(routing.sys, "platform", "win32")
    assert not routing.codex_headless_blocked()
    cfg = routing._default_routing_config()
    cands = routing._routable_candidates(
        "development", cfg.stages["development"], cfg,
        {"harness:claude", "harness:codex"}, "write", set(), set(), None,
    )
    assert ("codex", None) in cands


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_codex_blocked_by_explicit_negative_opt_out_on_windows(monkeypatch, caplog, value):
    monkeypatch.setattr(routing.sys, "platform", "win32")
    monkeypatch.setenv("DARKFAC_CODEX_HEADLESS_ALLOWED", value)
    assert routing.codex_headless_blocked()
    cfg = routing._default_routing_config()
    with caplog.at_level(logging.INFO):
        cands = routing._routable_candidates(
            "development", cfg.stages["development"], cfg,
            {"harness:claude", "harness:codex"}, "write", set(), set(), None,
        )
    assert ("codex", None) not in cands and any(h == "claude" for h, _ in cands)
    assert routing.CODEX_HEADLESS_BLOCK_REASON in caplog.text
    assert "DARKFAC_CODEX_HEADLESS_ALLOWED" in caplog.text


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything-else"])
def test_codex_allowed_by_affirmative_or_unrecognized_value_on_windows(monkeypatch, value):
    monkeypatch.setattr(routing.sys, "platform", "win32")
    monkeypatch.setenv("DARKFAC_CODEX_HEADLESS_ALLOWED", value)
    assert not routing.codex_headless_blocked()


def test_codex_never_blocked_off_windows_even_with_opt_out(monkeypatch):
    monkeypatch.setenv("DARKFAC_CODEX_HEADLESS_ALLOWED", "false")
    monkeypatch.setattr(routing.sys, "platform", "linux")
    assert not routing.codex_headless_blocked()
