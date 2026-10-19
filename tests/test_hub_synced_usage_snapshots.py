"""USR-232: the Hub shows synced snapshots and never probes off-VPS providers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.line import routing
from core.router.token_budget import _quota_headroom
from core.usage.adapters import (
    ClaudeCodeAccountAdapter,
    CodexAccountAdapter,
    GeminiAccountAdapter,
    GrokAccountAdapter,
    ProviderSpec,
)
from core.usage.history import record_probe
from core.usage.models import AccountConnectionStatus, ProviderFamily
from core.usage.monitor import AccountUsageMonitor
from hub.backend.models import UsageSyncPayload
from hub.backend.service import HubService

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "usage"
FIXTURE_PROVIDERS = ("anthropic", "openai", "google", "xai")


class _Headers:
    def __init__(self, headers):
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class _Body(_Headers):
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _fixture_usage(provider_id: str, tmp_path: Path, monkeypatch):
    path = next(FIXTURES_DIR.glob(f"{provider_id}_*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
    family = ProviderFamily.FRONTIER
    scratch = tmp_path / "scratch"
    if provider_id == "anthropic":
        adapter = ClaudeCodeAccountAdapter(ProviderSpec("anthropic", "Claude", family, "https://x"), scratch)
        monkeypatch.setattr(ClaudeCodeAccountAdapter, "_extract_claude_oauth_info", lambda self: ("t", None, "Pro"))
        monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _Headers(payload["headers"]))
        usage = adapter._probe_claude_unified_ratelimits()
    elif provider_id == "openai":
        adapter = CodexAccountAdapter(ProviderSpec("openai", "Codex", family, "https://x"), scratch)
        usage = record_probe(adapter._from_codex_response(payload), scratch)
    elif provider_id == "google":
        adapter = GeminiAccountAdapter(ProviderSpec("google", "Gemini", family, "https://x"), scratch)
        monkeypatch.setattr(GeminiAccountAdapter, "_find_live_ls_credentials", lambda self: ("c", [1]))
        monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _Body(payload))
        usage = adapter._probe_language_server()
    else:
        adapter = GrokAccountAdapter(ProviderSpec("xai", "Grok", family, "https://x"), scratch)
        monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "t")
        monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
        monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _Body(payload))
        usage = adapter._probe_grok_bot_session()
    assert usage is not None and usage.status == AccountConnectionStatus.CONNECTED
    return usage.model_copy(update={"checked_at": datetime.now(timezone.utc).isoformat()})


def test_hub_headroom_equals_router_headroom_for_real_fixtures(tmp_path, monkeypatch):
    usages = {pid: _fixture_usage(pid, tmp_path, monkeypatch) for pid in FIXTURE_PROVIDERS}
    monkeypatch.undo()  # from here on nothing is mocked: no probe may be needed
    state = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    for key in ("DARKHUB_USAGE_SNAPSHOT_DIR",):
        monkeypatch.delenv(key, raising=False)
    providers = state / "usage" / "providers"
    monitor = AccountUsageMonitor(providers, synced_only=True)
    for pid, usage in usages.items():
        monitor.save_snapshot(pid, json.loads(usage.model_dump_json()))

    report = {a.provider_id: a for a in monitor.inspect(force=True).accounts}
    for pid in FIXTURE_PROVIDERS:
        hub_headroom = _quota_headroom(report[pid])
        router_headroom, reason = routing._quota_headroom_diagnosis(pid)
        assert hub_headroom is not None, pid
        assert reason == "healthy", (pid, reason)
        assert hub_headroom == router_headroom, pid


def test_synced_monitor_never_probes_and_marks_missing_as_unsynced(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("off-VPS provider must not be probed")

    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", boom)
    monkeypatch.setattr("core.usage.adapters.subprocess.run", boom)
    monkeypatch.setattr("core.usage.adapters.subprocess.Popen", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    monitor = AccountUsageMonitor(tmp_path / "providers", synced_only=True)
    accounts = {a.provider_id: a for a in monitor.inspect(force=True).accounts}
    for pid in FIXTURE_PROVIDERS:
        assert accounts[pid].status == AccountConnectionStatus.UNKNOWN
        assert accounts[pid].status != AccountConnectionStatus.DISCONNECTED
        assert "Sem dado sincronizado" in accounts[pid].message
        assert accounts[pid].windows == []


def test_hub_service_persists_sync_in_configured_volume_dir(tmp_path, monkeypatch):
    volume = tmp_path / "hub-data" / "usage-snapshots"
    monkeypatch.setenv("DARKHUB_USAGE_SNAPSHOT_DIR", str(volume))
    service = HubService(data_dir=tmp_path / "hub-data")
    now = datetime.now(timezone.utc).isoformat()
    window = {"quota_id": "w", "label": "w", "used_percent": 20.0, "remaining_percent": 80.0, "window_duration_minutes": 300}
    service.sync_usage_data(
        UsageSyncPayload(client_node_id="n", accounts=[{"provider_id": "openai", "checked_at": now, "windows": [window]}])
    )
    assert (volume / "providers" / "openai.json").is_file()
    # A new service instance (redeploy with the same volume) still serves it.
    again = HubService(data_dir=tmp_path / "hub-data")
    accounts = {a.provider_id: a for a in again.get_account_usage(force=True).accounts}
    assert _quota_headroom(accounts["openai"]) == 80.0
    assert accounts["anthropic"].status == AccountConnectionStatus.UNKNOWN


def test_compose_sets_snapshot_dir_inside_named_volume():
    text = (Path(__file__).parent.parent / "deploy" / "dokploy" / "docker-compose.hub.yml").read_text(encoding="utf-8")
    assert "DARKHUB_USAGE_SNAPSHOT_DIR=/app/hub/data/usage-snapshots" in text
    assert "darkhub-hub-data:/app/hub/data" in text
