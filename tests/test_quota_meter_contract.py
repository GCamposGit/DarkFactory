"""Quota interpretation and evidence regressions."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.router.token_budget import _quota_headroom
from core.usage.adapters import (
    ClaudeCodeAccountAdapter,
    CodexAccountAdapter,
    GeminiAccountAdapter,
    GrokAccountAdapter,
    ProviderSpec,
)
from core.usage.history import history_path, read_history, record_probe
from core.usage.models import AccountConnectionStatus, ProviderAccountUsage, ProviderFamily, QuotaWindow


FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "xai_2026-10-01.json"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "usage"
SPEC = ProviderSpec("xai", "Grok", ProviderFamily.FRONTIER, "https://grok.com/?_s=usage")


class Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


def probe(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "fixture-token")
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: Response(payload))
    return GrokAccountAdapter(SPEC, tmp_path / "providers")._probe_grok_bot_session()


def test_real_grok_fixture_semantics_and_sanitized_history(tmp_path, monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"] | {"email": "must-not-persist@example.com", "accessToken": "secret"}
    usage = probe(tmp_path, monkeypatch, payload)
    assert usage.status == AccountConnectionStatus.CONNECTED
    assert usage.windows[0].used_percent == 1.4
    assert usage.windows[0].remaining_percent == 98.6
    assert _quota_headroom(usage) == 98.6
    rows = read_history(history_path(tmp_path / "providers", "xai"))
    assert rows[0]["raw_fields"]["usagePercent"] == 1.40324
    assert "email" not in rows[0]["raw_fields"]
    assert "accessToken" not in rows[0]["raw_fields"]
    from core.usage.history import sanitize_raw
    assert sanitize_raw("xai", {"grokPlanLabel": "someone@example.com"})["grokPlanLabel"] == "[redacted]"


class HeaderResponse:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class SummaryResponse(Response):
    status = 200


@pytest.mark.parametrize("provider_id,fixture_file,expected_windows,expected_headroom", [
    (
        "anthropic",
        "anthropic_2026-10-01.json",
        [
            ("claude:weekly", 87.0, 13.0, "2026-10-04T01:00:00+00:00"),
            ("claude:5h", 12.0, 88.0, "2026-10-01T16:50:00+00:00"),
        ],
        13.0,
    ),
    (
        "openai",
        "openai_2026-10-01.json",
        [
            ("codex:secondary", 53.0, 47.0, "2026-10-04T00:24:03+00:00"),
            ("codex:primary", 47.0, 53.0, "2026-10-01T16:57:11+00:00"),
        ],
        47.0,
    ),
    (
        "google",
        "google_2026-10-01.json",
        [
            ("antigravity:gemini-weekly", 85.0, 15.0, "2026-10-08T18:13:46Z"),
            ("antigravity:gemini-5h", 0.0, 100.0, "2026-10-01T18:13:46Z"),
        ],
        15.0,
    ),
    (
        "xai",
        "xai_2026-10-01.json",
        [
            ("grok:weekly_pool", 1.4, 98.6, "2026-10-06T22:15:57.642Z"),
        ],
        98.6,
    ),
])
def test_real_fixtures_payload_semantics_fails_if_inverted(
    tmp_path, monkeypatch, provider_id, fixture_file, expected_windows, expected_headroom
):
    fixture_data = json.loads((FIXTURES_DIR / fixture_file).read_text(encoding="utf-8"))
    payload = fixture_data["payload"]

    if provider_id == "anthropic":
        adapter = ClaudeCodeAccountAdapter(
            ProviderSpec("anthropic", "Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/usage"),
            tmp_path / "providers",
        )
        monkeypatch.setattr(
            ClaudeCodeAccountAdapter,
            "_extract_claude_oauth_info",
            lambda self: ("fixture-token", None, "Pro"),
        )
        monkeypatch.setattr(
            "core.usage.adapters.urllib.request.urlopen",
            lambda *a, **k: HeaderResponse(payload["headers"]),
        )
        usage = adapter._probe_claude_unified_ratelimits()

    elif provider_id == "openai":
        adapter = CodexAccountAdapter(
            ProviderSpec("openai", "Codex", ProviderFamily.FRONTIER, "https://chatgpt.com/codex/settings/usage"),
            tmp_path / "providers",
        )
        usage = adapter._from_codex_response(payload)
        usage = record_probe(usage, tmp_path / "providers")

    elif provider_id == "google":
        adapter = GeminiAccountAdapter(
            ProviderSpec("google", "Gemini", ProviderFamily.FRONTIER, "https://one.google.com/"),
            tmp_path / "providers",
        )
        monkeypatch.setattr(
            GeminiAccountAdapter,
            "_find_live_ls_credentials",
            lambda self: ("fixture-csrf", [12345]),
        )
        monkeypatch.setattr(
            "core.usage.adapters.urllib.request.urlopen",
            lambda *a, **k: SummaryResponse(payload),
        )
        usage = adapter._probe_language_server()

    elif provider_id == "xai":
        usage = probe(tmp_path, monkeypatch, payload)

    else:
        raise ValueError(provider_id)

    assert usage is not None
    assert usage.status == AccountConnectionStatus.CONNECTED
    assert _quota_headroom(usage) == expected_headroom

    # Check each expected window
    windows_by_id = {w.quota_id: w for w in usage.windows}
    for qid, exp_used, exp_remaining, exp_reset in expected_windows:
        assert qid in windows_by_id, f"Window {qid} missing from {list(windows_by_id)}"
        win = windows_by_id[qid]
        assert win.used_percent == exp_used
        assert win.remaining_percent == exp_remaining
        if exp_reset:
            assert win.resets_at == exp_reset

        # Strict test requirement: must fail if semantics are inverted (used <-> remaining)
        assert exp_used != exp_remaining, f"Window {qid} cannot have identical used and remaining for invert test"
        inverted_used = exp_remaining
        inverted_remaining = exp_used
        assert (win.used_percent != inverted_used or win.remaining_percent != inverted_remaining), (
            f"Window {qid} semantics are inverted!"
        )


@pytest.mark.parametrize("change", [
    {"usagePercent": "1.4"}, {"usagePercent": None},
    {"hasAvailableUsage": "true"}, {"currentPeriodStart": None},
    {"nextResetTimestampUtc": "not-a-date"},
])
def test_invalid_grok_payload_is_unknown(tmp_path, monkeypatch, caplog, change):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"] | change
    usage = probe(tmp_path, monkeypatch, payload)
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "received fields" in caplog.text


def test_grok_limited_signal_overrides_weekly_headroom(tmp_path, monkeypatch):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"] | {"hasAvailableUsage": False}
    usage = probe(tmp_path, monkeypatch, payload)
    assert usage.status == AccountConnectionStatus.LIMITED
    assert _quota_headroom(usage) == 0.0


@pytest.mark.parametrize("previous,current,start_offset,flag", [
    (30, 20, 2, "decreased"), (10, 70, 2, "jumped"),
    (None, 90, 0, "reset"),
])
def test_plausibility_fails_closed(tmp_path, previous, current, start_offset, flag):
    now = datetime.now(timezone.utc)
    start = now.replace(microsecond=0).isoformat()
    path = history_path(tmp_path / "providers", "xai")
    path.parent.mkdir(parents=True)
    if previous is not None:
        path.write_text(json.dumps({"checked_at": now.isoformat(), "quota_id": "grok:weekly_pool",
                                    "used_percent": previous, "period_start": start, "flags": []}) + "\n", encoding="utf-8")
    from datetime import timedelta
    period = start if start_offset == 0 else (now - timedelta(hours=start_offset)).isoformat()
    if previous is not None:
        # Explicitly same period for monotonicity comparisons.
        period = start
    usage = ProviderAccountUsage(provider_id="xai", provider_name="Grok", family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED, adapter="grok_bot_api", message="live", period_start=period,
        windows=[QuotaWindow(quota_id="grok:weekly_pool", label="week", used_percent=current,
                             remaining_percent=100-current, window_duration_minutes=10080)])
    result = record_probe(usage, tmp_path / "providers")
    assert result.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(result) is None
    assert flag in result.message


def test_audit_expect_detects_mismatch(tmp_path, monkeypatch, capsys):
    import scripts.quota_audit as command
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "fixture-token")
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: Response(payload))
    assert command.audit("xai", {"xai": 98}, tmp_path / "providers") == 0
    assert "usagePercent" in capsys.readouterr().out
    assert command.audit("xai", {"xai": 10}, tmp_path / "providers") == 1


@pytest.mark.parametrize("provider_id,expected_val,divergent_val", [
    ("xai", 98.6, 10.0),
    ("anthropic", 13.0, 80.0),
    ("openai", 47.0, 95.0),
    ("google", 15.0, 80.0),
])
def test_audit_expect_all_providers_detects_mismatch(
    tmp_path, monkeypatch, provider_id, expected_val, divergent_val
):
    import scripts.quota_audit as command

    fixture_dir = Path(__file__).parent / "fixtures" / "usage"
    xai_p = json.loads((fixture_dir / "xai_2026-10-01.json").read_text(encoding="utf-8"))["payload"]
    ant_p = json.loads((fixture_dir / "anthropic_2026-10-01.json").read_text(encoding="utf-8"))["payload"]
    oai_p = json.loads((fixture_dir / "openai_2026-10-01.json").read_text(encoding="utf-8"))["payload"]
    goo_p = json.loads((fixture_dir / "google_2026-10-01.json").read_text(encoding="utf-8"))["payload"]

    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "fixture-token")
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_extract_claude_oauth_info", lambda self: ("fixture-token", None, "Pro"))
    monkeypatch.setattr(GeminiAccountAdapter, "_find_live_ls_credentials", lambda self: ("fixture-csrf", [12345]))
    monkeypatch.setattr(CodexAccountAdapter, "_find_codex", lambda *a: "fake-codex")
    monkeypatch.setattr(CodexAccountAdapter, "_read_rate_limits", lambda *a, **k: oai_p)

    def mock_urlopen(req, *args, **kwargs):
        url = getattr(req, "full_url", str(req))
        if "cursor.sh" in url or "grok" in url:
            return Response(xai_p)
        if "anthropic.com" in url:
            return HeaderResponse(ant_p["headers"])
        if "127.0.0.1" in url or "language_server" in url:
            return SummaryResponse(goo_p)
        return Response({})

    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", mock_urlopen)

    assert command.audit(provider_id, {provider_id: expected_val}, tmp_path / "providers") == 0
    assert command.audit(provider_id, {provider_id: divergent_val}, tmp_path / "providers") == 1



def test_report_shows_source_raw_and_unknown_distinct_from_critical():
    from run_ticket import format_quota_report
    report = format_quota_report({
        "grok": {"provider": "xai", "headroom": 98.6, "status": "SAUDÁVEL",
                 "source": "grok_bot_api", "age": "2 min", "raw_fields": {"usagePercent": 1.40324}},
        "claude": {"provider": "anthropic", "headroom": None, "status": "SEM MEDIDOR / SUSPEITA",
                   "source": "claude_code_api", "age": "3 min", "raw_fields": {}},
        "codex": {"provider": "openai", "headroom": 10.0, "status": "CRÍTICO (<= 15%)",
                  "source": "codex_app_server", "age": "1 min", "raw_fields": {"usedPercent": 90}},
    })
    assert "grok_bot_api, 2 min, raw usagePercent=1.40324" in report
    assert "SEM MEDIDOR / SUSPEITA" in report
    assert "CRÍTICO (<= 15%)" in report


def test_grok_stale_snapshot_does_not_hide_missing_probe(tmp_path, monkeypatch, caplog):
    from datetime import timedelta
    directory = tmp_path / "providers"
    directory.mkdir()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    (directory / "xai.json").write_text(json.dumps({
        "checked_at": old, "status": "connected", "adapter": "grok_bot_api",
        "raw_fields": {"usagePercent": 1.0},
        "windows": [{"quota_id": "grok:weekly_pool", "label": "weekly",
                     "used_percent": 1.0, "remaining_percent": 99.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: None)
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    usage = GrokAccountAdapter(SPEC, directory).inspect()
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert usage.checked_at == old
    assert usage.windows[0].remaining_percent == 99.0
    assert "snapshot" in usage.message.lower()
    assert "2" in usage.message
    assert "grok bot" in usage.message.lower() or "grok cli" in usage.message.lower()
    assert "authenticate Grok Bot" in caplog.text


def test_grok_without_snapshot_or_local_session_has_no_fabricated_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: None)
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr("core.usage.adapters.shutil.which", lambda _: None)
    for key in SPEC.env_keys:
        monkeypatch.delenv(key, raising=False)

    usage = GrokAccountAdapter(SPEC, tmp_path / "providers").inspect()

    assert usage.status == AccountConnectionStatus.DISCONNECTED
    assert usage.windows == []
    assert usage.quota_supported is False
    assert "sessão grok" in usage.message.lower()
    assert "autentique" in usage.message.lower()


def test_grok_forced_probe_failure_marks_recent_snapshot_degraded(tmp_path, monkeypatch):
    directory = tmp_path / "providers"
    directory.mkdir()
    checked_at = datetime.now(timezone.utc).isoformat()
    (directory / "xai.json").write_text(json.dumps({
        "checked_at": checked_at, "status": "connected", "adapter": "grok_bot_api",
        "raw_fields": {"usagePercent": 4.0},
        "windows": [{"quota_id": "grok:weekly_pool", "label": "weekly",
                     "used_percent": 4.0, "remaining_percent": 96.0}],
    }), encoding="utf-8")
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: None)
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)

    usage = GrokAccountAdapter(SPEC, directory).inspect(force=True)

    assert usage.status == AccountConnectionStatus.DEGRADED
    assert usage.checked_at == checked_at
    assert usage.windows[0].remaining_percent == 96.0
    assert "há menos de 1 min" in usage.message


def test_history_rotates_without_rewriting_old_lines(tmp_path):
    from datetime import timedelta
    directory = tmp_path / "providers"
    path = history_path(directory, "xai")
    path.parent.mkdir(parents=True)
    old_line = json.dumps({"checked_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
                           "quota_id": "grok:weekly_pool", "used_percent": 5}) + "\n"
    path.write_text(old_line, encoding="utf-8")
    usage = ProviderAccountUsage(provider_id="xai", provider_name="Grok", family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED, adapter="grok_bot_api", message="live",
        windows=[QuotaWindow(quota_id="grok:weekly_pool", label="week", used_percent=10,
                             remaining_percent=90, window_duration_minutes=10080)])
    record_probe(usage, directory)
    archives = list(path.parent.glob("xai.*.jsonl"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == old_line
    assert len(read_history(path)) == 1


def test_corrupt_history_blocks_new_reading(tmp_path):
    directory = tmp_path / "providers"
    path = history_path(directory, "xai")
    path.parent.mkdir(parents=True)
    path.write_text("not-json\n", encoding="utf-8")
    usage = ProviderAccountUsage(provider_id="xai", provider_name="Grok", family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED, adapter="grok_bot_api", message="live",
        windows=[QuotaWindow(quota_id="grok:weekly_pool", label="week", used_percent=1,
                             remaining_percent=99, window_duration_minutes=10080)])
    checked = record_probe(usage, directory)
    assert checked.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(checked) is None
    assert "history unreadable" in checked.message


def test_owner_confirmation_is_bound_to_period_and_value(tmp_path, monkeypatch):
    import scripts.quota_audit as command
    from core.usage.history import confirmation_path
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"] | {"usagePercent": 90.0}
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "fixture-token")
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: Response(payload))
    directory = tmp_path / "providers"
    prior = history_path(directory, "xai")
    prior.parent.mkdir(parents=True)
    prior.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(),
        "quota_id": "grok:weekly_pool", "used_percent": 1.4,
        "period_start": payload["currentPeriodStart"], "flags": []}) + "\n", encoding="utf-8")
    assert command.audit("xai", {"xai": 10}, directory) == 1
    assert command.audit("xai", {"xai": 10}, directory, confirm=True) == 0
    assert confirmation_path(directory, "xai").is_file()
    changed = payload | {"usagePercent": 80.0}
    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: Response(changed))
    assert command.audit("xai", {"xai": 20}, directory) == 1


def test_router_uses_most_restrictive_real_window():
    usage = ProviderAccountUsage(provider_id="anthropic", provider_name="Claude", family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED, adapter="claude_code_api", message="live",
        windows=[QuotaWindow(quota_id="claude:weekly", label="weekly", remaining_percent=18),
                 QuotaWindow(quota_id="claude:5h", label="5h", remaining_percent=82)])
    assert _quota_headroom(usage) == 18


def test_openai_missing_bucket_is_degraded(tmp_path, caplog):
    from core.usage.adapters import CodexAccountAdapter
    adapter = CodexAccountAdapter(ProviderSpec("openai", "Codex", ProviderFamily.FRONTIER,
        "https://chatgpt.com/codex/settings/usage"), tmp_path)
    usage = adapter._from_codex_response({"unexpected": 1})
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "received fields" in caplog.text


@pytest.mark.parametrize("change", [
    {"windowDurationMins": None}, {"windowDurationMins": "300"},
    {"resetsAt": None}, {"resetsAt": "bad-reset"},
])
def test_openai_invalid_window_is_unknown(tmp_path, caplog, change):
    from core.usage.adapters import CodexAccountAdapter
    adapter = CodexAccountAdapter(ProviderSpec("openai", "Codex", ProviderFamily.FRONTIER,
        "https://chatgpt.com/codex/settings/usage"), tmp_path)
    slot = {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1800000000} | change
    usage = adapter._from_codex_response({"rateLimits": {"primary": slot}})
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "received fields" in caplog.text


def test_disconnected_snapshot_cannot_supply_headroom():
    usage = ProviderAccountUsage(provider_id="xai", provider_name="Grok", family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.DISCONNECTED, adapter="json_snapshot", message="disconnected",
        windows=[QuotaWindow(quota_id="grok:weekly_pool", label="weekly", remaining_percent=99)])
    assert _quota_headroom(usage) is None


def test_anthropic_partial_headers_are_degraded(tmp_path, monkeypatch, caplog):
    from core.usage.adapters import ClaudeCodeAccountAdapter
    adapter = ClaudeCodeAccountAdapter(ProviderSpec("anthropic", "Claude", ProviderFamily.FRONTIER,
        "https://claude.ai/settings/usage"), tmp_path)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_extract_claude_oauth_info",
                        lambda self: ("fixture-token", None, "Pro"))

    class HeaderResponse:
        headers = {"anthropic-ratelimit-unified-7d-utilization": "0.82"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: HeaderResponse())
    usage = adapter._probe_claude_unified_ratelimits()
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "received fields" in caplog.text


@pytest.mark.parametrize("bucket", [
    {"window": "weekly"},
    {"window": "weekly", "remainingFraction": "0.9"},
])
def test_google_invalid_summary_bucket_is_unknown(tmp_path, monkeypatch, caplog, bucket):
    from core.usage.adapters import GeminiAccountAdapter
    adapter = GeminiAccountAdapter(ProviderSpec("google", "Gemini", ProviderFamily.FRONTIER,
        "https://one.google.com/"), tmp_path)
    monkeypatch.setattr(GeminiAccountAdapter, "_find_live_ls_credentials",
                        lambda self: ("fixture-csrf", [12345]))

    class SummaryResponse(Response):
        status = 200

    payload = {"response": {"groups": [{"displayName": "Gemini", "buckets": [bucket]}]}}
    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen",
                        lambda *a, **k: SummaryResponse(payload))
    usage = adapter._probe_language_server()
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "received fields" in caplog.text
