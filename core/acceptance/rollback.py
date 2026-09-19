"""Rollback coordination, atomic recovery, and RPO/RTO verification for HF-15.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 11, line 328 & line 297 / HF-12 / INFRA-08)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 7, Scenario G8)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional, Union

from core.acceptance.models import RollbackExecutionRecord
from core.infra.backup_service import (
    BackupSnapshot,
    BackupStorageTarget,
    CloudBackupService,
)
from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.deployment_adapter import TargetConfig
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


class RollbackAdapter:
    """Adapter for restoring release targets and executing atomic rollback drills."""

    def __init__(
        self,
        backup_service: Optional[CloudBackupService] = None,
        coordinator: Optional[HF15RollbackCoordinator] = None,
    ) -> None:
        self.backup_service = backup_service or CloudBackupService()
        self.coordinator = coordinator

    def restore(
        self,
        previous: Union[ArtifactRef, str],
        backup: Union[Path, BackupSnapshot],
        target: Union[TargetConfig, Path],
        trigger_reason: str = "automated_rollback_restore",
        pre_rollback_digest: Optional[str] = None,
    ) -> RollbackExecutionRecord:
        """Restores target to previous version using backup snapshot.

        MANDATORY COUNTER-PROOF:
        'Rollback só JSON reprova. Restore no alvo, sem tratar dados cliente como descartáveis':
        - If the backup snapshot contains ONLY JSON configuration files without real data/database/state files
          -> raises ValueError("Rollback rejected: backup contains only JSON configuration; client data missing").
        - Verifies actual data files restoration and byte checksums.
        - Measures RTO and RPO accurately.
        """
        start_time = time.perf_counter()

        # 1. Resolve previous digest
        if isinstance(previous, str):
            previous_digest = previous
        else:
            previous_digest = (
                getattr(previous, "digest", None)
                or getattr(previous, "byte_digest", None)
                or getattr(previous, "oci_digest", None)
                or str(previous)
            )

        # 2. Resolve target directory and project ID
        if isinstance(target, TargetConfig):
            project_id = target.project_id
            target_dir = Path(
                target.metadata.get("state_directory")
                or target.metadata.get("data_dir")
                or target.metadata.get("restore_target")
                or (Path.cwd() / ".factory" / "workspace" / project_id)
            ).resolve()
        else:
            target_dir = Path(target).resolve()
            project_id = target_dir.name or "target_project"

        target_dir.mkdir(parents=True, exist_ok=True)

        # 3. Resolve backup metadata, manifest, and archive location
        archive_path: Optional[Path] = None
        created_at: datetime = datetime.now(UTC)
        snapshot_id: str = f"snp_manual_{uuid.uuid4().hex[:8]}"
        manifest: dict[str, str] = {}

        if isinstance(backup, BackupSnapshot):
            snapshot_id = backup.snapshot_id
            project_id = backup.project_id or project_id
            created_at = backup.created_at
            archive_path = Path(backup.storage_location).resolve()
            manifest = dict(backup.file_manifest)
        elif isinstance(backup, Path):
            backup_path = Path(backup).resolve()
            if not backup_path.exists():
                raise FileNotFoundError(f"Backup path does not exist: {backup_path}")

            created_at = datetime.fromtimestamp(backup_path.stat().st_mtime, tz=UTC)
            if backup_path.is_file():
                archive_path = backup_path
                snapshot_id = backup_path.stem.replace(".tar", "")
                with tarfile.open(archive_path, "r:*") as tar:
                    for member in tar.getmembers():
                        if member.isfile():
                            extracted = tar.extractfile(member)
                            if extracted:
                                manifest[member.name] = hashlib.sha256(extracted.read()).hexdigest()
            elif backup_path.is_dir():
                snapshot_id = f"snp_dir_{backup_path.name}"
                for p in sorted(backup_path.rglob("*")):
                    if p.is_file():
                        rel = p.relative_to(backup_path).as_posix()
                        manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            raise TypeError(f"Unsupported backup type: {type(backup)}")

        # 4. MANDATORY COUNTER-PROOF: 'Rollback só JSON reprova'
        # If backup contains ONLY JSON configuration files without real data/database/state files,
        # fail-closed immediately before mutating destination.
        if not manifest:
            raise ValueError("Rollback rejected: backup contains only JSON configuration; client data missing")

        file_names = list(manifest.keys())
        json_extensions = {".json", ".jsonc", ".json5"}
        all_json = all(Path(f).suffix.lower() in json_extensions for f in file_names)

        if all_json:
            logger.error("Rollback aborted: backup snapshot %s contains only JSON files", snapshot_id)
            raise ValueError("Rollback rejected: backup contains only JSON configuration; client data missing")

        # 5. Extract / Restore into target destination
        if archive_path and archive_path.is_file():
            with tarfile.open(archive_path, "r:*") as tar:
                try:
                    tar.extractall(target_dir, filter="data")
                except TypeError:
                    tar.extractall(target_dir)
        elif isinstance(backup, Path) and backup.is_dir():
            for rel_path in manifest:
                src_file = backup / rel_path
                dst_file = target_dir / rel_path
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)

        # 6. Verify actual data files restoration and byte checksums
        for rel_path, expected_checksum in manifest.items():
            restored_file = target_dir / rel_path
            if not restored_file.is_file():
                raise RuntimeError(f"Rollback verification failed: missing restored file '{rel_path}' in {target_dir}")
            actual_checksum = hashlib.sha256(restored_file.read_bytes()).hexdigest()
            if actual_checksum != expected_checksum:
                raise RuntimeError(
                    f"Rollback verification failed: checksum mismatch for '{rel_path}' "
                    f"(expected {expected_checksum}, got {actual_checksum})"
                )

        # 7. Measure accurate RTO and RPO
        rto_seconds = round(time.perf_counter() - start_time, 4)
        rpo_seconds = round(max(0.0, (datetime.now(UTC) - created_at).total_seconds()), 4)

        # 8. Cryptographic restoration evidence hash
        evidence_payload = f"{project_id}:{snapshot_id}:{previous_digest}:{rto_seconds}:{rpo_seconds}"
        evidence_hash = hashlib.sha256(evidence_payload.encode("utf-8")).hexdigest()

        # 9. Update target config last known good digest if applicable
        if isinstance(target, TargetConfig):
            target.last_known_good_digest = previous_digest

        record = RollbackExecutionRecord(
            rollback_id=f"rb_adapter_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            snapshot_id=snapshot_id,
            trigger_reason=trigger_reason,
            pre_rollback_digest=pre_rollback_digest or "failing_unverified",
            post_rollback_digest=previous_digest,
            rpo_seconds=rpo_seconds,
            rto_seconds=rto_seconds,
            success=True,
            evidence_hash=evidence_hash,
            timestamp=datetime.now(UTC),
        )

        logger.info(
            "RollbackAdapter restored target %s to digest %s (RTO=%.4fs, RPO=%.4fs)",
            project_id,
            previous_digest[:12],
            rto_seconds,
            rpo_seconds,
        )

        return record

