"""Headless Command-Line Interface for Release Lifecycle, Staging, Production & Backups (HF-12).

Conforms to:
- Universal Engineering Standards (AGENTS.md)
- Windows UTF-8 console output
- Structured JSON output with --json flag
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from core.infra.backup_service import CloudBackupService
from core.orchestrator.release_pipeline import (
    ClientAcceptanceRequiredError,
    ProjectTier,
    ReleasePipelineService,
)

# Enforce UTF-8 on Windows CLI
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_GLOBAL_PIPELINE: ReleasePipelineService | None = None
_GLOBAL_BACKUP_SERVICE: CloudBackupService | None = None


def get_pipeline() -> ReleasePipelineService:
    global _GLOBAL_PIPELINE
    if _GLOBAL_PIPELINE is None:
        _GLOBAL_PIPELINE = ReleasePipelineService()
    return _GLOBAL_PIPELINE


def get_backup_service() -> CloudBackupService:
    global _GLOBAL_BACKUP_SERVICE
    if _GLOBAL_BACKUP_SERVICE is None:
        _GLOBAL_BACKUP_SERVICE = CloudBackupService()
    return _GLOBAL_BACKUP_SERVICE


def cmd_build(args: argparse.Namespace) -> int:
    pipeline = get_pipeline()
    try:
        artifact = pipeline.build(
            project_id=args.project,
            git_sha=args.git_sha,
            image_tag=args.image_tag,
        )
        if args.json:
            print(json.dumps(artifact.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"[BUILD_SUCCESS] Artifact Digest: {artifact.artifact_digest}")
            print(f"Project: {artifact.project_id} | Git SHA: {artifact.git_sha} | Image: {artifact.image_tag}")
        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Build failed: {exc}", file=sys.stderr)
        return 1


def cmd_deploy_staging(args: argparse.Namespace) -> int:
    pipeline = get_pipeline()
    artifact = pipeline.get_artifact(args.artifact_id)
    if not artifact:
        msg = f"Artifact not found: {args.artifact_id}"
        if args.json:
            print(json.dumps({"status": "error", "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        return 1

    try:
        record = pipeline.deploy_staging(artifact)
        if args.json:
            print(json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"[STAGING_SUCCESS] Deployed {artifact.artifact_digest[:12]} to staging")
            print(f"Smoke Test: {record.smoke_test.status if record.smoke_test else 'None'}")
        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Staging deploy failed: {exc}", file=sys.stderr)
        return 1


def cmd_accept(args: argparse.Namespace) -> int:
    pipeline = get_pipeline()
    try:
        receipt = pipeline.record_client_acceptance(
            project_id=args.project,
            artifact_digest=args.artifact_id,
            client_id=args.client,
            approved_by=args.approved_by,
        )
        if args.json:
            print(json.dumps(receipt.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"[ACCEPTANCE_ISSUED] Receipt ID: {receipt.receipt_id}")
            print(f"Signed by {receipt.approved_by} for client {receipt.client_id}")
        return 0
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Client acceptance failed: {exc}", file=sys.stderr)
        return 1


def cmd_deploy_production(args: argparse.Namespace) -> int:
    pipeline = get_pipeline()
    artifact = pipeline.get_artifact(args.artifact_id)
    if not artifact:
        msg = f"Artifact not found: {args.artifact_id}"
        if args.json:
            print(json.dumps({"status": "error", "error": msg}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] {msg}", file=sys.stderr)
        return 1

    tier = ProjectTier(args.tier)
    try:
        record = pipeline.deploy_production(artifact, project_tier=tier)
        if args.json:
            print(json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False))
        else:
            print(f"[DELIVERED_SUCCESS] Artifact {artifact.artifact_digest[:12]} delivered to production")
            print(f"Tier: {tier.value} | Acceptance Receipt: {record.acceptance_receipt_id or 'N/A'}")
        return 0
    except ClientAcceptanceRequiredError as exc:
        if args.json:
            print(json.dumps({"status": "blocked_by_acceptance", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[BLOCKED_POLICY] Scenario G8: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Production deployment failed: {exc}", file=sys.stderr)
        return 1


def cmd_backup_and_drill(args: argparse.Namespace) -> int:
    backup_svc = get_backup_service()
    try:
        # 1. Create backup
        snapshot = backup_svc.create_backup(
            project_id=args.project,
            source_directory=args.source_dir,
        )
        # 2. Run restore drill in isolated sandbox destination
        drill = backup_svc.run_restore_drill(
            snapshot_id=snapshot.snapshot_id,
            isolated_destination=args.sandbox_dir,
        )
        data = {
            "snapshot": snapshot.model_dump(mode="json"),
            "drill": drill.model_dump(mode="json"),
            "restore_drill_passed": drill.success,
        }
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"[BACKUP_SUCCESS] Snapshot: {snapshot.snapshot_id} ({snapshot.files_count} files)")
            print(f"[DRILL_SUCCESS] Restore verified: {drill.success} ({drill.files_restored} files restored)")
        return 0 if drill.success else 1
    except Exception as exc:
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Backup drill failed: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Release Pipeline, Staging, Production & Backups CLI (HF-12)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # build
    p_build = subparsers.add_parser("build", help="Build an immutable release artifact")
    p_build.add_argument("--project", required=True, help="Project identifier")
    p_build.add_argument("--git-sha", required=True, help="Git commit SHA")
    p_build.add_argument("--image-tag", default=None, help="Container image tag")
    p_build.add_argument("--json", action="store_true", help="Format output as JSON")

    # deploy-staging
    p_stg = subparsers.add_parser("deploy-staging", help="Deploy artifact to staging and run smoke")
    p_stg.add_argument("--artifact-id", required=True, help="Artifact digest")
    p_stg.add_argument("--json", action="store_true", help="Format output as JSON")

    # accept
    p_acc = subparsers.add_parser("accept", help="Record client acceptance receipt")
    p_acc.add_argument("--project", required=True, help="Project identifier")
    p_acc.add_argument("--artifact-id", required=True, help="Artifact digest")
    p_acc.add_argument("--client", required=True, help="Client identifier")
    p_acc.add_argument("--approved-by", required=True, help="Approver full name")
    p_acc.add_argument("--json", action="store_true", help="Format output as JSON")

    # deploy-production
    p_prod = subparsers.add_parser("deploy-production", help="Deploy artifact to production (Scenario G8)")
    p_prod.add_argument("--artifact-id", required=True, help="Artifact digest")
    p_prod.add_argument("--tier", default="internal_free", choices=["commercial_paid", "internal_free"])
    p_prod.add_argument("--json", action="store_true", help="Format output as JSON")

    # backup-and-drill
    p_bak = subparsers.add_parser("backup-and-drill", help="Execute snapshot and proven restore drill")
    p_bak.add_argument("--project", required=True, help="Project identifier")
    p_bak.add_argument("--source-dir", required=True, help="Source directory to snapshot")
    p_bak.add_argument("--sandbox-dir", required=True, help="Isolated sandbox destination for drill")
    p_bak.add_argument("--json", action="store_true", help="Format output as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        return cmd_build(args)
    if args.command == "deploy-staging":
        return cmd_deploy_staging(args)
    if args.command == "accept":
        return cmd_accept(args)
    if args.command == "deploy-production":
        return cmd_deploy_production(args)
    if args.command == "backup-and-drill":
        return cmd_backup_and_drill(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
