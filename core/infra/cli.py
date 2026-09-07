"""Command-line interface for the Dark Factory infrastructure management module."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repository root is in sys.path when executed directly
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.infra.inventory import DEFAULT_INVENTORY_PATH, InventoryManager
from core.infra.models import NodeStatus


def setup_utf8_output() -> None:
    """Ensure standard output handles UTF-8 characters cleanly on Windows."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass


def cmd_list(manager: InventoryManager) -> int:
    """Print a clean tabular list of all registered infrastructure nodes."""
    inventory = manager.load_or_initialize()
    print(f"\n[DARKFAC INFRA] Inventario de Infraestrutura v{inventory.version}")
    print("=" * 85)
    print(f"{'ID':<22} | {'PAPEL':<18} | {'STATUS':<10} | {'CUSTO/MES':<10} | {'PROVEDOR'}")
    print("-" * 85)
    for n in inventory.nodes:
        status_label = n.status.value.upper()
        cost_label = f"US$ {n.cost_monthly_usd:.2f}"
        print(f"{n.id:<22} | {n.role.value:<18} | {status_label:<10} | {cost_label:<10} | {n.provider}")
    print("-" * 85)
    print(f"Total de nos: {len(inventory.nodes)} | Orcamento total estimado: US$ {inventory.total_monthly_budget_usd:.2f}/mes\n")
    return 0


def cmd_status(manager: InventoryManager) -> int:
    """Display overall status summary, active services and registered ADR count."""
    inventory = manager.load_or_initialize()
    active_count = sum(1 for n in inventory.nodes if n.status == NodeStatus.ACTIVE)
    standby_count = sum(1 for n in inventory.nodes if n.status == NodeStatus.STANDBY)
    planned_count = sum(1 for n in inventory.nodes if n.status == NodeStatus.PLANNED)

    total_services = sum(len(n.services) for n in inventory.nodes)
    active_services = sum(sum(1 for s in n.services if s.status == NodeStatus.ACTIVE) for n in inventory.nodes)

    print(f"\n[DARKFAC INFRA STATUS]")
    print(f"- Ultima atualizacao: {inventory.last_updated.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"- Total de nos: {len(inventory.nodes)} (Ativos: {active_count}, Standby: {standby_count}, Planejados: {planned_count})")
    print(f"- Servicos mapeados: {total_services} (Ativos: {active_services})")
    print(f"- Decisoes arquiteturais registradas: {len(inventory.adrs)}")
    print(f"- Custo total mensal: US$ {inventory.total_monthly_budget_usd:.2f}/mes\n")
    return 0


def cmd_inspect(manager: InventoryManager, node_id: str) -> int:
    """Print detailed technical specifications and services for a given node."""
    node = manager.get_node(node_id)
    if not node:
        print(f"Erro: No com ID '{node_id}' nao encontrado no inventario.")
        return 1

    print(f"\n[DETALHES DO NO: {node.id}]")
    print(f"- Nome: {node.name}")
    print(f"- Papel: {node.role.value}")
    print(f"- Status: {node.status.value.upper()}")
    print(f"- Provedor: {node.provider}")
    print(f"- Custo mensal: US$ {node.cost_monthly_usd:.2f}")
    print(f"- Tags: {', '.join(node.tags)}")

    if node.hardware:
        print("\n  [HARDWARE]")
        print(f"  * CPU: {node.hardware.cpu}")
        print(f"  * RAM: {node.hardware.ram_gb:.1f} GB")
        print(f"  * Storage Principal: {node.hardware.storage_primary}")
        if node.hardware.storage_secondary:
            print(f"  * Storage Secundario: {node.hardware.storage_secondary}")
        if node.hardware.gpu:
            print(f"  * GPU: {node.hardware.gpu}")
        if node.hardware.gpu_compute_capability:
            print(f"  * Compute Capability: {node.hardware.gpu_compute_capability}")
        if node.hardware.power_supply:
            print(f"  * Fonte: {node.hardware.power_supply}")
        if node.hardware.notes:
            print(f"  * Notas tecnicas: {node.hardware.notes}")

    if node.network:
        print("\n  [REDE & CONECTIVIDADE]")
        if node.network.private_ip:
            print(f"  * IP Privado: {node.network.private_ip}")
        if node.network.tailscale_ip:
            print(f"  * Tailscale Mesh: {node.network.tailscale_ip}")
        if node.network.public_dns:
            print(f"  * DNS Publico: {node.network.public_dns}")
        print(f"  * Cloudflare Tunnel: {'Sim' if node.network.cloudflare_tunnel else 'Nao'}")
        if node.network.notes:
            print(f"  * Politica de rede: {node.network.notes}")

    if node.services:
        print("\n  [SERVICOS]")
        for s in node.services:
            port_str = f" (Porta {s.port})" if s.port else ""
            print(f"  * [{s.status.value.upper()}] {s.name}{port_str}: {s.description}")

    print("")
    return 0


def cmd_export_markdown(manager: InventoryManager, output_path: str | None = None) -> int:
    """Export the inventory and ADR overview to Markdown."""
    summary_md = manager.export_markdown_summary()
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(summary_md, encoding="utf-8")
        print(f"Resumo exportado com sucesso para: {out.resolve()}")
    else:
        print(summary_md)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build command-line parser."""
    parser = argparse.ArgumentParser(description="Dark Factory Infrastructure Management CLI")
    parser.add_argument(
        "--inventory-path",
        default=str(DEFAULT_INVENTORY_PATH),
        help=f"Path to the inventory JSON file (default: {DEFAULT_INVENTORY_PATH})",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    subparsers.add_parser("list", help="List all infrastructure nodes")

    # status
    subparsers.add_parser("status", help="Show infrastructure status and counts")

    # inspect
    inspect_parser = subparsers.add_parser("inspect", help="Inspect a specific node")
    inspect_parser.add_argument("node_id", help="Node unique identifier")

    # export-markdown
    export_parser = subparsers.add_parser("export-markdown", help="Export summary as Markdown")
    export_parser.add_argument("--output", "-o", default=None, help="Optional output file path")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    setup_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)

    manager = InventoryManager(inventory_path=args.inventory_path)

    if args.subcommand == "list":
        return cmd_list(manager)
    elif args.subcommand == "status":
        return cmd_status(manager)
    elif args.subcommand == "inspect":
        return cmd_inspect(manager, args.node_id)
    elif args.subcommand == "export-markdown":
        return cmd_export_markdown(manager, args.output)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
