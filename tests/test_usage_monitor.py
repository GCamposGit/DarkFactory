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
    assert result.quota_supported is True
    assert [window.window_duration_minutes for window in result.windows] == [300, 10_080]
    assert [window.remaining_percent for window in result.windows] == [59.0, 69.0]
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
        "https://console.x.ai/",
        ("XAI_API_KEY",),
    )
    xai = __import__("core.usage.adapters", fromlist=["GrokAccountAdapter"]).GrokAccountAdapter(
        xai_spec,
        tmp_path,
    ).inspect()

    assert openai.status == AccountConnectionStatus.CONNECTED
    assert openai.adapter == "openai_api_key"
    assert openai.quota_supported is False
    assert xai.status == AccountConnectionStatus.CONNECTED
    assert xai.adapter == "xai_api_key"
    assert xai.quota_supported is False

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
