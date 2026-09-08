"""
Tests for DF-08: Route containment, SSRF prevention, and Host/Origin/Session restriction in DarkHub backend.
"""

import tempfile
import shutil
import json
from pathlib import Path
from unittest import mock
import urllib.request
import urllib.error
import pytest
from fastapi.testclient import TestClient

from hub.backend.main import app
from hub.backend.api import get_hub_service
from hub.backend.service import (
    HubService,
    is_destination_allowed,
    DisallowedDestinationError,
    SafeRedirectHandler,
)
from hub.backend.models import HealthStatus


@pytest.fixture
def access_service():
    """Provides an isolated HubService in a temporary directory."""
    temp_dir = tempfile.mkdtemp(prefix="darkhub_access_test_")
    data_dir = Path(temp_dir)

    default_data = [
        {
            "id": "test-ollama",
            "name": "Local Ollama Test",
            "url": "http://localhost:11434",
            "category": "local_cluster",
            "description": "Local AI cluster for testing",
            "tags": ["local"],
            "color": "#10b981",
            "is_favorite": True,
            "pinned": True,
            "is_local": True,
        },
        {
            "id": "test-claude",
            "name": "Claude Test",
            "url": "https://claude.ai/",
            "category": "llm_chat",
            "description": "Anthropic Claude interface",
            "tags": ["frontier"],
            "color": "#d97706",
            "is_favorite": False,
            "pinned": False,
            "is_local": False,
        },
    ]

    (data_dir / "default_services.json").write_text(json.dumps(default_data), encoding="utf-8")
    (data_dir / "default_prompts.json").write_text(json.dumps([]), encoding="utf-8")

    service = HubService(data_dir=data_dir)
    app.dependency_overrides[get_hub_service] = lambda: service

    yield service

    app.dependency_overrides.clear()
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Host Header Containment Tests
# ---------------------------------------------------------------------------

def test_host_header_restriction(access_service: HubService):
    """Foreign or malicious Host headers (e.g. DNS rebinding attempts) must be rejected with HTTP 400."""
    client = TestClient(app)

    # Malicious external hosts
    for bad_host in ["evil.com", "attacker.internal", "192.168.1.100:8888", "rebind.example.org"]:
        res = client.get("/api/services", headers={"Host": bad_host})
        assert res.status_code == 400
        assert "Invalid Host header" in res.text

    # Permitted local hosts
    for good_host in ["localhost", "localhost:8888", "127.0.0.1", "127.0.0.1:8888", "[::1]", "testserver"]:
        res = client.get("/api/services", headers={"Host": good_host})
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# 2. Origin Header Containment Tests
# ---------------------------------------------------------------------------

def test_origin_header_restriction(access_service: HubService):
    """Cross-origin requests with untrusted Origin headers must be rejected with HTTP 403."""
    client = TestClient(app)

    # Malicious origins
    for bad_origin in ["http://evil.com", "https://attacker.org", "http://192.168.1.50:3000"]:
        res = client.get("/api/services", headers={"Origin": bad_origin})
        assert res.status_code == 403
        assert "Invalid Origin header" in res.text

    # Permitted local origins
    for good_origin in [
        "http://localhost:8888",
        "http://127.0.0.1:8888",
        "http://localhost:3000",
        "http://testserver",
    ]:
        res = client.get("/api/services", headers={"Origin": good_origin})
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# 3. Session Token Validation Tests
# ---------------------------------------------------------------------------

def test_session_token_authentication(access_service: HubService):
    """Invalid session tokens must be rejected with HTTP 401; valid session tokens accepted."""
    client = TestClient(app)

    # 1. Fetch active session token
    res_session = client.get("/api/session")
    assert res_session.status_code == 200
    token = res_session.json()["session_token"]
    assert token == access_service.session_token

    # 2. Request with invalid session token -> rejected 401
    res_bad = client.get("/api/services", headers={"X-Hub-Session": "forged-session-token"})
    assert res_bad.status_code == 401
    assert "Invalid session token" in res_bad.text

    # 3. Request with valid session token -> accepted 200
    res_ok = client.get("/api/services", headers={"X-Hub-Session": token})
    assert res_ok.status_code == 200


