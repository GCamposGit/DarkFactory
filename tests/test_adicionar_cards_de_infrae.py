"""
Automated Test Suite for USR-15:
Adicionar cards de infraestrutura no Hub.

Reachability Contract:
python -m pytest tests/test_adicionar_cards_de_infrae.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.infra.cards import (
    InfraCard,
    InfraCardsReport,
    InfraLink,
    InfraServiceSummary,
    build_infra_cards_report,
    probe_liveness,
)
from core.infra.inventory import InventoryManager, build_default_inventory
from core.infra.models import (
    HardwareSpec,
    InfraInventory,
    InfraNode,
    NetworkSpec,
    NodeRole,
    NodeStatus,
    ServiceItem,
)
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient fixture."""
    return TestClient(app)


# =========================================================================
# 1. Domain Model & Builder Unit Tests
# =========================================================================

def test_infra_card_models_validation() -> None:
    """Verify that InfraLink, InfraCard, and InfraCardsReport validate cleanly."""
    link = InfraLink(
        title="Dokploy PaaS",
        url="https://dokploy.ggcampos.com",
        category="management",
        is_primary=True,
        badge="SSL 200 OK",
    )
    assert link.title == "Dokploy PaaS"
    assert link.url == "https://dokploy.ggcampos.com"
    assert link.is_primary is True
    assert link.badge == "SSL 200 OK"

    service = InfraServiceSummary(
        name="dokploy-paas",
        description="Dokploy orchestrator",
        status=NodeStatus.ACTIVE,
        port=3000,
        managed_by="docker",
    )
    assert service.port == 3000

    card = InfraCard(
        id="test-vps",
        name="Test VPS",
        role=NodeRole.CLOUD_VPS,
        role_label="Cloud VPS",
        provider="Hetzner",
        status=NodeStatus.ACTIVE,
        services=[service],
        links=[link],
        cost_monthly_usd=7.19,
    )
    assert card.id == "test-vps"
    assert card.role == NodeRole.CLOUD_VPS
    assert card.cost_monthly_usd == 7.19
    assert len(card.links) == 1

    report = InfraCardsReport(
        version="1.0.0",
        total_nodes=1,
        active_nodes=1,
        total_monthly_budget_usd=7.19,
        cards=[card],
    )
    assert report.total_nodes == 1
    assert report.total_monthly_budget_usd == 7.19

    # Test round-trip JSON serialization
    raw_json = report.model_dump_json()
    reparsed = InfraCardsReport.model_validate_json(raw_json)
    assert reparsed.cards[0].name == "Test VPS"


def test_build_infra_cards_report_from_default_inventory() -> None:
    """Verify that build_infra_cards_report extracts all nodes and specifications correctly."""
    inventory = build_default_inventory()
    report = build_infra_cards_report(inventory, probe_network_liveness=False)

    assert report.total_nodes >= 6
    assert report.active_nodes >= 1
    assert report.total_monthly_budget_usd > 0.0

    card_ids = [c.id for c in report.cards]
    assert "predator-neo-16" in card_ids
    assert "onprem-z97-server" in card_ids
    assert "darkfac-vps-primary" in card_ids or "cloud-vps-primary" in card_ids
    assert "cloudflare-edge" in card_ids
    assert "hostinger-web" in card_ids


# =========================================================================
# 2. System Links Verification (Acceptance Criteria)
# =========================================================================

def test_system_access_links_integrity() -> None:
    """Verify that every node card contains correct, valid system access URLs."""
    inventory = build_default_inventory()
    report = build_infra_cards_report(inventory, probe_network_liveness=False)
    cards_by_id = {c.id: c for c in report.cards}

    # 1. DarkFac VPS / Hetzner node must link to Dokploy PaaS and Tailscale
    vps_id = "darkfac-vps-primary" if "darkfac-vps-primary" in cards_by_id else "cloud-vps-primary"
    vps_card = cards_by_id[vps_id]
    vps_urls = [link.url for link in vps_card.links]
    assert any("dokploy.ggcampos.com" in url for url in vps_urls), "Dokploy URL missing"
    assert any("login.tailscale.com" in url for url in vps_urls), "Tailscale Admin URL missing"
    assert any("console.hetzner.cloud" in url for url in vps_urls), "Hetzner Console URL missing"

    # 2. Predator Workstation must link to Ollama local and Tailscale
    predator_card = cards_by_id["predator-neo-16"]
    predator_urls = [link.url for link in predator_card.links]
    assert any("localhost:11434" in url for url in predator_urls), "Ollama Local URL missing"
    assert any("login.tailscale.com" in url for url in predator_urls), "Tailscale URL missing"

    # 3. On-Premises Server must link to Google Remote Desktop and Tailscale
    onprem_card = cards_by_id["onprem-z97-server"]
    onprem_urls = [link.url for link in onprem_card.links]
    assert any("remotedesktop.google.com" in url for url in onprem_urls), "Google Remote Desktop missing"
    assert any("login.tailscale.com" in url for url in onprem_urls), "Tailscale URL missing"

    # 4. Cloudflare Edge must link to Cloudflare Dashboard
    cf_card = cards_by_id["cloudflare-edge"]
    cf_urls = [link.url for link in cf_card.links]
    assert any("dash.cloudflare.com" in url for url in cf_urls), "Cloudflare Dashboard missing"

    # 5. Hostinger must link to hPanel
    hostinger_card = cards_by_id["hostinger-web"]
    hostinger_urls = [link.url for link in hostinger_card.links]
    assert any("hpanel.hostinger.com" in url for url in hostinger_urls), "Hostinger hPanel missing"


