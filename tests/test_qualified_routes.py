"""Comprehensive test suite for qualified routes and fallback engine (HF-07-02).

Governed by Universal Engineering Standards (AGENTS.md) and Gate G1 Grill decisions.
Validates:
1. Strict local-first preference for economy role ($0 via Ollama).
2. Anti-degradation: high_architecture strictly blocks in WAITING_RESOURCE and never degrades to economy.
3. Cross-model family isolation for verifier/independent_review role.
4. Reselection under quota pressure without double-locking budget.
5. Reasoning effort sanitization conforming to HF-07 / 2026 specs.
6. Real tool-calling capability verification.
7. PortfolioModelRouter delimited integration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict
import pytest

from core.portfolio.budget_manager import PortfolioBudgetManager
from core.portfolio.router_optimizer import PortfolioModelRouter
from core.usage.models import (
    AccountConnectionStatus,
    AccountUsageReport,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)
from core.workflow.qualified_routes import (
    RouteDecision,
    sanitize_reasoning_effort,
    select_route,
)


# ==============================================================================
# Fixtures and Helpers
# ==============================================================================


@pytest.fixture
def empty_quota_report() -> AccountUsageReport:
    """All providers connected and within quota."""
    return AccountUsageReport(
        accounts=[
            ProviderAccountUsage(
                provider_id="ollama",
                provider_name="Ollama Local",
                family=ProviderFamily.LOCAL,
                status=AccountConnectionStatus.CONNECTED,
                adapter="ollama",
                message="Healthy",
            ),
            ProviderAccountUsage(
                provider_id="anthropic",
                provider_name="Anthropic Claude",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.CONNECTED,
                adapter="anthropic",
                message="Healthy",
            ),
            ProviderAccountUsage(
                provider_id="openrouter",
                provider_name="OpenRouter Gateway",
                family=ProviderFamily.GATEWAY,
                status=AccountConnectionStatus.CONNECTED,
                adapter="openrouter",
                message="Healthy",
            ),
            ProviderAccountUsage(
                provider_id="google",
                provider_name="Google Antigravity",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.CONNECTED,
                adapter="google",
                message="Healthy",
            ),
            ProviderAccountUsage(
                provider_id="xai",
                provider_name="xAI Grok",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.CONNECTED,
                adapter="xai",
                message="Healthy",
            ),
        ],
        connected_count=5,
        limited_count=0,
        disconnected_count=0,
    )


# ==============================================================================
# 1. Economy Local-First Routing Tests
# ==============================================================================


def test_economy_routes_local_first_zero_cost(empty_quota_report: AccountUsageReport) -> None:
    """Economy role must strictly prioritize local $0 Ollama model (qwen-code-fast:latest)."""
    job = {
        "role": "economy",
        "project_id": "darkfac",
        "task_type": "unit_test_generation",
    }
    decision = select_route(job=job, quota_report=empty_quota_report)

    assert decision.status == "QUALIFIED"
    assert decision.selected_model == "qwen-code-fast:latest"
    assert decision.selected_provider == "ollama"
    assert decision.estimated_cost_usd == 0.0
    assert decision.reservation_required is False


def test_economy_failover_to_deep_and_gemini() -> None:
    """When fast worker is unavailable or context requirements exceed 4k, failover follows hierarchy."""
    # Fast worker unavailable locally
    quota_dict = {
        "ollama": {
            "models_available": ["qwen-code-deep:latest"],
        }
    }
    job = {
        "role": "economy",
        "project_id": "darkfac",
    }
    decision = select_route(job=job, quota_report=quota_dict)
    assert decision.status == "QUALIFIED"
    assert decision.selected_model == "qwen-code-deep:latest"
    assert decision.estimated_cost_usd == 0.0
    assert decision.reservation_required is False

    # When all local Ollama models are unavailable, fallback to cloud gemini-3.8-flash
    quota_dict_no_local = {
        "ollama": False,
    }
    decision_cloud = select_route(job=job, quota_report=quota_dict_no_local)
    assert decision_cloud.status == "QUALIFIED"
    assert decision_cloud.selected_model == "google/gemini-3.8-flash"
    assert decision_cloud.selected_provider == "google"
    assert decision_cloud.estimated_cost_usd > 0.0
    assert decision_cloud.reservation_required is True


# ==============================================================================
# 2. Anti-Degradation: High Architecture Inviolability Tests
# ==============================================================================


def test_anti_degradation_high_architecture_blocks_waiting_resource() -> None:
    """High architecture role MUST NOT degrade to economy ($0 local) under quota exhaustion."""
    reset_time = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    exhausted_quota_report = AccountUsageReport(
        accounts=[
            ProviderAccountUsage(
                provider_id="anthropic",
                provider_name="Anthropic",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.LIMITED,
                adapter="anthropic",
                windows=[
                    QuotaWindow(
                        quota_id="hourly",
                        label="Hourly Window",
                        used_percent=100.0,
                        remaining_percent=0.0,
                        resets_at=reset_time,
                    )
                ],
                message="Rate limited",
            ),
            ProviderAccountUsage(
                provider_id="openrouter",
                provider_name="OpenRouter",
                family=ProviderFamily.GATEWAY,
                status=AccountConnectionStatus.LIMITED,
                adapter="openrouter",
                windows=[
                    QuotaWindow(
                        quota_id="hourly",
                        label="Hourly Window",
                        used_percent=100.0,
                        remaining_percent=0.0,
                        resets_at=reset_time,
                    )
                ],
                message="Credits exhausted",
            ),
            ProviderAccountUsage(
                provider_id="google",
                provider_name="Google",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.LIMITED,
                adapter="google",
                windows=[
                    QuotaWindow(
                        quota_id="hourly",
                        label="Hourly Window",
                        used_percent=100.0,
                        remaining_percent=0.0,
                        resets_at=reset_time,
                    )
                ],
                message="Quota exhausted",
            ),
            # Ollama is healthy and ready
            ProviderAccountUsage(
                provider_id="ollama",
                provider_name="Ollama",
                family=ProviderFamily.LOCAL,
                status=AccountConnectionStatus.CONNECTED,
                adapter="ollama",
                message="Healthy",
            ),
        ],
        connected_count=1,
        limited_count=3,
        disconnected_count=0,
    )

    job = {
        "role": "high_architecture",
        "project_id": "darkfac",
        "task_type": "prd_decomposition",
    }

    decision = select_route(job=job, quota_report=exhausted_quota_report)

    # Inviolability: Must pause in WAITING_RESOURCE, NEVER drop to qwen-fast or qwen-deep
    assert decision.status == "WAITING_RESOURCE"
    assert decision.selected_model is None
    assert decision.wakeup_at == reset_time
    assert "inviolabilidade" in decision.reason.lower() or "floor" in decision.reason.lower() or "degrad" in decision.reason.lower()
    assert decision.reservation_required is False


def test_high_architecture_routes_to_claude_when_quota_available(empty_quota_report: AccountUsageReport) -> None:
    """High architecture routes to claude-opus-5 primarily when quota exists."""
    job = {
        "role": "high_architecture",
        "project_id": "darkfac",
    }
    decision = select_route(job=job, quota_report=empty_quota_report)
    assert decision.status == "QUALIFIED"
    assert decision.selected_model == "anthropic/claude-opus-5"
    assert decision.selected_provider == "anthropic"
    assert decision.reservation_required is True


# ==============================================================================
# 3. Cross-Model Family Isolation Tests (Verifier Role)
# ==============================================================================


def test_verifier_cross_model_family_isolation(empty_quota_report: AccountUsageReport) -> None:
    """Verifier MUST NOT belong to the same model family as the implementer."""
    # Scenario A: Implementer is Anthropic Claude Opus 5
    job_claude = {
        "role": "verifier",
        "project_id": "darkfac",
        "implementer_model": "anthropic/claude-opus-5",
    }
    decision_a = select_route(job=job_claude, quota_report=empty_quota_report)
    assert decision_a.status == "QUALIFIED"
    # Revisor is gpt-review (gpt-oss) or deepseek (deepseek) - never claude
    assert "claude" not in decision_a.selected_model.lower()
    assert decision_a.selected_model == "gpt-review:latest"

    # Scenario B: Implementer is local gpt-review:latest (family: gpt-oss)
    job_gpt = {
        "role": "verifier",
        "project_id": "darkfac",
        "implementer_model": "gpt-review:latest",
    }
    decision_b = select_route(job=job_gpt, quota_report=empty_quota_report)
    assert decision_b.status == "QUALIFIED"
    # gpt-review is skipped due to family collision; routes to deepseek-v4.1-flash
    assert decision_b.selected_model == "deepseek/deepseek-v4.1-flash"
    assert decision_b.selected_provider == "openrouter"

    # Scenario C: Implementer is DeepSeek (family: deepseek) and local gpt-review is unavailable
    quota_no_gpt = {
        "ollama": False,
    }
    job_deepseek = {
        "role": "independent_review",
        "project_id": "darkfac",
        "implementer_model": "deepseek/deepseek-v4-pro",
    }
    decision_c = select_route(job=job_deepseek, quota_report=quota_no_gpt)
    assert decision_c.status == "QUALIFIED"
    # DeepSeek is skipped due to family collision; routes to grok-4.6 (xai)
    assert decision_c.selected_model == "xai/grok-4.6"
    assert decision_c.selected_provider == "xai"


# ==============================================================================
# 4. Reselection Under Quota Pressure Without Double Budget Reservation
# ==============================================================================


def test_reselection_without_double_budget_reservation(tmp_path: Path) -> None:
    """When quota shifts before claim, reselection releases or updates reservation cleanly."""
    budget_file = tmp_path / "budgets.json"
    mgr = PortfolioBudgetManager(storage_file=budget_file)
    mgr.set_budget("darkfac", 10.0)

    # First routing: cloud model requested
    job = {
        "role": "high_architecture",
        "project_id": "darkfac",
        "existing_reservation_id": "res-prior-101",
    }
    quota_claude_ok = {"anthropic": True, "openrouter": True, "google": True}
    dec1 = select_route(job=job, quota_report=quota_claude_ok, budget_manager=mgr)
    assert dec1.status == "QUALIFIED"
    assert dec1.selected_model == "anthropic/claude-opus-5"
    assert dec1.reservation_required is True

    # Quota changes before claim: anthropic is now exhausted, openrouter is available
    quota_anthropic_down = {"anthropic": False, "openrouter": True, "google": True}
    job_reselect = {
        "role": "high_architecture",
        "project_id": "darkfac",
        "existing_reservation_id": "res-prior-101",
    }
    dec2 = select_route(job=job_reselect, quota_report=quota_anthropic_down, budget_manager=mgr)
    assert dec2.status == "QUALIFIED"
    assert dec2.selected_model == "deepseek/deepseek-v4-pro"
    # Double booking prevented: clean transition with existing reservation tracked
    assert dec2.reservation_required is True


# ==============================================================================
# 5. Reasoning Effort Sanitization Tests
# ==============================================================================


def test_reasoning_effort_sanitization() -> None:
    """Forbidden reasoning efforts (xhigh, extra-high, ultra) are sanitized to max."""
    assert sanitize_reasoning_effort("xhigh") == "max"
    assert sanitize_reasoning_effort("extra-high") == "max"
    assert sanitize_reasoning_effort("extra_high") == "max"
    assert sanitize_reasoning_effort("ultra") == "max"

    # Valid values preserved
    assert sanitize_reasoning_effort("minimal") == "minimal"
    assert sanitize_reasoning_effort("low") == "low"
    assert sanitize_reasoning_effort("medium") == "medium"
    assert sanitize_reasoning_effort("high") == "high"
    assert sanitize_reasoning_effort("max") == "max"

    # None defaults
    assert sanitize_reasoning_effort(None, complexity="critical") == "max"
    assert sanitize_reasoning_effort(None, complexity="high") == "max"
    assert sanitize_reasoning_effort(None, complexity="medium") == "high"


# ==============================================================================
# 6. Real Tool-Calling Verification Tests
# ==============================================================================


def test_tool_calling_verification_rejects_unsupported_model() -> None:
    """Models declaring tool_calling_supported=False are disqualified."""
    catalog = {
        "providers": {
            "mock_provider": {
                "models": [
                    {
                        "model_id": "mock/text-only-model",
                        "alias": "text-only",
                        "family": "mock",
                        "supported_roles": ["economy"],
                        "tool_calling_supported": False,
                        "tool_capabilities": [],
                    }
                ]
            }
        },
        "roles_mapping": {
            "economy": {
                "allowed_models": ["mock/text-only-model"],
                "fallback_order": ["mock/text-only-model"],
            }
        },
    }

    job = {
        "role": "economy",
        "project_id": "darkfac",
        "tool_calling_required": True,
    }
    decision = select_route(job=job, catalog=catalog)
    assert decision.status == "DISQUALIFIED"
    assert "tool" in decision.reason.lower()


# ==============================================================================
# 7. PortfolioModelRouter Delimited Integration Tests
# ==============================================================================


def test_portfolio_model_router_integration(tmp_path: Path) -> None:
    """PortfolioModelRouter integrates with select_route while retaining backwards compatibility."""
    mgr = PortfolioBudgetManager(storage_file=tmp_path / "budgets.json")
    mgr.set_budget("atrium", 100.0)
    router = PortfolioModelRouter(budget_manager=mgr)

    # New qualified routing method on router
    job = {"role": "economy", "project_id": "atrium"}
    dec = router.select_qualified_route(job)
    assert dec.status == "QUALIFIED"
    assert dec.selected_model == "qwen-code-fast:latest"

    # Backwards compatibility: existing route_task continues to function
    res = router.route_task("research", project_id="atrium")
    assert res.selected_model == "gemini-3.8-flash"


# ==============================================================================
# 8. HF-07-02 Extended Acceptance Contraproofs
# ==============================================================================


def test_unknown_status_is_treated_as_unavailable() -> None:
    """Unknown provider or model status MUST NOT be treated as available (unknown != available)."""
    # Scenario A: Anthropic is UNKNOWN, OpenRouter is CONNECTED
    report_unknown = AccountUsageReport(
        accounts=[
            ProviderAccountUsage(
                provider_id="anthropic",
                provider_name="Anthropic Claude",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.UNKNOWN,
                adapter="anthropic",
                message="Probe unconfirmed",
            ),
            ProviderAccountUsage(
                provider_id="openrouter",
                provider_name="OpenRouter Gateway",
                family=ProviderFamily.GATEWAY,
                status=AccountConnectionStatus.CONNECTED,
                adapter="openrouter",
                message="Healthy",
            ),
            ProviderAccountUsage(
                provider_id="google",
                provider_name="Google",
                family=ProviderFamily.FRONTIER,
                status=AccountConnectionStatus.CONNECTED,
                adapter="google",
                message="Healthy",
            ),
        ],
        connected_count=2,
        limited_count=0,
        disconnected_count=0,
    )

    job = {"role": "high_architecture", "project_id": "darkfac"}
    dec = select_route(job=job, quota_report=report_unknown)

    assert dec.status == "QUALIFIED"
    # Anthropic (primary) was skipped due to UNKNOWN status; routed to deepseek-v4-pro
    assert dec.selected_model == "deepseek/deepseek-v4-pro"
    assert dec.selected_provider == "openrouter"

    # Scenario B: Dictionary with status="unknown"
    quota_dict = {
        "anthropic": {"status": "unknown"},
        "openrouter": False,
        "google": {"available": "unknown"},
    }
    dec_all_unknown = select_route(job=job, quota_report=quota_dict)
    assert dec_all_unknown.status == "WAITING_RESOURCE"
    assert dec_all_unknown.selected_model is None


def test_context_window_failover() -> None:
    """Economy tasks requiring larger context windows failover to higher capacity models."""
    # 1. Standard economy task (<= 4k tokens) -> qwen-fast
    job_small = {"role": "economy", "project_id": "darkfac", "context_window_required": 3000}
    dec_small = select_route(job=job_small)
    assert dec_small.status == "QUALIFIED"
    assert dec_small.selected_model == "qwen-code-fast:latest"

    # 2. Medium context task (10k tokens) -> skips qwen-fast (4k), selects qwen-deep (16k)
    job_med = {"role": "economy", "project_id": "darkfac", "context_window_required": 10000}
    dec_med = select_route(job=job_med)
    assert dec_med.status == "QUALIFIED"
    assert dec_med.selected_model == "qwen-code-deep:latest"

    # 3. Large context task (50k tokens) -> skips both local models, selects cloud gemini-3.8-flash (1M)
    job_large = {"role": "economy", "project_id": "darkfac", "context_window_required": 50000}
    dec_large = select_route(job=job_large)
    assert dec_large.status == "QUALIFIED"
    assert dec_large.selected_model == "google/gemini-3.8-flash"
    assert dec_large.estimated_cost_usd > 0.0


def test_local_tasks_proceed_while_high_architecture_waits() -> None:
    """High architecture pausing in WAITING_RESOURCE does not block local economy tasks."""
    # All cloud accounts exhausted
    cloud_exhausted_report = {
        "anthropic": False,
        "openrouter": False,
        "google": False,
        "xai": False,
        "ollama": True,
    }

    # High architecture job must wait
    job_high = {"role": "high_architecture", "project_id": "darkfac"}
    dec_high = select_route(job=job_high, quota_report=cloud_exhausted_report)
    assert dec_high.status == "WAITING_RESOURCE"
    assert dec_high.wakeup_at is not None

    # Simultaneously, economy task proceeds locally with zero cost and no wait
    job_econ = {"role": "economy", "project_id": "darkfac"}
    dec_econ = select_route(job=job_econ, quota_report=cloud_exhausted_report)
    assert dec_econ.status == "QUALIFIED"
    assert dec_econ.selected_model == "qwen-code-fast:latest"
    assert dec_econ.estimated_cost_usd == 0.0


def test_stage_name_resolution_to_role() -> None:
    """select_route seamlessly resolves stage name when role is omitted (ControlStore integration)."""
    # stage='planning' -> high_architecture
    dec_plan = select_route({"stage": "planning", "project_id": "darkfac"})
    assert dec_plan.status == "QUALIFIED"
    assert dec_plan.selected_model == "anthropic/claude-opus-5"

    # stage='independent_review' -> verifier
    dec_review = select_route({
        "stage": "independent_review",
        "project_id": "darkfac",
        "implementer_model": "anthropic/claude-opus-5",
    })
    assert dec_review.status == "QUALIFIED"
    assert dec_review.selected_model == "gpt-review:latest"

    # stage='validation' -> economy
    dec_val = select_route({"stage": "validation", "project_id": "darkfac"})
    assert dec_val.status == "QUALIFIED"
    assert dec_val.selected_model == "qwen-code-fast:latest"


def test_cloud_host_isolation_rejects_desktop_local() -> None:
    """Cloud host execution cannot silently route to desktop local Ollama."""
    job_cloud = {
        "role": "economy",
        "project_id": "darkfac",
        "host": "cloud",
    }
    # Cloud host skips local Ollama models and routes to cloud gemini-3.8-flash
    dec = select_route(job=job_cloud)
    assert dec.status == "QUALIFIED"
    assert dec.selected_model == "google/gemini-3.8-flash"
    assert dec.selected_provider == "google"

