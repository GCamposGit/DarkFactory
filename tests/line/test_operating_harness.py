"""USR-109: the harness the owner operates the factory through develops first (quota above the floor).

Owner decision: operating through Grok Build with quota left means Grok develops. The router used to rank the
`development` cascade only by headroom, so the harness the owner was working in was ignored. These tests pin the
rule and its limits: only `development`, only above the 15% floor, only among candidates that already passed every
filter (host caps, declared write mode, cooldown, exclusions, known quota), and a harness outside the cascade
(Grok, Antigravity) can be elected by the preference alone, never by the headroom ranking.

Everything is injected: `quota_lookup`, an explicit `config` and a temporary cooldown store. Grok and Antigravity
declare write through `monkeypatch` on `HARNESS_CAPABILITIES`, so these tests do not depend on whether their
runners already implement it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pytest

from core.line import agent_cli
from core.line.operating_harness import (
    AUTODETECT_ENV_SIGNALS,
    KNOWN_HARNESSES,
    KNOWN_PROCESS_HARNESSES,
    OPERATING_HARNESS_ENV,
    detect_operating_harness,
    declare_headless_process,
    harness_executor_env,
    headless_executor_env,
    resolve_operating_harness,
    resolve_process_identity,
)
from core.line.routing import (
    _HARNESS_TO_PROVIDER,
    RoutingConfig,
    StageRoute,
    _default_routing_config,
    _resolve_cascade,
    default_config_path,
    earliest_route_available_at,
    load_routing_config,
    pick,
)

_ALL_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
_DEFAULT_DEVELOPMENT = [("claude", "sonnet"), ("codex", None)]

Lookup = Callable[[str], Optional[float]]


def _lookup(default: Optional[float] = 50.0, **by_harness: Optional[float]) -> Lookup:
    """Quota double keyed by harness name (`claude=40.0, grok=None`); other harnesses read `default`."""
    by_provider = {_HARNESS_TO_PROVIDER[harness]: value for harness, value in by_harness.items()}

    def lookup(provider_id: str) -> Optional[float]:
        return by_provider[provider_id] if provider_id in by_provider else default

    return lookup


def _config(development: Optional[list[tuple[str, Optional[str]]]] = None) -> RoutingConfig:
    return RoutingConfig(
        stages={
            "grill": StageRoute(cascade=[("antigravity", None), ("claude", "sonnet"), ("codex", None)]),
            "planning": StageRoute(cascade=[("claude", "opus"), ("codex", None), ("antigravity", None)]),
            "development": StageRoute(cascade=list(development or _DEFAULT_DEVELOPMENT)),
            "integration": StageRoute(cascade=[("claude", "sonnet"), ("codex", None)]),
            "review": StageRoute(cascade="other_family_than_development"),
        }
    )


def _pick(
    tmp_path: Path,
    lookup: Optional[Lookup],
    *,
    stage: str = "development",
    caps: Optional[list[str]] = None,
    config: Optional[RoutingConfig] = None,
    **kwargs,
):
    kwargs.setdefault("complexity", "high")  # USR-378: the operating harness is preferred for high tickets only
    if lookup is not None:
        kwargs["quota_lookup"] = lookup
    return pick(
        stage,
        _ALL_CAPS if caps is None else caps,
        config=config or _config(),
        cooldown_path=tmp_path / "cooldowns.json",
        **kwargs,
    )


@pytest.fixture
def grok_and_antigravity_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare write for the two harnesses outside the default cascade, whatever agent_cli currently says."""
    for harness in ("grok", "antigravity"):
        monkeypatch.setitem(agent_cli.HARNESS_CAPABILITIES, harness, frozenset({"read", "write"}))


def _cooldown(tmp_path: Path, harness: str, minutes: float = 30.0) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    (tmp_path / "cooldowns.json").write_text(
        json.dumps({harness: {"until": until.isoformat(), "reason": "rate_limited"}}), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# (a) Detection and precedence
# --------------------------------------------------------------------------


def test_no_signal_means_no_operating_harness() -> None:
    assert detect_operating_harness({}) is None
    assert detect_operating_harness({"PATH": "/usr/bin", "CLAUDE_CODE_SESSION_ID": "abc"}) is None
    assert resolve_operating_harness(None, {}) is None


@pytest.mark.parametrize(("variable", "harness"), AUTODETECT_ENV_SIGNALS)
def test_every_autodetect_signal_maps_to_its_harness(variable: str, harness: str) -> None:
    assert detect_operating_harness({variable: "1"}) == harness
    assert detect_operating_harness({variable: "  "}) is None  # an empty value is not a signal


def test_the_detection_table_only_names_known_harnesses_and_covers_all_four() -> None:
    assert {harness for _name, harness in AUTODETECT_ENV_SIGNALS} == set(KNOWN_HARNESSES)


def test_the_two_claude_code_variables_are_one_signal_not_an_ambiguity() -> None:
    assert detect_operating_harness({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}) == "claude"


def test_signals_for_more_than_one_harness_are_ambiguous_and_never_guessed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="core.line.operating_harness"):
        assert detect_operating_harness({"CLAUDECODE": "1", "GROK_AGENT": "1"}) is None
    assert any("ambiguous" in record.getMessage() for record in caplog.records)


