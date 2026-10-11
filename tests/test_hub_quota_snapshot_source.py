"""USR-232 (USR-206, slice 1): the Hub reads synchronized quota snapshots instead of probing CLIs.

The Hub container on the VPS has no Claude, Codex, Grok or Antigravity CLI, so probing them there only
reports "not installed". The metering host (the Notebook) posts its real readings to
`POST /api/usage/sync`; with `DARKHUB_QUOTA_SOURCE=snapshot` the Hub shows exactly those, and with
`DARKHUB_USAGE_DIR` pointing into the named volume they survive a redeploy.

Reachability Contract: python -m pytest tests/test_hub_quota_snapshot_source.py -q
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient

import core.usage.monitor as monitor_module
from core.line.routing import _default_quota_headroom
from core.router.token_budget import _quota_headroom
from core.usage.adapters import (
    DEFAULT_PROVIDER_SPECS,
    SYNCED_SNAPSHOT_PROVIDER_IDS,
    ClaudeCodeAccountAdapter,
    CodexAccountAdapter,
    EnvironmentAccountAdapter,
    GeminiAccountAdapter,
    GrokAccountAdapter,
    OllamaAccountAdapter,
    ProviderSpec,
    SyncedSnapshotAdapter,
    build_default_adapters,
    build_synced_snapshot_adapters,
)
from core.usage.history import record_probe
from core.usage.models import AccountConnectionStatus, ProviderAccountUsage, ProviderFamily, QuotaWindow
from core.usage.monitor import AccountUsageMonitor
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.models import UsageSyncPayload
from hub.backend.service import HubService, resolve_quota_source

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "usage"
MEASURED_PROVIDERS = ("anthropic", "openai", "xai", "google")
SPECS = {spec.provider_id: spec for spec in DEFAULT_PROVIDER_SPECS}


@pytest.fixture(autouse=True)
def _isolate_hub_quota_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the host machine exports may change which source or directory the Hub uses."""
    for name in ("DARKHUB_QUOTA_SOURCE", "DARKHUB_USAGE_DIR", "DARKFAC_PROJECT_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for provider_id in MEASURED_PROVIDERS:
        monkeypatch.delenv(f"DARKFAC_{provider_id.upper()}_USAGE_JSON", raising=False)


@contextmanager
def probes_forbidden(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[str]]:
    """Record (and refuse) every way an adapter could reach a CLI, a process or the network."""
    calls: List[str] = []

    def trap(name: str):
        def _trap(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"unexpected probe: {name}")

        return _trap

    with monkeypatch.context() as patch:
        patch.setattr("subprocess.run", trap("subprocess.run"))
        patch.setattr("subprocess.Popen", trap("subprocess.Popen"))
        patch.setattr("shutil.which", trap("shutil.which"))
        patch.setattr("urllib.request.urlopen", trap("urllib.request.urlopen"))
        yield calls


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def host_reading(
    provider_id: str,
    remaining: float = 53.0,
    *,
    checked_at: Optional[str] = None,
    status: AccountConnectionStatus = AccountConnectionStatus.CONNECTED,
) -> Dict[str, Any]:
    """What the metering host posts for one provider: a `ProviderAccountUsage` dumped to JSON."""
    spec = SPECS[provider_id]
    usage = ProviderAccountUsage(
        provider_id=provider_id,
        provider_name=spec.provider_name,
        family=spec.family,
        status=status,
        adapter="host_probe",
        plan="Plus",
        quota_supported=True,
        windows=[
            QuotaWindow(
                quota_id=f"{provider_id}:weekly",
                label="1 semana",
                used_percent=round(100.0 - remaining, 2),
                remaining_percent=remaining,
                window_duration_minutes=10080,
                resets_at="2099-01-01T00:00:00+00:00",
            )
        ],
        message="Leitura do host medidor.",
        dashboard_url=spec.dashboard_url,
        checked_at=checked_at or _now(),
    )
    return usage.model_dump(mode="json")


def make_service(tmp_path: Path, name: str = "hub", usage_dir: Optional[Path] = None) -> HubService:
    """A HubService whose container-local files live under `tmp_path/name` and nowhere else."""
    root = tmp_path / name
    data_dir = root / "data"
    (data_dir / "demands").mkdir(parents=True, exist_ok=True)
    (data_dir / "demands" / "demands.json").write_text("[]", encoding="utf-8")
    service = HubService(data_dir=data_dir, usage_dir=usage_dir, project_root=root)
    # The metering host also syncs Ollama; with a snapshot the Ollama adapter has nothing to probe.
    service.account_usage_monitor.save_snapshot(
        "ollama",
        {"status": "connected", "plan": "local $0", "message": "Ollama sincronizado.", "checked_at": _now()},
    )
    return service


def account_for(report: Any, provider_id: str) -> ProviderAccountUsage:
    return next(item for item in report.accounts if item.provider_id == provider_id)


def lowest_remaining(account: ProviderAccountUsage) -> Optional[float]:
    known = [w.remaining_percent for w in account.windows if w.remaining_percent is not None]
    return min(known) if known else None


# --------------------------------------------------------------------------------------------
# Source selection
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [(None, "probe"), ("", "probe"), ("probe", "probe"), ("snapshot", "snapshot"), ("  SnapShot ", "snapshot")],
)
def test_resolve_quota_source(monkeypatch: pytest.MonkeyPatch, raw: Optional[str], expected: str) -> None:
    if raw is not None:
        monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", raw)
    assert resolve_quota_source() == expected


