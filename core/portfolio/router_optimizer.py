"""Paid-Account-First Model Router with Frontier Cost-Benefit Optimization (HF-23).

Governed by Universal Engineering Standards and Gate G1 Grill decisions.
Enforces the mandatory hierarchy:
1. Direct Paid Accounts First (when balance exists):
   - Gemini 3.8 Flash (Antigravity / Google)
   - Grok 4.6 high (xAI)
   - Luna xhigh / OpenAI / Claude 3.7 Sonnet
2. OpenRouter Frontier Cost-Benefit (when direct accounts lack balance or are degraded):
   - DeepSeek-V3 / Qwen-2.5-Coder at optimal $/token Pareto frontier
3. Local-First ($0) Fallback:
   - Forced whenever project reaches 100% budget cap (LOCAL_ONLY) or for micro-tasks
   - Runs Ollama qwen-fast / qwen-deep locally at $0
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from core.usage.api_credits import ApiCreditsMonitor
from core.workflow.qualified_routes import RouteDecision, select_route
from .budget_manager import PortfolioBudgetManager
from .models import ModelTier, RoutingPolicyResult

logger = logging.getLogger("darkfac.portfolio.router")

# Direct Paid Account Capabilities
PAID_DIRECT_MODELS = {
    "research": {"model": "gemini-3.8-flash", "provider": "antigravity", "cost_est": 0.0005},
    "web_research": {"model": "grok-4.6", "provider": "xai", "cost_est": 0.005},
    "architecture": {"model": "luna-xhigh", "provider": "openai", "cost_est": 0.015},
    "coding_high": {"model": "luna-xhigh", "provider": "openai", "cost_est": 0.015},
    "coding_medium": {"model": "gemini-3.8-flash", "provider": "antigravity", "cost_est": 0.0005},
    "coding_low": {"model": "qwen-code-fast:latest", "provider": "ollama", "cost_est": 0.0},
    "review": {"model": "grok-4.6", "provider": "xai", "cost_est": 0.005},
}

# OpenRouter Frontier Cost-Benefit Models (Fallback Tier 2)
OPENROUTER_FRONTIER_MODELS = {
    "research": {"model": "deepseek/deepseek-chat", "provider": "openrouter", "cost_est": 0.0002},
    "web_research": {"model": "deepseek/deepseek-chat", "provider": "openrouter", "cost_est": 0.0002},
    "architecture": {"model": "deepseek/deepseek-r1", "provider": "openrouter", "cost_est": 0.002},
    "coding_high": {"model": "qwen/qwen-2.5-coder-32b-instruct", "provider": "openrouter", "cost_est": 0.0002},
    "coding_medium": {"model": "qwen/qwen-2.5-coder-32b-instruct", "provider": "openrouter", "cost_est": 0.0002},
    "coding_low": {"model": "qwen-code-fast:latest", "provider": "ollama", "cost_est": 0.0},
    "review": {"model": "deepseek/deepseek-v3", "provider": "openrouter", "cost_est": 0.0002},
}

# Local $0 Fallback Models (Tier 3)
LOCAL_ZERO_MODELS = {
    "coding_high": "qwen-code-deep:latest",
    "architecture": "qwen-code-deep:latest",
    "default": "qwen-code-fast:latest",
}


class PortfolioModelRouter:
    """Resolves optimal model for a task applying the paid-first -> OpenRouter -> local-$0 hierarchy."""

    def __init__(
        self,
        budget_manager: Optional[PortfolioBudgetManager] = None,
        credits_monitor: Optional[ApiCreditsMonitor] = None,
        account_balances: Optional[Dict[str, float]] = None,
    ) -> None:
        self.budget_manager = budget_manager or PortfolioBudgetManager()
        self.credits_monitor = credits_monitor or ApiCreditsMonitor()
        self._manual_balances = account_balances

    def _get_provider_balance(self, provider: str) -> float:
        """Return available credit balance for provider in USD."""
        if self._manual_balances is not None and provider in self._manual_balances:
            return self._manual_balances[provider]

        try:
            report = self.credits_monitor.get_report(force_refresh=False)
            for card in report.accounts:
                if card.provider_id.lower() == provider.lower():
                    if card.available_credit_usd is not None:
                        return card.available_credit_usd
                    if card.is_connected:
                        return 100.0  # Connected subscription / active account
        except Exception as exc:
            logger.debug("Failed reading provider balance: %s", exc)

        # Antigravity/Gemini active by default in AGY harness
        if provider in ("antigravity", "google"):
            return 50.0
        return 0.0

    def route_task(self, task_type: str, project_id: str = "atrium") -> RoutingPolicyResult:
        """Resolve optimal model for the given task and project."""
        clean_task = task_type.lower().replace("-", "_")

        # 1. Check Project Budget Ceiling (Fail-Closed to Local $0 if 100% reached)
        if not self.budget_manager.is_paid_cloud_allowed(project_id):
            local_model = LOCAL_ZERO_MODELS.get(clean_task, LOCAL_ZERO_MODELS["default"])
            logger.info("Project '%s' in LOCAL_ONLY status; routing to %s ($0)", project_id, local_model)
            return RoutingPolicyResult(
                project_id=project_id,
                task_type=clean_task,
                selected_model=local_model,
                provider="ollama",
                tier=ModelTier.LOCAL_ZERO,
                estimated_cost_usd=0.0,
                reason="Project monthly budget ceiling reached (100%); fail-closed to $0 local Ollama.",
                fallback_model=None,
            )

        # 2. Check Tier 1: Direct Paid Accounts
        paid_spec = PAID_DIRECT_MODELS.get(clean_task, PAID_DIRECT_MODELS["coding_medium"])
        provider = paid_spec["provider"]

        if provider == "ollama" or self._get_provider_balance(provider) > 0:
            tier = ModelTier.LOCAL_ZERO if provider == "ollama" else ModelTier.PAID_DIRECT
            return RoutingPolicyResult(
                project_id=project_id,
                task_type=clean_task,
                selected_model=paid_spec["model"],
                provider=provider,
                tier=tier,
                estimated_cost_usd=paid_spec["cost_est"],
                reason=f"Selected paid account '{provider}' ({paid_spec['model']}) with active balance.",
                fallback_model=OPENROUTER_FRONTIER_MODELS.get(clean_task, {}).get("model"),
            )

        # 3. Check Tier 2: OpenRouter Frontier Cost-Benefit (Fallback)
        openrouter_balance = self._get_provider_balance("openrouter")
        if openrouter_balance > 0 or self._manual_balances is None:
            frontier_spec = OPENROUTER_FRONTIER_MODELS.get(clean_task, OPENROUTER_FRONTIER_MODELS["coding_medium"])
            return RoutingPolicyResult(
                project_id=project_id,
                task_type=clean_task,
                selected_model=frontier_spec["model"],
                provider="openrouter",
                tier=ModelTier.OPENROUTER_FRONTIER,
                estimated_cost_usd=frontier_spec["cost_est"],
                reason=f"Direct provider '{provider}' had insufficient balance; routed to OpenRouter Pareto frontier ({frontier_spec['model']}).",
                fallback_model=LOCAL_ZERO_MODELS.get(clean_task, LOCAL_ZERO_MODELS["default"]),
            )

        # 4. Tier 3: Local-First $0 Fallback
        local_model = LOCAL_ZERO_MODELS.get(clean_task, LOCAL_ZERO_MODELS["default"])
        return RoutingPolicyResult(
            project_id=project_id,
            task_type=clean_task,
            selected_model=local_model,
            provider="ollama",
            tier=ModelTier.LOCAL_ZERO,
            estimated_cost_usd=0.0,
            reason="Paid cloud accounts and OpenRouter credits exhausted; fail-closed to $0 local Ollama.",
            fallback_model=None,
        )

    def select_qualified_route(
        self,
        job: Any,
        catalog: Optional[Any] = None,
        quota_report: Optional[Any] = None,
    ) -> RouteDecision:
        """HF-07-02: Qualified route selection integrating with canonical contracts."""
        return select_route(
            job=job,
            catalog=catalog,
            quota_report=quota_report,
            budget_manager=self.budget_manager,
        )

