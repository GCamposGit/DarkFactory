"""Comprehensive test suite for HF-12: Staging, Production, Client Acceptance, Smoke, Rollback & Backups.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 5, 9, 12, line 264 & 297 / HF-12 / INFRA-08)
- HYBRID_AUTONOMY_REQUIREMENTS (Sections 5, 7, Scenario G8)
- Invariants:
  1. 'Mesmo artefato é promovido de staging para produção'
  2. Scenario G8: 'Projeto pagante preserva aceite antes do deploy; entrega só após provas operacionais;
     falha de produção abre recuperação e não para projetos independentes.'
  3. 'Restauração demonstrada em destino isolado, inclusive de credenciais cifradas.'
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from core.infra.backup_service import (
    BackupSnapshot,
    BackupStorageTarget,
    CloudBackupService,
    RestoreDrillResult,
)
from core.orchestrator.release_pipeline import (
    BuildArtifact,
    ClientAcceptanceReceipt,
    ClientAcceptanceRequiredError,
    DeploymentRecord,
    DeploymentStage,
    ProductionDeploymentFailedError,
    ProjectTier,
    ReleasePipelineService,
    RollbackReceipt,
    StagingValidationFailedError,
)


@pytest.fixture
def temp_service(tmp_path: Path) -> ReleasePipelineService:
    return ReleasePipelineService(storage_path=tmp_path / "release_registry.json")


@pytest.fixture
def temp_backup(tmp_path: Path) -> CloudBackupService:
    return CloudBackupService(backup_root=tmp_path / "backups")


# ---------------------------------------------------------------------------
# 1. Imutabilidade de Artefato & Build
# ---------------------------------------------------------------------------

def test_immutable_build_artifact_generation(temp_service: ReleasePipelineService) -> None:
    """Build generates an immutable BuildArtifact with reproducible digests."""
    git_sha = "a" * 40
    artifact = temp_service.build(
        project_id="proj-alpha",
        git_sha=git_sha,
        manifest_payload={"system": "linux", "arch": "x86_64", "ports": [8080]},
    )

    assert artifact.project_id == "proj-alpha"
    assert artifact.git_sha == git_sha
    assert len(artifact.artifact_digest) == 64
    assert len(artifact.manifest_digest) == 64
    assert artifact.build_id.startswith("bld_")

    # Retrieved artifact matches
    retrieved = temp_service.get_artifact(artifact.artifact_digest)
    assert retrieved is not None
    assert retrieved.artifact_digest == artifact.artifact_digest


# ---------------------------------------------------------------------------
# 2. Deploy em Staging com Preflight e Smoke
# ---------------------------------------------------------------------------

def test_staging_deployment_with_preflight_and_smoke(temp_service: ReleasePipelineService) -> None:
    """Staging deploy runs destination preflight and journey smoke test."""
    artifact = temp_service.build(project_id="proj-alpha", git_sha="b" * 40)

    # Success case
    record = temp_service.deploy_staging(artifact, preflight_passed=True, smoke_passed=True)
    assert record.stage == DeploymentStage.STAGING_DEPLOYED
    assert record.preflight.passed is True
    assert record.smoke_test is not None
    assert record.smoke_test.passed is True
    assert record.smoke_test.target_environment == "staging"

    # Preflight failure raises StagingValidationFailedError
    with pytest.raises(StagingValidationFailedError, match="preflight failed"):
        temp_service.deploy_staging(artifact, preflight_passed=False)

    # Smoke failure raises StagingValidationFailedError
    with pytest.raises(StagingValidationFailedError, match="smoke test failed"):
        temp_service.deploy_staging(artifact, smoke_passed=False)


# ---------------------------------------------------------------------------
# 3. Invariante: O Mesmo Artefato é Promovido (Zero Recompilations)
# ---------------------------------------------------------------------------

def test_same_artifact_promotion_invariant(temp_service: ReleasePipelineService) -> None:
    """The exact same artifact digest validated in staging is promoted to production."""
    artifact = temp_service.build(project_id="proj-internal", git_sha="c" * 40)

    stg_record = temp_service.deploy_staging(artifact)
    prod_record = temp_service.deploy_production(artifact, project_tier=ProjectTier.INTERNAL_FREE)

    assert stg_record.artifact_digest == artifact.artifact_digest
    assert prod_record.artifact_digest == artifact.artifact_digest
    assert stg_record.artifact_digest == prod_record.artifact_digest
    assert prod_record.stage == DeploymentStage.DELIVERED


# ---------------------------------------------------------------------------
# 4. Cenário G8: Cliente Pagante Bloqueia Produção sem Aceite
# ---------------------------------------------------------------------------

def test_scenario_g8_commercial_paid_blocks_without_client_acceptance(temp_service: ReleasePipelineService) -> None:
    """Commercial paid project is strictly blocked from production without client acceptance receipt."""
    artifact = temp_service.build(project_id="client-enterprise-corp", git_sha="d" * 40)
    temp_service.deploy_staging(artifact)

    # Attempt deploy to production without client acceptance -> BLOCKED!
    with pytest.raises(ClientAcceptanceRequiredError, match="requires client acceptance receipt"):
        temp_service.deploy_production(artifact, project_tier=ProjectTier.COMMERCIAL_PAID)

    # Ensure project was NOT delivered to production
    assert temp_service.get_current_stable_artifact("client-enterprise-corp") is None


# ---------------------------------------------------------------------------
# 5. Cenário G8: Cliente Pagante com Aceite Avança e Atinge DELIVERED
# ---------------------------------------------------------------------------

def test_scenario_g8_commercial_paid_with_acceptance_delivers(temp_service: ReleasePipelineService) -> None:
    """Commercial paid project with signed acceptance receipt successfully delivers to production."""
    artifact = temp_service.build(project_id="client-fintech", git_sha="e" * 40)
    temp_service.deploy_staging(artifact)

    # Issue client acceptance receipt
    receipt = temp_service.record_client_acceptance(
        project_id="client-fintech",
        artifact_digest=artifact.artifact_digest,
        client_id="fintech-cfo-group",
        approved_by="Alice Corporate VP",
    )
    assert receipt.project_id == "client-fintech"
    assert len(receipt.signature_hash) == 64

    # Production deploy now succeeds with receipt
    prod_record = temp_service.deploy_production(artifact, project_tier=ProjectTier.COMMERCIAL_PAID)
    assert prod_record.stage == DeploymentStage.DELIVERED
    assert prod_record.acceptance_receipt_id == receipt.receipt_id
    assert temp_service.get_current_stable_artifact("client-fintech") == artifact.artifact_digest


# ---------------------------------------------------------------------------
# 6. Falha em Produção Aciona Rollback Automático Imediato
# ---------------------------------------------------------------------------

def test_production_smoke_failure_triggers_automatic_rollback(temp_service: ReleasePipelineService) -> None:
    """Smoke failure in production automatically rolls back to previous stable artifact."""
    # 1. Establish stable baseline v1 in production
    art_v1 = temp_service.build(project_id="proj-rollback", git_sha="1" * 40)
    temp_service.deploy_staging(art_v1)
    temp_service.deploy_production(art_v1, project_tier=ProjectTier.INTERNAL_FREE)
    assert temp_service.get_current_stable_artifact("proj-rollback") == art_v1.artifact_digest

    # 2. Deploy buggy v2 to production where smoke fails
    art_v2 = temp_service.build(project_id="proj-rollback", git_sha="2" * 40)
    temp_service.deploy_staging(art_v2)

    with pytest.raises(ProductionDeploymentFailedError, match="Automatic rollback executed"):
        temp_service.deploy_production(art_v2, project_tier=ProjectTier.INTERNAL_FREE, smoke_passed=False)

    # 3. Verify rollback receipt was emitted and previous stable is maintained
    rollbacks = temp_service.get_rollbacks("proj-rollback")
    assert len(rollbacks) == 1
    rb = rollbacks[0]
    assert rb.failed_artifact_digest == art_v2.artifact_digest
    assert rb.restored_artifact_digest == art_v1.artifact_digest
    assert "Production journey smoke test failed" in rb.trigger_reason
    assert temp_service.get_current_stable_artifact("proj-rollback") == art_v1.artifact_digest


# ---------------------------------------------------------------------------
# 7. Cenário G8: Falha em Projeto A NÃO Para Projetos Independentes
# ---------------------------------------------------------------------------

def test_scenario_g8_failure_isolation_does_not_stop_independent_projects(temp_service: ReleasePipelineService) -> None:
    """Production failure in Project A opens recovery for A but does not affect Project B."""
    art_a = temp_service.build(project_id="proj-alpha-crm", git_sha="a" * 40)
    art_b = temp_service.build(project_id="proj-beta-payments", git_sha="b" * 40)

    temp_service.deploy_staging(art_a)
    temp_service.deploy_staging(art_b)

    # Project A fails in production
    with pytest.raises(ProductionDeploymentFailedError):
        temp_service.deploy_production(art_a, smoke_passed=False)

    # Project A has recovery opened
    deployments_a = temp_service.get_deployments("proj-alpha-crm")
    assert deployments_a[-1].stage == DeploymentStage.RECOVERY_IN_PROGRESS

    # Project B deploys normally without being blocked or stopped by Project A's failure!
    prod_b = temp_service.deploy_production(art_b, smoke_passed=True)
    assert prod_b.stage == DeploymentStage.DELIVERED
    assert temp_service.get_current_stable_artifact("proj-beta-payments") == art_b.artifact_digest


# ---------------------------------------------------------------------------
# 8. Backups: Snapshot com SHA-256 e Manifesto Consistente (INFRA-08)
# ---------------------------------------------------------------------------

def test_backup_creation_and_checksum_manifest(temp_backup: CloudBackupService, tmp_path: Path) -> None:
    """Backup service packages directory into authenticated archive with SHA-256."""
    src_dir = tmp_path / "app_data"
    src_dir.mkdir()
    (src_dir / "config.json").write_text('{"db": "ready"}', encoding="utf-8")
    (src_dir / "secrets.enc").write_text("encrypted_tokens_12345", encoding="utf-8")

    snapshot = temp_backup.create_backup("proj-db", src_dir)

    assert snapshot.project_id == "proj-db"
    assert snapshot.files_count == 2
    assert "config.json" in snapshot.file_manifest
    assert "secrets.enc" in snapshot.file_manifest
    assert len(snapshot.archive_checksum) == 64
    assert Path(snapshot.storage_location).is_file()


# ---------------------------------------------------------------------------
# 9. Backups: Restauração Demonstrada em Destino Isolado (INFRA-08)
# ---------------------------------------------------------------------------

def test_proven_restore_drill_in_isolated_sandbox(temp_backup: CloudBackupService, tmp_path: Path) -> None:
    """Restore drill unpackages and verifies 100% of files in isolated destination."""
    src_dir = tmp_path / "source_app"
    src_dir.mkdir()
    (src_dir / "index.html").write_text("<h1>Hello DarkFactory</h1>", encoding="utf-8")
    (src_dir / "key.pem").write_text("---SAFE-MOCK-KEY---", encoding="utf-8")

    snapshot = temp_backup.create_backup("proj-web", src_dir)

    # Execute drill in isolated sandbox destination
    sandbox_dest = tmp_path / "isolated_restore_sandbox"
    drill_result = temp_backup.run_restore_drill(snapshot.snapshot_id, sandbox_dest)

    assert drill_result.success is True
    assert drill_result.integrity_verified is True
    assert drill_result.files_restored == 2
    assert (sandbox_dest / "index.html").read_text(encoding="utf-8") == "<h1>Hello DarkFactory</h1>"
    assert (sandbox_dest / "key.pem").read_text(encoding="utf-8") == "---SAFE-MOCK-KEY---"


# ---------------------------------------------------------------------------
# 10. Política de Retenção de Backups
# ---------------------------------------------------------------------------

def test_backup_retention_policy_pruning(temp_backup: CloudBackupService, tmp_path: Path) -> None:
    """Retention policy prunes old snapshots exceeding maximum allowed count."""
    src_dir = tmp_path / "data"
    src_dir.mkdir()
    (src_dir / "data.txt").write_text("sample", encoding="utf-8")

    # Create 4 snapshots
    s1 = temp_backup.create_backup("proj-retain", src_dir)
    s2 = temp_backup.create_backup("proj-retain", src_dir)
    s3 = temp_backup.create_backup("proj-retain", src_dir)
    s4 = temp_backup.create_backup("proj-retain", src_dir)

    assert len(temp_backup.list_snapshots("proj-retain")) == 4

    # Apply retention keeping only the 2 newest
    pruned = temp_backup.apply_retention_policy("proj-retain", max_snapshots=2)
    assert len(pruned) == 2
    assert len(temp_backup.list_snapshots("proj-retain")) == 2
    assert s1.snapshot_id in pruned or s2.snapshot_id in pruned


# ---------------------------------------------------------------------------
# 11. CLI Headless Operação com --json
# ---------------------------------------------------------------------------

def test_cli_headless_release_commands(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Release CLI commands execute headlessly with --json output."""
    from core.orchestrator.release_cli import main

    # 1. Build
    ret_build = main([
        "build",
        "--project", "cli-test-proj",
        "--git-sha", "f" * 40,
        "--json",
    ])
    assert ret_build == 0
    out_build = capsys.readouterr().out
    data_build = json.loads(out_build)
    art_digest = data_build["artifact_digest"]
    assert len(art_digest) == 64

    # 2. Deploy Staging
    ret_stg = main(["deploy-staging", "--artifact-id", art_digest, "--json"])
    assert ret_stg == 0
    out_stg = capsys.readouterr().out
    data_stg = json.loads(out_stg)
    assert data_stg["stage"] == "staging_deployed"

    # 3. Deploy Production (Commercial paid requires acceptance)
    ret_prod_blocked = main([
        "deploy-production",
        "--artifact-id", art_digest,
        "--tier", "commercial_paid",
        "--json",
    ])
    assert ret_prod_blocked == 2  # Blocked by acceptance
    out_blocked = capsys.readouterr().out
    data_blocked = json.loads(out_blocked)
    assert data_blocked["status"] == "blocked_by_acceptance"

    # 4. Accept
    ret_acc = main([
        "accept",
        "--project", "cli-test-proj",
        "--artifact-id", art_digest,
        "--client", "cli-client-1",
        "--approved-by", "Bob Manager",
        "--json",
    ])
    assert ret_acc == 0
    out_acc = capsys.readouterr().out
    data_acc = json.loads(out_acc)
    assert "receipt_id" in data_acc

    # 5. Deploy Production (Now succeeds!)
    ret_prod_ok = main([
        "deploy-production",
        "--artifact-id", art_digest,
        "--tier", "commercial_paid",
        "--json",
    ])
    assert ret_prod_ok == 0
    out_prod = capsys.readouterr().out
    data_prod = json.loads(out_prod)
    assert data_prod["stage"] == "delivered"
