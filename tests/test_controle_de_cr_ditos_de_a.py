"""Tests for USR-12: API Credits & Billing Monitor ($).

Reachability Contract:
python -m pytest tests/test_controle_de_cr_ditos_de_a.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.usage.api_credits import (
    ApiCreditsMonitor,
    ApiCreditsReport,
    CreditAccountStatus,
    CreditOfficialLink,
    ProviderCreditCard,
)
from hub.backend.main import app


def test_credit_models_contract():
    """Verify that credit models enforce types and correct totals."""
    link1 = CreditOfficialLink(title="Activity", url="https://openrouter.ai/activity")
    link2 = CreditOfficialLink(title="Credits", url="https://openrouter.ai/settings/credits")
    card_openrouter = ProviderCreditCard(
        provider_id="openrouter",
        provider_name="OpenRouter",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        current_month_spend_usd=4.50,
        available_credit_usd=20.50,
        official_links=[link1, link2],
    )
    card_openai = ProviderCreditCard(
        provider_id="openai",
        provider_name="OpenAI API",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        current_month_spend_usd=12.30,
        available_credit_usd=None,
        official_links=[CreditOfficialLink(title="OpenAI Platform", url="https://platform.openai.com/home")],
    )

    report = ApiCreditsReport(
        accounts=[card_openrouter, card_openai],
        total_month_spend_usd=16.80,
        total_available_credit_usd=20.50,
    )

    assert report.total_month_spend_usd == 16.80
    assert report.total_available_credit_usd == 20.50
    assert len(report.accounts) == 2
    assert report.accounts[0].provider_id == "openrouter"
    assert report.accounts[0].available_credit_usd == 20.50
    assert report.accounts[1].official_links[0].url == "https://platform.openai.com/home"


def test_openrouter_inspection_with_mocked_network():
    """Verify OpenRouter credit balance and spend calculation."""
    monitor = ApiCreditsMonitor()

    mock_credits_response = json.dumps({
        "data": {
            "total_credits": 30.0,
            "total_usage": 8.75
        }
    }).encode("utf-8")

    mock_auth_response = json.dumps({
        "data": {
            "label": "darkfac-key",
            "usage": 8.75,
            "limit": 100.0,
            "is_free_tier": False
        }
    }).encode("utf-8")

    def fake_urlopen(req, timeout=6.0):
        url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        mock_resp = MagicMock()
        if "credits" in url:
            mock_resp.read.return_value = mock_credits_response
        else:
            mock_resp.read.return_value = mock_auth_response
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    with patch.object(monitor, "get_openrouter_key", return_value="sk-or-fake-key"):
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            card = monitor.inspect_openrouter()

            assert card.provider_id == "openrouter"
            assert card.status == CreditAccountStatus.ACTIVE
            assert card.is_connected is True
            assert card.available_credit_usd == pytest.approx(21.25)
            assert card.current_month_spend_usd == pytest.approx(8.75)
            assert any("openrouter.ai/activity" in l.url for l in card.official_links)
            assert any("openrouter.ai/settings/credits" in l.url for l in card.official_links)


def test_openrouter_inspection_without_key():
    """Verify OpenRouter gracefully reports disconnected when no key is set."""
    monitor = ApiCreditsMonitor()
    with patch.object(monitor, "get_openrouter_key", return_value=None):
        card = monitor.inspect_openrouter()
        assert card.provider_id == "openrouter"
        assert card.status == CreditAccountStatus.DISCONNECTED
        assert card.is_connected is False
        assert card.available_credit_usd is None
        assert card.current_month_spend_usd is None
        assert any("openrouter.ai/settings/credits" in l.url for l in card.official_links)


def test_openai_inspection_with_key_and_snapshot(tmp_path: Path):
    """Verify OpenAI credits resolution with key and local/env snapshot support."""
    snapshot_file = tmp_path / "openai.json"
    snapshot_file.write_text(
        json.dumps({
            "status": "active",
            "current_month_spend_usd": 15.20,
            "available_credit_usd": 40.00,
            "notes": "Snapshot faturamento aprovado"
        }),
        encoding="utf-8"
    )

    monitor = ApiCreditsMonitor(snapshot_dir=tmp_path)
    with patch.object(monitor, "get_openai_key", return_value="sk-fake-key"):
        card = monitor.inspect_openai()
        assert card.provider_id == "openai"
        assert card.status == CreditAccountStatus.ACTIVE
        assert card.is_connected is True
        assert card.current_month_spend_usd == 15.20
        assert card.available_credit_usd == 40.00
        assert any("platform.openai.com/home" in l.url for l in card.official_links)


def test_planned_providers_placeholders():
    """Verify placeholders for all planned accounts with status and links."""
    monitor = ApiCreditsMonitor()
    planned = monitor.inspect_planned_providers()

    expected_providers = {
        "anthropic", "google", "deepseek", "xai", "moonshot",
        "qwen", "siliconflow", "minimax", "zhipu"
    }

    found_providers = {p.provider_id for p in planned}
    assert expected_providers.issubset(found_providers), f"Missing planned providers: {expected_providers - found_providers}"

    for p in planned:
        assert p.status == CreditAccountStatus.PLANNING
        assert p.is_connected is False
        assert len(p.official_links) > 0
        assert p.official_links[0].url.startswith("http")


def test_api_credits_report_aggregation():
    """Verify full report generation aggregates spend and available credit."""
    monitor = ApiCreditsMonitor()

    fake_openrouter = ProviderCreditCard(
        provider_id="openrouter",
        provider_name="OpenRouter",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        current_month_spend_usd=5.00,
        available_credit_usd=15.00,
        official_links=[CreditOfficialLink(title="Credits", url="https://openrouter.ai/settings/credits")],
    )

    fake_openai = ProviderCreditCard(
        provider_id="openai",
        provider_name="OpenAI API",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        current_month_spend_usd=10.00,
        available_credit_usd=25.00,
        official_links=[CreditOfficialLink(title="Home", url="https://platform.openai.com/home")],
    )

    with patch.object(monitor, "inspect_openrouter", return_value=fake_openrouter):
        with patch.object(monitor, "inspect_openai", return_value=fake_openai):
            report = monitor.generate_report()
            assert report.total_month_spend_usd == 15.00
            assert report.total_available_credit_usd == 40.00
            assert len(report.accounts) >= 11  # openrouter + openai + 9 planned


def test_headless_rest_api_endpoints():
    """Verify GET /api/credits and POST /api/credits/refresh endpoints."""
    client = TestClient(app)

    # GET /api/credits
    resp_get = client.get("/api/credits")
    assert resp_get.status_code == 200
    data = resp_get.json()
    assert "total_month_spend_usd" in data
    assert "accounts" in data
    assert isinstance(data["accounts"], list)
    assert any(a["provider_id"] == "openrouter" for a in data["accounts"])
    assert any(a["provider_id"] == "openai" for a in data["accounts"])

    # Verify official URLs are present in response
    openrouter_acc = next(a for a in data["accounts"] if a["provider_id"] == "openrouter")
    openai_acc = next(a for a in data["accounts"] if a["provider_id"] == "openai")
    assert any("openrouter.ai/activity" in l["url"] or "openrouter.ai/settings/credits" in l["url"] for l in openrouter_acc["official_links"])
    assert any("platform.openai.com/home" in l["url"] for l in openai_acc["official_links"])

    # POST /api/credits/refresh
    resp_post = client.post("/api/credits/refresh")
    assert resp_post.status_code == 200
    data_refresh = resp_post.json()
    assert "total_month_spend_usd" in data_refresh

    # POST /api/credits/accounts/openai (update values)
    resp_update = client.post("/api/credits/accounts/openai", json={
        "current_month_spend_usd": 1.81,
        "available_credit_usd": 8.19,
        "notes": "Sincronizado com platform.openai.com (Personal)"
    })
    assert resp_update.status_code == 200
    updated_openai = resp_update.json()
    assert updated_openai["current_month_spend_usd"] == 1.81
    assert updated_openai["available_credit_usd"] == 8.19


def test_frontend_credits_integration():
    """Verify frontend HTML and scripts mount the credits monitor and contain official links."""
    base_dir = Path(__file__).resolve().parent.parent
    index_html = base_dir / "hub" / "frontend" / "index.html"
    credits_js = base_dir / "hub" / "frontend" / "credits.js"

    assert index_html.exists(), "hub/frontend/index.html must exist"
    assert credits_js.exists(), "hub/frontend/credits.js must exist"

    html_content = index_html.read_text(encoding="utf-8")
    assert "credits.js" in html_content, "index.html must reference credits.js"

    js_content = credits_js.read_text(encoding="utf-8")
    assert "/api/credits" in js_content, "credits.js must fetch from /api/credits"
    assert "https://platform.openai.com/home" in js_content or "official_links" in js_content
    assert "https://openrouter.ai" in js_content or "official_links" in js_content


def test_non_goals_isolation():
    """Verify that credits do not interfere with model selection or subscription quotas."""
    from core.usage.models import AccountUsageReport
    from core.router.model_router import recommend_model

    # Check that model router does not require or depend on credits
    decision = recommend_model(task_type="code", complexity="low", offline=True)
    assert decision.get("model") is not None

    # Check that AccountUsageReport remains strictly percentage/window quota
    assert "connected_count" in AccountUsageReport.model_fields
    assert "total_available_credit_usd" not in AccountUsageReport.model_fields