def test_the_live_session_signal_beats_a_stale_variable() -> None:
    environ = {OPERATING_HARNESS_ENV: "  Antigravity ", "CLAUDECODE": "1"}

    assert detect_operating_harness(environ) == "claude"
    assert detect_operating_harness({OPERATING_HARNESS_ENV: "antigravity", "CODEX_THREAD_ID": "t"}) == "codex"
    assert detect_operating_harness({"ANTIGRAVITY_SESSION_ID": "s"}) == "antigravity"


def test_a_stale_variable_cannot_resolve_an_ambiguous_signal() -> None:
    environ = {OPERATING_HARNESS_ENV: "codex", "CLAUDECODE": "1", "GROK_AGENT": "1"}

    assert detect_operating_harness(environ) is None


def test_without_a_session_signal_the_old_variable_is_not_reused() -> None:
    assert detect_operating_harness({OPERATING_HARNESS_ENV: "codex"}) is None
    assert detect_operating_harness({OPERATING_HARNESS_ENV: "gemini"}) is None
    assert detect_operating_harness({OPERATING_HARNESS_ENV: "gemini", "CLAUDECODE": "1"}) == "claude"


def test_the_explicit_argument_beats_the_session_signal_and_the_variable() -> None:
    environ = {OPERATING_HARNESS_ENV: "grok", "CLAUDECODE": "1"}

    assert resolve_operating_harness("Codex", environ) == "codex"
    assert resolve_operating_harness("", environ) == "claude"  # empty argument falls through to the signal
    assert resolve_operating_harness(None, {"CLAUDECODE": "1"}) == "claude"
    assert resolve_operating_harness(None, {}) is None


def test_an_invalid_explicit_argument_prefers_nobody_instead_of_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.operating_harness"):
        assert resolve_operating_harness("gemini", {"CLAUDECODE": "1"}) is None
    assert any("gemini" in record.getMessage() for record in caplog.records)


def test_pick_ignores_a_persisted_variable_without_a_session_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=95.0, codex=40.0)
    assert _pick(tmp_path, lookup) == ("claude", "sonnet")  # no signal: highest headroom

    monkeypatch.setenv(OPERATING_HARNESS_ENV, "codex")

    assert _pick(tmp_path, lookup) == ("claude", "sonnet")


def test_pick_autodetects_the_parent_harness_from_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=40.0, codex=95.0)
    assert _pick(tmp_path, lookup) == ("codex", None)

    monkeypatch.setenv("CLAUDECODE", "1")

    assert _pick(tmp_path, lookup) == ("claude", "sonnet")


def test_pick_with_an_ambiguous_environment_keeps_the_headroom_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")

    assert _pick(tmp_path, _lookup(claude=40.0, codex=95.0)) == ("codex", None)
    assert _pick(tmp_path, _lookup(claude=95.0, codex=40.0)) == ("claude", "sonnet")


def test_pick_argument_beats_the_session_signal_which_beats_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=50.0, codex=50.0, grok=50.0)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _pick(tmp_path, lookup) == ("claude", "sonnet")

    monkeypatch.setenv(OPERATING_HARNESS_ENV, "codex")
    assert _pick(tmp_path, lookup) == ("claude", "sonnet")  # live signal over a stale variable

    assert _pick(tmp_path, lookup, operating_harness="claude") == ("claude", "sonnet")  # argument over both


def test_an_invalid_pick_argument_is_ignored_and_the_stage_keeps_the_headroom_rule(tmp_path: Path) -> None:
    assert _pick(tmp_path, _lookup(claude=40.0, codex=95.0), operating_harness="gemini") == ("codex", None)


# --------------------------------------------------------------------------
# (b) The operating harness beats a harness with more headroom
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operating", "expected"),
    [
        ("claude", ("claude", "sonnet")),
        ("codex", ("codex", None)),
        ("grok", ("grok", None)),  # not in the cascade: elected as an extra candidate
        ("antigravity", ("antigravity", None)),  # same, once write is declared for it
    ],
)
def test_an_eligible_operating_harness_beats_the_one_with_more_headroom(
    operating: str,
    expected: tuple[str, Optional[str]],
    tmp_path: Path,
    grok_and_antigravity_write: None,
) -> None:
    # Every other harness has strictly more headroom than the operating one.
    levels = {harness: 90.0 for harness in KNOWN_HARNESSES}
    levels[operating] = 20.0

    choice = _pick(tmp_path, _lookup(**levels), operating_harness=operating)

    assert choice == expected


@pytest.mark.parametrize(
    ("operating", "headroom_winner"),
    [
        ("claude", ("codex", None)),
        ("codex", ("claude", "sonnet")),
        ("grok", ("claude", "sonnet")),
        ("antigravity", ("claude", "sonnet")),
    ],
)
def test_without_the_preference_the_same_quotas_elect_the_headroom_winner(
    operating: str,
    headroom_winner: tuple[str, Optional[str]],
    tmp_path: Path,
    grok_and_antigravity_write: None,
) -> None:
    """Control for the test above: the preference, not the quotas, is what changes the result."""
    levels = {harness: 90.0 for harness in KNOWN_HARNESSES}
    levels[operating] = 20.0

    assert _pick(tmp_path, _lookup(**levels)) == headroom_winner


