"""Tests for Telegram Token Rotation, Visibility, and Poller Robustness (USR-110)."""

from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.integrations.telegram import (
    TelegramConfig,
    TelegramGateway,
    _token_fp,
    load_telegram_config,
)
from core.integrations.telegram_webhooks import (
    get_webhook_registration_status,
    register_telegram_webhooks,
)
from hub.backend.main import app
from hub.backend.service import HubService
from scripts.telegram_rotate_check import (
    collect_sources,
    mask_token,
    probe_telegram_api,
    read_env_file,
)


def test_mask_token() -> None:
    assert mask_token(None) == "(vazio)"
    assert mask_token("") == "(vazio)"
    assert mask_token("123") == "***"
    assert mask_token("1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ") == "1234...wxyZ"
    assert _token_fp("1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ") == "1234...wxyZ"


def test_read_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Comment line\n"
        "TELEGRAM_OPS_BOT_TOKEN=123:ops\n"
        "TELEGRAM_OWNER_BOT_TOKEN='456:owner'\n"
        "EMPTY_VAR=\n",
        encoding="utf-8",
    )
    res = read_env_file(env_file)
    assert res["TELEGRAM_OPS_BOT_TOKEN"] == "123:ops"
    assert res["TELEGRAM_OWNER_BOT_TOKEN"] == "456:owner"
    assert res["EMPTY_VAR"] == ""


def test_webhook_registration_status_tracking_and_error_logging(caplog: pytest.LogCaptureHandler) -> None:
    fake_env = {
        "DARKHUB_ENV": "production",
        "DARKHUB_PUBLIC_URL": "https://darkhub.test",
        "TELEGRAM_OPS_BOT_TOKEN": "111:token_ops",
        "TELEGRAM_OWNER_BOT_TOKEN": "222:token_owner",
        "TELEGRAM_WEBHOOK_SECRET": "secret123",
    }

    # Simulate getWebhookInfo returning empty, and setWebhook failing with HTTP 401
    def fake_http(method: str, url: str, payload: Any) -> dict[str, Any]:
        if "getWebhookInfo" in url:
            return {"ok": True, "result": {"url": ""}}
        raise urllib.error.HTTPError(url=url, code=401, msg="Unauthorized", hdrs=None, fp=None)

    with caplog.at_level(logging.ERROR):
        results = register_telegram_webhooks(fake_env, http=fake_http, force=True)

    assert results.get("ops") == "failed"
    assert results.get("owner") == "failed"

    status = get_webhook_registration_status()
    assert "ops" in status
    assert status["ops"]["status"] == "failed"
    assert status["ops"]["last_error"] == "HTTP 401"
    assert "111:token_ops" not in str(status)  # never leaks token

    # Ensure error level log was emitted and token is NOT in log
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("Telegram webhook registration failed" in r.message for r in error_records)
    assert not any("111:token_ops" in r.message for r in error_records)


def test_load_telegram_config_warns_on_token_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureHandler
) -> None:
    config_dir = tmp_path / "project" / ".factory" / "telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "ops_config.json").write_text(
        json.dumps({"bot_token": "token_from_file_12345678"}), encoding="utf-8"
    )

    # In environment, set a DIFFERENT token
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "token_from_env_87654321")

    with caplog.at_level(logging.WARNING):
        cfg = load_telegram_config(role="ops", config_dir=config_dir)

    assert cfg.bot_token == "token_from_env_87654321"  # env still takes precedence
    # Check that warning about divergence was logged
    warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Telegram token sources diverge" in m for m in warn_msgs)
    # Check that token was masked, never logged in full
    assert not any("token_from_env_87654321" in m for m in warn_msgs)
    assert not any("token_from_file_12345678" in m for m in warn_msgs)


def test_poll_updates_handles_401_and_409_without_token_leak(caplog: pytest.LogCaptureHandler) -> None:
    cfg = TelegramConfig(
        bot_token="test_token_12345678",
        role="ops",
    )
    gw = TelegramGateway(cfg)

    # 1. Test HTTP 401
    with patch("urllib.request.urlopen") as mock_url:
        mock_url.side_effect = urllib.error.HTTPError("http://api/test", 401, "Unauthorized", None, None)
        with caplog.at_level(logging.ERROR):
            res = gw.poll_updates()
            assert res == []

    err_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any("HTTP 401 Unauthorized" in m for m in err_msgs)
    assert not any("test_token_12345678" in m for m in err_msgs)

    # 2. Test HTTP 409
    caplog.clear()
    with patch("urllib.request.urlopen") as mock_url:
        mock_url.side_effect = urllib.error.HTTPError("http://api/test", 409, "Conflict", None, None)
        with caplog.at_level(logging.ERROR):
            res = gw.poll_updates()
            assert res == []

    err_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
    assert any("HTTP 409 Conflict" in m for m in err_msgs)
    assert not any("test_token_12345678" in m for m in err_msgs)


def test_hub_status_includes_webhooks(tmp_path: Path) -> None:
    client = TestClient(app)
    resp = client.get("/api/integrations/telegram/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "webhooks" in data
    assert isinstance(data["webhooks"], dict)


def test_telegram_rotate_check_probe_sanitization() -> None:
    with patch("urllib.request.urlopen") as mock_url:
        mock_url.side_effect = urllib.error.HTTPError("http://api/secret_tok", 401, "Unauthorized", None, None)
        probe = probe_telegram_api("secret_token_12345")
        assert probe["get_me_ok"] is False
        assert "401" in probe["get_me_error"]
        assert "secret_tok" not in probe["get_me_error"]
