"""Verification suite for HF-03-01: Cloud Deployment Manifests.

Tests:
- docker-compose.cloud.yml structure, services, networks, and memory constraints.
- Dockerfile.cloud security, non-root user, and library pinning.
- env.cloud.example template completeness and absence of leaked credentials.
"""

from __future__ import annotations

import re
from pathlib import Path
import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = PROJECT_ROOT / "deploy" / "dokploy" / "docker-compose.cloud.yml"
DOCKERFILE_PATH = PROJECT_ROOT / "deploy" / "dokploy" / "Dockerfile.cloud"
ENV_TEMPLATE_PATH = PROJECT_ROOT / "deploy" / "dokploy" / "env.cloud.example"


def test_cloud_compose_manifest_structure() -> None:
    assert COMPOSE_PATH.is_file(), f"Missing compose file at {COMPOSE_PATH}"
    content = COMPOSE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    assert "services" in data
    services = data["services"]
    assert "darkfac-coordinator" in services
    assert "darkfac-worker" in services

    # Verify coordinator configuration
    coord = services["darkfac-coordinator"]
    assert coord["restart"] == "unless-stopped"
    assert "dokploy-network" in coord.get("networks", [])
    assert any("DARKFAC_HF02_DATABASE_URL" in env for env in coord.get("environment", []))
    assert any("DARKFAC_MAX_CONCURRENT_SLOTS" in env for env in coord.get("environment", []))

    # Verify coordinator resource bounds (must protect 4GB VPS)
    coord_res = coord.get("deploy", {}).get("resources", {})
    assert "limits" in coord_res
    assert coord_res["limits"]["memory"] == "1536M"
    assert coord_res["reservations"]["memory"] == "512M"

    # Verify worker configuration
    worker = services["darkfac-worker"]
    assert worker["restart"] == "unless-stopped"
    assert "dokploy-network" in worker.get("networks", [])
    worker_res = worker.get("deploy", {}).get("resources", {})
    # HF-27-09: worker memory raised to 2.5 GB (2560M) to fit the agent-CLI
    # toolchain (Claude Code / Codex) added to Dockerfile.cloud, within the
    # CX23's 4 GB budget alongside the coordinator.
    assert worker_res["limits"]["memory"] == "2560M"
    assert worker_res["reservations"]["memory"] == "256M"

    # Verify shared volumes and external network
    assert "volumes" in data
    assert "darkfac-artifacts" in data["volumes"]
    assert "darkfac-test-logs" in data["volumes"]
    assert data.get("networks", {}).get("dokploy-network", {}).get("external") is True


def test_cloud_dockerfile_security_and_pinning() -> None:
    assert DOCKERFILE_PATH.is_file(), f"Missing Dockerfile at {DOCKERFILE_PATH}"
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")

    # Verify non-root user creation and usage
    assert "useradd" in content
    assert "USER darkfac" in content

    # Verify core library pinning
    assert 'dbos==2.31.1' in content
    assert 'psycopg[binary]' in content
    assert 'psutil' in content


def test_cloud_env_template_safety_and_completeness() -> None:
    assert ENV_TEMPLATE_PATH.is_file(), f"Missing env template at {ENV_TEMPLATE_PATH}"
    content = ENV_TEMPLATE_PATH.read_text(encoding="utf-8")

    # Required variables must be declared
    assert "DARKFAC_HF02_DATABASE_URL=" in content
    assert "DARKFAC_MAX_CONCURRENT_SLOTS=2" in content
    assert "DARKFAC_COORDINATOR_PORT=8001" in content
    assert "DARKFAC_WORKER_ID=cloud-worker-1" in content

    # Prohibited: real passwords, real DSNs with credentials
    assert not re.search(r"postgresql://[^:]+:[^@]+@[^/]+/\w+", content), (
        "env.cloud.example must not contain live serialized DSNs"
    )
    assert "password=" not in content.lower() or "strong_password" in content.lower()