def test_the_preferred_harness_uses_the_model_the_cascade_gives_it(tmp_path: Path) -> None:
    config = _config([("claude", "haiku"), ("codex", "luna")])

    assert _pick(tmp_path, _lookup(claude=30.0, codex=80.0), config=config, operating_harness="claude") == (
        "claude",
        "haiku",
    )
    assert _pick(tmp_path, _lookup(claude=80.0, codex=30.0), config=config, operating_harness="codex") == (
        "codex",
        "luna",
    )


def test_a_harness_listed_twice_keeps_the_ranking_among_its_own_entries(tmp_path: Path) -> None:
    config = _config([("claude", "sonnet"), ("claude", "opus"), ("codex", None)])
    lookup = _lookup(claude=40.0, codex=95.0)

    assert _pick(tmp_path, lookup, config=config, operating_harness="claude", complexity="medium") == (
        "claude", "sonnet",
    )
    assert _pick(tmp_path, lookup, config=config, operating_harness="claude", complexity="high") == (
        "claude",
        "opus",
    )


def test_the_election_is_logged_at_info_level(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="core.line.routing"):
        _pick(tmp_path, _lookup(claude=95.0, codex=42.0), operating_harness="codex")

    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("operating_harness_preferred harness=codex remaining=42.0") for m in messages)


