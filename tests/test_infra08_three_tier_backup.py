"""Comprehensive deterministic test suite for INFRA-08: 3-Tier Backups (R2 + On-Premise).

Governed by:
- INFRA-08: Automação de Backups 3-Camadas (R2 + On-Premise)
- Gate G1 Decisions: AES-256-GCM, asymmetric retention (7d R2 / 120d On-Prem), dual-target dispatch.
- Invariant: Restauração demonstrada em destino isolado, fail-closed on tampering, zero data leakage.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from core.infra.backup_service import (
    BackupSnapshot,
    BackupStorageTarget,
    CloudBackupService,
)
from core.infra.cli import main as cli_main
from core.infra.crypto import (
    CorruptedArchiveError,
    DecryptionError,
    decrypt_bytes,
    decrypt_file,
    encrypt_bytes,
    encrypt_file,
)
from core.infra.postgres_dumper import PostgresDumper
from core.infra.r2_client import R2StorageClient


# ==============================================================================
# 1. Cryptography & Security Tests (AES-256-GCM / PBKDF2)
# ==============================================================================


def test_crypto_aes_gcm_roundtrip(tmp_path: Path) -> None:
    """Verifies encryption and decryption roundtrip for bytes and files."""
    secret_text = b"DATABASE_PASSWORD=super_secret_jwt_key_98765\nAPI_TOKEN=xyz"
    passphrase = "master-cluster-password-2026"

    # Bytes roundtrip
    encrypted = encrypt_bytes(secret_text, passphrase)
    assert encrypted != secret_text
    assert encrypted.startswith(b"DFENC01\0")

    decrypted = decrypt_bytes(encrypted, passphrase)
    assert decrypted == secret_text

    # File roundtrip
    src_file = tmp_path / "secrets.env"
    enc_file = tmp_path / "secrets.env.enc"
    dec_file = tmp_path / "secrets_restored.env"

    src_file.write_bytes(secret_text)
    encrypt_file(src_file, enc_file, passphrase)
    assert enc_file.is_file()
    assert enc_file.stat().st_size > len(secret_text)

    decrypt_file(enc_file, dec_file, passphrase)
    assert dec_file.is_file()
    assert dec_file.read_bytes() == secret_text


def test_crypto_tampering_fail_closed() -> None:
    """Tampering with any ciphertext bit or header byte raises DecryptionError or CorruptedArchiveError."""
    data = b"confidential transaction log"
    passphrase = "tamper-proof-passphrase"
    encrypted = bytearray(encrypt_bytes(data, passphrase))

    # Tamper with header magic
    corrupted_header = bytearray(encrypted)
    corrupted_header[0] = ord(b"X")
    with pytest.raises(CorruptedArchiveError):
        decrypt_bytes(bytes(corrupted_header), passphrase)

    # Tamper with ciphertext payload
    corrupted_payload = bytearray(encrypted)
    corrupted_payload[-5] ^= 0xFF
    with pytest.raises(DecryptionError):
        decrypt_bytes(bytes(corrupted_payload), passphrase)


def test_crypto_wrong_key_fails() -> None:
    """Decrypting with the incorrect key must fail authentication."""
    data = b"critical system dump"
    encrypted = encrypt_bytes(data, "correct-key-1")

    with pytest.raises(DecryptionError):
        decrypt_bytes(encrypted, "wrong-key-2")


def test_crypto_empty_passphrase_raises() -> None:
    """Empty passphrase must be rejected."""
    with pytest.raises(ValueError, match="cannot be empty"):
        encrypt_bytes(b"data", "")


# ==============================================================================
# 2. Cloudflare R2 Client & AWS SigV4 Tests
# ==============================================================================


def test_r2_client_mock_lifecycle(tmp_path: Path) -> None:
    """Verifies mock storage operations: upload, download, list, exists and delete."""
    client = R2StorageClient(is_mock=True)

    src = tmp_path / "artifact.tar.gz"
    src.write_text("tarball contents 12345", encoding="utf-8")

    # Upload
    info = client.upload_file(src, "backups/darkfac/artifact.tar.gz")
    assert info.key == "backups/darkfac/artifact.tar.gz"
    assert info.size_bytes == src.stat().st_size
    assert client.object_exists("backups/darkfac/artifact.tar.gz") is True

    # List
    listed = client.list_objects("backups/darkfac/")
    assert len(listed) == 1
    assert listed[0].key == "backups/darkfac/artifact.tar.gz"

    # Download
    dst = tmp_path / "downloaded.tar.gz"
    client.download_file("backups/darkfac/artifact.tar.gz", dst)
    assert dst.read_text(encoding="utf-8") == "tarball contents 12345"

    # Delete
    assert client.delete_object("backups/darkfac/artifact.tar.gz") is True
    assert client.object_exists("backups/darkfac/artifact.tar.gz") is False
    assert len(client.list_objects("backups/darkfac/")) == 0


def test_r2_sigv4_signing() -> None:
    """Verifies AWS SigV4 signer produces valid format headers with HMAC signature."""
    client = R2StorageClient(
        endpoint_url="https://test-account.r2.cloudflarestorage.com",
        access_key_id="test_key_id",
        secret_access_key="test_secret_access_key_abcdef123456",
        bucket_name="test-bucket",
        is_mock=False,
    )
    headers = {"Host": "test-account.r2.cloudflarestorage.com"}
    dt = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    payload_hash = hashlib.sha256(b"sample_payload").hexdigest()

    signed = client._sign_request("PUT", "/test-bucket/backup.enc", "", headers, payload_hash, dt)

    assert "Authorization" in signed
    assert "x-amz-date" in signed
    assert signed["x-amz-date"] == "20260925T120000Z"
    assert signed["x-amz-content-sha256"] == payload_hash
    auth = signed["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=test_key_id/20260925/auto/s3/aws4_request")
    assert "Signature=" in auth


# ==============================================================================
# 3. PostgreSQL Dumper Tests
# ==============================================================================


def test_postgres_dumper_deterministic(tmp_path: Path) -> None:
    """Verifies PostgreSQL dumper produces consistent SQL export."""
    dumper = PostgresDumper()
    dest = tmp_path / "postgres_dump.sql"

    res = dumper.dump(dest, force_synthetic=True)
    assert res.size_bytes > 0
    assert len(res.checksum_sha256) == 64
    assert dest.is_file()
    content = dest.read_text(encoding="utf-8")
    assert "CREATE DATABASE \"darkfac_core\"" in content
    assert "public.schema_migrations" in content


# ==============================================================================
# 4. 3-Tier Backup & Isolated Restore Drill Tests
# ==============================================================================


@pytest.fixture
def backup_env(tmp_path: Path) -> tuple[CloudBackupService, Path, Path, Path]:
    backup_root = tmp_path / "factory_backups"
    onprem_root = tmp_path / "drive_e_mirror"
    source_dir = tmp_path / "project_source"
    isolated_dest = tmp_path / "isolated_restore_sandbox"

    backup_root.mkdir()
    onprem_root.mkdir()
    source_dir.mkdir()
    isolated_dest.mkdir()

    # Seed source directory
    (source_dir / "config.json").write_text('{"env": "production"}', encoding="utf-8")
    (source_dir / "models.py").write_text("class ProjectRecord: pass", encoding="utf-8")

    r2_client = R2StorageClient(is_mock=True)
    dumper = PostgresDumper()
    service = CloudBackupService(
        backup_root=backup_root,
        onprem_root=onprem_root,
        r2_client=r2_client,
        postgres_dumper=dumper,
        encryption_key="vault-secret-key-infra08",
    )
    return service, source_dir, onprem_root, isolated_dest


def test_three_tier_backup_creation(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Verifies that 3-tier backup creates local tarball, AES-256-GCM archive, R2 upload, and on-prem mirror."""
    service, source_dir, onprem_root, _ = backup_env

    snap = service.create_three_tier_backup(
        project_id="darkfac-main",
        source_directory=source_dir,
        include_postgres=True,
    )

    # 1. Staging checks
    assert snap.project_id == "darkfac-main"
    assert snap.storage_target == BackupStorageTarget.THREE_TIER
    assert snap.encrypted is True
    assert len(snap.archive_checksum) == 64
    assert len(snap.encrypted_archive_checksum) == 64
    assert snap.files_count == 3  # config.json, models.py, database/postgres_dump.sql
    assert "database/postgres_dump.sql" in snap.file_manifest

    # 2. Local archive files
    assert Path(snap.storage_location).is_file()
    enc_local = Path(service.backup_root / f"{snap.snapshot_id}.tar.gz.enc")
    assert enc_local.is_file()

    # 3. Cloudflare R2 Upload
    assert snap.r2_object_key == f"backups/darkfac-main/{snap.snapshot_id}.tar.gz.enc"
    assert service.r2_client.object_exists(snap.r2_object_key) is True

    # 4. On-premise mirror (Drive E:)
    onprem_file = Path(snap.onprem_location)
    assert onprem_file.is_file()
    assert onprem_file.parent == onprem_root / "darkfac-main"
    assert onprem_file.read_bytes() == enc_local.read_bytes()


