"""Contract tests for harness capabilities (USR-67): declared, never assumed.

Root cause guarded here: the router used to know only harness names and quota, so the harness with the
most headroom (Antigravity) won the `development` stage even though it has no headless write mode, and
the refusal surfaced as a misleading `not_installed`. A stage now has a mode, every harness declares the
modes it implements, and `pick()` filters on that before it ranks by headroom.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.line import agent_cli
from core.line.agent_cli import HARNESS_CAPABILITIES, AgentRequest, run_agent, supports
from core.line.routing import (
    STAGE_MODES,
    RoutingConfig,
    default_config_path,
    load_routing_config,
    pick,
    stage_mode,
    validate_routing_config,
)

_ALL_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


def _full_headroom(_provider_id: str) -> float | None:
    return 100.0


def _pick(stage: str, caps: list[str], config: RoutingConfig, tmp_path: Path, **kwargs):
    return pick(
        stage, caps, config=config, quota_lookup=kwargs.pop("quota_lookup", _full_headroom),
        cooldown_path=tmp_path / "cooldowns.json", **kwargs,
    )


def _write_config(tmp_path: Path, development_cascade: list[list[str | None]], **stage_extras) -> RoutingConfig:
    path = tmp_path / "line_routing.json"
    path.write_text(
        json.dumps({"stages": {"development": {"cascade": development_cascade, **stage_extras}}}),
        encoding="utf-8",
    )
    return load_routing_config(path)


# --------------------------------------------------------------------------
# The declared table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "write"),
    [("claude", True), ("codex", True), ("grok", True), ("antigravity", False), ("openrouter", False)],
)
def test_write_is_declared_only_for_harnesses_whose_runner_implements_it(harness: str, write: bool) -> None:
    assert supports(harness, "write") is write
    assert supports(harness, "read") is True  # every runner can return text


def test_every_runner_has_a_declared_capability_entry_and_vice_versa() -> None:
    assert set(HARNESS_CAPABILITIES) == set(agent_cli._HARNESS_RUNNERS)
    assert all(modes <= {"read", "write"} and modes for modes in HARNESS_CAPABILITIES.values())


def test_an_unknown_harness_supports_nothing_and_lookup_is_case_insensitive() -> None:
    assert supports("mystery", "read") is False
    assert supports("mystery", "write") is False
    assert supports(" Claude ", "write") is True


def test_stage_modes_write_stages_are_development_and_integration_only() -> None:
    assert STAGE_MODES == {"development": "write", "integration": "write"}
    assert stage_mode("development") == "write" and stage_mode("integration") == "write"
    for stage in ("grill", "planning", "review", "distill", "anything-else"):
        assert stage_mode(stage) == "read"


# --------------------------------------------------------------------------
# validate_routing_config
# --------------------------------------------------------------------------


def test_the_real_routing_config_has_no_capability_violations() -> None:
    assert validate_routing_config(load_routing_config(default_config_path())) == []


def test_a_write_stage_listing_antigravity_is_a_violation_mutation_proof(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [["antigravity", None], ["claude", "sonnet"], ["grok", None]])

    violations = validate_routing_config(config)

    assert len(violations) == 1
    assert any("antigravity" in v and "development" in v and "'write'" in v for v in violations)
    assert not any("grok" in v for v in violations)  # Grok Build declares `write` since USR-109
    assert not any("claude" in v for v in violations)


def test_read_stages_may_list_any_harness_and_the_other_family_cascade_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "line_routing.json"
    path.write_text(
        json.dumps(
            {
                "stages": {
                    "grill": {"cascade": [["antigravity", None], ["grok", None]]},
                    "review": {"cascade": "other_family_than_development"},
                    "integration": {"cascade": "other_family_than_development"},
                }
            }
        ),
        encoding="utf-8",
    )
    violations = validate_routing_config(load_routing_config(path))

    assert violations and all("'integration'" in v for v in violations)  # only the write stage is flagged


# --------------------------------------------------------------------------
# pick(): capability filter before headroom ranking
# --------------------------------------------------------------------------


def test_write_never_elects_antigravity_even_with_full_headroom_and_first_in_the_cascade(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [["antigravity", None], ["codex", None]])

    def lookup(provider_id: str) -> float | None:
        return 100.0 if provider_id == "google" else 20.0  # antigravity has by far the most headroom

    choice = _pick("development", _ALL_CAPS, config, tmp_path, quota_lookup=lookup, mode="write")

    assert choice == ("codex", None)


def test_write_can_elect_grok_now_that_it_declares_write(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [["antigravity", None], ["grok", None]])

    choice = _pick("development", _ALL_CAPS, config, tmp_path, mode="write")

    assert choice == ("grok", None)


def test_write_with_only_read_only_harnesses_available_returns_none_not_openrouter(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [["antigravity", None]], openrouter_ok=True, openrouter_model="deepseek/x")

    choice = _pick(
        "development", ["harness:antigravity"], config, tmp_path,
        mode="write", openrouter_balance_lookup=lambda: 50.0,
    )

    assert choice is None  # the OpenRouter fallback is read-only, so it never serves a write stage


def test_mode_defaults_to_the_stage_mode_when_omitted(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [["antigravity", None], ["claude", "sonnet"]])

    # No `mode=`: development is a write stage, so Antigravity is skipped.
    assert _pick("development", _ALL_CAPS, config, tmp_path) == ("claude", "sonnet")
    assert _pick("development", ["harness:antigravity"], config, tmp_path) is None


def test_read_can_still_elect_antigravity(tmp_path: Path) -> None:
    config = load_routing_config(default_config_path())

    assert _pick("grill", ["harness:antigravity"], config, tmp_path) == ("antigravity", None)
    # Planning is high-tier only (USR-375 S5): a cheap/default model means wait (None), never a fallback.
    assert _pick("planning", ["harness:antigravity"], config, tmp_path) is None
    assert _pick("planning", ["harness:antigravity", "harness:claude"], config, tmp_path) == ("claude", "opus")
    # An explicit read mode overrides the stage default, so the capability filter is about the mode.
    custom = _write_config(tmp_path, [["antigravity", None], ["claude", "sonnet"]])
    assert _pick("development", _ALL_CAPS, custom, tmp_path, mode="read") == ("antigravity", None)


def test_read_keeps_the_openrouter_fallback(tmp_path: Path) -> None:
    config = load_routing_config(default_config_path())

    choice = _pick("grill", [], config, tmp_path, openrouter_balance_lookup=lambda: 10.0)

    assert choice == ("openrouter", config.stages["grill"].openrouter_model)


def test_planning_can_fallback_to_high_tier_openrouter(tmp_path: Path) -> None:
    """USR-434: when all harnesses are exhausted, planning can elect a high-tier OpenRouter model."""
    config = load_routing_config(default_config_path())
    choice = _pick("planning", [], config, tmp_path, openrouter_balance_lookup=lambda: 10.0)
    assert choice == ("openrouter", config.stages["planning"].openrouter_model)


# --------------------------------------------------------------------------
# Planning stage routing: Grok contingency and OpenRouter fallback (USR-434)
# --------------------------------------------------------------------------


def test_planning_elects_claude_when_claude_has_healthy_quota(tmp_path: Path) -> None:
    """Claude healthy -> planning elects Claude; Grok is not elected even with higher headroom."""
    config = load_routing_config(default_config_path())

    def quota_lookup(provider_id: str) -> float | None:
        if provider_id == "anthropic":
            return 80.0
        if provider_id == "openai":
            return 50.0
        if provider_id == "xai":
            return 95.0  # Grok has highest headroom
        return 100.0

    choice = _pick("planning", _ALL_CAPS, config, tmp_path, quota_lookup=quota_lookup)
    assert choice == ("claude", "opus")


def test_planning_elects_codex_when_codex_healthy_and_claude_critical(tmp_path: Path) -> None:
    """Codex healthy, Claude critical -> planning elects Codex; Grok is not elected."""
    config = load_routing_config(default_config_path())

    def quota_lookup(provider_id: str) -> float | None:
        if provider_id == "anthropic":
            return 10.0  # critical <= 15%
        if provider_id == "openai":
            return 60.0  # healthy
        if provider_id == "xai":
            return 95.0  # Grok has higher headroom
        return 100.0

    choice = _pick("planning", _ALL_CAPS, config, tmp_path, quota_lookup=quota_lookup)
    assert choice == ("codex", "gpt-sol")


def test_planning_elects_grok_when_claude_and_codex_both_critical(tmp_path: Path) -> None:
    """Claude and Codex both critical -> Grok is permitted as planning contingency and elected."""
    config = load_routing_config(default_config_path())

    def quota_lookup(provider_id: str) -> float | None:
        if provider_id == "anthropic":
            return 10.0  # critical
        if provider_id == "openai":
            return 5.0  # critical
        if provider_id == "xai":
            return 70.0  # healthy
        return 100.0

    choice = _pick("planning", _ALL_CAPS, config, tmp_path, quota_lookup=quota_lookup)
    assert choice == ("grok", "grok-4.7")


def test_planning_elects_grok_when_claude_and_codex_in_cooldown(tmp_path: Path) -> None:
    """Claude and Codex cooling -> Grok is permitted as planning contingency and elected."""
    config = load_routing_config(default_config_path())
    cooldowns_path = tmp_path / "cooldowns.json"
    cooldowns_path.write_text(
        json.dumps({
            "claude": {"until": "2099-01-01T00:00:00Z"},
            "codex": {"until": "2099-01-01T00:00:00Z"},
        }),
        encoding="utf-8",
    )

    choice = pick(
        "planning",
        _ALL_CAPS,
        config=config,
        quota_lookup=lambda _p: 80.0,
        cooldown_path=cooldowns_path,
    )
    assert choice == ("grok", "grok-4.7")


def test_planning_elects_openrouter_when_claude_codex_grok_all_critical(tmp_path: Path) -> None:
    """Claude, Codex and Grok all critical -> OpenRouter fallback elected with distill.openrouter."""
    config = load_routing_config(default_config_path())

    choice = _pick(
        "planning",
        _ALL_CAPS,
        config,
        tmp_path,
        quota_lookup=lambda _p: 5.0,  # all critical <= 15%
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert choice == ("openrouter", config.stages["planning"].openrouter_model)
    assert choice == ("openrouter", "deepseek/deepseek-v4.1-flash")


def test_planning_elects_openrouter_when_all_three_cooling_or_absent(tmp_path: Path) -> None:
    """Claude, Codex and Grok all cooling/absent -> OpenRouter fallback elected."""
    config = load_routing_config(default_config_path())
    cooldowns_path = tmp_path / "cooldowns.json"
    cooldowns_path.write_text(
        json.dumps({
            "claude": {"until": "2099-01-01T00:00:00Z"},
            "codex": {"until": "2099-01-01T00:00:00Z"},
            "grok": {"until": "2099-01-01T00:00:00Z"},
        }),
        encoding="utf-8",
    )

    choice = pick(
        "planning",
        _ALL_CAPS,
        config=config,
        quota_lookup=lambda _p: 80.0,
        cooldown_path=cooldowns_path,
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert choice == ("openrouter", config.stages["planning"].openrouter_model)
    assert choice == ("openrouter", "deepseek/deepseek-v4.1-flash")

    # Also absent from host_caps
    choice_absent = _pick(
        "planning",
        [],
        config,
        tmp_path,
        openrouter_balance_lookup=lambda: 10.0,
    )
    assert choice_absent == ("openrouter", config.stages["planning"].openrouter_model)


def test_dropping_a_capable_harness_from_host_caps_still_fails_closed(tmp_path: Path) -> None:
    config = load_routing_config(default_config_path())

    assert _pick("development", ["harness:claude"], config, tmp_path, quota_lookup=lambda _p: 5.0) is None


def test_pick_logs_the_capability_skip_at_info_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = _write_config(tmp_path, [["antigravity", None], ["claude", "sonnet"]])

    with caplog.at_level("INFO", logger="core.line.routing"):
        _pick("development", _ALL_CAPS, config, tmp_path)

    assert any("antigravity" in r.getMessage() and "'write'" in r.getMessage() for r in caplog.records)


def test_a_config_with_a_violation_is_loaded_with_a_warning_not_rejected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="core.line.routing"):
        config = _write_config(tmp_path, [["antigravity", None], ["claude", "sonnet"]])

    assert config.stages["development"].cascade[0] == ("antigravity", None)  # the file is honoured...
    assert any("antigravity" in r.getMessage() for r in caplog.records)  # ...but the contradiction is loud


def test_the_default_routing_config_is_consistent_with_the_capabilities() -> None:
    from core.line.routing import _default_routing_config

    assert validate_routing_config(_default_routing_config()) == []


# --------------------------------------------------------------------------
# run_agent: the refusal is a capability error, not a missing binary
# --------------------------------------------------------------------------


@pytest.mark.parametrize("harness", ["antigravity", "openrouter"])
def test_run_agent_refuses_write_on_a_read_only_harness_as_unsupported_mode(
    harness: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("no binary may be looked up for an unsupported mode")

    for finder in ("find_antigravity_binary", "find_grok_binary"):
        monkeypatch.setattr(agent_cli, finder, _boom)

    result = run_agent(AgentRequest(prompt="do work", cwd=tmp_path, mode="write", harness=harness))

    assert result.ok is False
    assert result.error_kind == "unsupported_mode"  # never the misleading `not_installed`
    assert harness in result.text and "write" in result.text and "read-only" in result.text
    assert result.duration_s == 0.0


def test_run_agent_does_not_refuse_a_declared_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_cli, "find_claude_binary", lambda: None)

    result = run_agent(AgentRequest(prompt="x", cwd=tmp_path, mode="write", harness="claude"))

    assert result.error_kind == "not_installed"  # got past the capability gate to the runner


def test_supports_antigravity_write_only_in_interactive_operating_context() -> None:
    """USR-436: agent_cli.supports only declares write for Antigravity when operating interactively."""
    assert supports("antigravity", "write") is False
    assert supports("antigravity", "write", execution_context="headless", operating_harness="antigravity") is False
    assert supports("antigravity", "write", execution_context="interactive", operating_harness="antigravity") is True
    assert supports("antigravity", "write", execution_context="interactive", operating_harness="claude") is False
    assert supports("antigravity", "read") is True