def test_nothing_is_logged_as_preferred_when_the_headroom_rule_decides(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="core.line.routing"):
        _pick(tmp_path, _lookup(claude=95.0, codex=42.0))

    assert not any("operating_harness_preferred" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------
# (c) ... and beats the complexity preference
# --------------------------------------------------------------------------


def test_the_operating_harness_beats_the_high_complexity_opus_preference(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    config = _config([("claude", "opus"), ("claude", "sonnet"), ("codex", None)])
    lookup = _lookup(claude=90.0, codex=30.0, grok=25.0, antigravity=25.0)

    assert _pick(tmp_path, lookup, config=config, complexity="high") == ("claude", "opus")  # control
    assert _pick(tmp_path, lookup, config=config, complexity="high", operating_harness="codex") == ("codex", None)
    assert _pick(tmp_path, lookup, config=config, complexity="high", operating_harness="grok") == ("grok", None)
    # Complexity no longer gates the interactive operator preference.
    assert _pick(tmp_path, lookup, config=config, complexity="critical", operating_harness="antigravity") == (
        "antigravity",
        None,
    )


def test_the_operating_harness_does_not_beat_the_low_complexity_luna_preference(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    config = _config([("claude", "sonnet"), ("codex", "luna")])
    lookup = _lookup(claude=30.0, codex=90.0, grok=25.0)

    assert _pick(tmp_path, lookup, config=config, complexity="low") == ("codex", "luna")  # control
    assert _pick(tmp_path, lookup, config=config, complexity="low", operating_harness="claude") == (
        "claude", "sonnet",
    )
    assert _pick(tmp_path, lookup, config=config, complexity="low", operating_harness="grok") == ("grok", None)


# --------------------------------------------------------------------------
# (d) An operating harness that did not survive the filters changes nothing
# --------------------------------------------------------------------------

# For a cascade member (claude) and for an extra candidate (grok): the rule that applies when it is out.


@pytest.mark.parametrize("quota", [15.0, 14.9, 3.0, 0.0], ids=lambda q: f"quota-{q}")
def test_a_critical_or_unknown_operating_harness_falls_back_to_headroom(
    quota: Optional[float], tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=quota, codex=60.0, grok=quota, antigravity=70.0)

    assert _pick(tmp_path, lookup, operating_harness="claude") == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness="grok") == ("codex", None)


def test_unknown_operating_quota_is_allowed_with_structured_warning(
    tmp_path: Path, grok_and_antigravity_write: None, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.routing"):
        assert _pick(tmp_path, _lookup(claude=None, codex=90.0), operating_harness="claude") == (
            "claude", "sonnet",
        )
    assert any(
        "operating_harness_quota_degraded harness=claude state=unknown reason=unknown" in record.getMessage()
        for record in caplog.records
    )


def test_stale_operating_quota_is_allowed_with_stale_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, grok_and_antigravity_write: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from core.line import routing

    monkeypatch.setattr(routing, "_quota_headroom_diagnosis", lambda provider: (None, "stale_snapshot"))
    monkeypatch.setattr(routing, "_default_weekly_slack", lambda _provider: None)
    with caplog.at_level(logging.WARNING, logger="core.line.routing"):
        assert _pick(tmp_path, None, operating_harness="antigravity") == ("antigravity", None)
    assert any(
        "operating_harness_quota_degraded harness=antigravity state=unknown reason=stale_snapshot" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("signal", ["explicit", "autodetect"])
def test_antigravity_operator_is_preferred_interactively_but_excluded_headless(
    signal: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    levels = _lookup(claude=90.0, codex=80.0, grok=70.0, antigravity=30.0)
    monkeypatch.delenv(OPERATING_HARNESS_ENV, raising=False)
    monkeypatch.delenv("ANTIGRAVITY_AGENT", raising=False)
    if signal == "explicit":
        monkeypatch.setenv("ANTIGRAVITY_SESSION_ID", "session-1")
    else:
        monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")

    assert _pick(tmp_path, levels) == ("antigravity", None)
    assert _pick(tmp_path, levels, execution_context="headless") == ("claude", "sonnet")


def test_a_cooling_operating_harness_falls_back_to_headroom(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=95.0, codex=60.0, grok=99.0)

    _cooldown(tmp_path, "claude")
    assert _pick(tmp_path, lookup, operating_harness="claude") == ("codex", None)

    _cooldown(tmp_path, "grok")
    assert _pick(tmp_path, lookup, operating_harness="grok") == ("claude", "sonnet")


def test_an_excluded_operating_harness_falls_back_to_headroom(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=40.0, codex=95.0, grok=99.0)

    # A retry after a failure excludes the route that just failed: the preference must not bring it back.
    assert _pick(tmp_path, lookup, operating_harness="claude", exclude=[("claude", "sonnet")]) == (
        "codex", None,
    )
    assert _pick(tmp_path, lookup, operating_harness="claude", exclude=["claude"]) == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness="grok", exclude=[("grok", None)]) == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness="grok", exclude=["grok"]) == ("codex", None)


def test_an_operating_harness_missing_from_host_caps_falls_back_to_headroom(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=40.0, codex=95.0, grok=99.0)
    caps_without_claude = ["harness:codex", "harness:grok"]
    caps_without_grok = ["harness:claude", "harness:codex"]

    assert _pick(tmp_path, lookup, caps=caps_without_claude, operating_harness="claude") == ("codex", None)
    assert _pick(tmp_path, lookup, caps=caps_without_grok, operating_harness="grok") == ("codex", None)


@pytest.mark.parametrize("operating", ["grok", "antigravity"])
def test_an_operating_harness_that_does_not_declare_write_falls_back_to_headroom(
    operating: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(agent_cli.HARNESS_CAPABILITIES, operating, frozenset({"read"}))
    lookup = _lookup(claude=40.0, codex=95.0, grok=99.0, antigravity=99.0)

    exec_ctx = "headless" if operating == "antigravity" else "interactive"
    assert _pick(tmp_path, lookup, operating_harness=operating, execution_context=exec_ctx) == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness=operating, mode="write", execution_context=exec_ctx) == ("codex", None)


def test_an_operating_harness_with_a_forbidden_model_falls_back_to_headroom(tmp_path: Path) -> None:
    config = _config([("claude", "fable"), ("codex", None)])

    assert _pick(tmp_path, _lookup(claude=95.0, codex=40.0), config=config, operating_harness="claude") == (
        "codex",
        None,
    )


def test_the_unknown_operating_harness_is_not_elected_by_the_unknown_quota_bootstrap(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    # Interactive operator unknown quota is its own policy, independent of the generic bootstrap.
    lookup = _lookup(claude=5.0, codex=5.0, grok=None)

    choice = _pick(tmp_path, lookup, operating_harness="grok", unknown_quota_last_resort_ok=True)

    assert choice == ("grok", None)


# --------------------------------------------------------------------------
# (e) The 15% floor is never relaxed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operating", KNOWN_HARNESSES)
def test_a_critical_operating_harness_is_never_elected_even_when_it_is_the_only_one(
    operating: str, tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(**{operating: 15.0})

    assert _pick(tmp_path, lookup, caps=[f"harness:{operating}"], operating_harness=operating) is None
    assert _pick(tmp_path, _lookup(**{operating: 15.1}), caps=[f"harness:{operating}"], operating_harness=operating) == (
        operating,
        "sonnet" if operating == "claude" else None,
    )


def test_a_custom_critical_threshold_is_the_floor_the_preference_respects(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    config = _config()
    config.pressure_thresholds["critical"] = 30.0

    assert _pick(tmp_path, _lookup(claude=95.0, codex=29.0), config=config, operating_harness="codex") == (
        "claude",
        "sonnet",
    )
    assert _pick(tmp_path, _lookup(claude=95.0, codex=31.0), config=config, operating_harness="codex") == (
        "codex",
        None,
    )


def test_everything_critical_returns_none_with_an_operating_harness_set(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(default=10.0)

    for operating in KNOWN_HARNESSES:
        assert _pick(tmp_path, lookup, operating_harness=operating, openrouter_balance_lookup=lambda: 50.0) is None


# --------------------------------------------------------------------------
# (f) Only the development stage
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operating", KNOWN_HARNESSES)
@pytest.mark.parametrize("stage", ["planning", "grill", "review", "integration"])
@pytest.mark.parametrize(
    "levels",
    [
        {"claude": 90.0, "codex": 30.0, "grok": 20.0, "antigravity": 40.0},
        {"claude": 30.0, "codex": 90.0, "grok": 99.0, "antigravity": 95.0},
        {"claude": 20.0, "codex": 20.0, "grok": 20.0, "antigravity": 20.0},
    ],
    ids=["claude-high", "codex-high", "all-low"],
)
def test_other_stages_ignore_the_operating_harness(
    stage: str, operating: str, levels: dict[str, float], tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(**levels)
    implementing = "claude" if stage == "review" else None

    without = _pick(tmp_path, lookup, stage=stage, implementing_harness=implementing)
    with_operating = _pick(
        tmp_path, lookup, stage=stage, implementing_harness=implementing, operating_harness=operating
    )

    assert with_operating == without


def test_a_harness_outside_a_non_development_cascade_is_not_added_to_it(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=30.0, codex=30.0, grok=100.0)

    for stage in ("planning", "grill", "integration"):
        choice = _pick(tmp_path, lookup, stage=stage, operating_harness="grok")
        assert choice is not None and choice[0] != "grok", stage


def test_the_session_signal_does_not_change_other_stages_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=90.0, codex=30.0)
    baseline = {stage: _pick(tmp_path, lookup, stage=stage) for stage in ("planning", "grill", "integration")}

    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")

    assert {stage: _pick(tmp_path, lookup, stage=stage) for stage in baseline} == baseline
    assert _pick(tmp_path, lookup) == ("codex", None)  # while development does follow it


# --------------------------------------------------------------------------
# (g) review never returns the implementer's own family
# --------------------------------------------------------------------------


@pytest.mark.parametrize("implementer", KNOWN_HARNESSES)
def test_the_review_cascade_never_contains_the_implementing_harness(implementer: str) -> None:
    review = StageRoute(cascade="other_family_than_development")

    cascade = _resolve_cascade(review, implementer)

    assert implementer not in [harness for harness, _model in cascade]
    assert {harness for harness, _model in cascade} == set(KNOWN_HARNESSES) - {implementer}


def test_grok_and_antigravity_reviewers_are_ordered_claude_then_codex() -> None:
    review = StageRoute(cascade="other_family_than_development")

    assert _resolve_cascade(review, "grok") == [("claude", None), ("codex", None), ("antigravity", None)]
    assert _resolve_cascade(review, "antigravity") == [("claude", None), ("codex", None), ("grok", None)]
    assert _resolve_cascade(review, "claude") == [("codex", None), ("grok", None), ("antigravity", None)]
    assert _resolve_cascade(review, "codex") == [("claude", None), ("grok", None), ("antigravity", None)]


@pytest.mark.parametrize("implementer", ["grok", "antigravity"])
def test_review_never_picks_the_grok_or_antigravity_implementer_even_with_the_most_headroom(
    implementer: str, tmp_path: Path
) -> None:
    lookup = _lookup(claude=20.0, codex=20.0, grok=100.0, antigravity=100.0)

    choice = _pick(tmp_path, lookup, stage="review", implementing_harness=implementer)

    assert choice is not None and choice[0] != implementer


# --------------------------------------------------------------------------
# (h) No signal: the router behaves as before
# --------------------------------------------------------------------------


def test_without_any_signal_the_development_choice_is_the_headroom_winner(tmp_path: Path) -> None:
    assert _pick(tmp_path, _lookup(claude=80.0, codex=60.0)) == ("claude", "sonnet")
    assert _pick(tmp_path, _lookup(claude=60.0, codex=80.0)) == ("codex", None)
    assert _pick(tmp_path, _lookup(claude=60.0, codex=60.0)) == ("claude", "sonnet")  # tie: cascade order
    assert _pick(tmp_path, _lookup(claude=60.0, codex=80.0), caps=["harness:claude"]) == (
        "claude", "sonnet",
    )


def test_without_any_signal_grok_and_antigravity_are_never_development_candidates(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=20.0, codex=20.0, grok=100.0, antigravity=100.0)

    assert _pick(tmp_path, lookup) == ("claude", "sonnet")


def test_the_real_development_cascade_includes_grok_in_both_configs() -> None:
    """USR-378: Grok is a cascade member, ranked by weekly slack like Claude and Codex."""
    for config in (load_routing_config(default_config_path()), _default_routing_config()):
        assert [harness for harness, _model in config.stages["development"].cascade] == ["claude", "codex", "grok"]


def test_pick_keeps_every_existing_parameter_and_a_keyword_only_operating_harness() -> None:
    import inspect

    parameters = inspect.signature(pick).parameters

    assert parameters["operating_harness"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["operating_harness"].default is None
    assert list(parameters)[:3] == ["stage", "host_caps", "exclude"]


# --------------------------------------------------------------------------
# When can a route come back: the operating harness counts for development
# --------------------------------------------------------------------------


def test_earliest_route_available_at_counts_the_operating_harness_outside_the_cascade(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    cooldowns = {
        "claude": {"until": (now + timedelta(minutes=50)).isoformat()},
        "codex": {"until": (now + timedelta(minutes=45)).isoformat()},
        "grok": {"until": (now + timedelta(minutes=10)).isoformat()},
    }
    (tmp_path / "cooldowns.json").write_text(json.dumps(cooldowns), encoding="utf-8")

    def when(**kwargs) -> datetime:
        return earliest_route_available_at(
            "development", _ALL_CAPS, _config(), now=now, cooldown_path=tmp_path / "cooldowns.json",
            reset_lookup=lambda _provider: None, **kwargs,
        )

    assert when() == now + timedelta(minutes=45)  # the cascade alone
    assert when(operating_harness="grok") == now + timedelta(minutes=10)  # Grok would be elected then
    assert when(operating_harness="claude") == now + timedelta(minutes=45)  # in the cascade: nothing extra
    assert (
        earliest_route_available_at(
            "planning", _ALL_CAPS, _config(), now=now, cooldown_path=tmp_path / "cooldowns.json",
            reset_lookup=lambda _provider: None, operating_harness="grok",
        )
        == now + timedelta(minutes=45)  # other stages never look at it
    )


# --------------------------------------------------------------------------
# (i) resolve_process_identity (USR-471 T1): one resolver for every executor
# --------------------------------------------------------------------------


def test_headless_process_never_inherits_the_parent_chats_preference() -> None:
    environ = {"DARKFAC_OPERATING_HARNESS": "antigravity", "CLAUDECODE": "1"}

    identity = resolve_process_identity(environ=environ, execution_context="headless")

    assert identity.harness is None
    assert identity.origin == "headless"
    assert identity.process_harness is None
    assert identity.process_harness_origin == "headless"
    assert identity.execution_context == "headless"
    assert identity.mode == "write"
    assert identity.write_capable is True


def test_headless_process_honors_its_own_explicit_declaration() -> None:
    identity = resolve_process_identity(
        environ={"DARKFAC_OPERATING_HARNESS": "grok"}, execution_context="headless", explicit="codex",
    )

    assert identity.harness == "codex"
    assert identity.origin == "explicit"


def test_headless_process_with_an_invalid_explicit_declaration_falls_back_to_headless(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.operating_harness"):
        identity = resolve_process_identity(environ={}, execution_context="headless", explicit="gemini")

    assert identity.harness is None
    assert identity.origin == "headless"
    assert any("gemini" in record.getMessage() for record in caplog.records)


def test_headless_antigravity_declared_explicitly_is_read_only() -> None:
    identity = resolve_process_identity(environ={}, execution_context="headless", explicit="antigravity")

    assert identity.harness == "antigravity"
    assert identity.mode == "read"


def test_headless_process_config_cannot_make_antigravity_a_writer() -> None:
    identity = resolve_process_identity(environ={
        "DARKFAC_EXECUTION_CONTEXT": "headless",
        "DARKFAC_PROCESS_HARNESS": "antigravity",
        "DARKFAC_WRITE_CAPABLE": "1",
    })

    assert identity.process_harness == "antigravity"
    assert identity.mode == "read"
    assert identity.write_capable is False


def test_interactive_antigravity_is_write() -> None:
    identity = resolve_process_identity(environ={}, execution_context="interactive", explicit="antigravity")

    assert identity.harness == "antigravity"
    assert identity.mode == "write"


@pytest.mark.parametrize(
    ("environ", "explicit", "harness", "origin"),
    [
        ({}, None, None, "none"),
        ({"CLAUDECODE": "1"}, None, "claude", "autodetect"),
        ({"DARKFAC_OPERATING_HARNESS": "codex"}, None, None, "none"),
        ({"DARKFAC_OPERATING_HARNESS": "codex", "CLAUDECODE": "1"}, None, "claude", "autodetect"),
        ({"DARKFAC_OPERATING_HARNESS": "codex", "CLAUDECODE": "1"}, "grok", "grok", "explicit"),
    ],
)
def test_interactive_process_precedence_is_argument_then_live_signal(
    environ: dict[str, str], explicit: Optional[str], harness: Optional[str], origin: str
) -> None:
    identity = resolve_process_identity(environ=environ, execution_context="interactive", explicit=explicit)

    assert identity.harness == harness
    assert identity.origin == origin


def test_process_identity_is_separate_from_operating_preference() -> None:
    identity = resolve_process_identity(
        environ={
            "DARKFAC_EXECUTION_CONTEXT": "interactive",
            "ANTIGRAVITY_SESSION_ID": "session-1",
            "DARKFAC_PROCESS_HARNESS": "codex",
        },
    )

    assert identity.harness == "antigravity"
    assert identity.process_harness == "codex"
    assert identity.process_harness_origin == "process_config"


def test_invalid_process_harness_and_conflicting_roots_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid DARKFAC_PROCESS_HARNESS"):
        resolve_process_identity(environ={"DARKFAC_PROCESS_HARNESS": "antigravity-ish"})

    with pytest.raises(ValueError, match="conflicting DARKFAC_PROJECT_ROOT and PROJECT_ROOT"):
        resolve_process_identity(environ={
            "DARKFAC_PROJECT_ROOT": str(tmp_path / "one"),
            "PROJECT_ROOT": str(tmp_path / "two"),
            "DARKFAC_STATE_ROOT": str(tmp_path / "state"),
        })

    with pytest.raises(ValueError, match="project root must be an absolute path"):
        resolve_process_identity(environ={
            "PROJECT_ROOT": "relative-project",
            "STATE_ROOT": str(tmp_path / "state"),
        })


def test_invalid_stale_variable_never_overrides_the_live_session_signal() -> None:
    identity = resolve_process_identity(
        environ={"DARKFAC_OPERATING_HARNESS": "antigravity-ish", "CODEX_THREAD_ID": "session"},
        execution_context="interactive",
    )

    assert identity.harness == "codex"
    assert identity.origin == "autodetect"


def test_execution_context_defaults_from_fleet_mode_env() -> None:
    assert resolve_process_identity(environ={}).execution_context == "interactive"
    assert resolve_process_identity(environ={"DARKFAC_FLEET_MODE": "1"}).execution_context == "headless"
    assert resolve_process_identity(environ={"DARKFAC_FLEET_MODE": "0"}).execution_context == "interactive"


def test_declared_execution_context_is_authoritative_and_invalid_or_conflicting_context_fails_closed() -> None:
    identity = resolve_process_identity(environ={"DARKFAC_EXECUTION_CONTEXT": "headless"})
    assert identity.execution_context == "headless"
    assert identity.harness is None

    with pytest.raises(ValueError, match="invalid DARKFAC_EXECUTION_CONTEXT"):
        resolve_process_identity(environ={"DARKFAC_EXECUTION_CONTEXT": "maybe"})
    with pytest.raises(ValueError, match="execution context mismatch"):
        resolve_process_identity(
            environ={"DARKFAC_EXECUTION_CONTEXT": "interactive"}, execution_context="headless",
        )


def test_invalid_write_capability_fails_closed() -> None:
    with pytest.raises(ValueError, match="invalid DARKFAC_WRITE_CAPABLE"):
        resolve_process_identity(
            environ={"DARKFAC_EXECUTION_CONTEXT": "headless", "DARKFAC_WRITE_CAPABLE": "sometimes"},
        )


def test_declaring_headless_process_replaces_inherited_interactive_contract(tmp_path: Path) -> None:
    env = {
        "DARKFAC_OPERATING_HARNESS": "antigravity",
        "ANTIGRAVITY_AGENT": "1",
        "DARKFAC_EXECUTION_CONTEXT": "interactive",
        "DARKFAC_EXECUTOR_MODE": "read",
        "DARKFAC_WRITE_CAPABLE": "0",
        "DARKFAC_FLEET_MODE": "0",
        "DARKFAC_PROCESS_HARNESS": "antigravity",
    }

    declare_headless_process(env, project_root=tmp_path, state_root=tmp_path / "state")

    identity = resolve_process_identity(environ=env)
    assert identity.harness is None
    assert identity.execution_context == "headless"
    assert identity.project_root == tmp_path.resolve()
    assert identity.state_root == (tmp_path / "state").resolve()
    assert identity.write_capable is True
    assert identity.process_harness is None
    assert identity.process_harness_origin == "declared_none"
    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    assert "ANTIGRAVITY_AGENT" not in env
    assert env["PROJECT_ROOT"] == str(tmp_path.resolve())
    assert env["STATE_ROOT"] == str((tmp_path / "state").resolve())
    assert env["DARKFAC_PROCESS_HARNESS"] == "none"


def test_harness_child_environment_sets_process_identity_and_explicit_roots(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    shared_state = tmp_path / "state"
    parent = {
        "DARKFAC_EXECUTION_CONTEXT": "headless",
        "DARKFAC_FLEET_MODE": "1",
        "DARKFAC_OPERATING_HARNESS": "antigravity",
        "ANTIGRAVITY_AGENT": "1",
        "PROJECT_ROOT": str(tmp_path / "stale-project"),
        "STATE_ROOT": str(tmp_path / "stale-state"),
        "PATH": "preserved",
    }

    env = harness_executor_env(
        parent,
        process_harness="codex",
        project_root=checkout,
        state_root=shared_state,
        execution_context="headless",
    )

    identity = resolve_process_identity(environ=env)
    assert identity.process_harness == "codex"
    assert identity.process_harness_origin == "process_config"
    assert identity.execution_context == "headless"
    assert identity.project_root == checkout.resolve()
    assert identity.state_root == shared_state.resolve()
    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    assert "ANTIGRAVITY_AGENT" not in env
    assert env["PROJECT_ROOT"] == str(checkout.resolve())
    assert env["STATE_ROOT"] == str(shared_state.resolve())
    assert env["DARKFAC_FLEET_MODE"] == "1"


@pytest.mark.parametrize("process_harness", KNOWN_PROCESS_HARNESSES)
def test_each_headless_harness_gets_the_same_explicit_process_contract(
    process_harness: str, tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    shared_state = tmp_path / "state"
    env = harness_executor_env(
        {
            "DARKFAC_EXECUTION_CONTEXT": "interactive",
            "DARKFAC_OPERATING_HARNESS": "antigravity",
            "ANTIGRAVITY_AGENT": "interactive-parent",
            "PROJECT_ROOT": str(tmp_path / "stale-checkout"),
            "STATE_ROOT": str(tmp_path / "stale-state"),
        },
        process_harness=process_harness,
        project_root=checkout,
        state_root=shared_state,
        execution_context="headless",
    )

    identity = resolve_process_identity(environ=env)
    assert identity.execution_context == "headless"
    assert identity.harness is None
    assert identity.process_harness == process_harness
    assert identity.project_root == checkout.resolve()
    assert identity.state_root == shared_state.resolve()
    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    assert env["PROJECT_ROOT"] == str(checkout.resolve())
    assert env["STATE_ROOT"] == str(shared_state.resolve())
    assert "ANTIGRAVITY_AGENT" not in env
    if process_harness == "antigravity":
        assert identity.mode == "read"
        assert identity.write_capable is False
    else:
        assert identity.mode == "write"
        assert identity.write_capable is True


def test_interactive_harness_child_preserves_preference_but_sets_process_identity(tmp_path: Path) -> None:
    env = harness_executor_env(
        {"ANTIGRAVITY_SESSION_ID": "chat-1", "DARKFAC_FLEET_MODE": "1"},
        process_harness="codex",
        project_root=tmp_path,
        state_root=tmp_path / ".factory",
        execution_context="interactive",
    )

    identity = resolve_process_identity(environ=env)
    assert identity.harness == "antigravity"
    assert identity.process_harness == "codex"
    assert identity.execution_context == "interactive"
    assert env["ANTIGRAVITY_SESSION_ID"] == "chat-1"
    assert "DARKFAC_FLEET_MODE" not in env


def test_agent_capability_check_uses_process_identity_not_parent_chat_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.line.agent_cli import supports

    monkeypatch.setenv("ANTIGRAVITY_SESSION_ID", "chat-1")
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "1")
    assert not supports("antigravity", "write")

    monkeypatch.delenv("DARKFAC_FLEET_MODE")
    assert supports("antigravity", "write")


def test_roots_default_from_the_environment_and_can_be_overridden(tmp_path: Path) -> None:
    identity = resolve_process_identity(
        environ={"DARKFAC_PROJECT_ROOT": str(tmp_path), "DARKFAC_STATE_ROOT": str(tmp_path / "state")},
    )

    assert identity.project_root == tmp_path.resolve()
    assert identity.state_root == (tmp_path / "state").resolve()

    overridden = resolve_process_identity(
        environ={}, project_root=tmp_path, state_root=tmp_path / "other-state",
    )

    assert overridden.project_root == tmp_path.resolve()
    assert overridden.state_root == (tmp_path / "other-state").resolve()

    generic_roots = resolve_process_identity(
        environ={"PROJECT_ROOT": str(tmp_path), "STATE_ROOT": str(tmp_path / "generic-state")},
    )
    assert generic_roots.project_root == tmp_path.resolve()
    assert generic_roots.state_root == (tmp_path / "generic-state").resolve()


def test_roots_default_to_project_root_slash_factory_without_any_signal(tmp_path: Path) -> None:
    identity = resolve_process_identity(environ={}, project_root=tmp_path)

    assert identity.state_root == tmp_path / ".factory"


def test_headless_executor_env_scrubs_parent_and_update_identity_and_foreign_run_markers(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "checkout"
    state_root = tmp_path / "shared-state"
    base = {
        "DARKFAC_OPERATING_HARNESS": "antigravity",
        "CODEX_THREAD_ID": "parent-chat",
        "DARKFAC_TICKET_ID": "USR-OTHER",
        "DARKFAC_RUN_ID": "foreign-run",
        "DARKFAC_PROJECT_ROOT": "foreign-checkout",
        "DARKFAC_STATE_ROOT": "foreign-state",
        "DARKFAC_FLEET_MODE": "0",
        "PATH": "preserved",
    }
    updates = {
        "DARKFAC_OPERATING_HARNESS": "grok",
        "ANTIGRAVITY_SESSION": "parent-session",
        "DARKFAC_EXECUTION_ID": "foreign-execution",
        "DARKFAC_PROJECT_ROOT": "wrong-checkout",
        "DARKFAC_STATE_ROOT": "wrong-state",
        "DARKFAC_FLEET_MODE": "0",
        "DARKFAC_TEST_WORKERS": "http://worker.example",
        "DARKFAC_CANDIDATE_DIGEST": "candidate-sha",
    }

    env = headless_executor_env(
        base,
        project_root=project_root,
        state_root=state_root,
        host_id="desktop",
        run_id="owned-run",
        updates=updates,
    )

    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    assert not any(name in env for name in (
        "CODEX_THREAD_ID", "ANTIGRAVITY_SESSION", "DARKFAC_TICKET_ID",
        "DARKFAC_EXECUTION_ID",
    ))
    assert env["DARKFAC_PROJECT_ROOT"] == str(project_root.resolve())
    assert env["DARKFAC_STATE_ROOT"] == str(state_root.resolve())
    assert env["PROJECT_ROOT"] == str(project_root.resolve())
    assert env["STATE_ROOT"] == str(state_root.resolve())
    assert env["DARKFAC_FLEET_MODE"] == "1"
    assert env["DARKFAC_EXECUTION_CONTEXT"] == "headless"
    assert env["DARKFAC_EXECUTOR_MODE"] == "write"
    assert env["DARKFAC_WRITE_CAPABLE"] == "1"
    assert env["DARKFAC_PROCESS_HARNESS"] == "none"
    assert env["DARKFAC_HOST_ID"] == "desktop"
    assert env["DARKFAC_RUN_ID"] == "owned-run"
    assert env["DARKFAC_TEST_WORKERS"] == "http://worker.example"
    assert env["DARKFAC_CANDIDATE_DIGEST"] == "candidate-sha"
    assert env["PATH"] == "preserved"


def test_non_fleet_executor_does_not_retain_fleet_or_host_identity_from_updates(tmp_path: Path) -> None:
    env = headless_executor_env(
        {"DARKFAC_FLEET_MODE": "1", "DARKFAC_HOST_ID": "foreign-host"},
        project_root=tmp_path,
        state_root=tmp_path / "state",
        fleet=False,
        updates={"DARKFAC_FLEET_MODE": "1", "DARKFAC_HOST_ID": "foreign-host"},
    )

    assert "DARKFAC_FLEET_MODE" not in env
    assert env["DARKFAC_EXECUTION_CONTEXT"] == "headless"
    assert env["DARKFAC_EXECUTOR_MODE"] == "write"
    assert env["DARKFAC_WRITE_CAPABLE"] == "1"
    assert "DARKFAC_HOST_ID" not in env
