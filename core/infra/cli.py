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

from core.infra.backup_service import CloudBackupService
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


def cmd_backup_run(
    project_id: str,
    source_dir: str,
    *,
    include_postgres: bool = True,
    encryption_key: str | None = None,
) -> int:
    """Execute 3-tier backup routine (Local Staging -> AES-256-GCM -> R2 + Drive E:)."""
    service = CloudBackupService(encryption_key=encryption_key)
    print(f"\n[DARKFAC BACKUP 3-TIER]")
    print(f"- Projeto: {project_id}")
    print(f"- Diretorio fonte: {Path(source_dir).resolve()}")
    print(f"- Incluir PostgreSQL: {'Sim' if include_postgres else 'Nao'}")

    try:
        snapshot = service.create_three_tier_backup(
            project_id=project_id,
            source_directory=source_dir,
            include_postgres=include_postgres,
            encryption_key=encryption_key,
        )
        print("\n[BACKUP CONCLUIDO COM SUCESSO]")
        print(f"- Snapshot ID: {snapshot.snapshot_id}")
        print(f"- Arquivos empacotados: {snapshot.files_count}")
        print(f"- Tamanho bruto: {snapshot.total_bytes} bytes")
        print(f"- Tamanho cifrado: {snapshot.encrypted_size_bytes} bytes")
        print(f"- Checksum (SHA-256): {snapshot.archive_checksum}")
        print(f"- R2 Object Key: {snapshot.r2_object_key}")
        print(f"- Espelho On-Premise: {snapshot.onprem_location}\n")
        return 0
    except Exception as exc:
        print(f"\n[ERRO NO BACKUP]: {exc}\n")
        return 1


def cmd_backup_restore(
    snapshot_id: str,
    destination: str,
    *,
    from_tier: str = "local",
    encryption_key: str | None = None,
) -> int:
    """Execute restore drill and integrity verification in sandbox destination."""
    service = CloudBackupService(encryption_key=encryption_key)
    print(f"\n[DARKFAC RESTORE DRILL]")
    print(f"- Snapshot ID: {snapshot_id}")
    print(f"- Origem (Tier): {from_tier.upper()}")
    print(f"- Destino isolado: {Path(destination).resolve()}")

    try:
        drill = service.run_restore_drill(
            snapshot_id=snapshot_id,
            isolated_destination=destination,
            from_tier=from_tier,
            encryption_key=encryption_key,
        )
        if drill.success:
            print("\n[RESTAURACAO DEMONSTRADA COM SUCESSO]")
            print(f"- Drill ID: {drill.drill_id}")
            print(f"- Arquivos restaurados: {drill.files_restored}")
            print(f"- Integridade 100% verificada: {drill.integrity_verified}")
            print(f"- Duracao: {drill.duration_seconds:.3f}s\n")
            return 0
        else:
            print(f"\n[FALHA NA RESTAURACAO]: {drill.error_message}\n")
            return 1
    except Exception as exc:
        print(f"\n[ERRO NO RESTORE DRILL]: {exc}\n")
        return 1


def cmd_backup_list(project_id: str | None = None) -> int:
    """List recorded backup snapshots."""
    service = CloudBackupService()
    snapshots = service.list_snapshots(project_id=project_id)
    print(f"\n[DARKFAC REGISTRO DE BACKUPS]")
    print("=" * 95)
    print(f"{'SNAPSHOT ID':<35} | {'PROJETO':<12} | {'TARGET':<10} | {'ARQUIVOS':<8} | {'DATA UTC'}")
    print("-" * 95)
    for s in snapshots:
        dt_str = s.created_at.strftime("%Y-%m-%d %H:%M")
        print(f"{s.snapshot_id:<35} | {s.project_id:<12} | {s.storage_target.value:<10} | {s.files_count:<8} | {dt_str}")
    print("-" * 95)
    print(f"Total de snapshots: {len(snapshots)}\n")
    return 0


def cmd_backup_retention(
    project_id: str,
    *,
    r2_days: int = 7,
    onprem_days: int = 120,
) -> int:
    """Apply asymmetric retention policy (7 days R2, 120 days On-Prem Drive E:)."""
    service = CloudBackupService()
    print(f"\n[DARKFAC POLITICA DE RETENCAO ASSIMETRICA]")
    print(f"- Projeto: {project_id}")
    print(f"- R2 Retention: {r2_days} dias")
    print(f"- On-Prem Retention: {onprem_days} dias")

    result = service.apply_tiered_retention_policy(
        project_id=project_id,
        r2_max_age_days=r2_days,
        onprem_max_age_days=onprem_days,
    )
    print("\n[RETENCAO APLICADA]")
    print(f"- Objetos R2 expurgados: {len(result['r2_pruned'])}")
    print(f"- Backups On-Prem expurgados: {len(result['onprem_pruned'])}\n")
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

    # backup-run
    bk_run = subparsers.add_parser("backup-run", help="Run automated 3-tier backup routine")
    bk_run.add_argument("--project-id", default="darkfac", help="Project identifier")
    bk_run.add_argument("--source-dir", default=".", help="Source directory to backup")
    bk_run.add_argument("--no-postgres", action="store_true", help="Do not include PostgreSQL dump")
    bk_run.add_argument("--encryption-key", default=None, help="Encryption passphrase or key")

    # backup-restore
    bk_res = subparsers.add_parser("backup-restore", help="Run restore drill in isolated destination")
    bk_res.add_argument("--snapshot-id", required=True, help="Snapshot ID to restore")
    bk_res.add_argument("--destination", required=True, help="Isolated sandbox destination folder")
    bk_res.add_argument(
        "--from-tier",
        choices=["local", "r2", "onprem"],
        default="local",
        help="Source tier to restore from (default: local)",
    )
    bk_res.add_argument("--encryption-key", default=None, help="Encryption passphrase or key")

    # backup-list
    bk_list = subparsers.add_parser("backup-list", help="List recorded backup snapshots")
    bk_list.add_argument("--project-id", default=None, help="Filter by project identifier")

    # backup-retention
    bk_ret = subparsers.add_parser("backup-retention", help="Apply asymmetric retention policy")
    bk_ret.add_argument("--project-id", default="darkfac", help="Project identifier")
    bk_ret.add_argument("--r2-days", type=int, default=7, help="Max age for R2 snapshots in days")
    bk_ret.add_argument("--onprem-days", type=int, default=120, help="Max age for on-prem snapshots in days")

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
    elif args.subcommand == "backup-run":
        return cmd_backup_run(
            project_id=args.project_id,
            source_dir=args.source_dir,
            include_postgres=not args.no_postgres,
            encryption_key=args.encryption_key,
        )
    elif args.subcommand == "backup-restore":
        return cmd_backup_restore(
            snapshot_id=args.snapshot_id,
            destination=args.destination,
            from_tier=args.from_tier,
            encryption_key=args.encryption_key,
        )
    elif args.subcommand == "backup-list":
        return cmd_backup_list(project_id=args.project_id)
    elif args.subcommand == "backup-retention":
        return cmd_backup_retention(
            project_id=args.project_id,
            r2_days=args.r2_days,
            onprem_days=args.onprem_days,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
