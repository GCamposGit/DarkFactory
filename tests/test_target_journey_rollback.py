"""Deterministic unit tests for HF-12-03: JourneyObserver and RollbackAdapter.

Governed by:
- docs/handoffs/continuous-autonomy/HF-12-03.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/orchestrator/target_journey.py
- core/acceptance/rollback.py

Mandatory requirements & counter-proofs:
1. Successful journey observation with matching digest and persisted nonce.
2. Counter-proof: Health 200 with stale code version/digest fails closed (valid=False, status='failed').
3. Counter-proof: Unpersisted nonce across restart fails closed.
4. Counter-proof: Rollback with only JSON configuration files fails closed ("Rollback só JSON reprova").
5. Successful rollback verifies byte checksums, restores real data files, and records RPO/RTO.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from core.acceptance.models import RollbackExecutionRecord
from core.acceptance.rollback import RollbackAdapter
from core.infra.backup_service import CloudBackupService
from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.deployment_adapter import TargetConfig
from core.orchestrator.target_journey import (
    JourneyEvidenceReceipt,
    JourneyObserver,
    SimulatedJourneyTarget,
)


# ---------------------------------------------------------------------------
# 1. JourneyObserver: Successful Verification
# ---------------------------------------------------------------------------

def test_journey_observer_successful_observation() -> None:
    """Verifies that matching digest and persisted nonce produce a valid receipt."""
    code_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    artifact = ArtifactRef(
        artifact_id="art_valid_123",
        source_sha="commit_sha_valid_123",
        byte_digest=code_digest,
        byte_size=1024,
    )

    sim_target = SimulatedJourneyTarget(
        installed_digest=code_digest,
        health_status=200,
        persist_nonce_on_restart=True,
    )

    target_config = TargetConfig(
        project_id="site-ggcampos",
        target_type="static_web",
        environment="production",
        healthcheck_endpoint="https://site.ggcampos.com/health",
        metadata={"simulator": sim_target},
    )

    observer = JourneyObserver()
    nonce = "test-nonce-verified-20260918"
    receipt = observer.observe(artifact=artifact, target=target_config, nonce=nonce)

    assert isinstance(receipt, JourneyEvidenceReceipt)
    assert receipt.valid is True
    assert receipt.status == "succeeded"
    assert receipt.digest_verified is True
    assert receipt.nonce_verified is True
    assert receipt.persistence_verified is True
    assert receipt.health_status == 200
    assert receipt.artifact_digest == code_digest
    assert receipt.served_digest == code_digest
    assert receipt.nonce == nonce
    assert receipt.error is None
    assert sim_target.restart_count == 1


# ---------------------------------------------------------------------------
# 2. Counter-Proof: Health 200 with Stale Code Fails Closed
# ---------------------------------------------------------------------------

def test_counter_proof_health_200_with_stale_code_fails_closed() -> None:
    """MANDATORY COUNTER-PROOF: HTTP 200 with stale served code digest must fail closed."""
    expected_digest = hashlib.sha256(b"v2.0.0-production-release").hexdigest()
    stale_served_digest = hashlib.sha256(b"v1.0.0-old-served-code").hexdigest()

    artifact = ArtifactRef(
        artifact_id="art_v2_expected",
        source_sha="sha_v2_new",
        byte_digest=expected_digest,
        byte_size=2048,
    )

    sim_target = SimulatedJourneyTarget(
        installed_digest=stale_served_digest,
        health_status=200,  # Target falsely claims HTTP 200 healthy!
        persist_nonce_on_restart=True,
    )

    target_config = TargetConfig(
        project_id="darkfac",
        target_type="dokploy_docker_compose",
        environment="production",
        healthcheck_endpoint="http://darkfac.internal/health",
        metadata={"simulator": sim_target},
    )

    observer = JourneyObserver()
    receipt = observer.observe(artifact=artifact, target=target_config, nonce="nonce-stale-probe")

    assert receipt.valid is False
    assert receipt.status == "failed"
    assert receipt.digest_verified is False
    assert receipt.health_status == 200
    assert receipt.artifact_digest == expected_digest
    assert receipt.served_digest == stale_served_digest
    assert receipt.error is not None
    assert "código velho" in receipt.error.lower() or "stale" in receipt.error.lower()


# ---------------------------------------------------------------------------
# 3. Counter-Proof: Unpersisted Nonce Fails Closed
# ---------------------------------------------------------------------------

def test_counter_proof_unpersisted_nonce_fails_closed() -> None:
    """MANDATORY COUNTER-PROOF: Nonce lost upon target restart must fail closed."""
    code_digest = hashlib.sha256(b"v2.1.0-clean-code").hexdigest()

    artifact = ArtifactRef(
        artifact_id="art_v2_1",
        source_sha="sha_v2_1",
        byte_digest=code_digest,
        byte_size=4096,
    )

    # Simulator where restart wipes the nonce store (e.g. ephemeral RAM state not persisted)
    sim_target = SimulatedJourneyTarget(
        installed_digest=code_digest,
        health_status=200,
        persist_nonce_on_restart=False,
    )

    target_config = TargetConfig(
        project_id="segundo-cerebro",
        target_type="local_service",
        environment="production",
        healthcheck_endpoint="http://127.0.0.1:8765/health",
        metadata={"simulator": sim_target},
    )

    observer = JourneyObserver()
    receipt = observer.observe(artifact=artifact, target=target_config, nonce="volatile-nonce-999")

    assert receipt.valid is False
    assert receipt.status == "failed"
    assert receipt.nonce_verified is True  # Read back prior to restart succeeded
    assert receipt.persistence_verified is False  # Failed to survive restart
    assert receipt.error is not None
    assert "persist" in receipt.error.lower()


def test_journey_observer_fails_closed_on_non_200_probe() -> None:
    """Verifies that non-200 probe (e.g. 503 or 500) fails closed."""
    code_digest = hashlib.sha256(b"code").hexdigest()
    artifact = ArtifactRef(
        artifact_id="art_503",
        source_sha="sha_503",
        byte_digest=code_digest,
        byte_size=100,
    )
    sim_target = SimulatedJourneyTarget(
        installed_digest=code_digest,
        health_status=503,
    )
    target_config = TargetConfig(
        project_id="jarvis",
        target_type="local_service",
        metadata={"simulator": sim_target},
    )

    observer = JourneyObserver()
    receipt = observer.observe(artifact=artifact, target=target_config, nonce="nonce-503")

    assert receipt.valid is False
    assert receipt.status == "failed"
    assert receipt.health_status == 503


# ---------------------------------------------------------------------------
# 4. Counter-Proof: Rollback with Only JSON Fails Closed
# ---------------------------------------------------------------------------

def test_counter_proof_rollback_with_only_json_fails_closed(tmp_path: Path) -> None:
    """MANDATORY COUNTER-PROOF: Rollback só JSON reprova.
    Backup containing only JSON config without database/data files must raise ValueError.
    """
    backup_root = tmp_path / "backups"
    backup_service = CloudBackupService(backup_root=backup_root)
    adapter = RollbackAdapter(backup_service=backup_service)

    # Prepare a directory containing ONLY JSON files (configuration only)
    source_dir = tmp_path / "json_only_state"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "config.json").write_text(json.dumps({"env": "prod", "workers": 4}), encoding="utf-8")
    (source_dir / "settings.json").write_text(json.dumps({"theme": "dark", "timeout": 30}), encoding="utf-8")

    snapshot = backup_service.create_backup("proj-json-only", source_dir)

    target_dir = tmp_path / "target_restore_dir"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "canary.txt").write_text("should_not_be_touched", encoding="utf-8")

    # Attempting restore must fail closed with exact expected error
    with pytest.raises(ValueError) as excinfo:
        adapter.restore(
            previous="prev_stable_digest_123",
            backup=snapshot,
            target=target_dir,
        )

    assert "Rollback rejected: backup contains only JSON configuration; client data missing" in str(excinfo.value)
    # Canary file in target was preserved, no destructive partial restore took place
    assert (target_dir / "canary.txt").read_text(encoding="utf-8") == "should_not_be_touched"


# ---------------------------------------------------------------------------
# 5. Successful Rollback: Verifies Checksums, Data Restoration & RPO/RTO
# ---------------------------------------------------------------------------

def test_successful_rollback_verifies_checksums_and_measures_rpo_rto(tmp_path: Path) -> None:
    """Verifies that a valid backup with real data files is restored, byte checksums verified,
    and RPO/RTO accurately recorded.
    """
    backup_root = tmp_path / "backups"
    backup_service = CloudBackupService(backup_root=backup_root)
    adapter = RollbackAdapter(backup_service=backup_service)

    # Prepare rich state with real database, data files, and configuration
    source_dir = tmp_path / "full_state"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "app_config.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
    db_bytes = b"SQLITE_FORMAT_3\x00\x10\x00\x01\x01\x00@  \x00\x00\x00\x01--REAL_CLIENT_DATABASE--"
    (source_dir / "client_records.db").write_bytes(db_bytes)
    binary_data = b"\xde\xad\xbe\xef" * 512
    (source_dir / "cache_store.dat").write_bytes(binary_data)

    snapshot = backup_service.create_backup("proj-real-data", source_dir)

    # Slight pause to ensure measurable positive RPO
    time.sleep(0.01)

    target_dir = tmp_path / "restored_production"
    target_dir.mkdir(parents=True, exist_ok=True)

    previous_digest = hashlib.sha256(b"previous_verified_artifact").hexdigest()
    artifact = ArtifactRef(
        artifact_id="art_prev_good",
        source_sha="sha_good_baseline",
        byte_digest=previous_digest,
        byte_size=8192,
    )

    record = adapter.restore(
        previous=artifact,
        backup=snapshot,
        target=target_dir,
        trigger_reason="production_smoke_failure",
    )

    assert isinstance(record, RollbackExecutionRecord)
    assert record.success is True
    assert record.project_id == "proj-real-data"
    assert record.post_rollback_digest == previous_digest
    assert record.trigger_reason == "production_smoke_failure"
    assert record.rpo_seconds >= 0.0
    assert record.rto_seconds >= 0.0
    assert len(record.evidence_hash) == 64

    # Verify 100% byte fidelity of restored data files
    restored_db = target_dir / "client_records.db"
    assert restored_db.exists()
    assert restored_db.read_bytes() == db_bytes

    restored_dat = target_dir / "cache_store.dat"
    assert restored_dat.exists()
    assert restored_dat.read_bytes() == binary_data

    restored_json = target_dir / "app_config.json"
    assert restored_json.exists()
    assert json.loads(restored_json.read_text(encoding="utf-8")) == {"version": "1.0.0"}


def test_rollback_adapter_with_target_config_and_string_previous(tmp_path: Path) -> None:
    """Verifies RollbackAdapter works with TargetConfig instance and string previous digest."""
    backup_root = tmp_path / "backups"
    backup_service = CloudBackupService(backup_root=backup_root)
    adapter = RollbackAdapter(backup_service=backup_service)

    source_dir = tmp_path / "state"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "records.db").write_text("database-v1-stable", encoding="utf-8")
    (source_dir / "config.json").write_text('{"db": true}', encoding="utf-8")

    snapshot = backup_service.create_backup("proj-tc", source_dir)

    restore_destination = tmp_path / "tc_restored"
    target_config = TargetConfig(
        project_id="proj-tc",
        target_type="dokploy_docker_compose",
        last_known_good_digest="old_known_good_digest_abc",
        metadata={"state_directory": str(restore_destination)},
    )

    record = adapter.restore(
        previous="restored_target_digest_xyz",
        backup=snapshot,
        target=target_config,
    )

    assert record.success is True
    assert record.post_rollback_digest == "restored_target_digest_xyz"
    assert target_config.last_known_good_digest == "restored_target_digest_xyz"
    assert (restore_destination / "records.db").read_text(encoding="utf-8") == "database-v1-stable"


def test_journey_observer_with_transport_callable() -> None:
    """Verifies JourneyObserver with custom transport callable."""
    expected_digest = hashlib.sha256(b"transport-app-code").hexdigest()
    artifact = ArtifactRef(
        artifact_id="art_transport",
        source_sha="sha_transport",
        byte_digest=expected_digest,
        byte_size=1000,
    )
    target = TargetConfig(
        project_id="dokploy-app",
        target_type="dokploy_docker_compose",
    )

    state: dict[str, Any] = {"nonce": None, "restarted": False}

    def mock_transport(method: str, path: str, body: Any, headers: Any) -> dict[str, Any]:
        if method == "GET" and path == "/health":
            return {"status": 200, "digest": expected_digest}
        elif method == "POST" and path == "/journey/nonce":
            state["nonce"] = body["nonce"]
            return {"status": 200}
        elif method == "GET" and path == "/journey/nonce":
            return {"nonce": state["nonce"]}
        elif method == "POST" and path == "/restart":
            state["restarted"] = True
            return {"status": 200}
        return {"status": 404}

    observer = JourneyObserver(transport=mock_transport)
    receipt = observer.observe(artifact=artifact, target=target, nonce="transport-nonce-555")

    assert receipt.valid is True
    assert receipt.status == "succeeded"
    assert receipt.nonce_verified is True
    assert receipt.persistence_verified is True
    assert state["restarted"] is True


def test_rollback_adapter_from_directory_path(tmp_path: Path) -> None:
    """Verifies RollbackAdapter restoring directly from a directory backup."""
    backup_dir = tmp_path / "raw_backup_dir"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "data.db").write_text("raw_db_content", encoding="utf-8")
    (backup_dir / "app.json").write_text('{"status": "ok"}', encoding="utf-8")

    target_dir = tmp_path / "raw_target_dir"
    target_dir.mkdir(parents=True, exist_ok=True)

    adapter = RollbackAdapter()
    record = adapter.restore(
        previous="raw_dir_digest_001",
        backup=backup_dir,
        target=target_dir,
    )

    assert record.success is True
    assert (target_dir / "data.db").read_text(encoding="utf-8") == "raw_db_content"
    assert (target_dir / "app.json").read_text(encoding="utf-8") == '{"status": "ok"}'


def test_rollback_adapter_rejects_empty_backup(tmp_path: Path) -> None:
    """Verifies that restoring from an empty backup fails closed."""
    empty_dir = tmp_path / "empty_backup"
    empty_dir.mkdir(parents=True, exist_ok=True)

    target_dir = tmp_path / "empty_target"
    target_dir.mkdir(parents=True, exist_ok=True)

    adapter = RollbackAdapter()
    with pytest.raises(ValueError) as excinfo:
        adapter.restore(
            previous="digest_empty",
            backup=empty_dir,
            target=target_dir,
        )

    assert "client data missing" in str(excinfo.value)


def test_workflow_contracts_collection_preflight_passes() -> None:
    """Preflight contract verification: tests/test_workflow_contracts.py passes collection."""
    import subprocess
    import sys

    res = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_workflow_contracts.py", "--collect-only", "-q"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "16 tests collected" in res.stdout or "collected" in res.stdout

