"""Tests for HF-14: Telegram Owner Pairing, Deduplication, and n8n Community Integration.

Validates all invariants:
- Strict authorization of users and chats
- Durable offset tracking and duplicate event idempotency (Scenario G5)
- Demand intake and Grill resolution via Telegram (Scenario G1)
- Release approval recording client acceptance (Scenario G8)
- Telegram outage non-blocking resilience and outbox queuing
- Secret redaction in messages and workflow JSON
- n8n preflight distinguishing generic links from real instances
- Docker Compose generation for Community self-hosted edition
- Headless CLI commands and DarkHub API endpoints
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from core.integrations.n8n import (
    N8nConfig,
    N8nManifestGenerator,
    N8nProbe,
    N8nWorkflowManager,
)
from core.integrations.telegram import (
    TelegramActionType,
    TelegramConfig,
    TelegramGateway,
    redact_secrets,
)
from core.integrations.telegram_n8n_cli import main as cli_main
from hub.backend.main import app


@pytest.fixture
def temp_telegram_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "telegram_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


@pytest.fixture
def auth_config() -> TelegramConfig:
    return TelegramConfig(
        bot_token="test_mock_token_12345",
        authorized_user_ids=[1001, 1002],
        authorized_chat_ids=[5001],
        webhook_secret_token="super_secret_webhook_token",
    )


def test_telegram_auth_accepts_authorized_user(temp_telegram_dir: Path, auth_config: TelegramConfig) -> None:
    """Authorized user is allowed to interact with DarkFac."""
    gateway = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir)
    update_data = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 1001, "first_name": "Owner"},
            "chat": {"id": 5001, "type": "private"},
            "date": 1700000000,
            "text": "/start",
        },
    }
    result = gateway.process_update(update_data)
    assert result.authorized is True
    assert result.action == TelegramActionType.START
    assert "Dark Factory" in result.response_text


def test_telegram_auth_rejects_unauthorized_user(temp_telegram_dir: Path, auth_config: TelegramConfig) -> None:
    """Unauthorized user is rejected fail-closed."""
    gateway = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir)
    update_data = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "from": {"id": 9999, "first_name": "Intruder"},
            "chat": {"id": 9999, "type": "private"},
            "date": 1700000001,
            "text": "/start",
        },
    }
    result = gateway.process_update(update_data)
    assert result.authorized is False
    assert result.action == TelegramActionType.UNAUTHORIZED
    assert "Access Denied" in result.response_text


def test_telegram_deduplication_ignores_duplicate_update_id(
    temp_telegram_dir: Path,
    auth_config: TelegramConfig,
) -> None:
    """Scenario G5: Duplicate update_id is processed once and ignored subsequently."""
    call_count = 0

    def mock_demand(text: str, user_id: int) -> Dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ticket_id": f"TICKET-{call_count}"}

    gateway = TelegramGateway(
        config=auth_config,
        state_dir=temp_telegram_dir,
        demand_handler=mock_demand,
    )

    payload = {
        "update_id": 42,
        "message": {
            "message_id": 20,
            "from": {"id": 1001},
            "chat": {"id": 5001},
            "date": 1700000002,
            "text": "/demand Add caching",
        },
    }

    res1 = gateway.process_update(payload)
    assert res1.duplicate is False
    assert res1.target_id == "TICKET-1"
    assert call_count == 1

    # Re-delivering exact same update
    res2 = gateway.process_update(payload)
    assert res2.duplicate is True
    assert call_count == 1  # handler not re-triggered


def test_telegram_durable_offset_advances_and_persists(
    temp_telegram_dir: Path,
    auth_config: TelegramConfig,
) -> None:
    """Offset advances with updates and survives service restart."""
    gw1 = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir)
    gw1.process_update({
        "update_id": 150,
        "message": {
            "message_id": 30,
            "from": {"id": 1001},
            "chat": {"id": 5001},
            "date": 1700000003,
            "text": "/status",
        },
    })
    assert gw1.last_offset == 151

    # Restart gateway reading same state directory
    gw2 = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir)
    assert gw2.last_offset == 151
    assert 150 in gw2.processed_update_ids


def test_telegram_demand_command_creates_ticket(temp_telegram_dir: Path, auth_config: TelegramConfig) -> None:
    """Demand command triggers intake handler and returns ticket ID."""
    recorded_text = ""

    def mock_demand(text: str, user_id: int) -> Dict[str, Any]:
        nonlocal recorded_text
        recorded_text = text
        return {"ticket_id": "DF-USR-99"}

    gateway = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir, demand_handler=mock_demand)
    result = gateway.process_update({
        "update_id": 300,
        "message": {
            "message_id": 40,
            "from": {"id": 1001},
            "chat": {"id": 5001},
            "date": 1700000004,
            "text": "/demand Implement DarkHub WebSocket stream",
        },
    })
    assert result.action == TelegramActionType.DEMAND
    assert result.target_id == "DF-USR-99"
    assert "DF-USR-99" in result.response_text
    assert recorded_text == "Implement DarkHub WebSocket stream"


def test_telegram_grill_callback_resumes_waiting_human_run(
    temp_telegram_dir: Path,
    auth_config: TelegramConfig,
) -> None:
    """Scenario G1: Inline callback answering Grill question resumes workflow."""
    grill_answers: Dict[str, str] = {}

    def mock_grill(ticket_id: str, choice: str, user_id: int) -> Dict[str, Any]:
        grill_answers[ticket_id] = choice
        return {"resumed": True, "ticket_id": ticket_id}

    gateway = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir, grill_handler=mock_grill)
    result = gateway.process_update({
        "update_id": 400,
        "callback_query": {
            "id": "cb-query-999",
            "from": {"id": 1001},
            "data": "cb:grill:TICKET-55:Option-PostgreSQL",
        },
    })
    assert result.action == TelegramActionType.GRILL
    assert result.target_id == "TICKET-55"
    assert result.resumed is True
    assert grill_answers["TICKET-55"] == "Option-PostgreSQL"

    # Duplicate callback query check
    dup_res = gateway.process_update({
        "update_id": 401,
        "callback_query": {
            "id": "cb-query-999",
            "from": {"id": 1001},
            "data": "cb:grill:TICKET-55:Option-PostgreSQL",
        },
    })
    assert dup_res.duplicate is True


def test_telegram_approve_release_records_acceptance(
    temp_telegram_dir: Path,
    auth_config: TelegramConfig,
) -> None:
    """Scenario G8: Release approval records client acceptance receipt."""
    approvals: Dict[str, str] = {}

    def mock_approval(project_id: str, digest: str, user_id: int) -> Dict[str, Any]:
        approvals[project_id] = digest
        return {"receipt_id": "RCPT-TEST-88", "project_id": project_id, "digest": digest}

    gateway = TelegramGateway(config=auth_config, state_dir=temp_telegram_dir, approval_handler=mock_approval)
    result = gateway.process_update({
        "update_id": 500,
        "callback_query": {
            "id": "cb-rel-1",
            "from": {"id": 1001},
            "data": "cb:release:darkfac-core:sha256deadbeef:approve",
        },
    })
    assert result.action == TelegramActionType.APPROVE
    assert result.target_id == "darkfac-core:sha256deadbeef"
    assert result.resumed is True
    assert approvals["darkfac-core"] == "sha256deadbeef"


def test_telegram_network_outage_does_not_fail_workflow(temp_telegram_dir: Path) -> None:
    """Telegram outage queues notification locally and does not raise unhandled exception."""
    cfg = TelegramConfig(bot_token="", authorized_user_ids=[1001])
    gateway = TelegramGateway(config=cfg, state_dir=temp_telegram_dir)

    # Calling send_message with no token or unreachable network
    delivered = gateway.send_message(chat_id=1001, text="Critical alert: build failed")
    assert delivered is False

    # Verify outbox item exists
    status = gateway.get_status()
    assert status["pending_outbox_notifications"] == 1


def test_telegram_message_sanitizes_secrets() -> None:
    """Tokens and passwords in messages are scrubbed."""
    raw = "Deploy failed: bot1234567890:ABC-DEF1234567890abcdef with ghp_123456789012345678901234567890"
    sanitized = redact_secrets(raw)
    assert "bot1234567890" not in sanitized
    assert "ghp_" not in sanitized
    assert "[REDACTED_SECRET]" in sanitized


def test_n8n_probe_distinguishes_generic_link_from_real_instance() -> None:
    """Rejects generic https://n8n.io and validates real instance with /healthz."""
    probe = N8nProbe()

    # Generic documentation link
    generic_report = probe.probe(target_url="https://n8n.io")
    assert generic_report.is_generic_placeholder is True
    assert generic_report.operational is False
    assert "public generic documentation link" in (generic_report.error or "")

    # Mock real instance
    def mock_http(url: str, timeout: float) -> Dict[str, Any]:
        return {
            "url": url,
            "is_generic_placeholder": False,
            "operational": True,
            "status_code": 200,
            "version": "1.52.0",
            "db_connected": True,
        }

    mock_probe = N8nProbe(http_client=mock_http)
    real_report = mock_probe.probe(target_url="http://vps.darkfac.internal:5678")
    assert real_report.is_generic_placeholder is False
    assert real_report.operational is True
    assert real_report.version == "1.52.0"
    assert real_report.db_connected is True


