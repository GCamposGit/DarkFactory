"""Tests for the darkfac-mailer client (USR-310)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from core.integrations import email_worker


class _FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _mailer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(email_worker.ENV_MAILER_URL, "https://darkfac-mailer.example.workers.dev")
    monkeypatch.setenv(email_worker.ENV_MAILER_TOKEN, "s3cr3t-token")


def test_send_grokbot_email_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["data"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(200, {"ok": True, "to": "guigrokchief@mail.grokbot.com"})

    monkeypatch.setattr(email_worker.urllib.request, "urlopen", fake_urlopen)

    result = email_worker.send_grokbot_email("Assunto", "Corpo do email")

    assert result == {"ok": True}
    assert captured["url"] == "https://darkfac-mailer.example.workers.dev"
    assert captured["headers"]["Authorization"] == "Bearer s3cr3t-token"
    assert captured["data"] == {"subject": "Assunto", "text": "Corpo do email"}


def test_send_grokbot_email_missing_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(email_worker.ENV_MAILER_URL, raising=False)
    monkeypatch.delenv(email_worker.ENV_MAILER_TOKEN, raising=False)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
    assert email_worker.ENV_MAILER_URL in result["error"]
    assert email_worker.ENV_MAILER_TOKEN in result["error"]


def test_send_grokbot_email_missing_only_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(email_worker.ENV_MAILER_TOKEN, raising=False)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
    assert "DARKFAC_MAILER_TOKEN" in result["error"]
    assert "s3cr3t" not in result["error"]


def test_send_grokbot_email_unauthorized_does_not_leak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"Unauthorized"))

    monkeypatch.setattr(email_worker.urllib.request, "urlopen", fake_urlopen)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
    assert "401" in result["error"]
    assert "s3cr3t-token" not in result["error"]


def test_send_grokbot_email_worker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 502, "Bad Gateway", {}, io.BytesIO(json.dumps({"ok": False, "error": "boom"}).encode())
        )

    monkeypatch.setattr(email_worker.urllib.request, "urlopen", fake_urlopen)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
    assert "502" in result["error"]


def test_send_grokbot_email_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(email_worker.urllib.request, "urlopen", fake_urlopen)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
    assert "network error" in result["error"]


def test_send_grokbot_email_unexpected_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(req, timeout=None):
        return _FakeResponse(200, {"ok": False, "error": "weird but 200"})

    monkeypatch.setattr(email_worker.urllib.request, "urlopen", fake_urlopen)

    result = email_worker.send_grokbot_email("Assunto", "Corpo")

    assert result["ok"] is False