def test_restore_drill_from_local_tier(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Restore drill unpackages and verifies checksums from local tier."""
    service, source_dir, _, isolated_dest = backup_env
    snap = service.create_three_tier_backup("darkfac-local", source_dir)

    drill = service.run_restore_drill(snap.snapshot_id, isolated_dest, from_tier="local")

    assert drill.success is True
    assert drill.integrity_verified is True
    assert drill.files_restored == 3
    assert (isolated_dest / "config.json").read_text(encoding="utf-8") == '{"env": "production"}'
    assert (isolated_dest / "database" / "postgres_dump.sql").is_file()


def test_restore_drill_from_r2_tier(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Restore drill downloads from Cloudflare R2, decrypts AES-256-GCM and verifies sandbox."""
    service, source_dir, _, isolated_dest = backup_env
    snap = service.create_three_tier_backup("darkfac-r2", source_dir)

    # Delete local unencrypted and encrypted files to guarantee restore comes purely from R2
    Path(snap.storage_location).unlink()
    Path(service.backup_root / f"{snap.snapshot_id}.tar.gz.enc").unlink()

    drill = service.run_restore_drill(snap.snapshot_id, isolated_dest, from_tier="r2")

    assert drill.success is True
    assert drill.integrity_verified is True
    assert drill.source_tier == "r2"
    assert (isolated_dest / "models.py").read_text(encoding="utf-8") == "class ProjectRecord: pass"


def test_restore_drill_from_onprem_tier(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Restore drill restores from on-premise mirror (Drive E:), decrypts and validates sandbox."""
    service, source_dir, _, isolated_dest = backup_env
    snap = service.create_three_tier_backup("darkfac-onprem", source_dir)

    # Delete local and R2 copies
    Path(snap.storage_location).unlink()
    Path(service.backup_root / f"{snap.snapshot_id}.tar.gz.enc").unlink()
    service.r2_client.delete_object(snap.r2_object_key)

    drill = service.run_restore_drill(snap.snapshot_id, isolated_dest, from_tier="onprem")

    assert drill.success is True
    assert drill.integrity_verified is True
    assert drill.source_tier == "onprem"
    assert (isolated_dest / "config.json").is_file()


def test_restore_drill_tampered_fails(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Corrupted archive in R2 causes restore drill failure."""
    service, source_dir, _, isolated_dest = backup_env
    snap = service.create_three_tier_backup("darkfac-tamper", source_dir)

    # Corrupt object in mock R2
    corrupted_data = bytearray(service.r2_client._mock_objects[snap.r2_object_key][0])
    corrupted_data[-10] ^= 0xAA
    service.r2_client._mock_objects[snap.r2_object_key] = (bytes(corrupted_data), datetime.now(UTC))

    with pytest.raises(DecryptionError):
        service.run_restore_drill(snap.snapshot_id, isolated_dest, from_tier="r2")


# ==============================================================================
# 5. Asymmetric Retention Policy Tests (7d R2 / 120d On-Prem)
# ==============================================================================


def test_asymmetric_retention_policy(backup_env: tuple[CloudBackupService, Path, Path, Path]) -> None:
    """Verifies that R2 snapshots older than 7d and on-prem older than 120d are pruned independently."""
    service, source_dir, _, _ = backup_env

    # 1. Create a snapshot that is 10 days old (should be pruned from R2, kept in on-prem)
    snap_10d = service.create_three_tier_backup("darkfac-ret", source_dir)
    t_10d = datetime.now(UTC) - timedelta(days=10)
    service._snapshots[snap_10d.snapshot_id] = snap_10d.model_copy(update={"created_at": t_10d})

    # 2. Create a snapshot that is 130 days old (should be pruned from both R2 and on-prem)
    snap_130d = service.create_three_tier_backup("darkfac-ret", source_dir)
    t_130d = datetime.now(UTC) - timedelta(days=130)
    service._snapshots[snap_130d.snapshot_id] = snap_130d.model_copy(update={"created_at": t_130d})

    # 3. Create a snapshot that is fresh (1 day old, kept in both)
    snap_fresh = service.create_three_tier_backup("darkfac-ret", source_dir)

    # Apply asymmetric policy: 7 days for R2, 120 days for On-Prem
    result = service.apply_tiered_retention_policy(
        "darkfac-ret",
        r2_max_age_days=7,
        onprem_max_age_days=120,
    )

    # R2 pruned: snap_10d and snap_130d
    assert snap_10d.r2_object_key in result["r2_pruned"]
    assert snap_130d.r2_object_key in result["r2_pruned"]
    assert snap_fresh.r2_object_key not in result["r2_pruned"]
    assert service.r2_client.object_exists(snap_fresh.r2_object_key) is True
    assert service.r2_client.object_exists(snap_10d.r2_object_key) is False

    # On-Prem pruned: only snap_130d (> 120 days)
    assert snap_130d.onprem_location in result["onprem_pruned"]
    assert snap_10d.onprem_location not in result["onprem_pruned"]
    assert Path(snap_10d.onprem_location).is_file()
    assert not Path(snap_130d.onprem_location).exists()


# ==============================================================================
# 6. CLI Commands Integration Tests
# ==============================================================================


def test_cli_backup_commands_integration(tmp_path: Path) -> None:
    """Verifies that CLI commands backup-run, backup-list, backup-restore, and backup-retention succeed."""
    source_dir = tmp_path / "cli_src"
    source_dir.mkdir()
    (source_dir / "app.py").write_text("print('cli')", encoding="utf-8")
    restore_dest = tmp_path / "cli_restore"

    # 1. backup-run
    ret_run = cli_main([
        "backup-run",
        "--project-id", "proj-cli",
        "--source-dir", str(source_dir),
    ])
    assert ret_run == 0

    # 2. backup-list
    ret_list = cli_main(["backup-list", "--project-id", "proj-cli"])
    assert ret_list == 0

    # 3. get snapshot ID
    service = CloudBackupService()
    snaps = service.list_snapshots("proj-cli")
    assert len(snaps) >= 1
    snap_id = snaps[0].snapshot_id

    # 4. backup-restore
    ret_res = cli_main([
        "backup-restore",
        "--snapshot-id", snap_id,
        "--destination", str(restore_dest),
        "--from-tier", "local",
    ])
    assert ret_res == 0
    assert (restore_dest / "app.py").read_text(encoding="utf-8") == "print('cli')"

    # 5. backup-retention
    ret_ret = cli_main([
        "backup-retention",
        "--project-id", "proj-cli",
        "--r2-days", "7",
        "--onprem-days", "120",
    ])
    assert ret_ret == 0
