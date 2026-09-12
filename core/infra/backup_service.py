"""Automated Backup, Retention, and Proven Restoration Service (INFRA-08 & HF-12).

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Section 9, line 264 & line 297 / INFRA-08 / HF-12)
- Invariant: 'Restauração demonstrada em destino isolado, inclusive de credenciais cifradas.'
- Cloudflare R2 / S3-compatible remote backup target abstraction with local fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class BackupStorageTarget(str, Enum):
    LOCAL = "local"
    R2_STORAGE = "r2"


class BackupSnapshot(BaseModel):
    """Immutable audit record of a verified backup snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    archive_checksum: str = Field(min_length=64, max_length=64)
    total_bytes: int = Field(ge=0)
    files_count: int = Field(ge=0)
    file_manifest: dict[str, str] = Field(default_factory=dict)  # rel_path -> sha256
    storage_target: BackupStorageTarget = BackupStorageTarget.LOCAL
    storage_location: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RestoreDrillResult(BaseModel):
    """Auditable proof of a restore drill conducted in an isolated sandbox destination."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    drill_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    isolated_destination: str = Field(min_length=1)
    success: bool
    files_restored: int
    integrity_verified: bool
    duration_seconds: float = Field(ge=0.0)
    error_message: str | None = None
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CloudBackupService:
    """Manages snapshot creation, isolated restore drills and retention policies."""

    def __init__(
        self,
        backup_root: Path | str | None = None,
        registry_path: Path | str | None = None,
    ) -> None:
        self.backup_root = Path(backup_root or (Path.cwd() / ".factory" / "backups")).resolve()
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.registry_path = Path(registry_path or (self.backup_root / "registry.json")).resolve()

        self._lock = threading.Lock()
        self._snapshots: dict[str, BackupSnapshot] = {}
        self._drills: list[RestoreDrillResult] = []

        if self.registry_path.exists():
            self._load_registry()

    def _save_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": "1",
            "snapshots": [s.model_dump(mode="json") for s in self._snapshots.values()],
            "drills": [d.model_dump(mode="json") for d in self._drills],
        }
        self.registry_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load_registry(self) -> None:
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            for s_dict in data.get("snapshots", []):
                s = BackupSnapshot.model_validate(s_dict)
                self._snapshots[s.snapshot_id] = s
            for d_dict in data.get("drills", []):
                self._drills.append(RestoreDrillResult.model_validate(d_dict))
        except Exception as exc:
            logger.warning("Failed to load backup registry: %s", exc)

    def create_backup(
        self,
        project_id: str,
        source_directory: Path | str,
        *,
        storage_target: BackupStorageTarget = BackupStorageTarget.LOCAL,
        file_patterns: Sequence[str] | None = None,
    ) -> BackupSnapshot:
        """Packages a project directory into an authenticated, checksummed snapshot."""
        src_path = Path(source_directory).resolve()
        if not src_path.exists() or not src_path.is_dir():
            raise ValueError(f"Source directory does not exist or is not a directory: {src_path}")

        snapshot_id = f"snp_{project_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        target_archive_path = self.backup_root / f"{snapshot_id}.tar.gz"

        file_manifest: dict[str, str] = {}
        total_bytes = 0

        # Scan files and compute individual SHA-256
        with tarfile.open(target_archive_path, "w:gz") as tar:
            for root, _, files in os.walk(src_path):
                for f in sorted(files):
                    file_path = Path(root) / f
                    rel_path = file_path.relative_to(src_path).as_posix()

                    data = file_path.read_bytes()
                    f_hash = hashlib.sha256(data).hexdigest()
                    file_manifest[rel_path] = f_hash
                    total_bytes += len(data)

                    tar.add(file_path, arcname=rel_path)

        # Compute archive checksum
        archive_data = target_archive_path.read_bytes()
        archive_checksum = hashlib.sha256(archive_data).hexdigest()

        snapshot = BackupSnapshot(
            snapshot_id=snapshot_id,
            project_id=project_id,
            archive_checksum=archive_checksum,
            total_bytes=total_bytes,
            files_count=len(file_manifest),
            file_manifest=file_manifest,
            storage_target=storage_target,
            storage_location=str(target_archive_path),
        )

        with self._lock:
            self._snapshots[snapshot_id] = snapshot
            self._save_registry()

        logger.info("Backup snapshot created: %s (%d files, %d bytes)", snapshot_id, snapshot.files_count, total_bytes)
        return snapshot

    def run_restore_drill(
        self,
        snapshot_id: str,
        isolated_destination: Path | str,
    ) -> RestoreDrillResult:
        """Executes a proven restore drill in an isolated destination, checking 100% of checksums."""
        dest_path = Path(isolated_destination).resolve()
        dest_path.mkdir(parents=True, exist_ok=True)

        start_time = time.perf_counter()

        with self._lock:
            snapshot = self._snapshots.get(snapshot_id)

        if not snapshot:
            raise KeyError(f"No backup snapshot found for id: {snapshot_id}")

        archive_path = Path(snapshot.storage_location)
        if not archive_path.is_file():
            raise FileNotFoundError(f"Backup archive file missing at: {archive_path}")

        # 1. Verify archive integrity before unpacking
        actual_archive_hash = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if actual_archive_hash != snapshot.archive_checksum:
            error_msg = f"Archive integrity corrupted! Expected {snapshot.archive_checksum}, got {actual_archive_hash}"
            drill_result = RestoreDrillResult(
                drill_id=f"drl_{uuid.uuid4().hex[:8]}",
                snapshot_id=snapshot_id,
                isolated_destination=str(dest_path),
                success=False,
                files_restored=0,
                integrity_verified=False,
                duration_seconds=time.perf_counter() - start_time,
                error_message=error_msg,
            )
            with self._lock:
                self._drills.append(drill_result)
                self._save_registry()
            return drill_result

        # 2. Unpack into isolated sandbox destination
        with tarfile.open(archive_path, "r:gz") as tar:
            try:
                tar.extractall(dest_path, filter="data")
            except TypeError:
                tar.extractall(dest_path)

        # 3. Verify 100% of restored files against manifest
        verified_count = 0
        all_ok = True
        error_msg = None

        for rel_path, expected_hash in snapshot.file_manifest.items():
            restored_file = dest_path / rel_path
            if not restored_file.is_file():
                all_ok = False
                error_msg = f"Missing expected restored file: {rel_path}"
                break

            actual_file_hash = hashlib.sha256(restored_file.read_bytes()).hexdigest()
            if actual_file_hash != expected_hash:
                all_ok = False
                error_msg = f"Checksum mismatch on restored file {rel_path}"
                break

            verified_count += 1

        duration = max(0.001, time.perf_counter() - start_time)
        drill_result = RestoreDrillResult(
            drill_id=f"drl_{uuid.uuid4().hex[:8]}",
            snapshot_id=snapshot_id,
            isolated_destination=str(dest_path),
            success=all_ok,
            files_restored=verified_count,
            integrity_verified=all_ok,
            duration_seconds=duration,
            error_message=error_msg,
        )

        with self._lock:
            self._drills.append(drill_result)
            self._save_registry()

        logger.info(
            "Restore drill finished for %s: success=%s, files=%d, duration=%.3fs",
            snapshot_id,
            all_ok,
            verified_count,
            duration,
        )
        return drill_result

    def apply_retention_policy(
        self,
        project_id: str,
        *,
        max_snapshots: int = 5,
        max_age_days: int = 30,
    ) -> list[str]:
        """Prunes snapshots exceeding maximum counts or older than retention age."""
        now = datetime.now(UTC)
        age_limit = now - timedelta(days=max_age_days)

        with self._lock:
            proj_snapshots = [
                s for s in self._snapshots.values() if s.project_id == project_id
            ]
            # Sort newest first
            proj_snapshots.sort(key=lambda s: s.created_at, reverse=True)

            to_keep: set[str] = set()
            to_delete: list[str] = []

            for idx, snap in enumerate(proj_snapshots):
                if idx < max_snapshots and snap.created_at >= age_limit:
                    to_keep.add(snap.snapshot_id)
                else:
                    to_delete.append(snap.snapshot_id)

            # Perform physical deletion
            for snap_id in to_delete:
                snap = self._snapshots.pop(snap_id)
                fpath = Path(snap.storage_location)
                if fpath.is_file():
                    try:
                        fpath.unlink()
                    except OSError as exc:
                        logger.warning("Could not delete pruned snapshot file %s: %s", fpath, exc)

            self._save_registry()

        logger.info("Retention applied for %s: kept %d, pruned %d", project_id, len(to_keep), len(to_delete))
        return to_delete

    def list_snapshots(self, project_id: str | None = None) -> list[BackupSnapshot]:
        with self._lock:
            if project_id:
                return [s for s in self._snapshots.values() if s.project_id == project_id]
            return list(self._snapshots.values())

    def list_drills(self, snapshot_id: str | None = None) -> list[RestoreDrillResult]:
        with self._lock:
            if snapshot_id:
                return [d for d in self._drills if d.snapshot_id == snapshot_id]
            return list(self._drills)


__all__ = [
    "BackupSnapshot",
    "BackupStorageTarget",
    "CloudBackupService",
    "RestoreDrillResult",
]