def test_invalid_quota_source_falls_back_to_probe_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "cloud")
    with caplog.at_level(logging.WARNING, logger="darkhub.service"):
        assert resolve_quota_source() == "probe"
    assert "DARKHUB_QUOTA_SOURCE" in caplog.text


def test_default_hub_keeps_probing_adapters(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    assert service.quota_source == "probe"
    by_id = {adapter.spec.provider_id: type(adapter) for adapter in service.account_usage_monitor.adapters}
    assert by_id["openai"] is CodexAccountAdapter
    assert by_id["xai"] is GrokAccountAdapter
    assert by_id["google"] is GeminiAccountAdapter
    assert by_id["anthropic"] is ClaudeCodeAccountAdapter
    assert SyncedSnapshotAdapter not in by_id.values()


def test_snapshot_mode_uses_snapshot_only_adapters_for_measured_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    service = make_service(tmp_path)

    assert service.quota_source == "snapshot"
    by_id = {adapter.spec.provider_id: type(adapter) for adapter in service.account_usage_monitor.adapters}
    assert {pid for pid, cls in by_id.items() if cls is SyncedSnapshotAdapter} == set(MEASURED_PROVIDERS)
    assert by_id["ollama"] is OllamaAccountAdapter
    assert by_id["openrouter"] is EnvironmentAccountAdapter


def test_synced_builder_keeps_provider_ids_and_order_of_the_default_builder(tmp_path: Path) -> None:
    default = list(build_default_adapters(tmp_path))
    synced = list(build_synced_snapshot_adapters(tmp_path))

    assert [a.spec for a in synced] == [a.spec for a in default]
    assert SYNCED_SNAPSHOT_PROVIDER_IDS == frozenset(MEASURED_PROVIDERS)
    for default_adapter, synced_adapter in zip(default, synced):
        if synced_adapter.spec.provider_id in SYNCED_SNAPSHOT_PROVIDER_IDS:
            assert isinstance(synced_adapter, SyncedSnapshotAdapter)
        else:
            assert type(synced_adapter) is type(default_adapter)


# --------------------------------------------------------------------------------------------
# Usage directory resolution and persistence
# --------------------------------------------------------------------------------------------


def test_usage_dir_explicit_argument_beats_env_and_env_beats_data_dir_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit_usage"
    from_env = tmp_path / "volume" / "usage"
    data_dir = tmp_path / "container" / "data"
    (data_dir / "demands").mkdir(parents=True)
    (data_dir / "demands" / "demands.json").write_text("[]", encoding="utf-8")

    default_service = HubService(data_dir=data_dir, project_root=tmp_path / "container")
    assert default_service.usage_dir == data_dir / "usage"

    monkeypatch.setenv("DARKHUB_USAGE_DIR", str(from_env))
    env_service = HubService(data_dir=data_dir, project_root=tmp_path / "container")
    assert env_service.usage_dir == from_env
    assert env_service.account_usage_monitor.snapshot_dir == from_env / "providers"

    explicit_service = HubService(data_dir=data_dir, usage_dir=explicit, project_root=tmp_path / "container")
    assert explicit_service.usage_dir == explicit
    assert explicit_service.account_usage_monitor.snapshot_dir == explicit / "providers"


def test_usage_dir_from_env_is_created_with_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "volume" / "nested" / "usage"
    monkeypatch.setenv("DARKHUB_USAGE_DIR", str(target))
    assert not target.exists()

    make_service(tmp_path)

    assert target.is_dir()


def test_synced_data_survives_a_redeploy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new container (empty data_dir) on the same named volume must still see the snapshots."""
    volume_usage = tmp_path / "darkhub-hub-data" / "usage"
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    monkeypatch.setenv("DARKHUB_USAGE_DIR", str(volume_usage))

    first = make_service(tmp_path, name="container-1")
    first.sync_usage_data(
        UsageSyncPayload(client_node_id="notebook", accounts=[host_reading("anthropic", remaining=13.0)])
    )
    first.api_credits_monitor.save_snapshot(
        "openrouter", {"status": "active", "is_connected": True, "available_credit_usd": 42.5}
    )
    assert (volume_usage / "providers" / "anthropic.json").is_file()

    second = make_service(tmp_path, name="container-2")
    claude = account_for(second.get_account_usage(force=True), "anthropic")

    assert claude.status == AccountConnectionStatus.CONNECTED
    assert claude.adapter == "synced_snapshot"
    assert lowest_remaining(claude) == 13.0
    # The other stores that live under usage_dir keep working on the same directory.
    credit = next(c for c in second.get_api_credits_report(force=False).accounts if c.provider_id == "openrouter")
    assert credit.available_credit_usd == 42.5
    assert second.model_usage_ledger.storage_dir == volume_usage


def test_two_monitor_instances_share_the_persisted_snapshot(tmp_path: Path) -> None:
    providers = tmp_path / "volume" / "usage" / "providers"
    writer = AccountUsageMonitor(providers, adapters=build_synced_snapshot_adapters(providers))
    writer.save_snapshot("openai", host_reading("openai", remaining=47.0))

    reader = AccountUsageMonitor(providers, adapters=build_synced_snapshot_adapters(providers))
    codex = account_for(reader.inspect(force=True), "openai")

    assert codex.status == AccountConnectionStatus.CONNECTED
    assert lowest_remaining(codex) == 47.0


def test_save_snapshot_replaces_atomically_and_leaves_no_temp_file(tmp_path: Path) -> None:
    providers = tmp_path / "providers"
    monitor = AccountUsageMonitor(providers, adapters=[])
    monitor.save_snapshot("xai", host_reading("xai", remaining=90.0))
    monitor.save_snapshot("xai", host_reading("xai", remaining=80.0))

    assert sorted(path.name for path in providers.iterdir()) == ["xai.json"]
    assert json.loads((providers / "xai.json").read_text(encoding="utf-8"))["windows"][0]["remaining_percent"] == 80.0


def test_failed_replace_keeps_previous_snapshot_and_cleans_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = tmp_path / "providers"
    monitor = AccountUsageMonitor(providers, adapters=[])
    monitor.save_snapshot("xai", host_reading("xai", remaining=90.0))

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(monitor_module.os, "replace", boom)
        with pytest.raises(OSError):
            monitor.save_snapshot("xai", host_reading("xai", remaining=10.0))

    assert sorted(path.name for path in providers.iterdir()) == ["xai.json"]
    assert json.loads((providers / "xai.json").read_text(encoding="utf-8"))["windows"][0]["remaining_percent"] == 90.0


def test_replace_denied_by_a_reader_falls_back_to_direct_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to replace a file another process has open; the sync must still land."""
    providers = tmp_path / "providers"
    monitor = AccountUsageMonitor(providers, adapters=[])
    monitor.save_snapshot("xai", host_reading("xai", remaining=90.0))

    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("file in use")

    with monkeypatch.context() as patch:
        patch.setattr(monitor_module.os, "replace", denied)
        patch.setattr(monitor_module.time, "sleep", lambda _seconds: None)
        monitor.save_snapshot("xai", host_reading("xai", remaining=70.0))

    assert sorted(path.name for path in providers.iterdir()) == ["xai.json"]
    assert json.loads((providers / "xai.json").read_text(encoding="utf-8"))["windows"][0]["remaining_percent"] == 70.0


# --------------------------------------------------------------------------------------------
# Snapshot-only behavior
# --------------------------------------------------------------------------------------------


def test_missing_snapshot_is_unknown_and_distinct_from_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    # Credentials on this machine must not turn "no data" into a guess.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(key, "not-a-real-key")
    service = make_service(tmp_path)

    with probes_forbidden(monkeypatch) as calls:
        report = service.get_account_usage(force=True)

    assert calls == []
    for provider_id in MEASURED_PROVIDERS:
        account = account_for(report, provider_id)
        assert account.status == AccountConnectionStatus.UNKNOWN
        assert account.status != AccountConnectionStatus.DISCONNECTED
        assert account.adapter == "synced_snapshot"
        assert account.message == "Sem dado sincronizado do host medidor."
        assert account.quota_supported is False
        assert account.windows == []
        assert account.dashboard_url == SPECS[provider_id].dashboard_url
    assert not any(
        item.status == AccountConnectionStatus.DISCONNECTED
        for item in report.accounts
        if item.provider_id in MEASURED_PROVIDERS
    )


def test_synced_snapshot_is_the_source_whatever_its_age_and_nothing_is_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    service = make_service(tmp_path)
    old = "2026-10-01T10:00:00+00:00"
    service.sync_usage_data(
        UsageSyncPayload(
            client_node_id="notebook",
            accounts=[host_reading(pid, remaining=60.0, checked_at=old) for pid in MEASURED_PROVIDERS],
        )
    )

    with probes_forbidden(monkeypatch) as calls:
        report = service.get_account_usage(force=True)

    assert calls == []
    for provider_id in MEASURED_PROVIDERS:
        account = account_for(report, provider_id)
        assert account.status == AccountConnectionStatus.CONNECTED
        assert account.adapter == "synced_snapshot"
        assert account.checked_at == old  # age is shown by a later slice; never refreshed here
        assert lowest_remaining(account) == 60.0
        assert account.message == "Leitura do host medidor."


def test_force_inspect_only_rereads_the_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    service = make_service(tmp_path)
    service.sync_usage_data(UsageSyncPayload(accounts=[host_reading("google", remaining=100.0)]))
    assert lowest_remaining(account_for(service.get_account_usage(force=True), "google")) == 100.0

    # Another process (a second worker, a manual copy) rewrites the file behind the Hub's cache.
    other = AccountUsageMonitor(service.account_usage_monitor.snapshot_dir, adapters=[])
    other.save_snapshot("google", host_reading("google", remaining=15.0))

    with probes_forbidden(monkeypatch) as calls:
        report = service.get_account_usage(force=True)

    assert calls == []
    assert lowest_remaining(account_for(report, "google")) == 15.0


def test_unreadable_snapshot_is_reported_as_no_synced_data(tmp_path: Path) -> None:
    providers = tmp_path / "providers"
    providers.mkdir()
    (providers / "anthropic.json").write_text("{not json", encoding="utf-8")
    adapter = next(a for a in build_synced_snapshot_adapters(providers) if a.spec.provider_id == "anthropic")

    account = adapter.inspect(force=True)

    assert account.status == AccountConnectionStatus.UNKNOWN
    assert account.adapter == "synced_snapshot"


def test_sync_endpoint_to_accounts_endpoint_in_snapshot_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "usr-232-telemetry-key")
    service = make_service(tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        sync = client.post(
            "/api/usage/sync",
            json={"client_node_id": "notebook", "accounts": [host_reading("openai", remaining=47.0)]},
            headers={"X-DarkFac-Telemetry-Key": "usr-232-telemetry-key"},
        )
        accounts = client.get("/api/usage/accounts", params={"refresh": "true"})
    finally:
        app.dependency_overrides.clear()

    assert sync.status_code == 200
    assert sync.json()["accounts_updated"] == 1
    assert accounts.status_code == 200
    by_id = {item["provider_id"]: item for item in accounts.json()["accounts"]}
    assert by_id["openai"]["status"] == "connected"
    assert by_id["openai"]["adapter"] == "synced_snapshot"
    assert by_id["openai"]["windows"][0]["remaining_percent"] == 47.0
    assert by_id["anthropic"]["status"] == "unknown"
    assert by_id["anthropic"]["message"] == "Sem dado sincronizado do host medidor."


# --------------------------------------------------------------------------------------------
# Contract: the Hub shows the router's headroom, from the real captured fixtures
# --------------------------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _SummaryResponse(_Response):
    status = 200


class _HeaderResponse:
    def __init__(self, headers: Any) -> None:
        self.headers = headers

    def __enter__(self) -> "_HeaderResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def metered_usage_from_fixture(provider_id: str, fixture_file: str, host_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ProviderAccountUsage:
    """The `ProviderAccountUsage` the metering host builds from a real captured source payload.

    Same conversion path as `tests/test_quota_meter_contract.py`, so no number is invented here.
    """
    payload = json.loads((FIXTURES_DIR / fixture_file).read_text(encoding="utf-8"))["payload"]
    spec = SPECS[provider_id]
    usage: Optional[ProviderAccountUsage]

    if provider_id == "anthropic":
        monkeypatch.setattr(
            ClaudeCodeAccountAdapter, "_extract_claude_oauth_info", lambda self: ("fixture-token", None, "Pro")
        )
        monkeypatch.setattr(
            "core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _HeaderResponse(payload["headers"])
        )
        usage = ClaudeCodeAccountAdapter(spec, host_dir)._probe_claude_unified_ratelimits()
    elif provider_id == "openai":
        adapter = CodexAccountAdapter(spec, host_dir)
        usage = record_probe(adapter._from_codex_response(payload), host_dir)
    elif provider_id == "google":
        monkeypatch.setattr(GeminiAccountAdapter, "_find_live_ls_credentials", lambda self: ("fixture-csrf", [12345]))
        monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _SummaryResponse(payload))
        usage = GeminiAccountAdapter(spec, host_dir)._probe_language_server()
    elif provider_id == "xai":
        monkeypatch.setattr(GrokAccountAdapter, "_extract_grok_bot_token", lambda self: "fixture-token")
        monkeypatch.setattr(GrokAccountAdapter, "_probe_grok_cli_session", lambda self: None)
        monkeypatch.setattr("core.usage.adapters.urllib.request.urlopen", lambda *a, **k: _Response(payload))
        usage = GrokAccountAdapter(spec, host_dir)._probe_grok_bot_session()
    else:  # pragma: no cover - guarded by the parametrization
        raise ValueError(provider_id)

    assert usage is not None
    return usage


