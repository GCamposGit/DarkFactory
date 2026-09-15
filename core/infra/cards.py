"""Headless domain models and builder for Dark Factory infrastructure cards."""

from __future__ import annotations

import logging
import socket
import urllib.request
from datetime import datetime, timezone
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field

from core.infra.models import (
    InfraInventory,
    InfraNode,
    NodeRole,
    NodeStatus,
    ServiceItem,
)

logger = logging.getLogger(__name__)

ROLE_LABELS: dict[NodeRole, str] = {
    NodeRole.DEV_WORKSTATION: "Workstation Dev",
    NodeRole.ON_PREM_SERVER: "Servidor On-Premises",
    NodeRole.CLOUD_VPS: "Cloud VPS",
    NodeRole.EDGE_GATEWAY: "Edge Gateway",
    NodeRole.MANAGED_SERVICE: "Serviço Gerenciado",
}


class InfraLink(BaseModel):
    """Direct actionable link for an infrastructure service or dashboard."""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(description="Descriptive link title (e.g. Dokploy PaaS, Tailscale Admin)")
    url: str = Field(description="Target HTTP/HTTPS URL")
    category: str = Field(default="system", description="Link category: management, app, mesh, cloud")
    is_primary: bool = Field(default=False, description="Whether this is the primary access action")
    badge: Optional[str] = Field(default=None, description="Optional badge or port label (e.g. :3000, SSL)")


class InfraServiceSummary(BaseModel):
    """Clean summary of a hosted service or container."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Service name or container slug")
    description: str = Field(description="Service description")
    status: NodeStatus = Field(default=NodeStatus.ACTIVE)
    port: Optional[int] = Field(default=None)
    managed_by: Optional[str] = Field(default=None)
    container_engine: Optional[str] = Field(default=None)


class InfraCard(BaseModel):
    """UI-ready headless data contract for a discrete infrastructure node card."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Node slug ID (e.g. darkfac-vps-primary)")
    name: str = Field(description="Human readable node name")
    role: NodeRole = Field(description="Operational role enum")
    role_label: str = Field(description="Localized role label for presentation")
    provider: str = Field(description="Provider or datacenter location")
    status: NodeStatus = Field(default=NodeStatus.ACTIVE)
    is_live_reachable: Optional[bool] = Field(default=None, description="Result of live network/liveness probe")
    hardware_summary: Optional[str] = Field(default=None, description="Compact hardware specs")
    network_summary: dict[str, Any] = Field(default_factory=dict, description="IPs, mesh and ingress specs")
    services: List[InfraServiceSummary] = Field(default_factory=list)
    links: List[InfraLink] = Field(default_factory=list)
    cost_monthly_usd: float = Field(default=0.0)
    tags: List[str] = Field(default_factory=list)
    notes: Optional[str] = Field(default=None)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InfraCardsReport(BaseModel):
    """Aggregated infrastructure cards payload for the Hub."""

    model_config = ConfigDict(extra="ignore")

    version: str = Field(default="1.0.0")
    total_nodes: int = Field(default=0)
    active_nodes: int = Field(default=0)
    total_monthly_budget_usd: float = Field(default=0.0)
    cards: List[InfraCard] = Field(default_factory=list)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _build_hardware_summary(node: InfraNode) -> Optional[str]:
    """Compose a concise human-readable hardware string."""
    if not node.hardware:
        return None
    hw = node.hardware
    parts: list[str] = []
    if hw.cpu:
        parts.append(hw.cpu.split("(")[0].strip())
    if hw.ram_gb:
        parts.append(f"{hw.ram_gb:g} GB RAM")
    if hw.storage_primary:
        parts.append(hw.storage_primary)
    if hw.gpu:
        parts.append(hw.gpu.split("(")[0].strip())
    return " · ".join(parts) if parts else None


def _build_network_summary(node: InfraNode) -> dict[str, Any]:
    """Compose network specifications dictionary."""
    if not node.network:
        return {}
    net = node.network
    summary: dict[str, Any] = {}
    if net.public_ip:
        summary["public_ip"] = net.public_ip
    if net.ipv6:
        summary["ipv6"] = net.ipv6
    if net.tailscale_ip:
        summary["tailscale_ip"] = net.tailscale_ip
    if net.public_dns:
        summary["public_dns"] = net.public_dns
    if net.cloudflare_tunnel:
        summary["cloudflare_tunnel"] = True
    if net.open_ports:
        summary["open_ports"] = net.open_ports
    return summary


