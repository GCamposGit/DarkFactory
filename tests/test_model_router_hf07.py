"""Automated verification suite for HF-07: AI Model Adapters, Quota Dispatch & Financial Reconciliation.

Governed by HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 2, 9, 12) & HYBRID_AUTONOMY_REQUIREMENTS (Section 4, 7, G6).
Validates:
1. Strict distinction between available credit, key limit, monthly spend, and cumulative key usage.
2. Correct parsing and reconciliation of OpenRouter's 'usage_monthly' vs 'usage' (cumulative).
3. High intelligence tier model resolution by harness (Antigravity, Grok Build, Claude Code, Codex).
4. Reasoning effort sanitization: 'high' default, 'max' for high complexity; never 'xhigh' or 'ultra'.
5. Guarantee that Fable-5.1 is NEVER configured as a default fallback.
6. Subscription quota pressure failover to healthier accounts or Pareto paid gateways.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from core.router.model_router import (
    HIGH_INTELLIGENCE_BY_HARNESS,
    _QUOTA_FAILOVER_MODELS,
    MODEL_MATRIX,
    sanitize_reasoning_effort,
)
from core.router.token_budget import TokenPressure, plan_token_stress
from core.usage.api_credits import (
    ApiCreditsMonitor,
    CreditAccountStatus,
    ProviderCreditCard,
)
from core.usage.models import (
    AccountConnectionStatus,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)


# ---------------------------------------------------------------------------
# 1. Financial Reconciliation: Saldo, Limite, Mês e Acumulado Distintos
# ---------------------------------------------------------------------------

def test_financial_reconciliation_distinguishes_saldo_limite_mes_acumulado():
    """Verify that ProviderCreditCard strictly separates all 4 financial metrics."""
    card = ProviderCreditCard(
        provider_id="openrouter",
        provider_name="OpenRouter",
        status=CreditAccountStatus.ACTIVE,
        is_connected=True,
        available_credit_usd=15.50,     # Saldo disponível
        credit_limit_usd=100.00,        # Limite da chave
        current_month_spend_usd=4.20,   # Gasto do mês vigente (usage_monthly)
        cumulative_spend_usd=38.70,     # Gasto acumulado vitalício da chave (usage)
    )

    # All four metrics must be distinct and non-overlapping
    assert card.available_credit_usd == 15.50
    assert card.credit_limit_usd == 100.00
    assert card.current_month_spend_usd == 4.20
    assert card.cumulative_spend_usd == 38.70
    assert card.current_month_spend_usd != card.cumulative_spend_usd


def test_openrouter_inspection_parses_usage_monthly_and_cumulative_usage():
    """Verify that inspect_openrouter reconciles usage_monthly vs cumulative usage accurately."""
    monitor = ApiCreditsMonitor()

    mock_credits_response = json.dumps({
        "data": {
            "total_credits": 50.00,
            "total_usage": 35.00,
        }
    }).encode("utf-8")

    mock_auth_response = json.dumps({
        "data": {
            "label": "darkfac-prod-key",
            "usage": 35.00,              # Gasto acumulado total
            "usage_monthly": 6.80,       # Gasto específico do mês vigente
            "limit": 100.00,             # Limite configurado
            "is_free_tier": False,
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

    with patch.object(monitor, "get_openrouter_key", return_value="sk-or-test-key"):
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            card = monitor.inspect_openrouter()

            assert card.provider_id == "openrouter"
            assert card.is_connected is True
            assert card.status == CreditAccountStatus.ACTIVE
            # Available balance = 50.00 - 35.00 = 15.00
            assert card.available_credit_usd == pytest.approx(15.00)
            # Monthly spend must reflect 'usage_monthly', NOT cumulative 'usage'
            assert card.current_month_spend_usd == pytest.approx(6.80)
            # Cumulative usage must reflect total 'usage'
            assert card.cumulative_spend_usd == pytest.approx(35.00)
            # Credit limit must reflect 'limit'
            assert card.credit_limit_usd == pytest.approx(100.00)


# ---------------------------------------------------------------------------
# 2. High Intelligence Resolution by Harness (HF-07 / Requirements Sec. 4)
# ---------------------------------------------------------------------------

def test_high_intelligence_resolution_by_harness():
    """Verify owner-specified high intelligence model preferences per harness."""
    # Antigravity -> Gemini 3.8 Flash
    assert HIGH_INTELLIGENCE_BY_HARNESS["antigravity"]["model"] == "gemini-3.8-flash"
    assert HIGH_INTELLIGENCE_BY_HARNESS["antigravity"]["provider"] == "google"

    # Grok Build -> grok-4.6
    assert HIGH_INTELLIGENCE_BY_HARNESS["grok_build"]["model"] == "grok-4.6"
    assert HIGH_INTELLIGENCE_BY_HARNESS["grok_build"]["provider"] == "xai"

    # Claude Code -> opus-5.1
    assert HIGH_INTELLIGENCE_BY_HARNESS["claude_code"]["model"] == "opus-5.1"
    assert HIGH_INTELLIGENCE_BY_HARNESS["claude_code"]["provider"] == "anthropic"

    # Codex -> astra-6 / gpt-6-astra
    assert HIGH_INTELLIGENCE_BY_HARNESS["codex"]["model"] == "gpt-6-astra"
    assert HIGH_INTELLIGENCE_BY_HARNESS["codex"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# 3. Reasoning Effort Constraints (HF-07 / Requirements Sec. 4)
# ---------------------------------------------------------------------------

def test_reasoning_effort_sanitization_rules():
    """Verify reasoning effort is high by default, max when justified, and forbids xhigh/ultra."""
    # Defaults
    assert sanitize_reasoning_effort(None, complexity="medium") == "high"
    assert sanitize_reasoning_effort(None, complexity="critical") == "max"
    assert sanitize_reasoning_effort("", complexity="high") == "max"

    # Supported standard efforts
    assert sanitize_reasoning_effort("low") == "low"
    assert sanitize_reasoning_effort("medium") == "medium"
    assert sanitize_reasoning_effort("high") == "high"
    assert sanitize_reasoning_effort("max") == "max"

    # Forbidden extra-high efforts must be sanitized down to max (never send invalid parameters)
    assert sanitize_reasoning_effort("xhigh") == "max"
    assert sanitize_reasoning_effort("extra-high") == "max"
    assert sanitize_reasoning_effort("extra_high") == "max"
    assert sanitize_reasoning_effort("ultra") == "max"


# ---------------------------------------------------------------------------
# 4. Invariant: Fable-5.1 is NEVER a Default Fallback (Scenario G6)
# ---------------------------------------------------------------------------

def test_fable_never_default_fallback():
    """Ensure Fable-5.1 is never present as a default or failover model."""
    for provider, model in _QUOTA_FAILOVER_MODELS.items():
        assert "fable" not in model.lower(), f"Fable found in failover models for {provider}"

    for task_name, strategy in MODEL_MATRIX.items():
        if isinstance(strategy, dict):
            for key, val in strategy.items():
                if isinstance(val, str):
                    assert "fable" not in val.lower(), f"Fable found in {task_name}.{key}"
                elif isinstance(val, dict):
                    for subk, subval in val.items():
                        if isinstance(subval, str):
                            assert "fable" not in subval.lower(), f"Fable found in {task_name}.{key}.{subk}"


# ---------------------------------------------------------------------------
# 5. Quota Failover and Paid API Gateway Dispatch
# ---------------------------------------------------------------------------

def test_quota_stress_routes_to_paid_api_before_exhaustion():
    """Verify that subscription under stress (<=15% remaining) fails over to paid API/gateway."""
    sub_account = ProviderAccountUsage(
        provider_id="openai",
        provider_name="OpenAI / Codex",
        account_label="OpenAI Subscription",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="openai_adapter",
        quota_supported=True,
        message="OK",
        windows=[
            QuotaWindow(
                quota_id="openai-5h",
                label="5h",
                used_percent=90.0,
                remaining_percent=10.0,
                window_duration_minutes=300,
            ),
        ],
    )
    paid_account = ProviderAccountUsage(
        provider_id="deepseek",
        provider_name="DeepSeek",
        account_label="DeepSeek Paid API",
        family=ProviderFamily.GATEWAY,
        status=AccountConnectionStatus.CONNECTED,
        adapter="deepseek_adapter",
        quota_supported=False,
        message="OK",
    )

    plan = plan_token_stress(
        task_type="coding",
        complexity="high",
        description="Implement complex durable coordinator",
        expected_steps=5,
        accounts=[sub_account, paid_account],
        preferred_provider="openai",
        remaining_hourly_percent=10.0,  # Below 15% threshold
        offline=False,
    )

    assert plan.use_paid_api is True
    assert plan.selected_provider in {"deepseek", "openrouter", "siliconflow"}
    assert plan.pressure in {TokenPressure.STRESSED, TokenPressure.CRITICAL}
    assert plan.failover_required is True