def test_n8n_manifest_generator_emits_valid_compose() -> None:
    """Docker compose manifest specifies n8n Community, PostgreSQL 16, and volumes."""
    compose_yaml = N8nManifestGenerator.generate_docker_compose(
        n8n_version="1.50.0",
        port=5678,
        webhook_url="https://n8n.darkfac.org",
    )
    assert "n8nio/n8n:1.50.0" in compose_yaml
    assert "postgres:16-alpine" in compose_yaml
    assert "n8n-postgres" in compose_yaml
    assert "N8N_ENCRYPTION_KEY" in compose_yaml
    assert "n8n_postgres_data" in compose_yaml
    assert "5678:5678" in compose_yaml


def test_n8n_workflow_sanitizer_redacts_credentials(tmp_path: Path) -> None:
    """Sanitizer scrubs credentials and parameters before Git export."""
    raw_workflow = {
        "name": "Production Deploy Notification",
        "nodes": [
            {
                "id": "1",
                "name": "Send Slack",
                "type": "n8n-nodes-base.slack",
                "credentials": {
                    "slackApi": {"id": "cred-12345", "name": "Slack Admin Token"}
                },
                "parameters": {
                    "api_token": "sk-slack-super-secret-token",
                    "channel": "#deploys",
                },
            }
        ],
        "connections": {},
        "pinData": {"1": [{"data": "secret test data"}]},
    }

    out_file = tmp_path / "sanitized_workflow.json"
    sanitized = N8nWorkflowManager.export_workflow(raw_workflow, output_path=out_file)

    assert sanitized["nodes"][0]["credentials"]["slackApi"]["id"] == "REDACTED"
    assert sanitized["nodes"][0]["parameters"]["api_token"] == "[REDACTED_SECRET]"
    assert sanitized["pinData"] == {}
    assert out_file.exists()

    # Import validation
    assert N8nWorkflowManager.import_workflow(sanitized) is True
    assert N8nWorkflowManager.import_workflow({"invalid": "data"}) is False


