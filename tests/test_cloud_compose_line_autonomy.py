"""Static checks for the line-autonomy compose changes (no docker, no network).

- `darkfac-pg-tailnet` publishes Postgres to the tailnet IP only.
- The canary polls every 15 minutes and receives the retry / dogfood env.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "deploy" / "dokploy" / "docker-compose.cloud.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_pg_tailnet_binds_only_to_the_tailnet_ip(compose: dict) -> None:
    service = compose["services"]["darkfac-pg-tailnet"]
    ports = service["ports"]
    assert ports == ["${DARKFAC_TAILNET_BIND_IP:-100.83.176.60}:5432:5432"]
    for port in ports:
        assert isinstance(port, str)
        # Must always name a host IP (never the bare "5432:5432" / ":5432:" forms)...
        assert re.match(r"^\$\{DARKFAC_TAILNET_BIND_IP:-100\.\d+\.\d+\.\d+\}:\d+:\d+$", port)
        # ...and never a wildcard/public bind.
        assert "0.0.0.0" not in port
        assert not port.startswith(("5432:", ":", "::"))


def test_pg_tailnet_default_ip_is_in_the_tailscale_cgnat_range(compose: dict) -> None:
    default = re.search(r"DARKFAC_TAILNET_BIND_IP:-(\d+)\.(\d+)\.\d+\.\d+", COMPOSE.read_text(encoding="utf-8"))
    assert default is not None
    first, second = int(default.group(1)), int(default.group(2))
    assert first == 100 and 64 <= second <= 127  # 100.64.0.0/10


def test_pg_tailnet_is_pinned_forwarder_on_dokploy_network(compose: dict) -> None:
    service = compose["services"]["darkfac-pg-tailnet"]
    image = service["image"]
    assert image.startswith("alpine/socat:") and not image.endswith((":latest", ":"))
    assert re.search(r":\d+\.\d+\.\d+", image), "image tag must be an exact version pin"
    assert service["restart"] == "unless-stopped"
    assert service["networks"] == ["dokploy-network"]
    command = " ".join(service["command"])
    assert "TCP-LISTEN:5432" in command
    assert "darkfaccore-postgresprimary-aebh67:5432" in command
    assert "build" not in service


def test_pg_tailnet_bind_failure_is_isolated(compose: dict) -> None:
    """Nothing depends on the forwarder and it defines no healthcheck others could wait on."""
    services = compose["services"]
    assert "healthcheck" not in services["darkfac-pg-tailnet"]
    assert "depends_on" not in services["darkfac-pg-tailnet"]
    for name, service in services.items():
        depends = service.get("depends_on") or {}
        assert "darkfac-pg-tailnet" not in depends, f"{name} must not depend on darkfac-pg-tailnet"


def test_no_service_publishes_a_wildcard_or_public_port(compose: dict) -> None:
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            text = str(port)
            assert "0.0.0.0" not in text, f"{name} publishes on all interfaces: {text}"
            assert text.startswith(("127.0.0.1:", "${DARKFAC_TAILNET_BIND_IP")), f"{name}: unbound port {text}"


def test_canary_polls_every_fifteen_minutes_with_retry_and_dogfood_env(compose: dict) -> None:
    canary = compose["services"]["darkfac-canary"]
    command = canary["command"]
    assert command[command.index("--every-seconds") + 1] == "900"
    env = "\n".join(canary["environment"])
    assert "DARKFAC_DOGFOOD_MIN_STREAK=${DARKFAC_DOGFOOD_MIN_STREAK:-1}" in env
    assert "DARKFAC_DOGFOOD_ENABLED=${DARKFAC_DOGFOOD_ENABLED:-false}" in env  # stays off by default
    assert "DARKFAC_CANARY_MAX_ATTEMPTS_PER_DAY=${DARKFAC_CANARY_MAX_ATTEMPTS_PER_DAY:-4}" in env


def test_worker_offloads_the_official_gate_to_the_desktop_test_worker(compose: dict) -> None:
    env = "\n".join(compose["services"]["darkfac-worker"]["environment"])
    assert "DARKFAC_TEST_WORKERS=${DARKFAC_TEST_WORKERS:-http://100.78.181.90:8080}" in env
    assert "DARKFAC_WORKER_TOKEN=${DARKFAC_WORKER_TOKEN:-}" in env
