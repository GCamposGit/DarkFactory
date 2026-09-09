"""
Reachability Contract & Unit Tests for Ticket USR-01.

Demanda: AI Account Monitor e monitor de Créditos não atualizam no darkhub 24/7.
Reachability Contract: python -m pytest tests/test_ai_account_monitor_e_moni.py -v
"""

import json
from pathlib import Path
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient

from core.usage.adapters import OllamaAccountAdapter, ProviderSpec
from core.usage.models import AccountConnectionStatus, ProviderFamily
from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import ApiCreditsMonitor, CreditAccountStatus
from hub.backend.main import app
from hub.backend.api import get_hub_service
from hub.backend.service import HubService
from scripts.sync_usage_to_cloud import collect_local_usage_payload, send_sync_payload


@pytest.fixture
def isolated_hub_service(tmp_path: Path) -> HubService:
    """Creates an isolated HubService with sandbox storage paths."""
    data_dir = tmp_path / "hub_data"
    usage_dir = tmp_path / "usage"
    demands_dir = tmp_path / "demands"
    data_dir.mkdir(parents=True, exist_ok=True)
    usage_dir.mkdir(parents=True, exist_ok=True)
    demands_dir.mkdir(parents=True, exist_ok=True)

    # Initialize minimal demands.json
    (demands_dir / "demands.json").write_text("[]", encoding="utf-8")

    service = HubService(
        data_dir=data_dir,
        usage_dir=usage_dir,
        project_root=tmp_path,
    )
    return service


def test_account_usage_monitor_save_snapshot(tmp_path: Path):
    """Verifies that AccountUsageMonitor persists snapshots and clears in-memory cache."""
    providers_dir = tmp_path / "providers"
    monitor = AccountUsageMonitor(providers_dir)

    # Initially empty (deepseek is disconnected when not configured)
    report1 = monitor.inspect(force=True)
    deepseek_acc = next(a for a in report1.accounts if a.provider_id == "deepseek")
    assert deepseek_acc.status == AccountConnectionStatus.DISCONNECTED

    # Save snapshot
    monitor.save_snapshot(
        "deepseek",
        {
            "status": "connected",
            "plan": "DeepSeek Pro",
            "account_label": "user@test.org",
            "windows": [
                {
                    "quota_id": "deepseek:daily",
                    "label": "Limite Diário",
                    "used_percent": 25.0,
                    "remaining_percent": 75.0,
                    "window_duration_minutes": 1440,
                }
            ],
            "message": "Snapshot de teste persistido.",
        },
    )

    # Re-inspect should load from saved snapshot immediately
    report2 = monitor.inspect(force=True)
    deepseek_updated = next(a for a in report2.accounts if a.provider_id == "deepseek")
    assert deepseek_updated.status == AccountConnectionStatus.CONNECTED
    assert deepseek_updated.plan == "DeepSeek Pro"
    assert len(deepseek_updated.windows) == 1
    assert deepseek_updated.windows[0].used_percent == 25.0


