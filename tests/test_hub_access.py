"""Security boundary tests for DarkHub's local HTTP surface and probes."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService, ProbeTargetError, _SafeProbeRedirectHandler


@pytest.fixture
def secured_service(tmp_path: Path) -> HubService:
    services = [
        {
            "id": "public-status",
            "name": "Public Status",
            "url": "https://example.com/status",
            "category": "infra_apis",
            "description": "Public status endpoint",
            "tags": ["status"],
            "is_local": False,
        },
        {
            "id": "local-ollama",
            "name": "Local Ollama",
            "url": "http://127.0.0.1:11434/",
            "category": "local_cluster",
            "description": "Local service",
            "tags": ["local"],
            "is_local": True,
        },
    ]
    (tmp_path / "default_services.json").write_text(json.dumps(services), encoding="utf-8")
    (tmp_path / "default_prompts.json").write_text("[]", encoding="utf-8")
    return HubService(data_dir=tmp_path, usage_dir=tmp_path / "usage", roadmap_root=tmp_path)


def test_invalid_host_origin_and_session_are_blocked() -> None:
    with TestClient(app, base_url="http://attacker.invalid") as foreign_host:
        assert foreign_host.get("/api/services").status_code == 400

    with TestClient(app) as client:
        invalid_origin = client.get(
            "/api/services",
            headers={"Origin": "https://attacker.invalid"},
        )
        assert invalid_origin.status_code == 403

        missing_session = client.get(
            "/api/services",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert missing_session.status_code == 401

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        allowed = client.get(
            "/api/services",
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert allowed.status_code == 200

    with TestClient(app) as client:
        client.cookies.set("darkhub_session", "invalid-session")
        invalid_session = client.get("/api/services")
        assert invalid_session.status_code == 401


def test_probe_requires_registered_url_and_destination_class(
    secured_service: HubService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    with pytest.raises(ProbeTargetError, match="registered"):
        secured_service.ping_url("missing", "https://example.com/status")
    with pytest.raises(ProbeTargetError, match="match"):
        secured_service.ping_url("public-status", "https://example.com/admin")
    with pytest.raises(ProbeTargetError, match="not allowed"):
        secured_service.validate_probe_target("http://127.0.0.1/private", is_local=False)


def test_health_api_maps_rejected_probe_to_bad_request(secured_service: HubService) -> None:
    app.dependency_overrides[get_hub_service] = lambda: secured_service
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/health/ping",
                params={"service_id": "public-status", "url": "http://127.0.0.1/private"},
            )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_redirects_are_revalidated_and_https_cannot_downgrade(
    secured_service: HubService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    handler = _SafeProbeRedirectHandler(
        lambda target: secured_service.validate_probe_target(target, is_local=False),
        initial_scheme="https",
    )

    with pytest.raises(ProbeTargetError, match="not allowed"):
        handler.redirect_request(
            MagicMock(), MagicMock(), 302, "Found", {}, "http://127.0.0.1/private"
        )
    with pytest.raises(ProbeTargetError, match="downgrade"):
        handler.redirect_request(
            MagicMock(), MagicMock(), 302, "Found", {}, "http://example.com/status"
        )
