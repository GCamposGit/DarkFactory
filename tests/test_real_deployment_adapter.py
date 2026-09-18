"""Automated test suite for HF-12-02: Real Build and Deployment Adapters.

Governed by HF-12-01, HF-12-02, CONTRACTS.md, and bindings/RELEASE.md.
Validates:
1. Cryptographic byte hashing and deterministic artifact reference emission.
2. Fail-closed detection of metadata/byte digest mismatch (BuildHashMismatchError).
3. Idempotent artifact promotion without redundant rebuilding.
4. Dokploy deployment lifecycle: start, polling reconcile, and installed digest query.
5. Automated rollback safeguard to last_known_good_digest on post-deploy failure.
6. LocalService deployment adapter healthcheck verification.
7. Hub webhook DokployDeployClient integration with DeploymentAdapter.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict
import pytest

from core.orchestrator.build_artifacts import (
    ArtifactRef,
    BuildHashMismatchError,
    BuildRecipe,
    BuildService,
    compute_bytes_digest,
)
from core.orchestrator.deployment_adapter import (
    DeploymentOperation,
    DeploymentStatus,
    DokployDeploymentAdapter,
    LocalServiceDeploymentAdapter,
    RollbackResult,
    TargetConfig,
)
from hub.backend.webhooks import DokployDeployClient


# ---------------------------------------------------------------------------
# 1. Byte Hashing and Cryptographic Integrity
# ---------------------------------------------------------------------------

def test_build_service_computes_exact_byte_hash(tmp_path: Path) -> None:
    ledger_file = tmp_path / "build_ledger.json"
    service = BuildService(ledger_path=ledger_file)

    payload = b"console.log('Production static web build bytes v1');"
    expected_hash = hashlib.sha256(payload).hexdigest()

    recipe = BuildRecipe(
        project_id="site-ggcampos",
        target="static_web",
        build_cmd="npm run build",
    )

    ref = service.build(
        source_sha="abc1234567890abcdef1234567890abcdef1234",
        recipe=recipe,
        simulated_bytes=payload,
    )

    assert ref.byte_digest == expected_hash
    assert ref.byte_size == len(payload)
    assert ref.verified is True
    assert ref.source_sha == "abc1234567890abcdef1234567890abcdef1234"


def test_build_service_rejects_byte_mismatch_fail_closed(tmp_path: Path) -> None:
    ledger_file = tmp_path / "build_ledger.json"
    service = BuildService(ledger_path=ledger_file)

    payload = b"Actual compiled binary bytes"
    fake_claimed_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    recipe = BuildRecipe(
        project_id="darkfac",
        target="dokploy_docker_compose",
        expected_byte_digest=fake_claimed_digest,
    )

    with pytest.raises(BuildHashMismatchError) as excinfo:
        service.build(
            source_sha="commit_sha_123",
            recipe=recipe,
            simulated_bytes=payload,
        )

    assert "hash mismatch" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 2. Idempotent Artifact Promotion Without Rebuild
# ---------------------------------------------------------------------------

def test_build_service_promotes_existing_artifact_without_rebuild(tmp_path: Path) -> None:
    ledger_file = tmp_path / "build_ledger.json"
    service = BuildService(ledger_path=ledger_file)

    payload = b"Immutable bundle v1.0.0"
    recipe = BuildRecipe(
        project_id="darkfac",
        target="dokploy_docker_compose",
    )

    # First build
    ref1 = service.build(
        source_sha="commit_sha_promo",
        recipe=recipe,
        simulated_bytes=payload,
    )

    # Second build with same source_sha & recipe: promotes directly
    ref2 = service.build(
        source_sha="commit_sha_promo",
        recipe=recipe,
        simulated_bytes=b"completely different bytes that should NOT be used because artifact already exists",
    )

    assert ref2.artifact_id == ref1.artifact_id
    assert ref2.byte_digest == ref1.byte_digest


# ---------------------------------------------------------------------------
# 3. Dokploy Deployment Adapter Lifecycle
# ---------------------------------------------------------------------------

def test_dokploy_adapter_lifecycle_start_reconcile_installed_digest() -> None:
    transport_calls: list[Dict[str, Any]] = []
    # 1st call: start -> POST /deploy
    # 2nd call: reconcile 1 -> GET /status
    # 3rd call: reconcile 2 -> GET /status
    mock_responses = [
        {"deployment_id": "dokploy_op_1", "status": "IN_PROGRESS"},
        {"status": "IN_PROGRESS", "id": "dokploy_op_1"},
        {"status": "SUCCEEDED", "id": "dokploy_op_1"},
    ]

    def mock_transport(method: str, url: str, data: Any, headers: Any) -> Dict[str, Any]:
        transport_calls.append({"method": method, "url": url})
        return mock_responses.pop(0) if mock_responses else {"status": "SUCCEEDED"}

    adapter = DokployDeploymentAdapter(transport=mock_transport)

    target_config = TargetConfig(
        project_id="darkfac",
        target_type="dokploy_docker_compose",
        api_url="https://dokploy.internal/api",
        api_key="secret-key-123",
        last_known_good_digest="sha256_prev_digest_000",
    )

    artifact = ArtifactRef(
        artifact_id="art_darkfac_001",
        source_sha="git_sha_deploy_01",
        byte_digest="sha256_new_digest_111",
        byte_size=1024,
    )

    # 1. Start deployment
    operation = adapter.start(artifact, target_config)
    assert operation.status == DeploymentStatus.IN_PROGRESS
    assert operation.external_operation_id is not None

    # 2. Reconcile in progress
    status_1 = adapter.reconcile(operation.operation_id)
    assert status_1 == DeploymentStatus.IN_PROGRESS

    # 3. Reconcile succeeded
    status_2 = adapter.reconcile(operation.operation_id)
    assert status_2 == DeploymentStatus.SUCCEEDED

    # 4. Check installed digest reflects newly deployed artifact
    installed = adapter.installed_digest(target_config)
    assert installed == "sha256_new_digest_111"


# ---------------------------------------------------------------------------
# 4. Automated Rollback Safeguards
# ---------------------------------------------------------------------------

def test_dokploy_adapter_automated_rollback() -> None:
    adapter = DokployDeploymentAdapter()

    target_config = TargetConfig(
        project_id="darkfac",
        target_type="dokploy_docker_compose",
        last_known_good_digest="sha256_lkg_stable_999",
    )

    artifact = ArtifactRef(
        artifact_id="art_broken_002",
        source_sha="git_sha_broken",
        byte_digest="sha256_broken_digest_888",
        byte_size=512,
    )

    op = adapter.start(artifact, target_config)
    assert op.status == DeploymentStatus.IN_PROGRESS

    # Post-deploy synthetic journey fails: execute rollback
    rollback_res = adapter.rollback(
        target_config=target_config,
        failed_digest=artifact.byte_digest,
        reason="Synthetic user journey returned 500 Internal Server Error",
    )

    assert rollback_res.status == DeploymentStatus.ROLLED_BACK
    assert rollback_res.failed_digest == "sha256_broken_digest_888"
    assert rollback_res.restored_digest == "sha256_lkg_stable_999"

    # Current installed digest is reverted to last known good
    assert adapter.installed_digest(target_config) == "sha256_lkg_stable_999"


# ---------------------------------------------------------------------------
# 5. Local Service Deployment Adapter
# ---------------------------------------------------------------------------

def test_local_service_deployment_adapter_lifecycle() -> None:
    adapter = LocalServiceDeploymentAdapter()

    target_config = TargetConfig(
        project_id="segundo-cerebro",
        target_type="local_service",
        service_name="segundo_cerebro_daemon",
        last_known_good_digest="sha256_cerebro_stable",
    )

    artifact = ArtifactRef(
        artifact_id="art_cerebro_001",
        source_sha="cerebro_sha_01",
        byte_digest="sha256_cerebro_new",
        byte_size=2048,
    )

    op = adapter.start(artifact, target_config)
    assert op.status == DeploymentStatus.IN_PROGRESS

    # Reconcile marks local service as succeeded and installed
    st = adapter.reconcile(op.operation_id)
    assert st == DeploymentStatus.SUCCEEDED
    assert adapter.installed_digest(target_config) == "sha256_cerebro_new"


# ---------------------------------------------------------------------------
# 6. Hub Webhooks DokployDeployClient Integration
# ---------------------------------------------------------------------------

def test_dokploy_deploy_client_webhooks_integration() -> None:
    adapter = DokployDeploymentAdapter()
    target_config = TargetConfig(
        project_id="site-ggcampos",
        target_type="static_web",
    )
    artifact = ArtifactRef(
        artifact_id="art_web_001",
        source_sha="web_sha_01",
        byte_digest="sha256_web_123",
        byte_size=100,
    )
    op = adapter.start(artifact, target_config)

    # Attach adapter to DokployDeployClient
    client = DokployDeployClient(
        deploy_url="https://dokploy.internal/webhook/deploy",
        deployment_adapter=adapter,
    )

    status = client.reconcile_deployment(op.operation_id)
    assert status in (DeploymentStatus.IN_PROGRESS, DeploymentStatus.SUCCEEDED)
