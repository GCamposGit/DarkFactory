"""Comprehensive test suite for Portfolio Efficiency, Capacity & Routing (HF-23).

Governed by Universal Engineering Standards (AGENTS.md).
Validates:
1. Slot capacity constraints (1 Heavy, 4 Light).
2. Weighted Fair Queueing (WFQ) with starvation protection.
3. Monthly budget tracking, 80% warning alerts, and 100% fail-closed cutoff.
4. Paid-account-first model routing hierarchy with OpenRouter and $0 local fallback.
5. CLI commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.portfolio.budget_manager import PortfolioBudgetManager
from core.portfolio.cli import main as cli_main
from core.portfolio.models import (
    BudgetStatus,
    JobSlotKind,
    ModelTier,
    ProjectWeightConfig,
    SlotCapacityConfig,
)
from core.portfolio.router_optimizer import PortfolioModelRouter
from core.portfolio.scheduler import PortfolioScheduler


@pytest.fixture
def mock_telegram(tmp_path: Path) -> TelegramGateway:
    cfg = TelegramConfig(bot_token="test_bot_token", authorized_chat_ids=[99999999])
    gw = TelegramGateway(config=cfg, state_dir=tmp_path / "tg")
    gw.send_message = lambda chat_id, text, buttons=None, parse_mode="HTML": True  # type: ignore
    return gw


# ==============================================================================
# 1. Slot Capacity & Worker Pool Tests
# ==============================================================================


def test_slot_capacity_enforcement(tmp_path: Path) -> None:
    scheduler = PortfolioScheduler(
        capacity_config=SlotCapacityConfig(max_heavy_slots=1, max_light_slots=4),
        storage_file=tmp_path / "sched_cap.json",
    )

    # Acquire 1st heavy slot -> Success
    h1 = scheduler.acquire_slot("job-h1", JobSlotKind.HEAVY, "worker-1")
    assert h1 is not None
    assert scheduler.can_acquire_slot(JobSlotKind.HEAVY) is False

    # Acquire 2nd heavy slot -> Blocked
    h2 = scheduler.acquire_slot("job-h2", JobSlotKind.HEAVY, "worker-2")
    assert h2 is None

    # Acquire 4 light slots -> Success
    l_slots = []
    for i in range(1, 5):
        slot = scheduler.acquire_slot(f"job-l{i}", JobSlotKind.LIGHT, f"worker-{i}")
        assert slot is not None
        l_slots.append(slot)

    assert scheduler.can_acquire_slot(JobSlotKind.LIGHT) is False

    # Acquire 5th light slot -> Blocked
    l5 = scheduler.acquire_slot("job-l5", JobSlotKind.LIGHT, "worker-5")
    assert l5 is None

    # Release heavy slot -> Can acquire heavy again
    assert scheduler.release_slot(h1.slot_id) is True
    assert scheduler.can_acquire_slot(JobSlotKind.HEAVY) is True
    h3 = scheduler.acquire_slot("job-h3", JobSlotKind.HEAVY, "worker-3")
    assert h3 is not None


# ==============================================================================
# 2. Weighted Fair Queueing & Starvation Protection Tests
# ==============================================================================


def test_weighted_fair_queueing_priority(tmp_path: Path) -> None:
    # Atrium weight 3.0, Jarvis weight 2.0, DarkFac weight 1.0
    weights = ProjectWeightConfig(
        weights={"atrium": 3.0, "jarvis": 2.0, "darkfac": 1.0},
        max_starvation_ticks=10,
    )
    scheduler = PortfolioScheduler(weight_config=weights, storage_file=tmp_path / "sched_wfq.json")

    for i in range(5):
        scheduler.enqueue_job(f"atrium-{i}", "atrium", JobSlotKind.LIGHT)
        scheduler.enqueue_job(f"jarvis-{i}", "jarvis", JobSlotKind.LIGHT)
        scheduler.enqueue_job(f"darkfac-{i}", "darkfac", JobSlotKind.LIGHT)

    dispatched = []
    for _ in range(6):
        job = scheduler.dequeue_next()
        assert job is not None
        dispatched.append(job["project_id"])

    # Atrium should be selected first due to highest weight
    assert dispatched[0] == "atrium"
    assert dispatched.count("atrium") >= dispatched.count("darkfac")


def test_starvation_guard_prevents_lockout(tmp_path: Path) -> None:
    # Set low starvation threshold (3 ticks)
    weights = ProjectWeightConfig(
        weights={"atrium": 10.0, "darkfac": 1.0},
        max_starvation_ticks=3,
    )
    scheduler = PortfolioScheduler(weight_config=weights, storage_file=tmp_path / "sched_starve.json")

    # 1 DarkFac job and 10 Atrium jobs
    scheduler.enqueue_job("darkfac-0", "darkfac", JobSlotKind.LIGHT)
    for i in range(10):
        scheduler.enqueue_job(f"atrium-{i}", "atrium", JobSlotKind.LIGHT)

    dispatched = []
    for _ in range(5):
        job = scheduler.dequeue_next()
        assert job is not None
        dispatched.append(job["project_id"])

    # DarkFac must be dispatched within max_starvation_ticks despite Atrium's 10x weight
    assert "darkfac" in dispatched


# ==============================================================================
# 3. Budget Manager & Threshold Tests
# ==============================================================================


def test_budget_manager_warning_and_cutoff(tmp_path: Path, mock_telegram: TelegramGateway) -> None:
    budget_file = tmp_path / "budgets.json"
    mgr = PortfolioBudgetManager(storage_file=budget_file, telegram_gateway=mock_telegram)

    mgr.set_budget("atrium", monthly_limit_usd=100.0)

    # Spend $50 (50%) -> ACTIVE
    b1 = mgr.record_spend("atrium", 50.0)
    assert b1.status == BudgetStatus.ACTIVE
    assert mgr.is_paid_cloud_allowed("atrium") is True

    # Spend $35 (total $85 = 85%) -> WARNING (>=80%)
    b2 = mgr.record_spend("atrium", 35.0)
    assert b2.status == BudgetStatus.WARNING
    assert mgr.is_paid_cloud_allowed("atrium") is True

    # Spend $20 (total $105 = 105%) -> LOCAL_ONLY (>=100%)
    b3 = mgr.record_spend("atrium", 20.0)
    assert b3.status == BudgetStatus.LOCAL_ONLY
    assert mgr.is_paid_cloud_allowed("atrium") is False


# ==============================================================================
# 4. Paid-Account-First Model Router Tests
# ==============================================================================


def test_model_router_paid_account_first(tmp_path: Path) -> None:
    budget_file = tmp_path / "budgets.json"
    mgr = PortfolioBudgetManager(storage_file=budget_file)
    mgr.set_budget("atrium", 50.0)

    # Active balance on direct paid providers
    balances = {"antigravity": 50.0, "xai": 20.0, "openai": 30.0, "openrouter": 15.0}
    router = PortfolioModelRouter(budget_manager=mgr, account_balances=balances)

    # Research -> Gemini 3.8 Flash (Antigravity)
    res_research = router.route_task("research", project_id="atrium")
    assert res_research.tier == ModelTier.PAID_DIRECT
    assert res_research.selected_model == "gemini-3.8-flash"
    assert res_research.provider == "antigravity"

    # Web Research -> Grok 4.6 (xAI)
    res_web = router.route_task("web_research", project_id="atrium")
    assert res_web.tier == ModelTier.PAID_DIRECT
    assert res_web.selected_model == "grok-4.6"

    # Architecture -> Luna xhigh (OpenAI)
    res_arch = router.route_task("architecture", project_id="atrium")
    assert res_arch.tier == ModelTier.PAID_DIRECT
    assert res_arch.selected_model == "luna-xhigh"


def test_model_router_openrouter_fallback(tmp_path: Path) -> None:
    budget_file = tmp_path / "budgets.json"
    mgr = PortfolioBudgetManager(storage_file=budget_file)
    mgr.set_budget("jarvis", 50.0)

    # Direct accounts have $0, but OpenRouter has credits
    balances = {"antigravity": 0.0, "xai": 0.0, "openai": 0.0, "openrouter": 25.0}
    router = PortfolioModelRouter(budget_manager=mgr, account_balances=balances)

    res = router.route_task("coding_high", project_id="jarvis")
    assert res.tier == ModelTier.OPENROUTER_FRONTIER
    assert res.provider == "openrouter"
    assert "qwen" in res.selected_model.lower()


def test_model_router_local_zero_fallback_on_budget_exhaustion(tmp_path: Path) -> None:
    budget_file = tmp_path / "budgets.json"
    mgr = PortfolioBudgetManager(storage_file=budget_file)
    mgr.set_budget("darkfac", 20.0)

    # Exhaust budget for darkfac
    mgr.record_spend("darkfac", 25.0)

    balances = {"antigravity": 50.0, "xai": 20.0, "openai": 30.0, "openrouter": 15.0}
    router = PortfolioModelRouter(budget_manager=mgr, account_balances=balances)

    # Must fail-closed to $0 local Ollama despite active cloud balances
    res = router.route_task("architecture", project_id="darkfac")
    assert res.tier == ModelTier.LOCAL_ZERO
    assert res.provider == "ollama"
    assert res.estimated_cost_usd == 0.0
    assert "budget ceiling reached" in res.reason.lower()


# ==============================================================================
# 5. CLI End-to-End Tests
# ==============================================================================


def test_portfolio_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    budget_file = tmp_path / "cli_budgets.json"
    scheduler_file = tmp_path / "cli_scheduler.json"
    monkeypatch.setattr("core.portfolio.budget_manager.DEFAULT_BUDGET_FILE", budget_file)
    monkeypatch.setattr("core.portfolio.scheduler.DEFAULT_SCHEDULER_FILE", scheduler_file)

    # 1. Status command
    assert cli_main(["status"]) == 0

    # 2. Set-budget command
    assert cli_main(["set-budget", "--project", "atrium", "--limit", "80"]) == 0

    # 3. Record-spend command
    assert cli_main(["record-spend", "--project", "atrium", "--amount", "15"]) == 0

    # 4. Route-task command
    assert cli_main(["route-task", "--task-type", "research", "--project", "atrium"]) == 0

    # 5. Enqueue-job command
    assert cli_main(["enqueue-job", "--job-id", "test-job-1", "--project", "atrium", "--kind", "light"]) == 0

    # 6. Dequeue-job command
    assert cli_main(["dequeue-job"]) == 0