@pytest.mark.parametrize(
    "provider_id,fixture_file,documented_headroom",
    [
        ("anthropic", "anthropic_2026-10-01.json", 13.0),
        ("openai", "openai_2026-10-01.json", 47.0),
        ("google", "google_2026-10-01.json", 15.0),
        ("xai", "xai_2026-10-01.json", 98.6),
    ],
)
def test_hub_headroom_equals_router_headroom_for_the_same_real_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    fixture_file: str,
    documented_headroom: float,
) -> None:
    # 1. The metering host measures the real captured payload and builds its sync payload.
    usage = metered_usage_from_fixture(provider_id, fixture_file, tmp_path / "metering_host" / "providers", monkeypatch)
    assert usage.status == AccountConnectionStatus.CONNECTED
    account = usage.model_dump(mode="json")
    account["checked_at"] = _now()  # the host posts a reading taken now; the fixture only fixes the numbers

    # 2. The Hub (production mode) ingests it through the sync path; the router reads the SAME file.
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_dir))
    service = make_service(tmp_path, usage_dir=state_dir / "usage")
    service.sync_usage_data(UsageSyncPayload(client_node_id="notebook", accounts=[account]))
    snapshot_file = state_dir / "usage" / "providers" / f"{provider_id}.json"
    assert snapshot_file.is_file()

    hub_account = account_for(service.get_account_usage(force=True), provider_id)
    with probes_forbidden(monkeypatch) as calls:
        router_headroom = _default_quota_headroom(provider_id)

    # 3. Identical headroom: the lowest remaining percent among the windows.
    assert calls == [], "the router had to probe instead of reading the synced snapshot"
    assert router_headroom is not None, "the router could not use the synced snapshot"
    assert hub_account.adapter == "synced_snapshot"
    assert hub_account.status == AccountConnectionStatus.CONNECTED
    assert lowest_remaining(hub_account) == router_headroom
    assert _quota_headroom(hub_account) == router_headroom
    assert router_headroom == documented_headroom