def _build_system_links(node: InfraNode) -> List[InfraLink]:
    """Generate official, valid system and dashboard URLs for the node."""
    links: List[InfraLink] = []

    # 1. DarkFac VPS Primary (Hetzner + Dokploy)
    if node.id in ("darkfac-vps-primary", "cloud-vps-primary"):
        if node.network and node.network.public_dns:
            fqdn = node.network.public_dns
            scheme = "https" if not fqdn.startswith("http") else ""
            url = f"https://{fqdn}" if scheme else fqdn
            links.append(
                InfraLink(
                    title="Dokploy PaaS",
                    url=url,
                    category="management",
                    is_primary=True,
                    badge="SSL 200 OK",
                )
            )
        links.append(
            InfraLink(
                title="Tailscale Admin",
                url="https://login.tailscale.com/admin/machines",
                category="mesh",
                badge="100.83.176.60" if node.network and node.network.tailscale_ip else None,
            )
        )
        links.append(
            InfraLink(
                title="DarkHub 24/7",
                url="https://darkhub.ggcampos.com",
                category="hub",
                badge="Cloud 24/7",
            )
        )
        links.append(
            InfraLink(
                title="Hetzner Console",
                url="https://console.hetzner.cloud",
                category="cloud",
                badge="#165058444",
            )
        )

    # 2. Predator Dev Workstation
    elif node.id == "predator-neo-16":
        links.append(
            InfraLink(
                title="Ollama Local (RTX 4070)",
                url="http://localhost:11434",
                category="app",
                is_primary=True,
                badge=":11434",
            )
        )
        links.append(
            InfraLink(
                title="Tailscale Admin",
                url="https://login.tailscale.com/admin/machines",
                category="mesh",
                badge="100.81.84.124" if node.network and node.network.tailscale_ip else None,
            )
        )

    # 3. On-Premises Server (desktop-g45ipem)
    elif node.id == "onprem-z97-server":
        links.append(
            InfraLink(
                title="Google Remote Desktop",
                url="https://remotedesktop.google.com/access",
                category="management",
                is_primary=True,
                badge="Chrome Desktop",
            )
        )
        links.append(
            InfraLink(
                title="Tailscale Admin",
                url="https://login.tailscale.com/admin/machines",
                category="mesh",
                badge="100.78.181.90" if node.network and node.network.tailscale_ip else None,
            )
        )

    # 4. Cloudflare Edge Gateway
    elif node.id == "cloudflare-edge":
        links.append(
            InfraLink(
                title="Cloudflare Dashboard",
                url="https://dash.cloudflare.com",
                category="cloud",
                is_primary=True,
                badge="Zero Trust",
            )
        )

    # 5. Hostinger Simple Web Hosting
    elif node.id == "hostinger-web":
        links.append(
            InfraLink(
                title="Hostinger hPanel",
                url="https://hpanel.hostinger.com",
                category="cloud",
                is_primary=True,
                badge="Web Hosting",
            )
        )

    # 6. n8n Automation Engine
    elif node.id == "n8n-automation":
        links.append(
            InfraLink(
                title="n8n Workflows",
                url="https://n8n.io",
                category="app",
                is_primary=True,
                badge="Automation",
            )
        )

    # 7. Google Platform (GCP & Drive)
    elif node.id == "google-platform":
        links.append(
            InfraLink(
                title="Google Cloud Console",
                url="https://console.cloud.google.com",
                category="cloud",
                is_primary=True,
                badge="GCP",
            )
        )
        links.append(
            InfraLink(
                title="Google Drive Storage",
                url="https://drive.google.com",
                category="cloud",
                badge="Cold Backup",
            )
        )

    return links


def probe_liveness(card: InfraCard, timeout: float = 0.5) -> bool:
    """Fast, fail-safe liveness check for node primary URL or IP port.

    Never raises exceptions; returns False on connection error/timeout.
    """
    primary_link = next((l for l in card.links if l.is_primary), None)
    if not primary_link:
        return card.status == NodeStatus.ACTIVE

    url = primary_link.url
    try:
        if url.startswith("http://localhost:") or url.startswith("http://127.0.0.1:"):
            # Local port probe via socket connect (fastest)
            port = int(url.split(":")[-1].split("/")[0])
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            return result == 0
        elif url.startswith("http://") or url.startswith("https://"):
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "DarkFac-Infra-Probe/1.0"},
                method="HEAD",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return bool(resp.status < 500)
    except Exception as exc:
        logger.debug("Liveness probe for %s (%s) returned unreachable: %s", card.id, url, exc)
        return False
    return False


def _matches_project(node: InfraNode, project_id: Optional[str]) -> bool:
    if not project_id:
        return True
    tag_set = {t.lower() for t in node.tags}
    target_tag = f"project:{project_id.lower()}"
    if target_tag in tag_set or "project:all" in tag_set or "shared" in tag_set:
        return True
    has_any_project_tag = any(t.startswith("project:") for t in tag_set)
    if not has_any_project_tag and project_id == "darkfac":
        return True
    return False


def build_infra_cards_report(
    inventory: InfraInventory,
    probe_network_liveness: bool = False,
    probe_timeout: float = 0.5,
    project_id: Optional[str] = None,
) -> InfraCardsReport:
    """Transform an InfraInventory into a UI-ready, headless InfraCardsReport."""
    cards: List[InfraCard] = []

    for node in inventory.nodes:
        if not _matches_project(node, project_id):
            continue
        service_summaries = [
            InfraServiceSummary(
                name=s.name,
                description=s.description,
                status=s.status,
                port=s.port,
                managed_by=s.managed_by,
                container_engine=s.container_engine,
            )
            for s in node.services
        ]

        links = _build_system_links(node)
        hardware_summary = _build_hardware_summary(node)
        network_summary = _build_network_summary(node)
        role_label = ROLE_LABELS.get(node.role, node.role.value.replace("_", " ").title())

        card = InfraCard(
            id=node.id,
            name=node.name,
            role=node.role,
            role_label=role_label,
            provider=node.provider,
            status=node.status,
            is_live_reachable=None,
            hardware_summary=hardware_summary,
            network_summary=network_summary,
            services=service_summaries,
            links=links,
            cost_monthly_usd=node.cost_monthly_usd,
            tags=node.tags,
            notes=node.hardware.notes if node.hardware and node.hardware.notes else None,
            updated_at=node.updated_at,
        )

        if probe_network_liveness:
            card.is_live_reachable = probe_liveness(card, timeout=probe_timeout)
        else:
            # Default to active status if not probing
            card.is_live_reachable = (node.status == NodeStatus.ACTIVE)

        cards.append(card)

    active_count = sum(1 for c in cards if c.status == NodeStatus.ACTIVE)
    total_cost = sum(c.cost_monthly_usd for c in cards)

    return InfraCardsReport(
        version=inventory.version,
        total_nodes=len(cards),
        active_nodes=active_count,
        total_monthly_budget_usd=round(total_cost, 2),
        cards=cards,
        observed_at=datetime.now(timezone.utc),
    )
