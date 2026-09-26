"""Unified verification tests for Model Router & Harness Selection across Dark Factory."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line.agent_cli import AgentRequest, AgentResult, run_agent
from core.line.routing import (
    RoutingConfig,
    StageRoute,
    _HARNESS_TO_PROVIDER,
    _default_quota_headroom,
    default_config_path,
    load_routing_config,
    pick,
    record_result,
)
from core.portfolio.models import ModelTier
from core.portfolio.router_optimizer import PortfolioModelRouter
from core.router.harness_router import HeadroomDynamicPolicy
from core.router.model_router import recommend_model
from core.usage.ledger import ModelUsageLedger
from core.usage.reservation import QuotaReservationManager


def _mock_snapshot(path: Path, provider_id: str, remaining_percent: float, age_seconds: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checked_at = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    data = {
        "provider_id": provider_id,
        "provider_name": provider_id.title(),
        "family": "frontier",
        "status": "connected",
        "adapter": "test_adapter",
        "quota_supported": True,
        "windows": [
            {
                "quota_id": f"{provider_id}:weekly",
                "label": "Limite Semanal",
                "remaining_percent": remaining_percent,
                "used_percent": round(100.0 - remaining_percent, 2),
                "metric": "subscription",
            }
        ],
        "checked_at": checked_at,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_dynamic_headroom_chooses_antigravity_with_real_critical_quotas(tmp_path: Path):
    """Codex at 2%, Claude at 3%, Grok at 2.8%, Antigravity at 61.5% -> Antigravity wins."""
    cfg = load_routing_config(default_config_path())

    def quota_lookup(provider_id: str) -> float | None:
        table = {
            "openai": 2.0,      # codex: CRITICAL (<= 15%)
            "anthropic": 3.0,   # claude: CRITICAL (<= 15%)
            "xai": 2.84,        # grok: CRITICAL (<= 15%)
            "google": 61.47,    # antigravity: HEALTHY (> 15%)
        }
        return table.get(provider_id)

    choice = pick(
        "development",
        ["harness:codex", "harness:claude", "harness:grok", "harness:antigravity"],
        config=cfg,
        quota_lookup=quota_lookup,
        cooldown_path=tmp_path / "cooldowns.json",
        complexity="medium",
    )
    assert choice == ("antigravity", None)


def test_stale_snapshot_triggers_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A snapshot older than 3600 seconds must be discarded, failing closed to ineligible."""
    provider_dir = tmp_path / "providers"
    monkeypatch.setattr("core.line.routing.REPO_ROOT", tmp_path)
    (tmp_path / ".factory" / "usage" / "providers").mkdir(parents=True, exist_ok=True)
    target = tmp_path / ".factory" / "usage" / "providers" / "google.json"

    # Write stale snapshot (2 hours old)
    _mock_snapshot(target, "google", 85.0, age_seconds=7200.0)

    headroom = _default_quota_headroom("google")
    assert headroom is None  # Must fail-closed!


def test_fresh_snapshot_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh snapshot (under 1 hour) must return valid headroom."""
    monkeypatch.setattr("core.line.routing.REPO_ROOT", tmp_path)
    (tmp_path / ".factory" / "usage" / "providers").mkdir(parents=True, exist_ok=True)
    target = tmp_path / ".factory" / "usage" / "providers" / "google.json"

    # Write fresh snapshot (10 minutes old)
    _mock_snapshot(target, "google", 72.5, age_seconds=600.0)

    headroom = _default_quota_headroom("google")
    assert headroom == 72.5


def test_high_intelligence_prefers_opus_sol_when_healthy(tmp_path: Path):
    """When planning or high complexity, Opus/Sol are prioritized if > 15%."""
    cfg = load_routing_config(default_config_path())

    def healthy_lookup(provider_id: str) -> float | None:
        table = {
            "openai": 50.0,
            "anthropic": 45.0,
            "google": 80.0,
        }
        return table.get(provider_id)

    choice = pick(
        "planning",
        ["harness:codex", "harness:claude", "harness:antigravity"],
        config=cfg,
        quota_lookup=healthy_lookup,
        cooldown_path=tmp_path / "cooldowns.json",
        complexity="high",
    )
    # Claude with Opus should be prioritized over Antigravity in high intelligence tier
    assert choice == ("claude", "opus")


def test_high_intelligence_falls_back_to_highest_headroom_when_opus_sol_critical(tmp_path: Path):
    """When Opus/Sol providers are critical (<= 15%), fallback to highest headroom."""
    cfg = load_routing_config(default_config_path())

    def critical_intel_lookup(provider_id: str) -> float | None:
        table = {
            "openai": 5.0,      # codex: CRITICAL
            "anthropic": 8.0,   # claude: CRITICAL
            "google": 65.0,     # antigravity: HEALTHY
        }
        return table.get(provider_id)

    choice = pick(
        "planning",
        ["harness:codex", "harness:claude", "harness:antigravity"],
        config=cfg,
        quota_lookup=critical_intel_lookup,
        cooldown_path=tmp_path / "cooldowns.json",
        complexity="high",
    )
    # Antigravity is chosen because Claude and Codex are in critical state
    assert choice == ("antigravity", None)


def test_autonomous_recommend_never_returns_fable_or_astra():
    """Verify recommendation engine forbids Fable and Astra."""
    rec = recommend_model("architecture", "critical")
    assert rec["model"] not in ("gpt-6-astra", "astra", "claude-fable", "fable")
    assert rec["provider"] in ("anthropic", "antigravity", "openrouter", "google", "openai")


def test_openrouter_fallback_requires_positive_balance():
    """OpenRouter is only selected if verified balance > 0."""
    cfg = load_routing_config(default_config_path())

    def all_critical(_pid: str) -> float | None:
        return 5.0

    # With zero balance -> rejected
    choice_zero = pick(
        "grill",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=all_critical,
        cooldown_path=Path("does-not-exist.json"),
        openrouter_balance_lookup=lambda: 0.0,
    )
    assert choice_zero is None

    # With positive balance -> accepted
    choice_positive = pick(
        "grill",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=all_critical,
        cooldown_path=Path("does-not-exist.json"),
        openrouter_balance_lookup=lambda: 12.50,
    )
    assert choice_positive == ("openrouter", cfg.stages["grill"].openrouter_model)


def test_quota_reservation_reduces_effective_headroom(tmp_path: Path):
    """Active reservation lease subtracts from available headroom."""
    res_mgr = QuotaReservationManager(path=tmp_path / "reservations.json")

    res_id = res_mgr.reserve("google", "antigravity", percent=10.0)
    assert res_mgr.get_active_reserved_percent("google") == 10.0

    res_mgr.release(res_id)
    assert res_mgr.get_active_reserved_percent("google") == 0.0


def test_headroom_dynamic_policy_orders_candidates_by_headroom(monkeypatch: pytest.MonkeyPatch):
    """HeadroomDynamicPolicy queries unified quota and orders descending."""
    def fake_lookup(prov: str) -> float | None:
        table = {"google": 60.0, "openai": 2.0, "anthropic": 10.0, "xai": 5.0}
        return table.get(prov)

    monkeypatch.setattr("core.line.routing._default_quota_headroom", fake_lookup)
    policy = HeadroomDynamicPolicy()
    candidates = policy.select_candidate_harnesses("development")
    assert candidates[0] == "antigravity"
