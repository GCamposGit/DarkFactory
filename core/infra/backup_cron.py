"""Autonomous Scheduled and Lifecycle Backup Daemon for Dark Factory (INFRA-08).

Governed by:
- Invariant: Zero manual intervention; backups operate 100% autonomously.
- Event-driven (post-deploy) and scheduled (daily cron) execution.
- Includes automated restore drill in sandbox and asymmetric retention pruning.
- Telegram emergency notification on any failure (fail-closed alert).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infra.backup_service import CloudBackupService
from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
from core.notifications.service import NotificationService

logger = logging.getLogger("darkfac.backup_cron")


class AutonomousBackupError(Exception):
    """Raised when an autonomous backup cycle or restore drill fails."""


def _notify_failure(title: str, message: str) -> None:
    """Dispatches emergency Telegram notification on backup failure."""
    try:
        service = NotificationService()
        service.notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.CRITICAL,
            title=title,
            message=message,
            channel=NotificationChannel.TELEGRAM,
            force=True,
        )
    except Exception as exc:
        logger.error("Failed to dispatch Telegram backup alert: %s", exc)


def run_autonomous_backup_cycle(
    project_id: str = "darkfac",
    source_dir: Path | str | None = None,
    *,
    encryption_key: str | None = None,
    run_drill: bool = True,
    r2_retention_days: int = 7,
    onprem_retention_days: int = 120,
) -> dict[str, Any]:
    """Executes a 100% autonomous 3-tier backup cycle: dump -> encrypt -> R2 -> on-prem -> drill -> prune."""
    if source_dir:
        src = Path(source_dir).resolve()
    elif (Path.cwd() / ".factory").is_dir():
        src = (Path.cwd() / ".factory").resolve()
    else:
        src = Path.cwd().resolve()
    service = CloudBackupService(encryption_key=encryption_key)

    logger.info("Starting autonomous backup cycle for project %s (source: %s)", project_id, src)

    try:
        # 1. 3-Tier Snapshot (Staging + AES-256-GCM + R2 + Drive E:)
        snapshot = service.create_three_tier_backup(
            project_id=project_id,
            source_directory=src,
            include_postgres=True,
            encryption_key=encryption_key,
        )

        # 2. Autonomous Restore Drill in Sandbox
        drill_verified = False
        if run_drill:
            with tempfile.TemporaryDirectory() as sandbox_dir:
                drill_tier = "r2" if snapshot.r2_object_key else "local"
                drill = service.run_restore_drill(
                    snapshot.snapshot_id,
                    sandbox_dir,
                    from_tier=drill_tier,
                    encryption_key=encryption_key,
                )
                if not drill.success or not drill.integrity_verified:
                    raise AutonomousBackupError(
                        f"Autonomous restore drill failed on tier '{drill_tier}': {drill.error_message}"
                    )
                drill_verified = True

        # 3. Asymmetric Retention Policy Pruning
        prune_result = service.apply_tiered_retention_policy(
            project_id=project_id,
            r2_max_age_days=r2_retention_days,
            onprem_max_age_days=onprem_retention_days,
        )

        summary = {
            "status": "success",
            "snapshot_id": snapshot.snapshot_id,
            "project_id": project_id,
            "files_count": snapshot.files_count,
            "total_bytes": snapshot.total_bytes,
            "encrypted_bytes": snapshot.encrypted_size_bytes,
            "r2_object_key": snapshot.r2_object_key,
            "onprem_location": snapshot.onprem_location,
            "drill_verified": drill_verified,
            "r2_pruned_count": len(prune_result.get("r2_pruned", [])),
            "onprem_pruned_count": len(prune_result.get("onprem_pruned", [])),
            "completed_at": datetime.now(UTC).isoformat(),
        }

        logger.info(
            "Autonomous backup cycle completed successfully for %s: snapshot=%s, drill_verified=%s",
            project_id,
            snapshot.snapshot_id,
            drill_verified,
        )
        return summary

    except Exception as exc:
        err_msg = f"Autonomous backup failed for project {project_id}: {exc}"
        logger.error(err_msg, exc_info=True)
        _notify_failure(
            title="Falha no Backup Autônomo 3-Camadas",
            message=f"A rotina autônoma de backup encontrou uma anomalia crítica:\n\n{exc}",
        )
        raise AutonomousBackupError(err_msg) from exc


def run_daemon(
    interval_hours: float = 24.0,
    project_id: str = "darkfac",
    source_dir: Path | str | None = None,
) -> None:
    """Runs autonomous backup cycles continuously in a background loop."""
    interval_sec = max(60.0, interval_hours * 3600.0)
    logger.info(
        "Autonomous backup daemon started. Interval: %.1f hours (%.0f seconds)",
        interval_hours,
        interval_sec,
    )

    while True:
        try:
            run_autonomous_backup_cycle(project_id=project_id, source_dir=source_dir)
        except Exception as exc:
            logger.error("Daemon cycle encountered error (will retry next interval): %s", exc)

        time.sleep(interval_sec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory Autonomous Backup Daemon / Runner")
    parser.add_argument("--once", action="store_true", help="Run a single autonomous backup cycle and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background daemon mode")
    parser.add_argument("--interval-hours", type=float, default=24.0, help="Daemon interval in hours (default: 24.0)")
    parser.add_argument("--project-id", default="darkfac", help="Project identifier")
    parser.add_argument("--source-dir", default=None, help="Source directory path")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.daemon:
        run_daemon(
            interval_hours=args.interval_hours,
            project_id=args.project_id,
            source_dir=args.source_dir,
        )
        return 0
    else:
        # Default or --once: run single autonomous cycle
        try:
            summary = run_autonomous_backup_cycle(
                project_id=args.project_id,
                source_dir=args.source_dir,
            )
            print(f"[AUTONOMOUS_BACKUP_SUCCESS] snapshot_id={summary['snapshot_id']}")
            return 0
        except Exception as exc:
            print(f"[AUTONOMOUS_BACKUP_FAIL] error={exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
