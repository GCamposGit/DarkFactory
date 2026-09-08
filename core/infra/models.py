"""Data models and Pydantic schemas for the Dark Factory infrastructure management module."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class NodeRole(str, Enum):
    """Primary operational role of an infrastructure node."""
    DEV_WORKSTATION = "dev_workstation"
    ON_PREM_SERVER = "on_prem_server"
    CLOUD_VPS = "cloud_vps"
    MANAGED_SERVICE = "managed_service"
    EDGE_GATEWAY = "edge_gateway"


class NodeStatus(str, Enum):
    """Lifecycle and operational state of an infrastructure node."""
    ACTIVE = "active"
    PROVISIONING = "provisioning"
    PLANNED = "planned"
    STANDBY = "standby"
    OFFLINE = "offline"


class HardwareSpec(BaseModel):
    """Physical or virtual hardware specifications of a node."""
    model_config = ConfigDict(extra="ignore")

    cpu: str = Field(description="CPU model and core/thread configuration")
    ram_gb: float = Field(description="Total RAM capacity in Gigabytes")
    storage_primary: str = Field(description="Primary boot/OS storage drive specification")
    storage_secondary: str | None = Field(default=None, description="Secondary or external bulk storage")
    gpu: str | None = Field(default=None, description="GPU model and VRAM specification")
    gpu_compute_capability: str | None = Field(default=None, description="CUDA compute capability if applicable")
    power_supply: str | None = Field(default=None, description="Power supply rating and efficiency")
    notes: str | None = Field(default=None, description="Hardware constraints, power notes or operational limits")


class NetworkSpec(BaseModel):
    """Network configuration, mesh connectivity and ingress specs."""
    model_config = ConfigDict(extra="ignore")

    private_ip: str | None = Field(default=None, description="Local LAN or internal IP")
    public_ip: str | None = Field(default=None, description="Public IPv4 address")
    ipv6: str | None = Field(default=None, description="Public IPv6 prefix/address")
    tailscale_ip: str | None = Field(default=None, description="Tailscale / WireGuard mesh IP")
    public_dns: str | None = Field(default=None, description="Public domain or subdomain")
    cloudflare_tunnel: bool = Field(default=False, description="Whether Cloudflare Tunnel is enabled")
    open_ports: list[int] = Field(default_factory=list, description="Explicit open network ports")
    notes: str | None = Field(default=None, description="Routing and firewall policies")


class ServiceItem(BaseModel):
    """A software service, container or workload assigned to a node."""
    model_config = ConfigDict(extra="ignore")

    name: str = Field(description="Service identifier or container name")
    description: str = Field(description="Operational function and responsibility")
    container_engine: str | None = Field(default="docker", description="Runtime engine (docker, podman, native)")
    port: int | None = Field(default=None, description="Primary listening port")
    status: NodeStatus = Field(default=NodeStatus.PLANNED, description="Deployment status of the service")
    managed_by: str | None = Field(default=None, description="Orchestrator (e.g., Dokploy, Coolify, systemd)")


class InfraNode(BaseModel):
    """Represents a discrete infrastructure node in the Dark Factory topology."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="Unique slug identifier (e.g., predator-neo-16, onprem-z97-server)")
    name: str = Field(description="Human-readable node name")
    role: NodeRole = Field(description="Operational role of the node")
    status: NodeStatus = Field(default=NodeStatus.ACTIVE, description="Current operational status")
    provider: str = Field(description="Physical location or cloud vendor (e.g., Local, Hetzner, Hostinger, Cloudflare)")
    hardware: HardwareSpec | None = Field(default=None, description="Hardware specs if bare-metal or VM")
    network: NetworkSpec | None = Field(default=None, description="Network configuration")
    services: list[ServiceItem] = Field(default_factory=list, description="Services hosted on this node")
    cost_monthly_usd: float = Field(default=0.0, description="Estimated monthly hosting cost in USD")
    tags: list[str] = Field(default_factory=list, description="Descriptive labels")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Last audit timestamp")


class ArchitectureDecisionRecord(BaseModel):
    """Architecture Decision Record (ADR) capturing infrastructure choices."""
    model_config = ConfigDict(extra="ignore")

    adr_id: str = Field(description="ADR number or slug (e.g., ADR-001)")
    title: str = Field(description="Title of the architecture decision")
    status: str = Field(default="ACCEPTED", description="Status: PROPOSED, ACCEPTED, SUPERSEDED, REJECTED")
    date: str = Field(description="ISO date string of the decision")
    context: str = Field(description="Context and motivation for the choice")
    decision: str = Field(description="Chosen solution and rationale")
    consequences: list[str] = Field(default_factory=list, description="Trade-offs, positive and negative consequences")
    alternatives_considered: list[str] = Field(default_factory=list, description="Evaluated alternatives and rejection reasons")


class InfraInventory(BaseModel):
    """Complete catalog of nodes, services, cloud assets and decisions."""
    model_config = ConfigDict(extra="ignore")

    version: str = Field(default="1.0.0", description="Schema version")
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    nodes: list[InfraNode] = Field(default_factory=list, description="All registered infrastructure nodes")
    adrs: list[ArchitectureDecisionRecord] = Field(default_factory=list, description="Architecture Decision Records")
    total_monthly_budget_usd: float = Field(default=0.0, description="Calculated total monthly operational cost")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Arbitrary metadata")
