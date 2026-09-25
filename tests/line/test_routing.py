"""Tests for core.line.routing — quota-aware harness routing (HF-27-03)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line.agent_cli import AgentResult
from core.line.routing import (
    RoutingConfig,
    StageRoute,
    default_config_path,
    load_routing_config,
    pick,
    record_result,
)


def _always_healthy(_provider_id: str) -> float | None:
    return 90.0


def test_default_config_file_is_loadable():
    cfg = load_routing_config(default_config_path())
    assert set(cfg.stages) == {"grill", "planning", "development", "review", "distill"}
    assert cfg.pressure_thresholds["critical"] == 15
    assert cfg.stages["development"].openrouter_model
    assert cfg.stages["review"].openrouter_model


def test_pick_walks_cascade_in_order():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "development",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("claude", "sonnet")


def test_pick_skips_harness_missing_from_host_caps():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "development",
        ["harness:codex"],  # no claude on this host
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("codex", None)


def test_pick_skips_critical_quota_account():
    cfg = load_routing_config(default_config_path())

    def lookup(provider_id: str) -> float | None:
        return 5.0 if provider_id == "anthropic" else 80.0  # claude is CRITICAL (<= 15)

    choice = pick(
        "development",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=lookup,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("codex", None)


def test_pick_unknown_quota_counts_as_ineligible_fail_closed():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "development",
        ["harness:claude"],
        config=cfg,
        quota_lookup=lambda _pid: None,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice is None


def test_record_result_sets_cooldown_then_next_pick_uses_next_in_cascade(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())

    result = AgentResult(
        ok=False,
        text="rate limited",
        harness="claude",
        model="sonnet",
        duration_s=1.0,
        error_kind="rate_limited",
        reset_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    record_result(result, config=cfg, cooldown_path=cooldown_path)

    assert cooldown_path.is_file()
    data = json.loads(cooldown_path.read_text(encoding="utf-8"))
    assert "claude" in data

    choice = pick(
        "development",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=cooldown_path,
    )
    assert choice == ("codex", None)


def test_record_result_uses_default_cooldown_when_no_reset_at(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())
    result = AgentResult(
        ok=False, text="rate limited", harness="codex", model=None, duration_s=1.0, error_kind="rate_limited",
    )
    record_result(result, config=cfg, cooldown_path=cooldown_path)
    data = json.loads(cooldown_path.read_text(encoding="utf-8"))
    until = datetime.fromisoformat(data["codex"]["until"])
    expected = datetime.now(timezone.utc) + timedelta(minutes=cfg.cooldown_default_minutes)
    assert abs((until - expected).total_seconds()) < 5


def test_record_result_ignores_non_rate_limited(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    result = AgentResult(ok=False, text="oops", harness="claude", duration_s=1.0, error_kind="crash")
    record_result(result, cooldown_path=cooldown_path)
    assert not cooldown_path.is_file()


def test_all_cascade_accounts_in_cooldown_development_returns_none(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())
    until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    cooldown_path.write_text(
        json.dumps(
            {
                "claude": {"until": until, "reason": "rate_limited"},
                "codex": {"until": until, "reason": "rate_limited"},
                "grok": {"until": until, "reason": "rate_limited"},
                "antigravity": {"until": until, "reason": "rate_limited"},
            }
        ),
        encoding="utf-8",
    )

    choice = pick(
        "development",
        ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=cooldown_path,
    )
    assert choice is None  # development has no openrouter_model configured


def test_all_cascade_accounts_in_cooldown_review_returns_openrouter(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())
    until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    cooldown_path.write_text(
        json.dumps(
            {
                "codex": {"until": until, "reason": "rate_limited"},
                "grok": {"until": until, "reason": "rate_limited"},
            }
        ),
        encoding="utf-8",
    )

    choice = pick(
        "review",
        ["harness:claude", "harness:codex", "harness:grok"],
        implementing_harness="claude",  # review cascade becomes [codex, grok]
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=cooldown_path,
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert choice == ("openrouter", cfg.stages["review"].openrouter_model)


def test_openrouter_respects_run_cap_budget(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())
    until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    cooldown_path.write_text(
        json.dumps({"codex": {"until": until}, "grok": {"until": until}}),
        encoding="utf-8",
    )
    choice = pick(
        "review",
        ["harness:claude", "harness:codex", "harness:grok"],
        implementing_harness="claude",
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=cooldown_path,
        spent_usd=cfg.run_caps.openrouter_usd,  # cap already exhausted
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert choice is None


def test_review_other_family_prefers_codex_when_dev_was_claude():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "review",
        ["harness:claude", "harness:codex", "harness:grok"],
        implementing_harness="claude",
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("codex", None)


def test_review_other_family_prefers_claude_when_dev_was_codex():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "review",
        ["harness:claude", "harness:codex", "harness:grok"],
        implementing_harness="codex",
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("claude", None)


def test_exclude_skips_named_harness():
    cfg = load_routing_config(default_config_path())
    choice = pick(
        "development",
        ["harness:claude", "harness:codex"],
        exclude=["claude"],
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    assert choice == ("codex", None)


def test_editing_json_config_changes_cascade_without_code_change(tmp_path):
    custom_path = tmp_path / "custom_line_routing.json"
    custom_path.write_text(
        json.dumps(
            {
                "pressure_thresholds": {"guarded": 50, "stressed": 25, "critical": 10},
                "cooldown_default_minutes": 60,
                "stages": {
                    "development": {
                        "cascade": [["codex", None], ["claude", "sonnet"]],
                        "openrouter_ok": False,
                    }
                },
                "run_caps": {
                    "agent_calls": 14,
                    "validate_iterations_per_ticket": 3,
                    "review_rounds": 2,
                    "wall_clock_hours": 6,
                    "openrouter_usd": 2.0,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_routing_config(custom_path)
    choice = pick(
        "development",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    # codex now comes first in the edited cascade, purely from JSON.
    assert choice == ("codex", None)


def test_load_routing_config_falls_back_to_defaults_when_file_missing(tmp_path):
    missing = tmp_path / "nope.json"
    cfg = load_routing_config(missing)
    assert isinstance(cfg, RoutingConfig)
    assert cfg.pressure_thresholds["critical"] == 15.0
    assert cfg.stages["development"].cascade[0] == ("antigravity", None)


def test_pick_dynamic_headroom_prefers_antigravity_over_critical_codex_claude():
    """Real scenario: Codex at 2%, Claude at 3%, Grok at 2.8%, Antigravity at 61.5%."""
    cfg = load_routing_config(default_config_path())

    def real_like_lookup(provider_id: str) -> float | None:
        table = {
            "openai": 2.0,      # codex: CRITICAL (<= 15%)
            "anthropic": 3.0,   # claude: CRITICAL (<= 15%)
            "xai": 2.84,        # grok: CRITICAL (<= 15%)
            "google": 61.47,    # antigravity: HEALTHY (> 15%)
        }
        return table.get(provider_id)

    choice = pick(
        "development",
        ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        config=cfg,
        quota_lookup=real_like_lookup,
        cooldown_path=Path("does-not-exist.json"),
    )
    # Antigravity must be selected since all others are critical!
    assert choice == ("antigravity", None)


def test_pick_skips_forbidden_autonomous_models():
    """Models like Fable and Astra must be strictly skipped autonomously."""
    custom_cfg = RoutingConfig(
        forbidden_autonomous_models=["gpt-6-astra", "claude-fable"],
        stages={
            "planning": StageRoute(
                cascade=[("codex", "gpt-6-astra"), ("claude", "opus")],
            )
        }
    )

    choice = pick(
        "planning",
        ["harness:codex", "harness:claude"],
        config=custom_cfg,
        quota_lookup=_always_healthy,
        cooldown_path=Path("does-not-exist.json"),
    )
    # codex with gpt-6-astra is forbidden; falls through to claude opus
    assert choice == ("claude", "opus")


def test_quota_reservation_manager_reserve_and_release(tmp_path):
    from core.usage.reservation import QuotaReservationManager

    res_file = tmp_path / "reservations.json"
    mgr = QuotaReservationManager(path=res_file)

    assert mgr.get_active_reserved_percent("google") == 0.0

    res_id = mgr.reserve("google", "antigravity", percent=5.0)
    assert mgr.get_active_reserved_percent("google") == 5.0

    res_id2 = mgr.reserve("google", "antigravity", percent=3.0)
    assert mgr.get_active_reserved_percent("google") == 8.0

    assert mgr.release(res_id) is True
    assert mgr.get_active_reserved_percent("google") == 3.0

    assert mgr.release(res_id2) is True
    assert mgr.get_active_reserved_percent("google") == 0.0

