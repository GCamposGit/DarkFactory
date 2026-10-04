"""Line unblock fixes: rate-limit message recognition (C), write-route policy (A), Codex/Claude quota
knowledge for the cloud worker and the `last_resort` bootstrap (B), validate diagnostics (D)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line import diagnostics
from core.line.agent_cli import _classify_error, _extract_reset_at
from core.line.routing import (
    RoutingConfig,
    StageRoute,
    load_routing_config,
    pick,
    validate_routing_config,
)
from core.usage import adapters

NOW = datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)
ALL_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


# --------------------------------------------------------------------------
# C: rate-limit messages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your session limit · resets 1:50am (UTC)",
        "Weekly limit reached ∙ resets Oct 3, 9am (UTC)",
        "Claude Opus limit reached, resets 13:50 (UTC)",
        "You have hit your limit",
        "5-hour limit reached",
        "Error: 429 Too Many Requests",
        "usage limit exceeded",
    ],
)
def test_subscription_limit_messages_are_rate_limited(text: str) -> None:
    assert _classify_error(text) == "rate_limited"


@pytest.mark.parametrize("text", ["segmentation fault", "connection reset by peer", "boom", ""])
def test_unrelated_errors_stay_crash(text: str) -> None:
    assert _classify_error(text) == "crash"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hit your session limit · resets 1:50am (UTC)", datetime(2026, 10, 2, 1, 50, tzinfo=timezone.utc)),
        ("resets 13:50 (UTC)", datetime(2026, 10, 1, 13, 50, tzinfo=timezone.utc)),
        ("resets 9am (UTC)", datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)),
        ("resets Oct 3, 9am (UTC)", datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)),
        ("resets Oct 3 at 9:30pm (UTC)", datetime(2026, 10, 3, 21, 30, tzinfo=timezone.utc)),
        ("resets Jan 2, 9am (UTC)", datetime(2027, 1, 2, 9, 0, tzinfo=timezone.utc)),
    ],
)
def test_clock_reset_times_parse_to_the_next_utc_instant(text: str, expected: datetime) -> None:
    assert _extract_reset_at(text, now=NOW) == expected


@pytest.mark.parametrize("text", ["resets 3", "resets soon", "resets 25:99 (UTC)", "resets 9am (Nowhere/Land)"])
def test_unparseable_clock_reset_is_none(text: str) -> None:
    assert _extract_reset_at(text, now=NOW) is None


def test_relative_reset_still_wins() -> None:
    parsed = _extract_reset_at("try again in 2 hours")
    assert parsed is not None and parsed > datetime.now(timezone.utc) + timedelta(hours=1, minutes=50)


# --------------------------------------------------------------------------
# A: development never routes to a read-only harness
# --------------------------------------------------------------------------


def test_repo_config_has_no_openrouter_for_development_and_is_consistent() -> None:
    cfg = load_routing_config()
    assert cfg.stages["development"].openrouter_ok is False
    assert validate_routing_config(cfg) == []


def test_validate_flags_openrouter_ok_on_a_write_stage() -> None:
    cfg = RoutingConfig(
        stages={"development": StageRoute(cascade=[("claude", "sonnet")], openrouter_ok=True, openrouter_model="x/y")}
    )
    assert any("openrouter_ok" in v for v in validate_routing_config(cfg))


def test_development_with_claude_cooling_and_codex_unknown_gets_no_route_not_openrouter(tmp_path: Path) -> None:
    cooldowns = tmp_path / "cooldowns.json"
    cooldowns.write_text(
        json.dumps({"claude": {"until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}),
        encoding="utf-8",
    )
    cfg = load_routing_config()
    cfg.stages["development"].openrouter_ok = True  # even a stale config must not leak a read-only harness
    route = pick(
        "development", ALL_CAPS, config=cfg, quota_lookup=lambda _p: None,
        cooldown_path=cooldowns, openrouter_balance_lookup=lambda: 5.0,
        unknown_quota_last_resort_ok=False,
    )
    assert route is None


# --------------------------------------------------------------------------
# B: unknown quota bootstrap + real data inside the container
# --------------------------------------------------------------------------


def _quota(table: dict[str, float | None]):
    return lambda provider: table.get(provider)


def _pick(stage: str, table: dict[str, float | None], tmp_path: Path, **kw):
    return pick(
        stage, ALL_CAPS, config=load_routing_config(), quota_lookup=_quota(table),
        cooldown_path=tmp_path / "cd.json", openrouter_balance_lookup=lambda: 5.0, **kw,
    )


def test_unknown_quota_is_fail_closed_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", raising=False)
    assert _pick("development", {"anthropic": 5.0, "openai": None}, tmp_path) is None


def test_last_resort_elects_unknown_codex_when_nothing_is_known_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", "last_resort")
    assert _pick("development", {"anthropic": 5.0, "openai": None}, tmp_path) == ("codex", None)


def test_last_resort_prefers_known_healthy_over_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", "last_resort")
    assert _pick("development", {"anthropic": 80.0, "openai": None}, tmp_path) == ("claude", "sonnet")


def test_last_resort_never_elects_known_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", "last_resort")
    assert _pick("development", {"anthropic": 5.0, "openai": 10.0}, tmp_path) is None


def test_last_resort_skips_a_harness_in_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", "last_resort")
    (tmp_path / "cd.json").write_text(
        json.dumps({"codex": {"until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}),
        encoding="utf-8",
    )
    assert _pick("development", {"anthropic": None, "openai": None}, tmp_path) == ("claude", "sonnet")


def test_last_resort_beats_openrouter_for_a_read_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_ROUTING_UNKNOWN_QUOTA", "last_resort")
    route = _pick("review", {"anthropic": 80.0, "openai": None, "xai": 1.0, "google": 1.0}, tmp_path,
                  implementing_harness="claude")
    assert route is not None and route[0] == "codex"
    monkeypatch.delenv("DARKFAC_ROUTING_UNKNOWN_QUOTA")
    route = _pick("review", {"anthropic": 80.0, "openai": None, "xai": 1.0, "google": 1.0}, tmp_path,
                  implementing_harness="claude")
    assert route is not None and route[0] == "openrouter"


def _rollout(sessions: Path, event_time: datetime, primary_used: float, secondary_used: float, resets_in_h: float) -> None:
    day = sessions / "2026" / "10" / "01"
    day.mkdir(parents=True, exist_ok=True)
    reset = int((event_time + timedelta(hours=resets_in_h)).timestamp())
    event = {
        "timestamp": event_time.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": primary_used, "window_minutes": 300, "resets_at": reset},
                "secondary": {"used_percent": secondary_used, "window_minutes": 10080, "resets_at": reset + 86400},
                "plan_type": "plus",
            },
        },
    }
    (day / "rollout-2026-10-01T00-00-00-abc.jsonl").write_text(
        json.dumps({"type": "session_meta"}) + "\n" + json.dumps(event) + "\n" + json.dumps({"type": "other"}) + "\n",
        encoding="utf-8",
    )


def test_codex_rate_limits_are_read_from_session_files(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _rollout(tmp_path, now - timedelta(minutes=5), primary_used=24.0, secondary_used=31.0, resets_in_h=3)
    found = adapters.latest_codex_rate_limits(tmp_path, now=now)
    assert found is not None
    stamp, payload = found
    assert stamp == (now - timedelta(minutes=5)).replace(microsecond=stamp.microsecond)
    assert payload["rateLimits"]["primary"]["usedPercent"] == 24.0


def test_codex_adapter_uses_session_files_when_no_live_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    _rollout(tmp_path / "home" / "sessions", now - timedelta(minutes=5), 24.0, 31.0, 3)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(adapters.CodexAccountAdapter, "_find_codex", staticmethod(lambda: None))
    adapter = adapters.CodexAccountAdapter(adapters.DEFAULT_PROVIDER_SPECS[0], tmp_path / "snap")
    usage = adapter.inspect()
    assert usage.adapter == "codex_session_files"
    assert {round(w.remaining_percent) for w in usage.windows} == {76, 69}


def test_expired_windows_are_dropped_not_assumed_healthy(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _rollout(tmp_path, now - timedelta(hours=10), 90.0, 95.0, resets_in_h=-5)  # primary already reset
    found = adapters.latest_codex_rate_limits(tmp_path, now=now)
    assert found is not None
    assert "primary" not in found[1]["rateLimits"] and "secondary" in found[1]["rateLimits"]
    _rollout(tmp_path, now - timedelta(days=9), 90.0, 95.0, resets_in_h=-200)  # both reset
    assert adapters.latest_codex_rate_limits(tmp_path, now=now) is None


def test_claude_oauth_token_falls_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapters.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-from-env")
    token, _email, _plan = adapters.ClaudeCodeAccountAdapter._extract_claude_oauth_info()
    assert token == "tok-from-env"
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    assert adapters.ClaudeCodeAccountAdapter._extract_claude_oauth_info()[0] is None


# --------------------------------------------------------------------------
# D: raw tail in diagnostics
# --------------------------------------------------------------------------


def test_redacted_tail_keeps_the_end_and_redacts() -> None:
    text = "x" * 5000 + " sk-ant-" + "B" * 30 + " [ERROR] Candidate worktree is dirty"
    tail = diagnostics.redacted_tail(text)
    assert tail.endswith("Candidate worktree is dirty")
    assert "sk-ant-" not in tail and "[REDACTED]" in tail
    assert len(tail) < diagnostics.RAW_TAIL_CHARS + 100


def test_attempt_log_has_raw_tail_section_only_when_given() -> None:
    kw = dict(iteration=1, harness="claude", model="s", error_kind=None, duration_s=1.0, output="head")
    assert "Raw output tail" not in diagnostics.format_attempt_log("development", **kw)
    assert "the end" in diagnostics.format_attempt_log("development", raw_tail="...the end", **kw)


def test_line_validate_names_the_paths_a_failed_runner_left_dirty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("line_validate_dirty", repo_root / "scripts" / "line_validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / "wt"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.test"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, capture_output=True)

    module.report_dirty_checkout(root)
    assert "checkout is clean" in capsys.readouterr().out
    (root / "stray_dir").mkdir()
    (root / "stray_dir" / "left.txt").write_text("x", encoding="utf-8")
    module.report_dirty_checkout(root)
    assert "stray_dir/left.txt" in capsys.readouterr().out


def test_attempt_log_lists_every_remote_dispatch_line_even_when_the_tail_cut_them_off() -> None:
    noise = "x" * 6000
    raw = (
        "[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)\n"
        "[line_validate] snapshot abc checked out\n"
        f"{noise}\n"
        "[REMOTE] no remote worker used; running the suite locally on this host\n"
        "[HARNESS_FAIL]\n"
    )
    log = diagnostics.format_attempt_log(
        "development", iteration=1, harness="claude", model="sonnet", error_kind=None,
        duration_s=1.0, output="verdict=FAILED", raw_tail=raw,
    )
    before, section = log.split("## Remote dispatch lines (redacted)", 1)
    assert "unreachable (worker offline or port blocked)" in section
    assert "[line_validate] snapshot abc checked out" in section
    assert "no remote worker used" in section
    assert "[HARNESS_FAIL]" not in section
    # the first [REMOTE] line is outside the 3000-char raw tail: only the dedicated section keeps it
    assert "unreachable" not in before


def test_attempt_log_says_when_remote_dispatch_was_not_attempted() -> None:
    log = diagnostics.format_attempt_log(
        "development", iteration=1, harness="claude", model="sonnet", error_kind=None,
        duration_s=1.0, output="o", raw_tail="plain output",
    )
    assert "remote dispatch was not attempted" in log


def test_remote_dispatch_lines_are_redacted_and_capped() -> None:
    secret = "sk-" + "ant-" + "api03-" + "FakeSecretToken1234567890"
    raw = "\n".join([f"[REMOTE] line {i} {secret}" for i in range(100)])
    lines = diagnostics.remote_dispatch_lines(raw)
    assert len(lines) <= diagnostics.REMOTE_SECTION_MAX_LINES
    assert lines[-1].startswith("[REMOTE] line 99")
    assert all(secret not in line for line in lines)
