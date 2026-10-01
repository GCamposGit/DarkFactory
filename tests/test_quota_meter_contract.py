"""Quota interpretation and evidence regressions."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.router.token_budget import _quota_headroom
from core.usage.adapters import GrokAccountAdapter, ProviderSpec
from core.usage.history import history_path, read_history, record_probe
from core.usage.models import AccountConnectionStatus, ProviderAccountUsage, ProviderFamily, QuotaWindow


FIXTURE = Path(__file__).parent / "fixtures" / "usage" / "xai_2026-10-01.json"
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
    (directory / "xai.json").write_text(json.dumps({"checked_at": old, "status": "connected",
        "windows": [{"quota_id": "grok:weekly_pool", "remaining_percent": 99}]}), encoding="utf-8")
    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: None)
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    usage = GrokAccountAdapter(SPEC, directory).inspect()
    assert usage.status == AccountConnectionStatus.DEGRADED
    assert _quota_headroom(usage) is None
    assert "token or app missing" in caplog.text


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
