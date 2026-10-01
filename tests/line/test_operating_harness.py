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
    OPERATING_HARNESS_ENV,
    detect_operating_harness,
    resolve_operating_harness,
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
    lookup: Lookup,
    *,
    stage: str = "development",
    caps: Optional[list[str]] = None,
    config: Optional[RoutingConfig] = None,
    **kwargs,
):
    return pick(
        stage,
        _ALL_CAPS if caps is None else caps,
        config=config or _config(),
        quota_lookup=lookup,
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


def test_the_explicit_variable_beats_autodetection_and_is_normalized() -> None:
    environ = {OPERATING_HARNESS_ENV: "  Grok ", "CLAUDECODE": "1"}

    assert detect_operating_harness(environ) == "grok"


def test_the_explicit_variable_resolves_what_autodetection_finds_ambiguous() -> None:
    environ = {OPERATING_HARNESS_ENV: "codex", "CLAUDECODE": "1", "GROK_AGENT": "1"}

    assert detect_operating_harness(environ) == "codex"


def test_an_invalid_variable_is_ignored_with_a_warning_and_autodetection_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.operating_harness"):
        assert detect_operating_harness({OPERATING_HARNESS_ENV: "gemini", "CLAUDECODE": "1"}) == "claude"
        assert detect_operating_harness({OPERATING_HARNESS_ENV: "gemini"}) is None
    assert sum("gemini" in record.getMessage() for record in caplog.records) == 2


def test_the_explicit_argument_beats_the_variable_and_autodetection() -> None:
    environ = {OPERATING_HARNESS_ENV: "grok", "CLAUDECODE": "1"}

    assert resolve_operating_harness("Codex", environ) == "codex"
    assert resolve_operating_harness("", environ) == "grok"  # empty argument falls through to the variable
    assert resolve_operating_harness(None, {"CLAUDECODE": "1"}) == "claude"
    assert resolve_operating_harness(None, {}) is None


def test_an_invalid_explicit_argument_prefers_nobody_instead_of_falling_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.operating_harness"):
        assert resolve_operating_harness("gemini", {"CLAUDECODE": "1"}) is None
    assert any("gemini" in record.getMessage() for record in caplog.records)


def test_pick_reads_the_variable_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lookup = _lookup(claude=95.0, codex=40.0)
    assert _pick(tmp_path, lookup) == ("claude", "sonnet")  # no signal: highest headroom

    monkeypatch.setenv(OPERATING_HARNESS_ENV, "codex")

    assert _pick(tmp_path, lookup) == ("codex", None)


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


def test_pick_argument_beats_the_variable_which_beats_autodetection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=50.0, codex=50.0, grok=50.0)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert _pick(tmp_path, lookup) == ("claude", "sonnet")

    monkeypatch.setenv(OPERATING_HARNESS_ENV, "codex")
    assert _pick(tmp_path, lookup) == ("codex", None)  # variable over autodetection

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
    config = _config([("claude", "haiku"), ("codex", "gpt-x")])

    assert _pick(tmp_path, _lookup(claude=30.0, codex=80.0), config=config, operating_harness="claude") == (
        "claude",
        "haiku",
    )
    assert _pick(tmp_path, _lookup(claude=80.0, codex=30.0), config=config, operating_harness="codex") == (
        "codex",
        "gpt-x",
    )


def test_a_harness_listed_twice_keeps_the_ranking_among_its_own_entries(tmp_path: Path) -> None:
    config = _config([("claude", "sonnet"), ("claude", "opus"), ("codex", None)])
    lookup = _lookup(claude=40.0, codex=95.0)

    assert _pick(tmp_path, lookup, config=config, operating_harness="claude") == ("claude", "sonnet")
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
    assert _pick(tmp_path, lookup, config=config, complexity="critical", operating_harness="antigravity") == (
        "antigravity",
        None,
    )


def test_the_operating_harness_beats_the_low_complexity_luna_preference(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    config = _config([("claude", "sonnet"), ("codex", "luna")])
    lookup = _lookup(claude=30.0, codex=90.0, grok=25.0)

    assert _pick(tmp_path, lookup, config=config, complexity="low") == ("codex", "luna")  # control
    assert _pick(tmp_path, lookup, config=config, complexity="low", operating_harness="claude") == (
        "claude",
        "sonnet",
    )
    assert _pick(tmp_path, lookup, config=config, complexity="low", operating_harness="grok") == ("grok", None)


# --------------------------------------------------------------------------
# (d) An operating harness that did not survive the filters changes nothing
# --------------------------------------------------------------------------

# For a cascade member (claude) and for an extra candidate (grok): the rule that applies when it is out.


@pytest.mark.parametrize("quota", [15.0, 14.9, 3.0, 0.0, None], ids=lambda q: f"quota-{q}")
def test_a_critical_or_unknown_operating_harness_falls_back_to_headroom(
    quota: Optional[float], tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=quota, codex=60.0, grok=quota, antigravity=70.0)

    assert _pick(tmp_path, lookup, operating_harness="claude") == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness="grok") == ("codex", None)


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
    assert _pick(tmp_path, lookup, operating_harness="claude", exclude=[("claude", "sonnet")]) == ("codex", None)
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

    assert _pick(tmp_path, lookup, operating_harness=operating) == ("codex", None)
    assert _pick(tmp_path, lookup, operating_harness=operating, mode="write") == ("codex", None)


def test_an_operating_harness_with_a_forbidden_model_falls_back_to_headroom(tmp_path: Path) -> None:
    config = _config([("claude", "fable"), ("codex", None)])

    assert _pick(tmp_path, _lookup(claude=95.0, codex=40.0), config=config, operating_harness="claude") == (
        "codex",
        None,
    )


def test_the_unknown_operating_harness_is_not_elected_by_the_unknown_quota_bootstrap(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    # Cascade members all critical, the extra operating harness unknown: last_resort may only bootstrap
    # cascade members, never the extra candidate.
    lookup = _lookup(claude=5.0, codex=5.0, grok=None)

    choice = _pick(tmp_path, lookup, operating_harness="grok", unknown_quota_last_resort_ok=True)

    assert choice is None


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


def test_the_environment_variable_does_not_change_other_stages_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = _lookup(claude=90.0, codex=30.0)
    baseline = {stage: _pick(tmp_path, lookup, stage=stage) for stage in ("planning", "grill", "integration")}

    monkeypatch.setenv(OPERATING_HARNESS_ENV, "codex")

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
    assert _pick(tmp_path, _lookup(claude=60.0, codex=80.0), caps=["harness:claude"]) == ("claude", "sonnet")


def test_without_any_signal_grok_and_antigravity_are_never_development_candidates(
    tmp_path: Path, grok_and_antigravity_write: None
) -> None:
    lookup = _lookup(claude=20.0, codex=20.0, grok=100.0, antigravity=100.0)

    assert _pick(tmp_path, lookup) == ("claude", "sonnet")


def test_the_real_development_cascade_keeps_grok_out_so_the_pump_never_picks_it_by_headroom() -> None:
    """Design decision: Grok develops only as the operating harness, never as a cascade member."""
    for config in (load_routing_config(default_config_path()), _default_routing_config()):
        assert [harness for harness, _model in config.stages["development"].cascade] == ["claude", "codex"]


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