def test_ollama_adapter_with_snapshot_and_custom_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Verifies that OllamaAccountAdapter checks snapshots first and honors OLLAMA_BASE_URL."""
    spec = ProviderSpec("ollama", "Ollama Local", ProviderFamily.LOCAL, "http://localhost:11434")
    adapter = OllamaAccountAdapter(spec, tmp_path)

    # 1. With snapshot present: loads from snapshot
    snapshot_file = tmp_path / "ollama.json"
    snapshot_file.write_text(
        json.dumps({
            "status": "connected",
            "plan": "local $0",
            "message": "Cluster RTX 4070 sincronizado via snapshot.",
        }),
        encoding="utf-8",
    )
    res1 = adapter.inspect()
    assert res1.status == AccountConnectionStatus.CONNECTED
    assert "RTX 4070" in res1.message

    # 2. Without snapshot, when server is offline:
    snapshot_file.unlink()
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://100.81.84.124:11434")
    res2 = adapter.inspect()
    # Host is offline in unit test environment, fails gracefully to disconnected without throwing exception
    assert res2.status == AccountConnectionStatus.DISCONNECTED
    assert res2.provider_id == "ollama"


def test_usage_sync_endpoint_auth_fail_closed(isolated_hub_service: HubService, monkeypatch: pytest.MonkeyPatch):
    """Enforces fail-closed security for POST /api/usage/sync when key is missing or invalid."""
    app.dependency_overrides[get_hub_service] = lambda: isolated_hub_service
    client = TestClient(app)

    payload = {
        "client_node_id": "test-workstation",
        "accounts": [{"provider_id": "openai", "status": "connected"}],
        "credits": [{"provider_id": "openrouter", "status": "active"}],
    }

    # 1. When DARKFAC_TELEMETRY_KEY is not set on the server -> 503 Service Unavailable
    monkeypatch.delenv("DARKFAC_TELEMETRY_KEY", raising=False)
    resp1 = client.post("/api/usage/sync", json=payload)
    assert resp1.status_code == 503
    assert "telemetry" in resp1.text.lower() or "disabled" in resp1.text.lower()

    # 2. When server has key, but client sends wrong or missing key -> 401 Unauthorized
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "secret-key-12345")
    resp2 = client.post("/api/usage/sync", json=payload)
    assert resp2.status_code == 401

    resp3 = client.post(
        "/api/usage/sync",
        json=payload,
        headers={"X-DarkFac-Telemetry-Key": "wrong-secret"},
    )
    assert resp3.status_code == 401

    app.dependency_overrides.clear()


def test_usage_sync_endpoint_ingests_and_updates_reports(isolated_hub_service: HubService, monkeypatch: pytest.MonkeyPatch):
    """Verifies that POST /api/usage/sync ingests accounts and credits, updating reports."""
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "prod-telemetry-secret-xyz")
    app.dependency_overrides[get_hub_service] = lambda: isolated_hub_service
    client = TestClient(app)

    sync_payload = {
        "client_node_id": "predator-neo-16",
        "accounts": [
            {
                "provider_id": "openai",
                "status": "connected",
                "plan": "ChatGPT Plus",
                "account_label": "…0d64df",
                "windows": [
                    {
                        "quota_id": "codex:weekly",
                        "label": "Limite Semanal (1 semana)",
                        "used_percent": 12.5,
                        "remaining_percent": 87.5,
                        "window_duration_minutes": 10080,
                    }
                ],
                "message": "Sincronizado da estação de trabalho.",
            },
            {
                "provider_id": "ollama",
                "status": "connected",
                "plan": "local $0",
                "message": "Cluster RTX 4070 online com 8 modelos.",
            }
        ],
        "credits": [
            {
                "provider_id": "openrouter",
                "status": "active",
                "is_connected": True,
                "available_credit_usd": 42.50,
                "current_month_spend_usd": 7.50,
                "notes": "Sincronizado via token local.",
            }
        ],
    }

    # 1. Post sync payload
    sync_resp = client.post(
        "/api/usage/sync",
        json=sync_payload,
        headers={"X-DarkFac-Telemetry-Key": "prod-telemetry-secret-xyz"},
    )
    assert sync_resp.status_code == 200
    data = sync_resp.json()
    assert data["status"] == "synchronized"
    assert data["accounts_updated"] == 2
    assert data["credits_updated"] == 1
    assert data["client_node_id"] == "predator-neo-16"

    # 2. Verify /api/usage/accounts reflects synced accounts
    accounts_resp = client.get("/api/usage/accounts")
    assert accounts_resp.status_code == 200
    acc_data = accounts_resp.json()
    openai_acc = next(a for a in acc_data["accounts"] if a["provider_id"] == "openai")
    assert openai_acc["status"] == "connected"
    assert openai_acc["plan"] == "ChatGPT Plus"
    assert openai_acc["account_label"] == "…0d64df"
    assert len(openai_acc["windows"]) == 1

    ollama_acc = next(a for a in acc_data["accounts"] if a["provider_id"] == "ollama")
    assert ollama_acc["status"] == "connected"
    assert "RTX 4070" in ollama_acc["message"]

    # 3. Verify /api/credits reflects synced credits
    credits_resp = client.get("/api/credits")
    assert credits_resp.status_code == 200
    cred_data = credits_resp.json()
    openrouter_cred = next(c for c in cred_data["accounts"] if c["provider_id"] == "openrouter")
    assert openrouter_cred["is_connected"] is True
    assert openrouter_cred["available_credit_usd"] == 42.50
    assert openrouter_cred["current_month_spend_usd"] == 7.50

    app.dependency_overrides.clear()


def test_sync_usage_client_script_dry_run(tmp_path: Path):
    """Verifies that collect_local_usage_payload builds a compliant sync payload."""
    payload = collect_local_usage_payload(tmp_path, node_id="test-box", force_probe=False)
    assert payload["client_node_id"] == "test-box"
    assert "timestamp" in payload
    assert isinstance(payload["accounts"], list)
    assert isinstance(payload["credits"], list)
