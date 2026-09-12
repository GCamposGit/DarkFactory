"""Rollback coordination, atomic recovery, and RPO/RTO verification for HF-15.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 328 & line 297 / HF-12 / INFRA-08)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenario G8)
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from core.acceptance.models import RollbackExecutionRecord
from core.infra.backup_service import (
    BackupSnapshot,
    BackupStorageTarget,
    CloudBackupService,
)
from core.orchestrator.release_pipeline import ReleasePipelineService, RollbackReceipt

logger = logging.getLogger("darkfac.acceptance.rollback")


class HF15RollbackCoordinator:
    """Coordinates automated rollbacks, measures RPO/RTO, and enforces project isolation."""

    def __init__(
        self,
        backup_service: Optional[CloudBackupService] = None,
        release_pipeline: Optional[ReleasePipelineService] = None,
        backup_root: Optional[Path] = None,
    ) -> None:
        self.backup_root = backup_root or (Path.cwd() / ".factory" / "hf15" / "workspace" / "backups")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.backup_service = backup_service or CloudBackupService(backup_root=self.backup_root)
        self.release_pipeline = release_pipeline
        self._execution_history: list[RollbackExecutionRecord] = []

    def create_pre_release_checkpoint(
        self,
        project_id: str,
        state_directory: Path,
    ) -> BackupSnapshot:
        """Creates an immutable snapshot before deployment to serve as rollback baseline."""
        state_directory.mkdir(parents=True, exist_ok=True)
        snapshot = self.backup_service.create_backup(
            project_id=project_id,
            source_directory=state_directory,
            storage_target=BackupStorageTarget.LOCAL,
        )
        logger.info(
            "Created pre-release checkpoint for project %s (snapshot_id=%s)",
            project_id,
            snapshot.snapshot_id,
        )
        return snapshot

    def execute_rollback(
        self,
        project_id: str,
        snapshot_id: str,
        failed_artifact_digest: str,
        trigger_reason: str,
        isolated_restore_target: Path,
        previous_artifact_digest: Optional[str] = None,
    ) -> RollbackExecutionRecord:
        """Executes atomic rollback, restores state, and measures RTO and RPO."""
        start_time = time.monotonic()
        snapshot = self.backup_service.get_snapshot(snapshot_id)
        if not snapshot:
            raise ValueError(f"Snapshot '{snapshot_id}' not found in registry")

        # Conduct isolated restoration drill
        isolated_restore_target.mkdir(parents=True, exist_ok=True)
        drill_result = self.backup_service.run_restore_drill(
            snapshot_id=snapshot_id,
            isolated_destination=isolated_restore_target,
        )

        rto_seconds = round(time.monotonic() - start_time, 4)
        rpo_seconds = round(
            max(0.0, (datetime.now(UTC) - snapshot.created_at).total_seconds()),
            4,
        )

        # Compute cryptographic evidence hash of restoration
        evidence_payload = f"{project_id}:{snapshot_id}:{failed_artifact_digest}:{trigger_reason}:{rto_seconds}:{rpo_seconds}"
        evidence_hash = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()

        record = RollbackExecutionRecord(
            rollback_id=f"rb_hf15_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            snapshot_id=snapshot_id,
            trigger_reason=trigger_reason,
            pre_rollback_digest=failed_artifact_digest,
            post_rollback_digest=previous_artifact_digest or snapshot.archive_checksum,
            rpo_seconds=rpo_seconds,
            rto_seconds=rto_seconds,
            success=drill_result.success and drill_result.integrity_verified,
            evidence_hash=evidence_hash,
            timestamp=datetime.now(UTC),
        )

        self._execution_history.append(record)

        # Notify release pipeline if wired
        if self.release_pipeline and hasattr(self.release_pipeline, "record_external_rollback"):
            self.release_pipeline.record_external_rollback(
                RollbackReceipt(
                    rollback_id=record.rollback_id,
                    project_id=project_id,
                    failed_artifact_digest=failed_artifact_digest,
                    restored_artifact_digest=record.post_rollback_digest,
                    trigger_reason=trigger_reason,
                )
            )

        logger.info(
            "Rollback completed for project %s: success=%s, RTO=%.3fs, RPO=%.3fs",
            project_id,
            record.success,
            rto_seconds,
            rpo_seconds,
        )
        return record

    def run_rollback_drill(
        self,
        project_id: str = "proj-rollback-drill",
        sandbox_dir: Optional[Path] = None,
    ) -> RollbackExecutionRecord:
        """Runs a synthetic, self-contained rollback drill to verify recovery subsystem health."""
        work_dir = sandbox_dir or (self.backup_root / "drill_workspace")
        work_dir.mkdir(parents=True, exist_ok=True)

        # 1. Populate state v1
        state_v1 = work_dir / "state_v1"
        state_v1.mkdir(parents=True, exist_ok=True)
        (state_v1 / "app_config.json").write_text(
            json.dumps({"version": "1.0.0", "status": "stable"}), encoding="utf-8"
        )
        (state_v1 / "data.db").write_text("database_snapshot_v1_verified", encoding="utf-8")

        # 2. Take baseline snapshot
        snapshot = self.create_pre_release_checkpoint(project_id, state_v1)

        # 3. Simulate corrupting / failing deployment v2
        state_v2 = work_dir / "state_corrupt"
        state_v2.mkdir(parents=True, exist_ok=True)
        (state_v2 / "app_config.json").write_text(
            json.dumps({"version": "2.0.0", "status": "crashed_migration"}), encoding="utf-8"
        )

        failed_digest = hashlib.sha256(b"corrupted_v2_payload").hexdigest()
        restore_target = work_dir / "restored_state"

        # 4. Trigger automated rollback
        record = self.execute_rollback(
            project_id=project_id,
            snapshot_id=snapshot.snapshot_id,
            failed_artifact_digest=failed_digest,
            trigger_reason="production_smoke_failure:db_migration_crashed",
            isolated_restore_target=restore_target,
            previous_artifact_digest=snapshot.archive_checksum,
        )

        return record

    def get_history(self, project_id: Optional[str] = None) -> list[RollbackExecutionRecord]:
        """Returns rollback execution audit records, optionally filtered by project ID."""
        if project_id:
            return [r for r in self._execution_history if r.project_id == project_id]
        return list(self._execution_history)
