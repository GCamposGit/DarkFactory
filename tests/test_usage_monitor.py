"""Regression tests for account quota monitoring and model telemetry."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.usage.adapters import (
    AccountUsageAdapter,
    CodexAccountAdapter,
    EnvironmentAccountAdapter,
    ProviderSpec,
)
from core.usage.ledger import ModelUsageLedger
from core.usage.store import UsageStoreCorruptionError
from core.usage.models import (
    AccountConnectionStatus,
    ModelCallEvent,
    ModelModality,
    ModelTier,
    ProviderAccountUsage,
    ProviderFamily,
    QuotaWindow,
)
from core.usage.monitor import AccountUsageMonitor
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


OPENAI_SPEC = ProviderSpec(
    "openai",
    "OpenAI / Codex",
    ProviderFamily.FRONTIER,
    "https://chatgpt.com/codex/settings/usage",
)


class _StaticAdapter(AccountUsageAdapter):
    def __init__(self, spec: ProviderSpec, snapshot_dir: Path, *, fails: bool = False) -> None:
        super().__init__(spec, snapshot_dir)
        self.fails = fails

    def inspect(self) -> ProviderAccountUsage:
        if self.fails:
            raise RuntimeError("provider unavailable")
        return ProviderAccountUsage(
            provider_id=self.spec.provider_id,
            provider_name=self.spec.provider_name,
            family=self.spec.family,
            status=AccountConnectionStatus.CONNECTED,
            adapter="test",
            message="connected",
        )


def test_codex_parser_preserves_5h_and_weekly_windows(tmp_path: Path) -> None:
    adapter = CodexAccountAdapter(OPENAI_SPEC, tmp_path)
    result = adapter._from_codex_response(
        {
            "accountId": "account-123456",
            "rateLimitsByLimitId": {
                "codex": {
                    "planType": "plus",
                    "primary": {
                        "usedPercent": 41,
                        "windowDurationMins": 300,
                        "resetsAt": 1_800_000_000,
                    },
                    "secondary": {
                        "usedPercent": 31,
                        "windowDurationMins": 10_080,
                        "resetsAt": 1_800_100_000,
                    },
                }
            },
        }
    )

    assert result.status == AccountConnectionStatus.CONNECTED
    assert [window.window_duration_minutes for window in result.windows] == [10_080, 300]
    assert [window.remaining_percent for window in result.windows] == [69.0, 59.0]
    assert [window.label for window in result.windows] == ["Limite Semanal (1 semana)", "Janela Móvel (5h)"]
    assert result.account_label == "…123456"


def test_snapshot_keeps_unknown_percent_distinct_from_zero(tmp_path: Path) -> None:
    spec = ProviderSpec("qwen", "Qwen", ProviderFamily.CHINESE, "https://example.test")
    adapter = EnvironmentAccountAdapter(spec, tmp_path)
    unknown = adapter._from_snapshot({"status": "connected", "windows": [{"id": "daily"}]})
    measured = adapter._from_snapshot(
        {"status": "connected", "windows": [{"id": "daily", "remaining_percent": 73}]}
    )

    assert unknown.windows[0].used_percent is None
    assert unknown.windows[0].remaining_percent is None
    assert measured.windows[0].used_percent == 27.0
    assert measured.windows[0].remaining_percent == 73.0


def test_openai_and_xai_api_keys_are_connected_fallbacks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("core.usage.adapters.shutil.which", lambda _: None)
    monkeypatch.setattr(CodexAccountAdapter, "_find_codex", lambda self: None)
    from core.usage.adapters import GrokAccountAdapter, GeminiAccountAdapter
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setenv("OPENAI_API_KEY", "configured")
    monkeypatch.setenv("XAI_API_KEY", "configured")
    openai_spec = ProviderSpec(
        "openai",
        "OpenAI / Codex",
        ProviderFamily.FRONTIER,
        "https://chatgpt.com/codex/settings/usage",
        ("OPENAI_API_KEY",),
    )
    openai = CodexAccountAdapter(openai_spec, tmp_path).inspect()
    xai_spec = ProviderSpec(
        "xai",
        "xAI / Grok",
        ProviderFamily.FRONTIER,
        "https://grok.com/?_s=usage",
        ("XAI_API_KEY",),
    )
    xai = GrokAccountAdapter(xai_spec, tmp_path).inspect()

    assert openai.status == AccountConnectionStatus.CONNECTED
    assert openai.adapter == "openai_api_key"
    assert openai.quota_supported is False
    assert xai.status == AccountConnectionStatus.CONNECTED
    assert xai.adapter == "xai_api_key"
    assert xai.quota_supported is False


def test_gemini_adapter_parses_live_language_server_response(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import GeminiAccountAdapter
    spec = ProviderSpec("google", "Google / Gemini", ProviderFamily.FRONTIER, "https://one.google.com/")
    adapter = GeminiAccountAdapter(spec, tmp_path)

    dummy_usage = ProviderAccountUsage(
        provider_id="google",
        provider_name="Google / Gemini",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="antigravity_rpc",
        plan="Pro",
        account_label="user@example.com",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="antigravity:gemini-3.8-flash",
                label="Gemini 3.8 Flash · 5h",
                used_percent=15.0,
                remaining_percent=85.0,
                window_duration_minutes=300,
                resets_at="2026-09-07T01:48:27Z",
                metric="subscription",
            )
        ],
        message="Quotas lidas em tempo real do Language Server local do Antigravity.",
        dashboard_url=spec.dashboard_url,
    )
    monkeypatch.setattr(GeminiAccountAdapter, "_probe_language_server", lambda self: dummy_usage)
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert result.adapter == "antigravity_rpc"
    assert result.plan == "Pro"
    assert result.quota_supported is True
    assert len(result.windows) == 1
    assert result.windows[0].used_percent == 15.0
    assert result.windows[0].remaining_percent == 85.0


def test_gemini_adapter_parses_retrieve_user_quota_summary(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import GeminiAccountAdapter
    spec = ProviderSpec("google", "Google / Gemini", ProviderFamily.FRONTIER, "https://one.google.com/")
    adapter = GeminiAccountAdapter(spec, tmp_path)

    dummy_usage = ProviderAccountUsage(
        provider_id="google",
        provider_name="Google / Gemini",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="antigravity_rpc",
        plan="Pro",
        account_label="user@example.com",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="antigravity:gemini-weekly",
                label="Limite Semanal (1 semana)",
                used_percent=38.5,
                remaining_percent=61.5,
                window_duration_minutes=10080,
                resets_at="2026-09-30T12:24:20Z",
                metric="subscription",
            ),
            QuotaWindow(
                quota_id="antigravity:gemini-5h",
                label="Janela Móvel (5h)",
                used_percent=23.4,
                remaining_percent=76.6,
                window_duration_minutes=300,
                resets_at="2026-09-25T20:06:53Z",
                metric="subscription",
            ),
        ],
        message="Quotas lidas em tempo real do Language Server local do Antigravity.",
        dashboard_url=spec.dashboard_url,
    )
    monkeypatch.setattr(GeminiAccountAdapter, "_probe_language_server", lambda self: dummy_usage)
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert len(result.windows) == 2
    assert result.windows[0].window_duration_minutes == 10080
    assert result.windows[0].used_percent == 38.5
    assert result.windows[1].window_duration_minutes == 300
    assert result.windows[1].used_percent == 23.4


def test_grok_adapter_parses_live_supergrok_usage(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import GrokAccountAdapter
    spec = ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://grok.com/?_s=usage")
    adapter = GrokAccountAdapter(spec, tmp_path)

    dummy_usage = ProviderAccountUsage(
        provider_id="xai",
        provider_name="xAI / Grok",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.LIMITED,
        adapter="grok_cli_auth",
        plan="SuperGrok",
        account_label="user@example.com",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="grok:weekly_pool",
                label="SuperGrok · 1 semana",
                used_percent=100.0,
                remaining_percent=0.0,
                window_duration_minutes=10080,
                resets_at=None,
                metric="shared_compute_pool",
            )
        ],
        message="Sessão SuperGrok autenticada via Grok Build CLI; cota semanal esgotada (0% disponível).",
        dashboard_url=spec.dashboard_url,
    )
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: dummy_usage)
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.LIMITED
    assert result.adapter == "grok_cli_auth"
    assert result.plan == "SuperGrok"
    assert result.quota_supported is True
    assert len(result.windows) == 1
    assert result.windows[0].used_percent == 100.0
    assert result.windows[0].remaining_percent == 0.0


def test_claude_adapter_detects_cli_auth(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import ClaudeCodeAccountAdapter
    spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/billing")
    adapter = ClaudeCodeAccountAdapter(spec, tmp_path)

    dummy_usage = ProviderAccountUsage(
        provider_id="anthropic",
        provider_name="Anthropic / Claude",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="claude_code",
        plan="Claude Pro",
        account_label="user@example.com",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="claude:5h",
                label="Claude Pro · 5h",
                used_percent=0.0,
                remaining_percent=100.0,
                window_duration_minutes=300,
                resets_at=None,
                metric="subscription",
            ),
            QuotaWindow(
                quota_id="claude:weekly",
                label="Claude Pro · 1 semana",
                used_percent=0.0,
                remaining_percent=100.0,
                window_duration_minutes=10080,
                resets_at=None,
                metric="subscription",
            ),
        ],
        message="Sessão Claude Code validada no terminal.",
        dashboard_url=spec.dashboard_url,
    )
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_unified_ratelimits", lambda self: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_find_claude", lambda *_: "mock_claude.exe")
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_cli", lambda self, exe: dummy_usage)
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert result.adapter == "claude_code"
    assert result.plan == "Claude Pro"
    assert result.quota_supported is True
    assert len(result.windows) == 2


def test_claude_adapter_parses_live_unified_ratelimits(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import ClaudeCodeAccountAdapter
    spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/billing")
    adapter = ClaudeCodeAccountAdapter(spec, tmp_path)

    dummy_usage = ProviderAccountUsage(
        provider_id="anthropic",
        provider_name="Anthropic / Claude",
        family=ProviderFamily.FRONTIER,
        status=AccountConnectionStatus.CONNECTED,
        adapter="claude_code_api",
        plan="Claude Pro",
        account_label="test@example.com",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id="claude:weekly",
                label="Limite Semanal (1 semana)",
                used_percent=94.0,
                remaining_percent=6.0,
                window_duration_minutes=10080,
                resets_at="2026-09-27T01:00:00+00:00",
                metric="subscription",
            ),
            QuotaWindow(
                quota_id="claude:5h",
                label="Janela Móvel (5h)",
                used_percent=45.0,
                remaining_percent=55.0,
                window_duration_minutes=300,
                resets_at="2026-09-25T19:30:00+00:00",
                metric="subscription",
            ),
        ],
        message="Quotas lidas em tempo real da API unificada do Claude Code.",
        dashboard_url=spec.dashboard_url,
    )
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_unified_ratelimits", lambda self: dummy_usage)
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert result.adapter == "claude_code_api"
    assert len(result.windows) == 2
    assert result.windows[0].window_duration_minutes == 10080
    assert result.windows[0].used_percent == 94.0
    assert result.windows[1].window_duration_minutes == 300
    assert result.windows[1].used_percent == 45.0


def test_claude_adapter_fallback_to_api_key(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import ClaudeCodeAccountAdapter
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_unified_ratelimits", lambda self: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_find_claude", lambda *_: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_credentials", lambda self: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    spec = ProviderSpec(
        "anthropic",
        "Anthropic / Claude",
        ProviderFamily.FRONTIER,
        "https://claude.ai/settings/billing",
        ("ANTHROPIC_API_KEY",),
    )
    result = ClaudeCodeAccountAdapter(spec, tmp_path).inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert result.adapter == "anthropic_api_key"
    assert result.quota_supported is False


def test_claude_adapter_disconnected_when_no_auth(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import ClaudeCodeAccountAdapter
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_unified_ratelimits", lambda self: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_find_claude", lambda *_: None)
    monkeypatch.setattr(ClaudeCodeAccountAdapter, "_probe_claude_credentials", lambda self: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    spec = ProviderSpec(
        "anthropic",
        "Anthropic / Claude",
        ProviderFamily.FRONTIER,
        "https://claude.ai/settings/billing",
        ("ANTHROPIC_API_KEY",),
    )
    result = ClaudeCodeAccountAdapter(spec, tmp_path).inspect()

    assert result.status == AccountConnectionStatus.DISCONNECTED
    assert result.adapter == "claude_code"
    assert "npm install" in result.message


def test_monitor_isolates_one_provider_failure(tmp_path: Path) -> None:
    good_spec = ProviderSpec("openai", "OpenAI", ProviderFamily.FRONTIER, "https://example.test")
    bad_spec = ProviderSpec("google", "Gemini", ProviderFamily.FRONTIER, "https://example.test")
    monitor = AccountUsageMonitor(
        tmp_path,
        adapters=[
            _StaticAdapter(good_spec, tmp_path),
            _StaticAdapter(bad_spec, tmp_path, fails=True),
        ],
        cache_ttl_sec=0,
    )

    report = monitor.inspect(force=True)

    assert report.connected_count == 1
    assert report.accounts[0].status == AccountConnectionStatus.CONNECTED
    assert report.accounts[1].status == AccountConnectionStatus.DEGRADED
    assert "Falha isolada" in report.accounts[1].message


def test_model_ledger_aggregates_modalities_and_is_idempotent(tmp_path: Path) -> None:
    ledger = ModelUsageLedger(tmp_path)
    text_event = ModelCallEvent(
        invocation_id="same-call-001",
        provider="ollama",
        model="qwen3:8b",
        tier=ModelTier.LOCAL,
        harness="gpt-review",
        modality=ModelModality.TEXT,
        input_tokens=10,
        output_tokens=20,
        source="test",
    )
    image_event = ModelCallEvent(
        invocation_id="image-call-001",
        provider="local_procedural",
        model="svg_renderer",
        tier=ModelTier.PROCEDURAL,
        harness="visual-studio",
        modality=ModelModality.IMAGE,
        source="test",
    )

    ledger.record(text_event)
    ledger.record(text_event)
    ledger.record(image_event)
    report = ModelUsageLedger(tmp_path).report()

    assert report.total_calls == 2
    assert report.total_tokens == 30
    assert {row.modality for row in report.aggregates} == {
        ModelModality.TEXT,
        ModelModality.IMAGE,
    }
    assert {row.harness for row in report.aggregates} == {"gpt-review", "visual-studio"}


def test_independent_ledger_instances_serialize_transactions(tmp_path: Path) -> None:
    def record(index: int) -> None:
        ModelUsageLedger(tmp_path).record(
            ModelCallEvent(
                invocation_id=f"parallel-{index:03d}",
                provider="ollama",
                model="qwen3:8b",
                tier=ModelTier.LOCAL,
                harness="parallel-test",
                source="test",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(24)))

    assert ModelUsageLedger(tmp_path).report().total_calls == 24

def test_usage_api_returns_partial_accounts_and_ingests_harness_events(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "test-telemetry-key")
    service = HubService(data_dir=tmp_path)
    spec = ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://example.test")
    service.account_usage_monitor = AccountUsageMonitor(
        tmp_path / "usage" / "providers",
        adapters=[_StaticAdapter(spec, tmp_path)],
        cache_ttl_sec=0,
    )
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        accounts = client.get("/api/usage/accounts")
        event = {
            "invocation_id": "grok-call-001",
            "provider": "xai",
            "model": "grok-4.6",
            "tier": "frontier",
            "harness": "grok-build",
            "modality": "text",
            "source": "external_harness",
        }
        unauthorized = client.post("/api/usage/models/events", json=event)
        first = client.post("/api/usage/models/events", json=event, headers={"X-DarkFac-Telemetry-Key": "test-telemetry-key"})
        second = client.post("/api/usage/models/events", json=event, headers={"X-DarkFac-Telemetry-Key": "test-telemetry-key"})
        models = client.get("/api/usage/models")
    finally:
        app.dependency_overrides.clear()

    assert accounts.status_code == 200
    assert accounts.json()["accounts"][0]["provider_id"] == "xai"
    assert unauthorized.status_code == 401
    assert first.status_code == 200
    assert second.status_code == 200
    assert models.json()["total_calls"] == 1
    assert models.json()["aggregates"][0]["harness"] == "grok-build"


def test_hub_page_loads_usage_monitor_script() -> None:
    frontend = Path(__file__).resolve().parents[1] / "hub" / "frontend"
    assert '/static/usage.js' in (frontend / "index.html").read_text(encoding="utf-8")
    script = (frontend / "usage.js").read_text(encoding="utf-8")
    assert "/api/usage/accounts" in script
    assert "Percentual indisponível" in script
    assert "Saldo:" in script


def test_replay_after_raw_event_retention_does_not_duplicate(tmp_path: Path) -> None:
    ledger = ModelUsageLedger(tmp_path, max_events=10)
    events = [
        ModelCallEvent(
            invocation_id=f"retained-{index:03d}",
            provider="ollama",
            model="qwen3:8b",
            tier=ModelTier.LOCAL,
            harness="retention-test",
            source="test",
        )
        for index in range(11)
    ]
    for event in events:
        ledger.record(event)

    ledger.record(events[0])

    assert ledger.report().total_calls == 11
    raw = json.loads((tmp_path / "model_usage.json").read_text(encoding="utf-8"))
    assert len(raw["events"]) == 10
    assert len(raw["invocations"]) == 11


def test_corrupt_ledger_is_quarantined_and_report_fails_closed(tmp_path: Path) -> None:
    ledger_path = tmp_path / "model_usage.json"
    ledger_path.write_text("{not-json", encoding="utf-8")
    ledger = ModelUsageLedger(tmp_path)

    with pytest.raises(UsageStoreCorruptionError, match="quarantined"):
        ledger.report()

    assert not ledger_path.exists()
    assert len(list(tmp_path.glob("model_usage.corrupt-*.json"))) == 1


def test_aggregates_can_be_recomputed_from_durable_invocations(tmp_path: Path) -> None:
    ledger = ModelUsageLedger(tmp_path)
    for index in range(3):
        ledger.record(
            ModelCallEvent(
                invocation_id=f"recompute-{index}",
                provider="ollama",
                model="qwen3:8b",
                tier=ModelTier.LOCAL,
                harness="recompute-test",
                source="test",
            )
        )

    ledger_path = tmp_path / "model_usage.json"
    raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    next(iter(raw["aggregates"].values()))["call_count"] = 999
    ledger_path.write_text(json.dumps(raw), encoding="utf-8")

    ledger.recompute_aggregates()

    assert ledger.report().total_calls == 3


def test_independent_processes_serialize_usage_transactions(tmp_path: Path) -> None:
    processes = []
    for process_index in range(4):
        script = (
            "from pathlib import Path; "
            "from core.usage.ledger import ModelUsageLedger; "
            "from core.usage.models import ModelCallEvent, ModelTier; "
            f"ledger=ModelUsageLedger(Path({str(tmp_path)!r})); "
            f"prefix='process-{process_index}-'; "
            "[ledger.record(ModelCallEvent(invocation_id=prefix+str(i), "
            "provider='ollama', model='qwen3:8b', tier=ModelTier.LOCAL, "
            "harness='process-test', source='test')) for i in range(5)]"
        )
        processes.append(
            subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stdout + stderr

    assert ModelUsageLedger(tmp_path).report().total_calls == 20


def test_codex_find_prefers_native_exe_over_cmd(tmp_path: Path, monkeypatch) -> None:
    fake_cmd = tmp_path / "codex.cmd"
    fake_cmd.write_text("@echo off", encoding="utf-8")

    monkeypatch.setattr("core.usage.adapters.shutil.which", lambda _: str(fake_cmd))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Place fake_exe in ~/.codex/.sandbox-bin/codex.exe
    target = tmp_path / ".codex" / ".sandbox-bin" / "codex.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("binary", encoding="utf-8")

    resolved = CodexAccountAdapter._find_codex()
    assert resolved == str(target)


def test_subprocess_creationflags_applied_on_windows(monkeypatch) -> None:
    recorded_flags = []

    def fake_run(*args, **kwargs):
        recorded_flags.append(kwargs.get("creationflags"))
        class FakeResult:
            returncode = 0
            stdout = '{"checks": {"auth.credentials": {"status": "ok", "summary": "auth is configured"}}}'
        return FakeResult()

    monkeypatch.setattr("core.usage.adapters.subprocess.run", fake_run)
    monkeypatch.setattr("core.usage.adapters.os.name", "nt")

    CodexAccountAdapter._codex_doctor("codex.exe")
    expected_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert len(recorded_flags) == 1
    assert recorded_flags[0] == expected_flag


def test_grok_probe_cli_session_handles_supergrok_without_forcing_zero_and_refreshes(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import GrokAccountAdapter
    auth_dir = tmp_path / ".grok"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_file = auth_dir / "auth.json"
    auth_file.write_text(json.dumps({
        "default": {
            "key": "expired_token",
            "refresh_token": "valid_refresh",
            "oidc_issuer": "https://auth.example.com",
            "oidc_client_id": "client_123",
            "expires_at": "2020-01-01T00:00:00Z",
        }
    }), encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    refresh_called = []
    user_called = []

    class DummyResponse:
        def __init__(self, data: bytes):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "oauth2/token" in url:
            refresh_called.append(url)
            return DummyResponse(json.dumps({
                "access_token": "refreshed_access_token",
                "expires_in": 3600,
            }).encode("utf-8"))
        if "cli-chat-proxy.grok.com" in url:
            user_called.append(req.headers.get("Authorization"))
            return DummyResponse(json.dumps({
                "email": "user@example.com",
                "hasGrokCodeAccess": True,
            }).encode("utf-8"))
        raise RuntimeError(f"Unexpected url {url}")

    monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", fake_urlopen)

    spec = ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://grok.com/?_s=usage")
    adapter = GrokAccountAdapter(spec, tmp_path / "no_snapshot")
    usage = adapter._probe_grok_cli_session()

    assert usage is not None
    assert usage.status == AccountConnectionStatus.CONNECTED
    assert usage.plan == "SuperGrok"
    assert usage.quota_supported is False
    assert usage.windows == []
    assert "não expõe medidor percentual numérico" in usage.message
    assert len(refresh_called) == 1
    assert len(user_called) == 1
    assert user_called[0] == "Bearer refreshed_access_token"


def test_grok_adapter_probes_bot_session_live_percentage(tmp_path: Path, monkeypatch) -> None:
    from core.usage.adapters import GrokAccountAdapter

    class DummyResponse:
        def __init__(self, data: bytes):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    bot_payload = {
        "currentPeriodStart": "2026-09-08T22:15:57.642Z",
        "nextResetTimestampUtc": "2026-09-15T22:15:57.642Z",
        "usagePercent": 2.141932,
        "hasAvailableUsage": True,
        "hasNonZeroIncludedLimit": True,
        "grokPlanLabel": "SuperGrok",
    }

    monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda *args: "mock_jwt_token")
    monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
    monkeypatch.setattr(
        "core.usage.adapters.urllib.request.urlopen",
        lambda req, *args, **kwargs: DummyResponse(json.dumps(bot_payload).encode("utf-8")),
    )

    spec = ProviderSpec("xai", "xAI / Grok", ProviderFamily.FRONTIER, "https://grok.com/?_s=usage")
    adapter = GrokAccountAdapter(spec, tmp_path / "no_snapshot")
    result = adapter.inspect()

    assert result.status == AccountConnectionStatus.CONNECTED
    assert result.adapter == "grok_bot_api"
    assert result.plan == "SuperGrok"
    assert result.quota_supported is True
    assert len(result.windows) == 2
    assert result.windows[0].quota_id == "grok:weekly_pool"
    assert result.windows[0].used_percent == 97.86
    assert result.windows[0].remaining_percent == 2.14
    assert result.windows[0].resets_at == "2026-09-15T22:15:57.642Z"
    assert result.windows[1].quota_id == "grok:5h"
    assert "Pool semanal e janela móvel" in result.message


