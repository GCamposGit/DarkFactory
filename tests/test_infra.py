"""Deterministic unit tests for the Dark Factory infrastructure management module."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.infra.cli import main as infra_cli_main
from core.infra.inventory import DEFAULT_INVENTORY_PATH, InventoryManager, build_default_inventory
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


def test_models_validation_and_serialization() -> None:
    """Validate that Pydantic models instantiate, validate types, and serialize cleanly."""
    hw = HardwareSpec(
        cpu="Intel i7-4790K",
        ram_gb=16.0,
        storage_primary="240 GB SSD",
        storage_secondary="3 TB HDD",
        gpu="2x GTX 980 Ti",
        gpu_compute_capability="5.2",
        power_supply="1200W",
    )
    assert hw.ram_gb == 16.0
    assert hw.gpu_compute_capability == "5.2"

    net = NetworkSpec(private_ip="192.168.1.100", cloudflare_tunnel=True, open_ports=[80, 443])
    assert net.cloudflare_tunnel is True
    assert 80 in net.open_ports

    service = ServiceItem(
        name="postgres-test",
        description="Test database",
        status=NodeStatus.ACTIVE,
        port=5432,
    )
    assert service.port == 5432

    node = InfraNode(
        id="test-node",
        name="Test Node",
        role=NodeRole.CLOUD_VPS,
        status=NodeStatus.ACTIVE,
        provider="Hetzner",
        hardware=hw,
        network=net,
        services=[service],
        cost_monthly_usd=10.5,
    )
    assert node.id == "test-node"
    assert node.cost_monthly_usd == 10.5

    # Serialize to JSON and parse back
    raw_json = node.model_dump_json()
    reparsed = InfraNode.model_validate_json(raw_json)
    assert reparsed.name == "Test Node"
    assert reparsed.hardware is not None
    assert reparsed.hardware.ram_gb == 16.0


def test_default_inventory_has_all_user_components() -> None:
    """Verify that build_default_inventory includes predator, on-prem, vps, cloudflare, hostinger, n8n, and google."""
    inv = build_default_inventory()
    assert len(inv.nodes) >= 7
    node_ids = {n.id for n in inv.nodes}

    assert "predator-neo-16" in node_ids
    assert "onprem-z97-server" in node_ids
    assert "cloud-vps-primary" in node_ids
    assert "cloudflare-edge" in node_ids
    assert "hostinger-web" in node_ids
    assert "n8n-automation" in node_ids
    assert "google-platform" in node_ids

    # Verify ADRs are registered
    assert len(inv.adrs) == 4
    adr_ids = {a.adr_id for a in inv.adrs}
    assert {"ADR-001", "ADR-002", "ADR-003", "ADR-004"}.issubset(adr_ids)

    # Check On-Premise GPU specs
    onprem = next(n for n in inv.nodes if n.id == "onprem-z97-server")
    assert onprem.hardware is not None
    assert "980 Ti" in str(onprem.hardware.gpu)
    assert onprem.hardware.gpu_compute_capability == "5.2"
    assert onprem.hardware.ram_gb == 16.0

    # Check Predator specs
    predator = next(n for n in inv.nodes if n.id == "predator-neo-16")
    assert predator.hardware is not None
    assert "4070" in str(predator.hardware.gpu)
    assert predator.hardware.ram_gb == 32.0


def test_inventory_manager_crud_in_temp_dir(tmp_path: Path) -> None:
    """Test loading, saving, updating node status and exporting markdown in an isolated path."""
    test_file = tmp_path / "infra" / "test_inventory.json"
    manager = InventoryManager(inventory_path=test_file)

    # 1. Initialize
    inv = manager.load_or_initialize()
    assert test_file.exists()
    assert len(inv.nodes) >= 7

    # 2. Query node
    predator = manager.get_node("predator-neo-16")
    assert predator is not None
    assert predator.status == NodeStatus.ACTIVE

    non_existent = manager.get_node("non-existent-id")
    assert non_existent is None

    # 3. Update status
    success = manager.update_node_status("cloud-vps-primary", NodeStatus.PROVISIONING)
    assert success is True

    updated_vps = manager.get_node("cloud-vps-primary")
    assert updated_vps is not None
    assert updated_vps.status == NodeStatus.PROVISIONING

    # 4. Export Markdown summary
    summary_md = manager.export_markdown_summary()
    assert "# Dark Factory - Mapeamento e Status de Infraestrutura" in summary_md
    assert "predator-neo-16" in summary_md
    assert "ADR-001" in summary_md


def test_infra_cli_commands(tmp_path: Path) -> None:
    """Test CLI commands: list, status, inspect, and export-markdown."""
    test_file = tmp_path / "inventory_cli.json"
    inv_arg = ["--inventory-path", str(test_file)]

    # 1. list
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = infra_cli_main(inv_arg + ["list"])
    assert ret == 0
    output = buf.getvalue()
    assert "predator-neo-16" in output
    assert "onprem-z97-server" in output

    # 2. status
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = infra_cli_main(inv_arg + ["status"])
    assert ret == 0
    output = buf.getvalue()
    assert "Total de nos:" in output
    assert "Decisoes arquiteturais registradas:" in output

    # 3. inspect
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = infra_cli_main(inv_arg + ["inspect", "onprem-z97-server"])
    assert ret == 0
    output = buf.getvalue()
    assert "Intel Core i7-4790K" in output
    assert "Corsair AX1200" in output

    # inspect non-existent
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = infra_cli_main(inv_arg + ["inspect", "fake-node-id"])
    assert ret == 1

    # 4. export-markdown to file
    out_md = tmp_path / "exported.md"
    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = infra_cli_main(inv_arg + ["export-markdown", "-o", str(out_md)])
    assert ret == 0
    assert out_md.exists()
    assert "ADR-001" in out_md.read_text(encoding="utf-8")