# ---------------------------------------------------------------------------
# 4. SSRF Destination Policy Tests
# ---------------------------------------------------------------------------

def test_destination_allowlist_policy():
    """Verify that is_destination_allowed strictly blocks cloud metadata and non-HTTP protocols."""
    # Forbidden destinations (cloud metadata, internal AWS/GCP, broadcast, non-HTTP)
    blocked_cases = [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://169.254.170.2/v2/metadata",
        "http://metadata.google.internal/",
        "http://instance-data/latest/meta-data/",
        "file:///etc/passwd",
        "gopher://127.0.0.1:25/",
        "ftp://example.com/file",
        "http://255.255.255.255/broadcast",
        "http://0.0.0.0/internal",
        "http://10.0.0.1/admin",
        "http://172.16.0.1/secret",
        "http://192.168.1.1/router",
    ]

    for blocked_url in blocked_cases:
        allowed, reason = is_destination_allowed(blocked_url)
        assert not allowed, f"URL {blocked_url} should have been blocked! Reason: {reason}"

    # Permitted destinations (local AI cluster and valid external web services)
    permitted_cases = [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://localhost:8888",
        "https://claude.ai/",
        "https://chatgpt.com/",
        "https://openrouter.ai/models",
        "https://api.github.com/repos",
    ]

    for permitted_url in permitted_cases:
        allowed, reason = is_destination_allowed(permitted_url)
        assert allowed, f"URL {permitted_url} should be permitted! Reason: {reason}"


# ---------------------------------------------------------------------------
# 5. Health Check SSRF & Registered Service ID Restriction Tests
# ---------------------------------------------------------------------------

def test_health_ping_requires_registered_service(access_service: HubService):
    """Health check endpoint must reject arbitrary URLs or unregistered service IDs."""
    client = TestClient(app)

    # 1. Unregistered service ID -> 404
    res_unregistered = client.get(
        "/api/health/ping",
        params={"service_id": "attacker-id", "url": "http://169.254.169.254/latest/meta-data"},
    )
    assert res_unregistered.status_code == 404

    # 2. Registered service ID with mismatched URL -> 400
    res_mismatched = client.get(
        "/api/health/ping",
        params={"service_id": "test-ollama", "url": "http://169.254.169.254/latest/meta-data"},
    )
    assert res_mismatched.status_code == 400
    assert "Provided URL does not match" in res_mismatched.text


def test_ping_url_directly_blocks_cloud_metadata(access_service: HubService):
    """ping_url must reject probing cloud metadata even if requested programmatically."""
    result = access_service.ping_url(
        service_id="metadata-test",
        url="http://169.254.169.254/latest/meta-data/",
    )
    assert result.status == HealthStatus.OFFLINE
    assert result.latency_ms is None
    assert "SSRF policy" in (result.error or "")


# ---------------------------------------------------------------------------
# 6. SSRF Redirect Containment Tests
# ---------------------------------------------------------------------------

def test_safe_redirect_handler_intercepts_ssrf_redirect(access_service: HubService):
    """
    If a target server returns a 302 redirect pointing to cloud metadata or a forbidden destination,
    SafeRedirectHandler must intercept it and block following the redirect.
    """
    handler = SafeRedirectHandler()
    fake_req = mock.MagicMock()

    # Case A: Redirect to forbidden metadata
    with pytest.raises(DisallowedDestinationError, match="SSRF Redirect Blocked"):
        handler.redirect_request(
            req=fake_req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://169.254.169.254/latest/meta-data",
        )

    # Case B: Redirect to private network
    with pytest.raises(DisallowedDestinationError, match="SSRF Redirect Blocked"):
        handler.redirect_request(
            req=fake_req,
            fp=None,
            code=301,
            msg="Moved Permanently",
            headers={},
            newurl="http://10.0.0.1/internal-admin",
        )

    # Case C: Redirect to valid public domain passes handler check
    with mock.patch("urllib.request.HTTPRedirectHandler.redirect_request", return_value=fake_req):
        redirected_req = handler.redirect_request(
            req=fake_req,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://claude.ai/login",
        )
        assert redirected_req is not None