def test_hub_endpoints_telegram_and_n8n() -> None:
    """FastAPI endpoints for Telegram webhook, telegram status, and n8n status."""
    client = TestClient(app)

    # Status endpoints
    status_resp = client.get("/api/integrations/telegram/status")
    assert status_resp.status_code == 200
    assert "last_offset" in status_resp.json()

    n8n_resp = client.get("/api/integrations/n8n/status?url=https://n8n.io")
    assert n8n_resp.status_code == 200
    assert n8n_resp.json()["is_generic_placeholder"] is True

    # Telegram webhook test
    webhook_payload = {
        "update_id": 999,
        "message": {
            "message_id": 1,
            "from": {"id": 9999},
            "chat": {"id": 9999},
            "date": 1700000000,
            "text": "/start",
        },
    }
    hook_resp = client.post("/api/webhooks/telegram", json=webhook_payload)
    assert hook_resp.status_code == 200
    assert hook_resp.json()["authorized"] is False  # unknown user


def test_cli_headless_telegram_and_n8n_commands(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """CLI subcommands execute headlessly with --json output."""
    # 1. telegram-status
    capsys.readouterr()
    exit_code = cli_main(["telegram-status", "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    status = json.loads(captured.out)
    assert "last_offset" in status

    # 2. n8n-preflight generic
    capsys.readouterr()
    exit_code = cli_main(["n8n-preflight", "--url", "https://n8n.io", "--json"])
    assert exit_code == 2  # generic placeholder
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["is_generic_placeholder"] is True

    # 3. n8n-generate-compose
    capsys.readouterr()
    compose_path = tmp_path / "test-compose.yml"
    exit_code = cli_main(["n8n-generate-compose", "--output", str(compose_path), "--json"])
    assert exit_code == 0
    assert compose_path.exists()
    assert "postgres:16-alpine" in compose_path.read_text(encoding="utf-8")
