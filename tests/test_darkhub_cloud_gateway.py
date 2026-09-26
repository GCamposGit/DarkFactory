"""Acceptance and reachability tests for USR-18:
DarkHub 24/7 & Gateway de Webhooks Autonomos no Dokploy (INFRA-09 / DF-20 / DF-21).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from core.orchestrator.delivery import DeliveryRisk
from hub.backend.api import get_hub_service
from hub.backend.main import app, get_allowed_hosts
from hub.backend.service import HubService
from hub.backend.webhooks import (
    CloudGatewayStatus,
    DokployDeployClient,
    DokployDeployTrigger,
    WebhookEngine,
    WebhookEventRecord,
    verify_github_signature,
)


@pytest.fixture
def temp_project_dir():
    """Provides an isolated directory for testing WebhookEngine and HubService persistence."""
    temp_dir = tempfile.mkdtemp(prefix="darkhub_gateway_test_")
    proj_path = Path(temp_dir)
    factory_dir = proj_path / ".factory"
    factory_dir.mkdir(parents=True, exist_ok=True)
    hub_data = proj_path / "hub" / "data"
    hub_data.mkdir(parents=True, exist_ok=True)

    (hub_data / "default_services.json").write_text("[]", encoding="utf-8")
    (hub_data / "default_prompts.json").write_text("[]", encoding="utf-8")

    yield proj_path

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def gateway_service(temp_project_dir: Path):
    """Provides HubService instance bound to temporary test directory."""
    service = HubService(
        project_root=temp_project_dir,
        data_dir=temp_project_dir / "hub" / "data",
    )
    app.dependency_overrides[get_hub_service] = lambda: service
    yield service
    app.dependency_overrides.clear()


def generate_signature(payload: Dict[str, Any], secret: str) -> str:
    """Helper to compute valid GitHub X-Hub-Signature-256 header."""
    raw_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


# ==============================================================================
# 1. HMAC-SHA256 Signature Verification Tests
# ==============================================================================


def test_verify_github_signature_valid():
    """Valid HMAC-SHA256 signature must be verified successfully."""
    secret = "super_secret_webhook_key_123"
    payload = {"zen": "Mind your words, they become actions.", "hook_id": 42}
    raw_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig_header = generate_signature(payload, secret)

    result = verify_github_signature(raw_bytes, sig_header, secret)
    assert result.valid is True
    assert "verified successfully" in result.reason


def test_verify_github_signature_tampered_payload():
    """Altered payload must fail signature verification."""
    secret = "super_secret_webhook_key_123"
    payload = {"zen": "Original text"}
    tampered_bytes = json.dumps({"zen": "Tampered text"}).encode("utf-8")
    sig_header = generate_signature(payload, secret)

    result = verify_github_signature(tampered_bytes, sig_header, secret)
    assert result.valid is False
    assert "mismatch" in result.reason.lower()


def test_verify_github_signature_missing_header():
    """Missing or malformed signature header must be rejected fail-closed."""
    secret = "super_secret_webhook_key_123"
    raw_bytes = b'{"action":"test"}'

    res_none = verify_github_signature(raw_bytes, None, secret)
    assert res_none.valid is False

    res_bad_prefix = verify_github_signature(raw_bytes, "md5=123456", secret)
    assert res_bad_prefix.valid is False


def test_verify_github_signature_fail_closed_when_secret_absent_in_prod():
    """If secret is not set, require_secret=True must fail closed."""
    raw_bytes = b'{"action":"test"}'
    result = verify_github_signature(raw_bytes, "sha256=abcdef", secret=None, require_secret=True)
    assert result.valid is False
    assert "fail-closed" in result.reason.lower()


# ==============================================================================
# 2. Idempotency & Replay Attack Prevention Tests
# ==============================================================================


def test_webhook_idempotency_prevents_replay(temp_project_dir: Path):
    """Replaying the same X-GitHub-Delivery must be detected and ignored."""
    engine = WebhookEngine(project_root=temp_project_dir)
    secret = "test_secret"
    delivery_id = "delivery-uuid-12345-abc"
    payload = {"zen": "Avoid duplication.", "hook_id": 99}
    sig = generate_signature(payload, secret)

    # First delivery: processed
    first_record = engine.process_webhook(
        event_type="ping",
        delivery_id=delivery_id,
        payload=payload,
        signature_header=sig,
        secret=secret,
    )
    assert first_record.status == "processed"
    assert first_record.action_taken == "pong"

    # Second delivery with exact same delivery_id: ignored duplicate
    second_record = engine.process_webhook(
        event_type="ping",
        delivery_id=delivery_id,
        payload=payload,
        signature_header=sig,
        secret=secret,
    )
    assert second_record.status == "ignored"
    assert second_record.action_taken == "duplicate_ignored"


# ==============================================================================
# 3. Autonomous Event Dispatching & DF-20 Integration Tests
# ==============================================================================


def test_webhook_push_event_dispatching(temp_project_dir: Path):
    """Push event to main branch triggers deployment handling."""
    engine = WebhookEngine(project_root=temp_project_dir)
    secret = "push_secret"
    delivery_id = "push-delivery-001"
    payload = {
        "ref": "refs/heads/main",
        "after": "c" * 40,
        "forced": False,
        "commits": [{"id": "c" * 40, "message": "feat: autonomous cloud gateway"}],
        "repository": {"full_name": "GCamposGit/DarkFactory"},
        "sender": {"login": "octocat"},
    }
    sig = generate_signature(payload, secret)

    record = engine.process_webhook(
        event_type="push",
        delivery_id=delivery_id,
        payload=payload,
        signature_header=sig,
        secret=secret,
    )

    assert record.status == "processed"
    assert "push_processed" in record.action_taken
    assert record.repository == "GCamposGit/DarkFactory"
    assert record.ref == "refs/heads/main"


def test_webhook_pull_request_df20_policy_evaluation(temp_project_dir: Path):
    """Pull request synchronize event invokes DF-20 deterministic delivery evaluation."""
    engine = WebhookEngine(project_root=temp_project_dir)
    secret = "pr_secret"
    delivery_id = "pr-delivery-555"
    head_sha = "b" * 40
    base_sha = "a" * 40

    payload = {
        "action": "synchronize",
        "number": 42,
        "repository": {"full_name": "GCamposGit/DarkFactory"},
        "sender": {"login": "antigravity"},
        "pull_request": {
            "number": 42,
            "state": "open",
            "draft": False,
            "base": {"sha": base_sha},
            "head": {"sha": head_sha},
        },
        "checks": [
            {"name": "trusted-pr-policy", "head_sha": head_sha, "status": "completed", "conclusion": "success"},
            {"name": "pr-validation", "head_sha": head_sha, "status": "completed", "conclusion": "success"},
        ],
    }
    sig = generate_signature(payload, secret)

    record = engine.process_webhook(
        event_type="pull_request",
        delivery_id=delivery_id,
        payload=payload,
        signature_header=sig,
        secret=secret,
    )

    assert record.status == "processed"
    assert "delivery_evaluated_eligible" in record.action_taken
    assert record.details.get("delivery_evaluation", {}).get("eligible") is True


# ==============================================================================
# 4. Security Containment & Cloudflare Zero Trust Tests
# ==============================================================================


def test_allowed_hosts_containment_with_cloud_domain(gateway_service: HubService):
    """Configuring DARKHUB_ALLOWED_HOSTS permits darkhub.ggcampos.com while blocking foreign hosts."""
    client = TestClient(app)

    with mock.patch.dict(os.environ, {"DARKHUB_ALLOWED_HOSTS": "darkhub.ggcampos.com,dokploy.ggcampos.com"}):
        allowed = get_allowed_hosts()
        assert "darkhub.ggcampos.com" in allowed
        assert "dokploy.ggcampos.com" in allowed

        # Permitted cloud domain
        res_cloud = client.get("/api/services", headers={"Host": "darkhub.ggcampos.com"})
        assert res_cloud.status_code == 200

        # Foreign unpermitted domain blocked with 400
        res_evil = client.get("/api/services", headers={"Host": "evil-phishing.org"})
        assert res_evil.status_code == 400
        assert "Invalid Host header" in res_evil.text


def test_cloudflare_zero_trust_access_header_enforcement(gateway_service: HubService):
    """When CLOUDFLARE_ZERO_TRUST_REQUIRED is active, missing identity header is rejected on UI routes."""
    client = TestClient(app)

    with mock.patch.dict(os.environ, {"CLOUDFLARE_ZERO_TRUST_REQUIRED": "true"}):
        # Protected API route without Cf-Access-Authenticated-User-Email
        res_no_cf = client.get("/api/services", headers={"Host": "localhost"})
        assert res_no_cf.status_code == 403
        assert "Cloudflare Access" in res_no_cf.text

        # With verified Cloudflare Access user email
        res_with_cf = client.get(
            "/api/services",
            headers={"Host": "localhost", "Cf-Access-Authenticated-User-Email": "operator@ggcampos.com"},
        )
        assert res_with_cf.status_code == 200


# ==============================================================================
# 5. Dokploy Deploy Client & REST Endpoints Tests
# ==============================================================================


def test_dokploy_deploy_client_skipped_when_empty():
    """DokployDeployClient gracefully skips when URL is not set."""
    client = DokployDeployClient(deploy_url="")
    result = client.trigger_deploy(service_name="darkhub")
    assert result.status == "skipped"
    assert "not configured" in result.message


def test_dokploy_deploy_client_multi_service_json_and_trigger_all():
    """DokployDeployClient parses DOKPLOY_DEPLOY_URLS JSON and triggers all services."""
    env_urls = json.dumps({
        "darkhub": "https://dokploy.example/api/deploy/compose/hub123",
        "worker": "https://dokploy.example/api/deploy/application/app456",
    })
    with mock.patch.dict(os.environ, {"DOKPLOY_DEPLOY_URLS": env_urls}):
        client = DokployDeployClient()
        assert "darkhub" in client.service_urls
        assert "worker" in client.service_urls
        assert client.service_urls["darkhub"] == "https://dokploy.example/api/deploy/compose/hub123"
        assert client.service_urls["worker"] == "https://dokploy.example/api/deploy/application/app456"

        # Mock urllib to test trigger_deploy_all
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = mock.MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b'{"success": true}'
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            results = client.trigger_deploy_all()
            assert len(results) == 2
            assert all(r.status == "success" for r in results)
            assert {r.service_name for r in results} == {"darkhub", "worker"}


def test_dokploy_deploy_client_multi_service_comma_separated():
    """DokployDeployClient parses comma-separated DOKPLOY_DEPLOY_URLS format."""
    raw = "darkhub=https://dokploy.example/hub, worker=https://dokploy.example/worker "
    with mock.patch.dict(os.environ, {"DOKPLOY_DEPLOY_URLS": raw}):
        client = DokployDeployClient()
        assert client.service_urls["darkhub"] == "https://dokploy.example/hub"
        assert client.service_urls["worker"] == "https://dokploy.example/worker"


def test_webhook_push_multi_service_dispatching(temp_project_dir: Path):
    """Push event triggers deployment for all configured multi-services."""
    engine = WebhookEngine(project_root=temp_project_dir)
    engine.deploy_client.service_urls = {
        "darkhub": "https://dokploy.example/hub",
        "worker": "https://dokploy.example/worker",
    }
    secret = "push_multi_secret"
    delivery_id = "push-multi-001"
    payload = {
        "ref": "refs/heads/main",
        "after": "d" * 40,
        "forced": False,
        "commits": [{"id": "d" * 40, "message": "feat: multi-service continuous deploy"}],
        "repository": {"full_name": "GCamposGit/DarkFactory"},
        "sender": {"login": "octocat"},
    }
    sig = generate_signature(payload, secret)

    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"success": true}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        record = engine.process_webhook(
            event_type="push",
            delivery_id=delivery_id,
            payload=payload,
            signature_header=sig,
            secret=secret,
        )

        assert record.status == "processed"
        assert record.action_taken == "push_processed_and_deploy_triggered"
        dokploy_deploy = record.details.get("dokploy_deploy")
        assert isinstance(dokploy_deploy, list)
        assert len(dokploy_deploy) == 2
        assert {d["service_name"] for d in dokploy_deploy} == {"darkhub", "worker"}



def test_rest_api_webhook_github_endpoint(gateway_service: HubService):
    """POST /api/webhooks/github handles valid payload and enforces secret verification."""
    client = TestClient(app)
    secret = "api_secret_test_xyz"
    payload = {"zen": "Determinism builds resilience.", "hook_id": 1001}
    sig = generate_signature(payload, secret)

    with mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": secret}):
        # Valid signature succeeds
        res_valid = client.post(
            "/api/webhooks/github",
            json=payload,
            headers={
                "Host": "localhost",
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "api-delivery-001",
                "X-Hub-Signature-256": sig,
            },
        )
        assert res_valid.status_code == 200
        data = res_valid.json()
        assert data["status"] == "processed"
        assert data["action_taken"] == "pong"

        # Tampered or invalid signature returns 401
        res_invalid = client.post(
            "/api/webhooks/github",
            json=payload,
            headers={
                "Host": "localhost",
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "api-delivery-002",
                "X-Hub-Signature-256": "sha256=badbadbadbadbadbadbadbadbadbad",
            },
        )
        assert res_invalid.status_code == 401


def test_rest_api_webhook_events_and_test_simulation(gateway_service: HubService):
    """POST /api/webhooks/test simulates an event and GET /api/webhooks/events lists it."""
    client = TestClient(app)

    # 1. Simulate a test webhook
    res_test = client.post(
        "/api/webhooks/test",
        json={"event_type": "ping", "payload": {"zen": "Testing simulation"}},
        headers={"Host": "localhost"},
    )
    assert res_test.status_code == 200
    sim_data = res_test.json()
    assert sim_data["status"] == "processed"
    assert sim_data["event_type"] == "ping"

    # 2. Query historical events
    res_events = client.get("/api/webhooks/events?limit=10", headers={"Host": "localhost"})
    assert res_events.status_code == 200
    events_list = res_events.json()
    assert len(events_list) >= 1
    assert any(ev["delivery_id"] == sim_data["delivery_id"] for ev in events_list)


def test_rest_api_cloud_gateway_status_and_deploy_trigger(gateway_service: HubService):
    """GET /api/cloud/status and POST /api/cloud/deploy return correct operational state."""
    client = TestClient(app)

    # Status endpoint
    res_status = client.get("/api/cloud/status", headers={"Host": "localhost"})
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert "allowed_hosts" in status_data
    assert "total_events_received" in status_data
    assert "configured_deploy_services" in status_data

    # Deploy endpoint
    res_deploy = client.post(
        "/api/cloud/deploy",
        json={"service_name": "darkhub"},
        headers={"Host": "localhost"},
    )
    assert res_deploy.status_code == 200
    deploy_data = res_deploy.json()
    assert deploy_data["service_name"] == "darkhub"
    assert deploy_data["status"] in ("skipped", "success", "failed")


# ==============================================================================
# 6. Dokploy Manifests & Compose Verification Tests
# ==============================================================================


def test_dokploy_manifests_structure():
    """Verifies that Dockerfile.hub and docker-compose.hub.yml exist and contain required contracts."""
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile_path = repo_root / "deploy" / "dokploy" / "Dockerfile.hub"
    compose_path = repo_root / "deploy" / "dokploy" / "docker-compose.hub.yml"
    env_path = repo_root / "deploy" / "dokploy" / "env.hub.example"

    assert dockerfile_path.exists(), "Dockerfile.hub must exist"
    assert compose_path.exists(), "docker-compose.hub.yml must exist"
    assert env_path.exists(), "env.hub.example must exist"

    df_content = dockerfile_path.read_text(encoding="utf-8")
    assert "python:3.12-slim" in df_content
    assert "HEALTHCHECK" in df_content
    assert "USER darkfac" in df_content

    compose_content = compose_path.read_text(encoding="utf-8")
    assert "darkhub.ggcampos.com" in compose_content
    assert "traefik.http.routers.darkhub" in compose_content
    assert "darkhub-factory-data" in compose_content
