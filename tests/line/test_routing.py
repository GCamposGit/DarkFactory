"""Tests for core.line.routing — quota-aware harness routing (HF-27-03)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line.agent_cli import AgentResult
from core.line.routing import (
    HarnessPin,
    RoutingConfig,
    StageRoute,
    _quota_headroom_diagnosis,
    default_config_path,
    harness_cli_pin,
    load_routing_config,
    pick,
    record_result,
    validate_routing_config,
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


def _write_codex_snapshot(providers_dir: Path, *, checked_at: str, remaining_percent, **extra) -> None:
    providers_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "provider_id": "openai",
        "provider_name": "OpenAI / Codex",
        "family": "frontier",
        "status": "connected",
        "adapter": "codex_app_server",
        "quota_supported": True,
        "windows": [{"id": "secondary", "remaining_percent": remaining_percent, "window_duration_minutes": 10080}],
        "message": "evidence",
        "checked_at": checked_at,
    }
    payload.update(extra)
    (providers_dir / "openai.json").write_text(json.dumps(payload), encoding="utf-8")


def test_pick_development_fails_closed_on_stale_codex_snapshot_then_recovers_after_verified_read(
    tmp_path, monkeypatch
):
    """USR-214: a Codex account the factory could never re-verify (old snapshot, probe unavailable)
    must never be read as healthy quota, even though it reports 56% remaining; once a current,
    verified reading lands, pick('development') elects Codex again."""
    from core.usage.adapters import CodexAccountAdapter

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(CodexAccountAdapter, "_find_codex", lambda self: None)
    monkeypatch.setattr(CodexAccountAdapter, "_from_session_files", lambda self: None)

    providers_dir = tmp_path / "usage" / "providers"
    cfg = load_routing_config(default_config_path())

    old_checked_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    _write_codex_snapshot(providers_dir, checked_at=old_checked_at, remaining_percent=56.0)

    stale_choice = pick(
        "development", ["harness:codex"], config=cfg, quota_lookup=None, cooldown_path=Path("does-not-exist.json"),
    )
    assert stale_choice is None  # the 40-day-old reading is never trusted, even though it says 56% remaining

    fresh_checked_at = datetime.now(timezone.utc).isoformat()
    _write_codex_snapshot(providers_dir, checked_at=fresh_checked_at, remaining_percent=56.0)

    fresh_choice = pick(
        "development", ["harness:codex"], config=cfg, quota_lookup=None, cooldown_path=Path("does-not-exist.json"),
    )
    assert fresh_choice == ("codex", None)  # a validated current read above the 15% floor elects Codex again


def test_pick_development_fails_closed_on_suspicious_codex_reading_even_when_fresh(tmp_path, monkeypatch):
    """A fresh Codex reading that fails the plausibility check (USR-214, `record_probe`) must never be
    read as healthy quota, even though its `checked_at` is current."""
    from core.usage.adapters import CodexAccountAdapter

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(CodexAccountAdapter, "_find_codex", lambda self: None)
    monkeypatch.setattr(CodexAccountAdapter, "_from_session_files", lambda self: None)

    providers_dir = tmp_path / "usage" / "providers"
    _write_codex_snapshot(
        providers_dir,
        checked_at=datetime.now(timezone.utc).isoformat(),
        remaining_percent=None,
        status="degraded",
        plausibility_flags=["secondary: used_percent decreased in same period"],
    )

    cfg = load_routing_config(default_config_path())
    choice = pick(
        "development", ["harness:codex"], config=cfg, quota_lookup=None, cooldown_path=Path("does-not-exist.json"),
    )
    assert choice is None


def test_quota_headroom_diagnosis_distinguishes_stale_probe_unavailable_suspicious_and_healthy(
    tmp_path, monkeypatch
):
    """USR-214 criterion: the result must tell apart a stale disk reading, a probe that could not be
    reached despite being forced, a suspicious (implausible) reading, and a genuinely healthy one —
    without ever carrying raw probe output or credentials."""
    from core.usage.adapters import CodexAccountAdapter

    from core.usage.models import AccountConnectionStatus, ProviderAccountUsage, ProviderFamily

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    providers_dir = tmp_path / "usage" / "providers"
    monkeypatch.setattr(CodexAccountAdapter, "_find_codex", lambda self: None)

    # No snapshot on disk at all (so no re-probe was forced), but the only other evidence this host
    # has — Codex's own old session logs — is itself too old to trust: "stale_snapshot", not
    # "probe_unavailable" (nothing was even attempted beyond reading what already existed).
    old_session_checked_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    monkeypatch.setattr(
        CodexAccountAdapter,
        "_from_session_files",
        lambda self: ProviderAccountUsage(
            provider_id="openai", provider_name="OpenAI / Codex", family=ProviderFamily.FRONTIER,
            status=AccountConnectionStatus.CONNECTED, adapter="codex_session_files",
            message="old session evidence", checked_at=old_session_checked_at,
        ),
    )
    headroom, reason = _quota_headroom_diagnosis("openai")
    assert (headroom, reason) == (None, "stale_snapshot")

    # Now neither a snapshot nor usable session evidence exists, and the live probe is unreachable.
    monkeypatch.setattr(CodexAccountAdapter, "_from_session_files", lambda self: None)
    headroom, reason = _quota_headroom_diagnosis("openai")
    assert (headroom, reason) == (None, "probe_unavailable")

    # A verified, current reading: healthy, with the real headroom value.
    _write_codex_snapshot(providers_dir, checked_at=datetime.now(timezone.utc).isoformat(), remaining_percent=56.0)
    headroom, reason = _quota_headroom_diagnosis("openai")
    assert (headroom, reason) == (56.0, "healthy")

    # The same reading, but old: never trusted, and tagged as a stale snapshot rather than critical.
    old_checked_at = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    _write_codex_snapshot(providers_dir, checked_at=old_checked_at, remaining_percent=56.0)
    headroom, reason = _quota_headroom_diagnosis("openai")
    assert (headroom, reason) == (None, "probe_unavailable")  # disk was stale, so a re-probe was forced and failed

    # A fresh reading that fails the plausibility check must not be confused with either of the above.
    _write_codex_snapshot(
        providers_dir,
        checked_at=datetime.now(timezone.utc).isoformat(),
        remaining_percent=None,
        status="degraded",
        plausibility_flags=["secondary: used_percent decreased in same period"],
    )
    headroom, reason = _quota_headroom_diagnosis("openai")
    assert (headroom, reason) == (None, "suspicious_reading")


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


def test_record_result_ignores_kinds_that_are_not_quota_or_crash(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    for kind in ("timeout", "empty_output", "auth_expired", "not_installed"):
        result = AgentResult(ok=False, text="oops", harness="claude", duration_s=1.0, error_kind=kind)
        record_result(result, cooldown_path=cooldown_path)
    assert not cooldown_path.is_file()


def test_record_result_puts_a_crashing_harness_in_a_short_cooldown(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    cfg = load_routing_config(default_config_path())
    record_result(
        AgentResult(ok=False, text="sandbox boom", harness="codex", duration_s=1.0, error_kind="crash"),
        config=cfg, cooldown_path=cooldown_path,
    )
    entry = json.loads(cooldown_path.read_text(encoding="utf-8"))["codex"]
    until = datetime.fromisoformat(entry["until"])
    assert entry["reason"] == "crash"
    minutes = (until - datetime.now(timezone.utc)).total_seconds() / 60
    assert 5 < minutes <= cfg.crash_cooldown_minutes  # short, nowhere near the 60 min quota cooldown

    # The next pick skips the crashing harness instead of choosing it again immediately.
    quota = lambda provider: 90.0 if "openai" in provider or "codex" in provider else 50.0
    choice = pick("development", ["harness:claude", "harness:codex"], cooldown_path=cooldown_path, quota_lookup=quota, config=cfg)
    assert choice is not None and choice[0] != "codex"


def test_a_crash_never_shortens_an_existing_longer_cooldown(tmp_path):
    cooldown_path = tmp_path / "cooldowns.json"
    long_until = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    cooldown_path.write_text(json.dumps({"codex": {"until": long_until, "reason": "rate_limited"}}), encoding="utf-8")
    record_result(
        AgentResult(ok=False, text="boom", harness="codex", duration_s=1.0, error_kind="crash"),
        cooldown_path=cooldown_path,
    )
    assert json.loads(cooldown_path.read_text(encoding="utf-8"))["codex"]["until"] == long_until


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
    # Development needs `write`: the defaults only list harnesses that declare it.
    assert cfg.stages["development"].cascade[0] == ("claude", "sonnet")
    assert validate_routing_config(cfg) == []


def test_pick_dynamic_headroom_prefers_antigravity_over_critical_codex_claude():
    """Real scenario: Codex at 2%, Claude at 3%, Grok at 2.8%, Antigravity at 61.5%.

    A read stage still elects the only healthy harness (Antigravity); a write stage must NOT, because
    Antigravity has no write mode: it fails closed instead of electing a harness that cannot run it.
    """
    cfg = load_routing_config(default_config_path())

    def real_like_lookup(provider_id: str) -> float | None:
        table = {
            "openai": 2.0,      # codex: CRITICAL (<= 15%)
            "anthropic": 3.0,   # claude: CRITICAL (<= 15%)
            "xai": 2.84,        # grok: CRITICAL (<= 15%)
            "google": 61.47,    # antigravity: HEALTHY (> 15%)
        }
        return table.get(provider_id)

    caps = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
    read_choice = pick(
        "grill", caps, config=cfg, quota_lookup=real_like_lookup, cooldown_path=Path("does-not-exist.json"),
    )
    assert read_choice == ("antigravity", None)

    write_choice = pick(
        "development", caps, config=cfg, quota_lookup=real_like_lookup, cooldown_path=Path("does-not-exist.json"),
    )
    assert write_choice is None


def test_planning_elects_codex_high_tier_model_when_claude_quota_is_unknown():
    cfg = load_routing_config(default_config_path())
    lookup = lambda provider: None if provider == "anthropic" else 80.0  # noqa: E731

    choice = pick(
        "planning",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=lookup,
        cooldown_path=Path("does-not-exist.json"),
    )

    assert choice == ("codex", "gpt-sol")


def test_planning_still_refuses_codex_without_a_high_tier_model():
    cfg = RoutingConfig(stages={"planning": StageRoute(cascade=[("claude", "opus"), ("codex", None)])})
    lookup = lambda provider: None if provider == "anthropic" else 80.0  # noqa: E731

    choice = pick(
        "planning",
        ["harness:claude", "harness:codex"],
        config=cfg,
        quota_lookup=lookup,
        cooldown_path=Path("does-not-exist.json"),
    )

    assert choice is None


def test_planning_codex_model_and_effort_reach_the_cli_argv():
    from core.line.agent_cli import AgentRequest, build_codex_argv

    cfg = load_routing_config(default_config_path())
    model, effort = harness_cli_pin("codex", "gpt-sol", stage="planning", config=cfg)
    req = AgentRequest(prompt="x", cwd=Path("."), mode="read", harness="codex", model=model, effort=effort)

    argv = build_codex_argv("codex", req, Path("out.txt"))

    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="high"' in argv


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


# --------------------------------------------------------------------------
# harness_cli_pin (USR-201): declared model/effort per harness per stage
# --------------------------------------------------------------------------


def test_harness_cli_pin_returns_the_declared_model_and_effort():
    cfg = load_routing_config(default_config_path())

    model, effort = harness_cli_pin("codex", None, stage="development", config=cfg)

    assert model == "gpt-6-luna"
    assert effort == "high"


def test_harness_cli_pin_falls_back_to_the_cascade_model_without_a_pin():
    cfg = RoutingConfig(stages={"development": StageRoute(cascade=[("antigravity", "flash")])})

    model, effort = harness_cli_pin("antigravity", "flash", stage="development", config=cfg)

    assert model == "flash"
    assert effort is None


def test_harness_cli_pin_ignores_a_forbidden_pinned_model():
    cfg = RoutingConfig(
        forbidden_autonomous_models=["gpt-6-astra"],
        stages={
            "development": StageRoute(
                cascade=[("codex", None)],
                harness_pins={"codex": HarnessPin(model="gpt-6-astra", effort="high")},
            )
        },
    )

    model, effort = harness_cli_pin("codex", None, stage="development", config=cfg)

    assert model is None  # the forbidden pin is dropped, and the cascade model was None
    assert effort == "high"  # effort is unaffected by the model-forbidding check


def test_harness_cli_pin_on_an_unknown_stage_returns_the_cascade_model_unchanged():
    cfg = load_routing_config(default_config_path())

    model, effort = harness_cli_pin("codex", "some-model", stage="no-such-stage", config=cfg)

    assert model == "some-model"
    assert effort is None


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


_DEV_CAPS = ["harness:claude", "harness:codex", "harness:grok"]
_NO_COOLDOWN = Path("does-not-exist.json")


def _dev_pick(table: dict[str, float], **kwargs):
    return pick(
        "development", _DEV_CAPS, config=load_routing_config(default_config_path()),
        quota_lookup=table.get, cooldown_path=_NO_COOLDOWN, **kwargs,
    )


def test_grok_is_electable_in_development_when_it_has_the_most_headroom():
    table = {"anthropic": 40.0, "openai": 30.0, "xai": 90.0}
    assert _dev_pick(table, complexity="medium")[0] == "grok"
    assert _dev_pick({"anthropic": 5.0, "openai": 5.0, "xai": 50.0}, complexity="medium")[0] == "grok"


def test_development_ranks_by_weekly_slack_and_falls_back_to_headroom():
    table = {"anthropic": 80.0, "openai": 60.0, "xai": 50.0}
    slack = {"anthropic": 0.2, "openai": 0.9, "xai": 0.5}
    assert _dev_pick(table, complexity="medium", slack_lookup=slack.get)[0] == "codex"
    # one candidate without a reset: slack is ignored and plain headroom decides
    partial = {"anthropic": 0.2, "openai": 0.9}
    assert _dev_pick(table, complexity="medium", slack_lookup=partial.get)[0] == "claude"


def test_single_critical_floor_is_the_registry_floor():
    from core.models.registry import QUOTA_FLOOR_MIN
    from core.router.token_budget import TokenPressure, _pressure_for

    assert load_routing_config(default_config_path()).pressure_thresholds["critical"] == QUOTA_FLOOR_MIN
    assert _pressure_for(QUOTA_FLOOR_MIN) == TokenPressure.CRITICAL
    assert _pressure_for(QUOTA_FLOOR_MIN + 0.1) != TokenPressure.CRITICAL
    floor = {"anthropic": QUOTA_FLOOR_MIN, "openai": QUOTA_FLOOR_MIN, "xai": QUOTA_FLOOR_MIN}
    assert _dev_pick(floor) is None



def _write_anthropic_snapshot(providers_dir, checked_at, five_used, five_reset, weekly_used=20.0):
    providers_dir.mkdir(parents=True, exist_ok=True)
    wk_reset = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    windows = [
        {"quota_id": "claude:5h", "used_percent": five_used, "remaining_percent": 100 - five_used,
         "window_duration_minutes": 300, "resets_at": five_reset, "metric": "subscription"},
        {"quota_id": "claude:weekly", "used_percent": weekly_used, "remaining_percent": 100 - weekly_used,
         "window_duration_minutes": 10080, "resets_at": wk_reset, "metric": "subscription"},
    ]
    (providers_dir / "anthropic.json").write_text(json.dumps({
        "provider_id": "anthropic", "status": "connected", "adapter": "claude_statusline",
        "quota_supported": True, "windows": windows, "checked_at": checked_at,
    }), encoding="utf-8")


def test_anthropic_stale_snapshot_held_until_5h_reset_with_margin(tmp_path, monkeypatch):
    """USR-416: between Claude sessions the statusline snapshot goes stale; routing keeps it (used + 10
    points) until the 5h window resets instead of treating the Claude quota as unknown."""
    from core.usage.adapters import ClaudeCodeAccountAdapter

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_find_claude", staticmethod(lambda: None))
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_credentials", lambda self: None)
    monkeypatch.setattr("core.line.routing._snapshot_max_age", lambda _dir: 1800.0)
    providers_dir = tmp_path / "usage" / "providers"
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    _write_anthropic_snapshot(providers_dir, old, 30.0, future)
    assert _quota_headroom_diagnosis("anthropic") == (60.0, "healthy")  # 100 - (30 + 10)

    _write_anthropic_snapshot(providers_dir, old, 92.0, future)
    assert _quota_headroom_diagnosis("anthropic") == (0.0, "healthy")  # stays below the 15% floor

    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _write_anthropic_snapshot(providers_dir, old, 30.0, past)
    headroom, reason = _quota_headroom_diagnosis("anthropic")
    assert headroom is None and reason != "healthy"  # 5h window already reset: unknown again


def test_claude_headless_probe_reads_rate_limit_events_and_writes_fresh_snapshot(tmp_path, monkeypatch):
    """USR-416: `claude -p --output-format stream-json` rate_limit_event -> fresh snapshot (fake subprocess)."""
    import subprocess

    from core.usage import adapters as adapters_mod
    from core.usage.adapters import ClaudeCodeAccountAdapter, build_default_adapters

    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_find_claude", staticmethod(lambda: "claude"))
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_cli", lambda self, exe: None)
    reset = int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())
    lines = [
        {"type": "system"},
        # CLI 2.1.x shape (captured 28/10): both windows under `unifiedWindows`.
        {"type": "rate_limit_event", "rate_limit_info": {"rateLimitType": "five_hour", "resetsAt": reset, "unifiedWindows": {
            "five_hour": {"utilization": 0.25, "resetsAt": reset}, "seven_day": {"utilization": 0.65, "resetsAt": reset + 86400}}}},
    ]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(x) for x in lines), "")

    monkeypatch.setattr(adapters_mod.subprocess, "run", fake_run)
    adapter = next(a for a in build_default_adapters(tmp_path) if a.spec.provider_id == "anthropic")
    usage = adapter.inspect(force=True)
    assert usage.quota_supported and usage.adapter == "claude_code_headless"
    used = {w.quota_id: w.used_percent for w in usage.windows}
    assert used == {"claude:5h": 25.0, "claude:weekly": 65.0}
    assert "-p" in calls[0][0] and "--model" in calls[0][0]
    assert (tmp_path / "anthropic.json").is_file()


def test_pick_development_elects_antigravity_when_operating_interactively_without_monkeypatch(tmp_path: Path):
    """USR-436: Interactive operating Antigravity elects Antigravity for development above floor without monkeypatch."""
    caps = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
    lookup = lambda p: 60.0 if p == "google" else (80.0 if p == "xai" else 50.0)

    winner = pick(
        "development",
        caps,
        quota_lookup=lookup,
        operating_harness="antigravity",
        execution_context="interactive",
        cooldown_path=tmp_path / "cd1.json",
    )
    assert winner == ("antigravity", None)

    headless_winner = pick(
        "development",
        caps,
        quota_lookup=lookup,
        operating_harness="antigravity",
        execution_context="headless",
        cooldown_path=tmp_path / "cd2.json",
    )
    assert headless_winner != ("antigravity", None)

    cascade_winner = pick(
        "development",
        caps,
        quota_lookup=lookup,
        operating_harness=None,
        execution_context="interactive",
        cooldown_path=tmp_path / "cd3.json",
    )
    assert cascade_winner != ("antigravity", None)