# =========================================================================
# 3. Liveness Probing & Fault-Tolerance Tests
# =========================================================================

def test_probe_liveness_graceful_failure() -> None:
    """Verify probe_liveness returns False without raising exceptions on unreachable targets."""
    card = InfraCard(
        id="offline-node",
        name="Offline Node",
        role=NodeRole.DEV_WORKSTATION,
        role_label="Workstation Dev",
        provider="Local",
        status=NodeStatus.ACTIVE,
        links=[
            InfraLink(
                title="Unreachable Port",
                url="http://127.0.0.1:59999",
                is_primary=True,
            )
        ],
    )
    # Probing an unopened local port must return False quickly without crashing
    reachable = probe_liveness(card, timeout=0.2)
    assert reachable is False


def test_probe_liveness_successful_mock() -> None:
    """Verify probe_liveness returns True when primary link responds with HTTP success."""
    card = InfraCard(
        id="mock-node",
        name="Mock Node",
        role=NodeRole.CLOUD_VPS,
        role_label="Cloud VPS",
        provider="Hetzner",
        status=NodeStatus.ACTIVE,
        links=[
            InfraLink(
                title="Mock Web",
                url="https://example.com",
                is_primary=True,
            )
        ],
    )
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        reachable = probe_liveness(card, timeout=0.5)
        assert reachable is True


# =========================================================================
# 4. REST API Endpoint Tests (Headless & Deterministic)
# =========================================================================

def test_api_get_infra_cards(client: TestClient) -> None:
    """Verify GET /api/infra/cards returns 200 and a complete InfraCardsReport."""
    resp = client.get("/api/infra/cards")
    assert resp.status_code == 200
    data = resp.json()

    assert "total_nodes" in data
    assert "active_nodes" in data
    assert "total_monthly_budget_usd" in data
    assert "cards" in data
    assert isinstance(data["cards"], list)
    assert data["total_nodes"] >= 6
    assert data["total_monthly_budget_usd"] > 0

    node_ids = [c["id"] for c in data["cards"]]
    assert "predator-neo-16" in node_ids
    assert "onprem-z97-server" in node_ids
    assert "cloudflare-edge" in node_ids


def test_api_post_infra_cards_refresh(client: TestClient) -> None:
    """Verify POST /api/infra/cards/refresh forces probe refresh and returns 200."""
    with patch("core.infra.cards.probe_liveness", return_value=True):
        resp = client.post("/api/infra/cards/refresh?timeout=0.1")
        assert resp.status_code == 200
        data = resp.json()
        assert "cards" in data
        assert all(c["is_live_reachable"] is True for c in data["cards"])


def test_api_get_single_node_card(client: TestClient) -> None:
    """Verify GET /api/infra/cards/{node_id} returns 200 for existing node and 404 for missing."""
    # Existing node: predator-neo-16
    resp_predator = client.get("/api/infra/cards/predator-neo-16")
    assert resp_predator.status_code == 200
    predator = resp_predator.json()
    assert predator["id"] == "predator-neo-16"
    assert predator["role"] == "dev_workstation"
    assert "RTX 4070" in predator["hardware_summary"]

    # Missing node: non-existent-slug
    resp_missing = client.get("/api/infra/cards/non-existent-slug")
    assert resp_missing.status_code == 404
    assert "not found" in resp_missing.json()["detail"].lower()


# =========================================================================
# 5. Frontend Integration & Reachability Tests
# =========================================================================

def test_frontend_infra_assets_mounted() -> None:
    """Verify index.html and infra.js exist, are linked, and contain necessary selectors."""
    root_dir = Path(__file__).resolve().parent.parent
    html_path = root_dir / "hub" / "frontend" / "index.html"
    js_path = root_dir / "hub" / "frontend" / "infra.js"

    assert html_path.exists(), "hub/frontend/index.html must exist"
    assert js_path.exists(), "hub/frontend/infra.js must exist"

    html_content = html_path.read_text(encoding="utf-8")
    assert "infra.js" in html_content, "index.html must reference infra.js"

    js_content = js_path.read_text(encoding="utf-8")
    assert "/api/infra/cards" in js_content, "infra.js must fetch /api/infra/cards"
    assert "mountInfraMonitor" in js_content, "infra.js must define mountInfraMonitor"
    assert "renderInfraCards" in js_content, "infra.js must define renderInfraCards"
    assert "refresh-infra-cards" in js_content, "infra.js must attach refresh button event"


# =========================================================================
# 6. Non-Goals Compliance Test
# =========================================================================

def test_non_goals_no_administrative_mutation_endpoints(client: TestClient) -> None:
    """Ensure read-only contract: no management/mutation routes exist on /api/infra."""
    # Attempting POST to create or DELETE a card must return 405 Method Not Allowed or 404
    resp_post = client.post("/api/infra/cards", json={"id": "fake"})
    assert resp_post.status_code in (404, 405)

    resp_delete = client.delete("/api/infra/cards/predator-neo-16")
    assert resp_delete.status_code in (404, 405)
