"""Verification suite for n8n Dokploy Deployment Manifests.

Tests:
- docker-compose.n8n.yml structure, services, networks, memory bounds, and Traefik labels.
- env.n8n.example template completeness, safety, and absence of leaked credentials.
"""

from __future__ import annotations

import re
from pathlib import Path
import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = PROJECT_ROOT / "deploy" / "dokploy" / "docker-compose.n8n.yml"
ENV_TEMPLATE_PATH = PROJECT_ROOT / "deploy" / "dokploy" / "env.n8n.example"


def test_n8n_compose_manifest_structure() -> None:
    assert COMPOSE_PATH.is_file(), f"Missing compose file at {COMPOSE_PATH}"
    content = COMPOSE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    assert "services" in data
    services = data["services"]
    assert "n8n" in services
    assert "n8n-postgres" in services

    # Verify n8n-postgres service
    pg = services["n8n-postgres"]
    assert pg["restart"] == "unless-stopped"
    assert "dokploy-network" in pg.get("networks", [])
    assert "healthcheck" in pg
    pg_res = pg.get("deploy", {}).get("resources", {})
    assert pg_res["limits"]["memory"] == "256M"
    assert pg_res["reservations"]["memory"] == "64M"

    # Verify n8n application service
    app = services["n8n"]
    assert app["restart"] == "unless-stopped"
    assert "dokploy-network" in app.get("networks", [])
    assert app.get("depends_on", {}).get("n8n-postgres", {}).get("condition") == "service_healthy"

    # Verify environment variables
    env_vars = app.get("environment", [])
    assert any("N8N_PORT=5678" in env for env in env_vars)
    assert any("N8N_PROTOCOL=http" in env for env in env_vars)
    assert any("EXECUTIONS_DATA_PRUNE=true" in env for env in env_vars)

    # Verify memory limits protect the 4GB VPS
    app_res = app.get("deploy", {}).get("resources", {})
    assert app_res["limits"]["memory"] == "1024M"
    assert app_res["reservations"]["memory"] == "256M"

    # Verify Traefik reverse proxy labels for TLS
    labels = app.get("labels", [])
    assert "traefik.enable=true" in labels
    assert "traefik.docker.network=dokploy-network" in labels
    assert any("Host(`n8n.ggcampos.com`)" in l for l in labels)
    assert any("certresolver=letsencrypt" in l for l in labels)
    assert any("loadbalancer.server.port=5678" in l for l in labels)

    # Verify shared volumes and external dokploy-network
    assert "volumes" in data
    assert "n8n-app-data" in data["volumes"]
    assert "n8n-postgres-data" in data["volumes"]
    assert data.get("networks", {}).get("dokploy-network", {}).get("external") is True


def test_n8n_env_template_safety_and_completeness() -> None:
    assert ENV_TEMPLATE_PATH.is_file(), f"Missing env template at {ENV_TEMPLATE_PATH}"
    content = ENV_TEMPLATE_PATH.read_text(encoding="utf-8")

    # Required variables must be declared
    assert "N8N_HOST=n8n.ggcampos.com" in content
    assert "N8N_WEBHOOK_URL=https://n8n.ggcampos.com/" in content
    assert "N8N_DB_USER=n8n" in content
    assert "N8N_DB_NAME=n8n" in content
    assert "N8N_ENCRYPTION_KEY=" in content
    assert "N8N_API_KEY=" in content

    # Prohibited: real hardcoded secrets
    assert not re.search(r"N8N_ENCRYPTION_KEY=[a-f0-9]{64}", content), (
        "env.n8n.example must not contain live encryption keys"
    )
    assert "strong_random" in content.lower()
