"""Durable inventory management and reporting for Dark Factory infrastructure."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.infra.models import (
    ArchitectureDecisionRecord,
    HardwareSpec,
    InfraInventory,
    InfraNode,
    NetworkSpec,
    NodeRole,
    NodeStatus,
    ServiceItem,
)

logger = logging.getLogger(__name__)

DEFAULT_INVENTORY_PATH = Path(".factory/infra/inventory.json")


def build_default_inventory() -> InfraInventory:
    """Instantiate the initial hybrid infrastructure inventory for Dark Factory."""
    now = datetime.now(timezone.utc)

    # 1. Dev Machine: Predator Helios Neo 16 (ai-notebook)
    predator_node = InfraNode(
        id="predator-neo-16",
        name="Predator Helios Neo 16 (ai-notebook)",
        role=NodeRole.DEV_WORKSTATION,
        status=NodeStatus.ACTIVE,
        provider="Local (Workstation)",
        hardware=HardwareSpec(
            cpu="Intel Core i7 (13th/14th Gen HX)",
            ram_gb=32.0,
            storage_primary="NVMe SSD 1 TB",
            gpu="NVIDIA GeForce RTX 4070 Laptop (8 GB GDDR6 Ada Lovelace)",
            gpu_compute_capability="8.9",
            power_supply="Dedicated Laptop Adapter 330W",
            notes="Windows 11 25H2; Tailscale v1.102.3; primary interactive engineering environment; local high-speed AI inference (Ollama Qwen 2.5/Llama 3.3, faster-whisper).",
        ),
        network=NetworkSpec(
            private_ip="DHCP Local LAN",
            tailscale_ip="100.81.84.124",
            notes="Direct development station; connected to on-premise and VPS via Tailscale mesh (ai-notebook).",
        ),
        services=[
            ServiceItem(
                name="antigravity-ide",
                description="Google Antigravity & developer environment",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
            ServiceItem(
                name="ollama-local",
                description="Local frontier AI acceleration via RTX 4070 (8GB VRAM FP16/INT4)",
                status=NodeStatus.ACTIVE,
                port=11434,
                container_engine=None,
            ),
            ServiceItem(
                name="darkfac-runner",
                description="Dark Factory Core runtime and deterministic harness",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
            ServiceItem(
                name="tailscale-agent",
                description="Tailscale mesh node (100.81.84.124, v1.102.3)",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
        ],
        cost_monthly_usd=0.0,
        tags=["workstation", "primary-dev", "gpu-ada", "active", "tailscale-active"],
        updated_at=now,
    )

    # 2. On-Premises Server: i7-4790K / 2x GTX 980 Ti (desktop-g45ipem)
    onprem_node = InfraNode(
        id="onprem-z97-server",
        name="On-Premises Z97 Dedicated Server (desktop-g45ipem)",
        role=NodeRole.ON_PREM_SERVER,
        status=NodeStatus.ACTIVE,
        provider="Local (Home Lab / On-Prem)",
        hardware=HardwareSpec(
            cpu="Intel Core i7-4790K (4C/8T @ 4.0 GHz, Haswell)",
            ram_gb=16.0,
            storage_primary="Kingston SSDNow V300 240 GB 2.5'' SSD (C:)",
            storage_secondary="Hitachi HDS723030ALA640 3 TB 3.5'' 7200 RPM HDD (E:) + Samsung P3 1 TB Ext",
            gpu="2x Zotac AMP Extreme GeForce GTX 980 Ti 6 GB (Dual-GPU, Maxwell)",
            gpu_compute_capability="5.2",
            power_supply="Corsair AX1200 1200 W 80+ Gold Fully Modular",
            notes=(
                "Windows 10 22H2; Tailscale v1.102.3; Docker Desktop ativo com repositório de dados no Drive E: (3 TB). "
                "Acesso visual via Google Remote Desktop. Ideal para storage denso, backups e workers locais."
            ),
        ),
        network=NetworkSpec(
            private_ip="DHCP Local LAN",
            tailscale_ip="100.78.181.90",
            cloudflare_tunnel=True,
            notes="Secure node on Tailscale mesh (desktop-g45ipem); reachable from ai-notebook at 100.78.181.90.",
        ),
        services=[
            ServiceItem(
                name="docker-desktop-onprem",
                description="Docker Desktop (WSL2 backend) com armazenamento alocado no Drive E: (3 TB)",
                status=NodeStatus.ACTIVE,
                managed_by="Docker Desktop",
            ),
            ServiceItem(
                name="tailscale-agent",
                description="Tailscale mesh node (100.78.181.90, v1.102.3)",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
            ServiceItem(
                name="google-remote-desktop",
                description="Acesso visual e controle remoto direto via Google Chrome Remote Desktop",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
            ServiceItem(
                name="storage-backup-vault",
                description="Local storage target (Drive E: 3 TB Hitachi HDD) para dumps e containers",
                status=NodeStatus.ACTIVE,
            ),
        ],
        cost_monthly_usd=0.0,
        tags=["on-prem", "storage-vault", "docker-active", "tailscale-active", "active"],
        updated_at=now,
    )

    # 3. Cloud VPS: Hetzner Cloud Production Hub (darkfac-vps-primary)
    cloud_vps_node = InfraNode(
        id="darkfac-vps-primary",
        name="Hetzner CX23 (#165058444)",
        role=NodeRole.CLOUD_VPS,
        status=NodeStatus.ACTIVE,
        provider="Hetzner Cloud (Falkenstein, Germany)",
        hardware=HardwareSpec(
            cpu="2 vCPU x86 (Hetzner CX23)",
            ram_gb=4.0,
            storage_primary="40 GB NVMe SSD",
            power_supply="Hetzner Datacenter Park Falkenstein (100% Green Energy)",
            notes="Server ID: #165058444; 20 TB tráfego mensal; SLA 99.9%; faturamento ~US$ 7.19/mês ($0.010/h + $0.001/h IPv4).",
        ),
        network=NetworkSpec(
            public_ip="178.105.73.168",
            ipv6="2a01:4f8:c014:634::/64",
            public_dns="dokploy.ggcampos.com",
            cloudflare_tunnel=True,
            tailscale_ip="100.83.176.60",
            open_ports=[22, 80, 443, 3000],
            notes="Hetzner Server #165058444. IPv4 178.105.73.168. FQDN: https://dokploy.ggcampos.com (SSL 200 OK). Tailscale: 100.83.176.60.",
        ),
        services=[
            ServiceItem(
                name="dokploy-paas",
                description="Dokploy self-hosted PaaS orchestrator (Docker Swarm, Traefik & Git Webhooks)",
                status=NodeStatus.ACTIVE,
                port=3000,
                managed_by="systemd / docker",
            ),
            ServiceItem(
                name="tailscale-agent",
                description="Tailscale mesh node (100.83.176.60, active point-to-point tunnel)",
                status=NodeStatus.ACTIVE,
                container_engine=None,
            ),
            ServiceItem(
                name="multi-tenant-postgres",
                description="Instância central PostgreSQL 16+ com bancos de dados isolados por projeto em NVMe",
                status=NodeStatus.ACTIVE,
                port=5432,
                managed_by="Dokploy",
            ),
            ServiceItem(
                name="darkhub-247-gateway",
                description="DarkHub Core 24/7 & Gateway de Webhooks Autônomos com SSL e proteção Cloudflare Zero Trust (USR-18)",
                status=NodeStatus.ACTIVE,
                port=8000,
                managed_by="Dokploy",
            ),
            ServiceItem(
                name="valkey-redis",
                description="Broker de cache e filas de alta performance em memória",
                status=NodeStatus.PLANNED,
                port=6379,
                managed_by="Dokploy",
            ),
            ServiceItem(
                name="s3-backup-agent",
                description="Dumps diários automáticos para Cloudflare R2 e replicação para o desktop-g45ipem (3 TB)",
                status=NodeStatus.PLANNED,
            ),
        ],
        cost_monthly_usd=7.19,
        tags=["cloud", "vps", "hetzner", "cx23", "falkenstein", "server-165058444", "dokploy-active", "tailscale-active", "production", "active"],
        updated_at=now,
    )

    # 4. Cloudflare Edge Gateway
    cloudflare_node = InfraNode(
        id="cloudflare-edge",
        name="Cloudflare Global Network & Security",
        role=NodeRole.EDGE_GATEWAY,
        status=NodeStatus.ACTIVE,
        provider="Cloudflare",
        network=NetworkSpec(
            public_dns="Managed DNS Zone",
            cloudflare_tunnel=True,
            notes="DNS management, Email Routing, DDoS mitigation, SSL Edge termination, Zero Trust Access.",
        ),
        services=[
            ServiceItem(
                name="cloudflare-dns",
                description="Authoritative DNS with fast propagation and proxy capabilities",
                status=NodeStatus.ACTIVE,
            ),
            ServiceItem(
                name="email-routing",
                description="Inbound email forwarder to personal address without separate mail server costs",
                status=NodeStatus.ACTIVE,
            ),
            ServiceItem(
                name="cloudflare-tunnel",
                description="Secure outbound-only tunnel to connect VPS and On-Prem without open firewall ports",
                status=NodeStatus.ACTIVE,
            ),
        ],
        cost_monthly_usd=0.0,
        tags=["edge", "dns", "security", "tunnel", "active"],
        updated_at=now,
    )

    # 5. Hostinger Web Hosting
    hostinger_node = InfraNode(
        id="hostinger-web",
        name="Hostinger Simple Web Hosting",
        role=NodeRole.MANAGED_SERVICE,
        status=NodeStatus.ACTIVE,
        provider="Hostinger",
        network=NetworkSpec(
            public_dns="Public websites / landing pages",
            notes="Traditional shared hosting or basic VPS account used for simple static/PHP sites.",
        ),
        services=[
            ServiceItem(
                name="static-web-hosting",
                description="Landing pages, showcase sites and marketing portals",
                status=NodeStatus.ACTIVE,
            )
        ],
        cost_monthly_usd=4.0,
        tags=["hosting", "web", "external", "active"],
        updated_at=now,
    )

    # 6. n8n Automation Engine
    n8n_node = InfraNode(
        id="n8n-automation",
        name="n8n Community Self-Hosted (VPS Hetzner CX23)",
        role=NodeRole.MANAGED_SERVICE,
        status=NodeStatus.ACTIVE,
        provider="Dokploy PaaS / Docker (darkfac-vps-primary)",
        network=NetworkSpec(
            public_dns="n8n.ggcampos.com",
            open_ports=[5678],
            notes="Traefik TLS reverse proxy on dokploy-network. Automated Let's Encrypt certificates. Webhooks enabled.",
        ),
        services=[
            ServiceItem(
                name="n8n-app",
                description="n8n Community workflow automation engine for autonomous DarkFac agents",
                status=NodeStatus.ACTIVE,
                port=5678,
                managed_by="Dokploy / Docker Compose",
            ),
            ServiceItem(
                name="n8n-postgres",
                description="Dedicated PostgreSQL 16 database for n8n workflow state and credentials",
                status=NodeStatus.ACTIVE,
                port=5432,
                managed_by="Dokploy / Docker Compose",
            ),
        ],
        cost_monthly_usd=0.0,
        tags=["automation", "workflows", "webhooks", "n8n-community", "dokploy", "vps", "active"],
        updated_at=now,
    )

    # 7. Google Cloud & Drive Storage
    google_node = InfraNode(
        id="google-platform",
        name="Google Account (GCP & Drive Storage)",
        role=NodeRole.MANAGED_SERVICE,
        status=NodeStatus.ACTIVE,
        provider="Google",
        services=[
            ServiceItem(
                name="google-drive-cold-storage",
                description="Off-site secondary replication destination for critical database dumps and configs",
                status=NodeStatus.ACTIVE,
            ),
            ServiceItem(
                name="google-oauth-zero-trust",
                description="Identity Provider (IdP) for Cloudflare Access authentication",
                status=NodeStatus.ACTIVE,
            ),
        ],
        cost_monthly_usd=0.0,
        tags=["cloud", "storage", "auth", "active"],
        updated_at=now,
    )

    # Architecture Decision Records
    adrs = [
        ArchitectureDecisionRecord(
            adr_id="ADR-001",
            title="Hybrid Multi-Project Infrastructure Topology",
            status="ACCEPTED",
            date="2026-09-07",
            context=(
                "Need to transition from managing a single isolated project to hosting and managing "
                "multiple projects simultaneously with high reliability, cost-efficiency, and security."
            ),
            decision=(
                "Adopt a 3-tier Hybrid Topology: (1) Workstation for high-speed local dev and modern RTX 4070 AI inference; "
                "(2) On-Premises i7-4790K server for dense 3TB local backups, CI runner, and staging workers; "
                "(3) Cloud VPS running Dokploy/Docker for 24/7 client-facing projects, APIs, and databases."
            ),
            consequences=[
                "Zero operational waste: existing hardware is maximized for its true strengths.",
                "Client-facing services stay online 24/7 on high-speed datacenter fiber without domestic internet risks.",
                "Sensitive backups exist simultaneously in the cloud and on local physical media.",
            ],
            alternatives_considered=[
                "Host everything on home on-premise server: Rejected due to ISP dynamic IP, residential power cuts, and upload limitations.",
                "Host everything on AWS/GCP hyperscalers: Rejected due to prohibitive and unpredictable costs for multi-project dev.",
            ],
        ),
        ArchitectureDecisionRecord(
            adr_id="ADR-002",
            title="VPS Selection & PaaS Orchestration (Dokploy vs Coolify)",
            status="ACCEPTED",
            date="2026-09-07",
            context=(
                "Multi-project scaling requires automated container orchestration, Git-push deployments, "
                "automated SSL certificates, and multi-tenant isolation without complex Kubernetes overhead."
            ),
            decision=(
                "Provision a budget high-performance VPS (Hetzner CPX21/CPX31 or Hostinger KVM) running Dokploy "
                "(or Coolify) atop Docker Engine. Use Dokploy for lightweight resource efficiency (~350MB idle RAM)."
            ),
            consequences=[
                "Instant creation of new isolated projects with dedicated subdomains and SSL in minutes.",
                "Developers deploy via git push without manual SSH/Nginx/Certbot maintenance.",
                "Slight learning curve for the PaaS interface.",
            ],
            alternatives_considered=[
                "Manual Docker Compose per project with manual Nginx reverse proxy: Rejected due to maintenance overhead.",
                "Kubernetes / k3s: Rejected as overly complex for initial multi-project operational scale.",
            ],
        ),
        ArchitectureDecisionRecord(
            adr_id="ADR-003",
            title="Database Architecture & Zero-Cost Off-Site Backups",
            status="ACCEPTED",
            date="2026-09-07",
            context="Multiple projects require reliable SQL databases with high performance and zero data loss risk.",
            decision=(
                "Standardize on PostgreSQL 16+ deployed via Dokploy on NVMe storage. Implement an automated cron "
                "worker streaming daily compressed and encrypted dumps to Cloudflare R2 (10GB free tier, zero egress fees) "
                "with mirror sync to the On-Premises 3TB storage vault."
            ),
            consequences=[
                "No per-database monthly fee on cloud providers.",
                "RPO < 24 hours (or < 6 hours for critical tables) with zero egress egress cost.",
                "Complete portability: database can be restored anywhere in under 10 minutes.",
            ],
            alternatives_considered=[
                "Managed AWS RDS / DigitalOcean Managed DB: Rejected due to high baseline costs (~$15-30/mo per instance).",
                "SQLite for everything: Rejected due to multi-user concurrency and API separation limits.",
            ],
        ),
        ArchitectureDecisionRecord(
            adr_id="ADR-004",
            title="Secure Edge Networking via Cloudflare Tunnels and Tailscale Mesh",
            status="ACCEPTED",
            date="2026-09-07",
            context=(
                "Need to access internal administrative dashboards, on-premise staging, and VPS services "
                "securely without exposing dangerous ports (e.g. 22 SSH, 5432 Postgres, 11434 Ollama) to the internet."
            ),
            decision=(
                "Use Cloudflare Tunnels (cloudflared) for public web applications with Cloudflare WAF, and use "
                "Tailscale mesh VPN for private node-to-node management (Predator <-> On-Premise <-> VPS)."
            ),
            consequences=[
                "Zero open ports required on home router or VPS management interfaces.",
                "Cloudflare Access protects admin panels with Google OAuth single-sign-on.",
                "End-to-end encrypted WireGuard performance between all nodes.",
            ],
            alternatives_considered=[
                "Opening ports on home router with Dynamic DNS: Rejected due to severe security and scanning risks.",
                "Traditional OpenVPN server: Rejected due to configuration overhead and certificate management.",
            ],
        ),
    ]

    nodes = [predator_node, onprem_node, cloud_vps_node, cloudflare_node, hostinger_node, n8n_node, google_node]
    total_cost = sum(n.cost_monthly_usd for n in nodes)

    return InfraInventory(
        version="1.0.0",
        last_updated=now,
        nodes=nodes,
        adrs=adrs,
        total_monthly_budget_usd=total_cost,
        metadata={
            "owner": "Dark Factory Core",
            "managed_by": "core.infra",
            "topology_type": "Hybrid (Workstation + On-Premise + Cloud VPS + Edge)",
        },
    )


class InventoryManager:
    """Manages reading, persisting, and querying the Dark Factory infrastructure inventory."""

    def __init__(self, inventory_path: Path | str = DEFAULT_INVENTORY_PATH) -> None:
        self.inventory_path = Path(inventory_path)

    def load_or_initialize(self) -> InfraInventory:
        """Load inventory from file, or initialize with defaults if not present."""
        if not self.inventory_path.exists():
            inventory = build_default_inventory()
            self.save(inventory)
            return inventory

        try:
            content = self.inventory_path.read_text(encoding="utf-8")
            data = json.loads(content)
            return InfraInventory.model_validate(data)
        except Exception as exc:
            logger.warning("Failed to load inventory from %s, falling back to defaults: %s", self.inventory_path, exc)
            return build_default_inventory()

    def save(self, inventory: InfraInventory) -> None:
        """Atomically persist inventory to disk."""
        self.inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory.last_updated = datetime.now(timezone.utc)
        inventory.total_monthly_budget_usd = sum(n.cost_monthly_usd for n in inventory.nodes)
        
        # Serialize to formatted JSON
        serialized = inventory.model_dump_json(indent=2)
        self.inventory_path.write_text(serialized, encoding="utf-8")
        logger.info("Saved infrastructure inventory to %s", self.inventory_path)

    def get_node(self, node_id: str) -> InfraNode | None:
        """Retrieve a node by its unique ID."""
        inventory = self.load_or_initialize()
        for node in inventory.nodes:
            if node.id == node_id:
                return node
        return None

    def update_node_status(self, node_id: str, new_status: NodeStatus) -> bool:
        """Update the operational status of an existing node."""
        inventory = self.load_or_initialize()
        for node in inventory.nodes:
            if node.id == node_id:
                node.status = new_status
                node.updated_at = datetime.now(timezone.utc)
                self.save(inventory)
                return True
        return False

    def export_markdown_summary(self) -> str:
        """Generate a clean Markdown status overview suitable for reports or docs."""
        inventory = self.load_or_initialize()
        lines: list[str] = [
            "# Dark Factory - Mapeamento e Status de Infraestrutura",
            "",
            f"- **Versão do Inventário**: `{inventory.version}`",
            f"- **Última Atualização**: `{inventory.last_updated.isoformat()}`",
            f"- **Orçamento Mensal Estimado**: `US$ {inventory.total_monthly_budget_usd:.2f}/mês`",
            "",
            "## 1. Nós da Topologia Híbrida",
            "",
            "| ID | Nome | Papel | Provedor | Status | Custo/mês |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for n in inventory.nodes:
            status_indicator = "🟢" if n.status == NodeStatus.ACTIVE else ("🟡" if n.status == NodeStatus.STANDBY else "⚪")
            lines.append(
                f"| `{n.id}` | **{n.name}** | `{n.role.value}` | {n.provider} | {status_indicator} `{n.status.value}` | US$ {n.cost_monthly_usd:.2f} |"
            )

        lines.extend([
            "",
            "## 2. Decisões Arquiteturais Registradas (ADRs)",
            "",
        ])

        for adr in inventory.adrs:
            lines.extend([
                f"### {adr.adr_id}: {adr.title} (`{adr.status}`)",
                f"- **Data**: {adr.date}",
                f"- **Contexto**: {adr.context}",
                f"- **Decisão**: {adr.decision}",
                "- **Consequências & Benefícios**:",
            ])
            for cons in adr.consequences:
                lines.append(f"  - {cons}")
            lines.append("")

        return "\n".join(lines)
