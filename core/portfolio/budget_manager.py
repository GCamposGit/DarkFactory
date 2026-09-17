"""Project Budget Ceiling and Financial Guardrail Manager (HF-23).

Governed by Universal Engineering Standards and Gate G1 Grill decisions.
Enforces monthly USD budget caps per project (Atrium $25, Jarvis $35, DarkFac $50):
- Warning alert dispatched via Telegram when project spend reaches >= 80%.
- Fail-Closed cutoff to $0 local models (LOCAL_ONLY) when spend reaches 100%.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from core.integrations.telegram import TelegramGateway, load_telegram_config
from .models import BudgetStatus, ProjectBudgetConfig, utc_now_iso

logger = logging.getLogger("darkfac.portfolio.budget")

DEFAULT_PORTFOLIO_DIR = Path(".factory/portfolio")
DEFAULT_BUDGET_FILE = DEFAULT_PORTFOLIO_DIR / "budgets.json"

DEFAULT_BUDGETS_USD: Dict[str, float] = {
    "atrium": 25.0,
    "jarvis": 35.0,
    "darkfac": 50.0,
}


class PortfolioBudgetManager:
    """Manages monthly project spending ceilings, warnings, and fail-closed cutoffs."""

    def __init__(
        self,
        storage_file: Optional[Path] = None,
        telegram_gateway: Optional[TelegramGateway] = None,
    ) -> None:
        self.storage_file = storage_file or DEFAULT_BUDGET_FILE
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        self._telegram_gateway = telegram_gateway
        self._budgets: Dict[str, ProjectBudgetConfig] = {}
        self._load()

    def _get_telegram_gateway(self) -> Optional[TelegramGateway]:
        if self._telegram_gateway is None:
            try:
                cfg = load_telegram_config()
                if cfg.bot_token:
                    self._telegram_gateway = TelegramGateway(config=cfg)
            except Exception:
                pass
        return self._telegram_gateway

    def _load(self) -> None:
        """Load budgets from JSON storage or initialize with defaults."""
        if self.storage_file.is_file():
            try:
                raw = json.loads(self.storage_file.read_text(encoding="utf-8"))
                for p_id, data in raw.items():
                    self._budgets[p_id] = ProjectBudgetConfig.model_validate(data)
            except Exception as exc:
                logger.warning("Failed to load portfolio budgets: %s", exc)

        # Seed defaults if not present
        for p_id, default_limit in DEFAULT_BUDGETS_USD.items():
            if p_id not in self._budgets:
                self._budgets[p_id] = ProjectBudgetConfig(
                    project_id=p_id,
                    monthly_limit_usd=default_limit,
                    current_spent_usd=0.0,
                    status=BudgetStatus.ACTIVE,
                )

    def _save(self) -> None:
        """Persist budgets to JSON file."""
        data = {p_id: b.model_dump() for p_id, b in self._budgets.items()}
        self.storage_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_budget(self, project_id: str) -> ProjectBudgetConfig:
        """Retrieve budget configuration for a specific project."""
        if project_id not in self._budgets:
            default_limit = DEFAULT_BUDGETS_USD.get(project_id, 25.0)
            self._budgets[project_id] = ProjectBudgetConfig(
                project_id=project_id,
                monthly_limit_usd=default_limit,
                current_spent_usd=0.0,
                status=BudgetStatus.ACTIVE,
            )
            self._save()
        return self._budgets[project_id]

    def set_budget(self, project_id: str, monthly_limit_usd: float) -> ProjectBudgetConfig:
        """Update or create a project's monthly spending limit in USD."""
        budget = self.get_budget(project_id)
        budget.monthly_limit_usd = monthly_limit_usd
        budget.updated_at = utc_now_iso()
        self._evaluate_thresholds(budget)
        self._save()
        logger.info("Updated budget for '%s': $%.2f/month", project_id, monthly_limit_usd)
        return budget

    def record_spend(self, project_id: str, amount_usd: float) -> ProjectBudgetConfig:
        """Increment project spending and enforce 80% warning and 100% fail-closed cutoff."""
        budget = self.get_budget(project_id)
        budget.current_spent_usd = round(budget.current_spent_usd + max(0.0, amount_usd), 4)
        budget.updated_at = utc_now_iso()

        self._evaluate_thresholds(budget)
        self._save()
        return budget

    def _evaluate_thresholds(self, budget: ProjectBudgetConfig) -> None:
        """Check spend against alert and cutoff thresholds and trigger notifications."""
        pct = budget.utilization_pct

        if pct >= 100.0:
            previous_status = budget.status
            budget.status = BudgetStatus.LOCAL_ONLY
            logger.critical(
                "🚨 Project '%s' reached 100%% of budget ceiling ($%.2f / $%.2f). Forced to $0 local models.",
                budget.project_id,
                budget.current_spent_usd,
                budget.monthly_limit_usd,
            )
            if previous_status != BudgetStatus.LOCAL_ONLY:
                self._send_telegram_alert(
                    f"🚨 <b>Teto Orçamentário Atingido [100%] — {budget.project_id.upper()}</b>\n"
                    f"Gasto: <b>${budget.current_spent_usd:.2f}</b> / Limite: <b>${budget.monthly_limit_usd:.2f}</b>\n"
                    f"O projeto foi automaticamente migrado para <b>LOCAL_ONLY ($0 Ollama)</b> para prevenir custos adicionais."
                )
                budget.last_alert_at = utc_now_iso()

        elif pct >= 80.0:
            previous_status = budget.status
            budget.status = BudgetStatus.WARNING
            logger.warning(
                "⚠️ Project '%s' reached %.1f%% of budget ceiling ($%.2f / $%.2f).",
                budget.project_id,
                pct,
                budget.current_spent_usd,
                budget.monthly_limit_usd,
            )
            if previous_status == BudgetStatus.ACTIVE:
                self._send_telegram_alert(
                    f"⚠️ <b>Alerta de Orçamento [80%] — {budget.project_id.upper()}</b>\n"
                    f"Gasto acumulado atingiu <b>{pct:.1f}%</b> (${budget.current_spent_usd:.2f} / ${budget.monthly_limit_usd:.2f}).\n"
                    f"Resta <b>${budget.remaining_usd:.2f}</b> antes do corte automático para modelos locais."
                )
                budget.last_alert_at = utc_now_iso()

        else:
            budget.status = BudgetStatus.ACTIVE

    def _send_telegram_alert(self, text: str) -> None:
        """Send notification via Telegram if configured."""
        tg = self._get_telegram_gateway()
        if tg and tg.config.authorized_chat_ids:
            try:
                tg.send_message(chat_id=tg.config.authorized_chat_ids[0], text=text)
            except Exception as exc:
                logger.warning("Failed sending Telegram budget alert: %s", exc)

    def is_paid_cloud_allowed(self, project_id: str) -> bool:
        """Return True if project has remaining budget for paid cloud APIs."""
        budget = self.get_budget(project_id)
        return budget.status != BudgetStatus.LOCAL_ONLY

    def reset_monthly_spend(self, project_id: str) -> ProjectBudgetConfig:
        """Reset current spend for a new monthly billing cycle."""
        budget = self.get_budget(project_id)
        budget.current_spent_usd = 0.0
        budget.status = BudgetStatus.ACTIVE
        budget.updated_at = utc_now_iso()
        self._save()
        logger.info("Reset monthly spend for '%s'", project_id)
        return budget

    def list_budgets(self) -> List[ProjectBudgetConfig]:
        """Return all project budget configurations."""
        return list(self._budgets.values())
