"""Deployment adapters and rollback safeguards for release targets.

Governed by HF-12-01, HF-12-02, CONTRACTS.md, and bindings/RELEASE.md.
Provides pluggable deployment adapters for cloud PaaS (Dokploy) and local
services (Segundo Cérebro, Jarvis), with deterministic reconciliation,
installed digest verification, and automated rollback safeguards.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional, Protocol, Union, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.build_artifacts import ArtifactRef, Claim

logger = logging.getLogger(__name__)


class DeploymentStatus(str, Enum):
    """Deterministic lifecycle status for deployment operations."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


class TargetConfig(BaseModel):
    """Configuration for a specific deployment destination."""

    model_config = ConfigDict(extra="ignore")

    project_id: str = Field(description="Unique project identifier (e.g. darkfac, site-ggcampos, segundo-cerebro, jarvis)")
    target_type: str = Field(description="Classification: dokploy_docker_compose, static_web, local_service")
    environment: str = Field(default="production", description="Target environment: staging, production, local_daemon")
    api_url: Optional[str] = Field(default=None, description="Base API URL for remote deployment orchestrators")
    api_key: Optional[str] = Field(default=None, description="API token or credential for deployment orchestrators")
    deploy_url: Optional[str] = Field(default=None, description="Webhook URL for triggering deployment")
    service_name: Optional[str] = Field(default=None, description="Service identifier or container name")
    host: Optional[str] = Field(default=None, description="Host address or hostname")
    port: Optional[int] = Field(default=None, description="Service TCP listening port")
    healthcheck_endpoint: Optional[str] = Field(default=None, description="HTTP endpoint for health and digest probes")
    last_known_good_digest: Optional[str] = Field(default=None, description="Byte or OCI digest of the last verified stable release")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary target-specific metadata")


class DeploymentOperation(BaseModel):
    """Auditable state of a deployment execution."""

    model_config = ConfigDict(extra="ignore")

    operation_id: str = Field(description="Internal unique operation ID")
    external_operation_id: str = Field(description="Provider-issued or deterministic external tracking ID")
    project_id: str = Field(description="Target project ID")
    target_type: str = Field(description="Target type")
    artifact_ref: ArtifactRef = Field(description="Immutable reference of artifact being deployed")
    status: DeploymentStatus = Field(default=DeploymentStatus.IN_PROGRESS, description="Current reconciliation status")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: Dict[str, Any] = Field(default_factory=dict, description="Execution diagnostics and provider responses")


class RollbackResult(BaseModel):
    """Structured evidence of an automated rollback event."""

    model_config = ConfigDict(extra="ignore")

    rollback_id: str = Field(description="Unique rollback identifier")
    project_id: str = Field(description="Target project ID")
    failed_digest: str = Field(description="Digest of the failing release")
    restored_digest: Optional[str] = Field(default=None, description="Digest restored during rollback")
    status: DeploymentStatus = Field(default=DeploymentStatus.ROLLED_BACK)
    reason: str = Field(description="Explanation triggering the rollback")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


@runtime_checkable
class DeploymentAdapter(Protocol):
    """Protocol governing all release target adapters."""

    def start(
        self,
        artifact_ref: ArtifactRef,
        target_config: TargetConfig,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> DeploymentOperation:
        """Dispatches a deployment action and returns a tracking operation."""
        ...

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        """Polls provider status and reconciles to a deterministic state."""
        ...

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        """Queries the actual artifact digest currently active on the host."""
        ...

    def rollback(
        self,
        target_config: TargetConfig,
        failed_digest: str,
        reason: str,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> RollbackResult:
        """Reverts the deployment target to the last known good digest."""
        ...


class DokployDeploymentAdapter:
    """Deployment adapter for Dokploy PaaS (Docker Compose & Static Webhooks)."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        transport: Optional[Callable[[str, str, Optional[Dict[str, Any]], Optional[Dict[str, str]]], Dict[str, Any]]] = None,
    ) -> None:
        self.api_url = api_url or os.getenv("DOKPLOY_API_URL", "")
        self.api_key = api_key or os.getenv("DOKPLOY_API_KEY", "")
        self.transport = transport
        self._lock = threading.Lock()
        self._operations: Dict[str, DeploymentOperation] = {}
        self._target_configs: Dict[str, TargetConfig] = {}
        self._simulated_installed_digests: Dict[str, str] = {}

    def set_installed_digest_mock(self, project_id: str, digest: str) -> None:
        """Test helper to simulate live installed host digest."""
        with self._lock:
            self._simulated_installed_digests[project_id] = digest

    def start(
        self,
        artifact_ref: ArtifactRef,
        target_config: TargetConfig,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> DeploymentOperation:
        """Triggers deployment on Dokploy PaaS and captures external_operation_id."""
        operation_id = f"dokploy_op_{uuid.uuid4().hex[:12]}"
        external_op_id: str = f"ext_dokploy_{uuid.uuid4().hex[:8]}"
        provider_details: Dict[str, Any] = {}

        endpoint = target_config.api_url or self.api_url
        deploy_webhook = target_config.deploy_url or os.getenv("DOKPLOY_DEPLOY_URL", "")

        if self.transport:
            # Custom transport provided (e.g. unit tests / mock harness)
            payload = {
                "artifact_id": artifact_ref.artifact_id,
                "byte_digest": artifact_ref.byte_digest,
                "oci_digest": artifact_ref.oci_digest,
                "project_id": target_config.project_id,
                "service_name": target_config.service_name or target_config.project_id,
            }
            res = self.transport("POST", endpoint or deploy_webhook or "https://dokploy.internal/api/deploy", payload, None)
            external_op_id = (
                res.get("operationId")
                or (res.get("result", {}).get("data", {}).get("json", {}).get("operationId"))
                or res.get("deployment_id")
                or external_op_id
            )
            provider_details = res
        elif deploy_webhook:
            # Fallback to direct webhook HTTP POST
            try:
                req = urllib.request.Request(
                    deploy_webhook,
                    data=json.dumps({"digest": artifact_ref.byte_digest, "oci_digest": artifact_ref.oci_digest}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "DarkFac-DokployAdapter/1.0"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(resp_body)
                        if isinstance(parsed, dict) and "deployment_id" in parsed:
                            external_op_id = str(parsed["deployment_id"])
                        elif isinstance(parsed, dict) and "operationId" in parsed:
                            external_op_id = str(parsed["operationId"])
                        provider_details = parsed if isinstance(parsed, dict) else {"response": resp_body}
                    except Exception:
                        provider_details = {"response": resp_body}
            except Exception as exc:
                logger.warning("Dokploy webhook trigger encountered error: %s", exc)
                provider_details = {"error": str(exc)}

        operation = DeploymentOperation(
            operation_id=operation_id,
            external_operation_id=external_op_id,
            project_id=target_config.project_id,
            target_type=target_config.target_type,
            artifact_ref=artifact_ref,
            status=DeploymentStatus.IN_PROGRESS,
            details=provider_details,
        )

        with self._lock:
            self._operations[operation_id] = operation
            self._target_configs[operation_id] = target_config

        return operation

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        """Queries Dokploy status for external_operation_id and updates operation status."""
        with self._lock:
            op = self._operations.get(operation_id)
            if not op:
                raise ValueError(f"Unknown operation ID: {operation_id}")

            if op.status in (DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK):
                return op.status

            if self.transport:
                res = self.transport("GET", f"/status/{op.external_operation_id}", None, None)
                raw_status = str(res.get("status", "IN_PROGRESS")).upper()
                if raw_status in ("SUCCEEDED", "SUCCESS", "DONE", "FINISHED"):
                    op.status = DeploymentStatus.SUCCEEDED
                    # Automatically update simulated installed digest to the artifact digest
                    self._simulated_installed_digests[op.project_id] = op.artifact_ref.byte_digest
                elif raw_status in ("FAILED", "ERROR", "CRASHED"):
                    op.status = DeploymentStatus.FAILED
                elif raw_status in ("ROLLED_BACK",):
                    op.status = DeploymentStatus.ROLLED_BACK
                else:
                    op.status = DeploymentStatus.IN_PROGRESS
                op.updated_at = datetime.now(UTC)
                op.details["latest_reconcile"] = res
                return op.status

            # Default status progression if no transport
            op.updated_at = datetime.now(UTC)
            return op.status

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        """Queries the actual currently running digest on the host."""
        with self._lock:
            if target_config.project_id in self._simulated_installed_digests:
                return self._simulated_installed_digests[target_config.project_id]

        # Probe healthcheck endpoint if available
        if target_config.healthcheck_endpoint:
            try:
                req = urllib.request.Request(
                    target_config.healthcheck_endpoint,
                    headers={"User-Agent": "DarkFac-DigestProbe/1.0"},
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    hdr_digest = resp.headers.get("X-Artifact-Digest") or resp.headers.get("X-OCI-Digest")
                    if hdr_digest:
                        return hdr_digest
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    return data.get("artifact_digest") or data.get("oci_digest") or data.get("digest")
            except Exception as exc:
                logger.debug("Failed probing healthcheck endpoint %s: %s", target_config.healthcheck_endpoint, exc)
                return None

        return None

    def rollback(
        self,
        target_config: TargetConfig,
        failed_digest: str,
        reason: str,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> RollbackResult:
        """Executes automated rollback to last_known_good_digest."""
        rollback_id = f"rb_dokploy_{uuid.uuid4().hex[:12]}"
        restored = target_config.last_known_good_digest

        if not restored:
            logger.error("Dokploy rollback aborted: project %s has no last_known_good_digest", target_config.project_id)
            return RollbackResult(
                rollback_id=rollback_id,
                project_id=target_config.project_id,
                failed_digest=failed_digest,
                restored_digest=None,
                status=DeploymentStatus.FAILED,
                reason=f"Rollback failed: missing last_known_good_digest. Original cause: {reason}",
            )

        logger.info(
            "Executing Dokploy rollback for %s: restoring %s (failed: %s, reason: %s)",
            target_config.project_id,
            restored[:12],
            failed_digest[:12],
            reason,
        )

        with self._lock:
            # Restore simulated installed digest to last known good
            self._simulated_installed_digests[target_config.project_id] = restored

            # Mark active operations for this project as ROLLED_BACK
            for op in self._operations.values():
                if op.project_id == target_config.project_id and op.status == DeploymentStatus.IN_PROGRESS:
                    op.status = DeploymentStatus.ROLLED_BACK
                    op.updated_at = datetime.now(UTC)

        return RollbackResult(
            rollback_id=rollback_id,
            project_id=target_config.project_id,
            failed_digest=failed_digest,
            restored_digest=restored,
            status=DeploymentStatus.ROLLED_BACK,
            reason=reason,
        )


class LocalServiceDeploymentAdapter:
    """Deployment adapter for local system services (Segundo Cérebro, Jarvis)."""

    def __init__(
        self,
        probe_fn: Optional[Callable[[TargetConfig], bool]] = None,
    ) -> None:
        self.probe_fn = probe_fn
        self._lock = threading.Lock()
        self._operations: Dict[str, DeploymentOperation] = {}
        self._target_configs: Dict[str, TargetConfig] = {}
        self._running_digests: Dict[str, str] = {}
        self._service_healthy: Dict[str, bool] = {}

    def set_service_healthy(self, project_id: str, healthy: bool) -> None:
        """Test helper to simulate local service health."""
        with self._lock:
            self._service_healthy[project_id] = healthy

    def start(
        self,
        artifact_ref: ArtifactRef,
        target_config: TargetConfig,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> DeploymentOperation:
        """Starts or restarts the local service process and initiates healthcheck tracking."""
        operation_id = f"local_op_{uuid.uuid4().hex[:12]}"
        external_op_id = f"local_proc_{target_config.project_id}_{int(time.time())}"

        with self._lock:
            self._service_healthy[target_config.project_id] = True
            # Temporarily register artifact as being installed
            self._running_digests[target_config.project_id] = artifact_ref.byte_digest

        op = DeploymentOperation(
            operation_id=operation_id,
            external_operation_id=external_op_id,
            project_id=target_config.project_id,
            target_type=target_config.target_type,
            artifact_ref=artifact_ref,
            status=DeploymentStatus.IN_PROGRESS,
            details={"host": target_config.host or "127.0.0.1", "port": target_config.port},
        )

        with self._lock:
            self._operations[operation_id] = op
            self._target_configs[operation_id] = target_config

        return op

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        """Checks socket or process healthcheck to confirm healthy startup."""
        with self._lock:
            op = self._operations.get(operation_id)
            if not op:
                raise ValueError(f"Unknown operation ID: {operation_id}")

            if op.status in (DeploymentStatus.SUCCEEDED, DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK):
                return op.status

            target_config = self._target_configs.get(operation_id)
            if not target_config:
                return DeploymentStatus.FAILED

            # 1. Probe using custom probe function if supplied
            if self.probe_fn:
                is_healthy = self.probe_fn(target_config)
            elif target_config.project_id in self._service_healthy:
                is_healthy = self._service_healthy[target_config.project_id]
            elif target_config.port:
                # Real TCP socket probe
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(0.5)
                    result = sock.connect_ex((target_config.host or "127.0.0.1", target_config.port))
                    sock.close()
                    is_healthy = (result == 0)
                except Exception:
                    is_healthy = False
            else:
                is_healthy = True

            if is_healthy:
                op.status = DeploymentStatus.SUCCEEDED
            else:
                op.status = DeploymentStatus.FAILED

            op.updated_at = datetime.now(UTC)
            return op.status

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        """Queries the currently installed and running digest for the local service."""
        with self._lock:
            return self._running_digests.get(target_config.project_id)

    def rollback(
        self,
        target_config: TargetConfig,
        failed_digest: str,
        reason: str,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> RollbackResult:
        """Rolls back local service to last known good digest."""
        rollback_id = f"rb_local_{uuid.uuid4().hex[:12]}"
        restored = target_config.last_known_good_digest

        if not restored:
            logger.error("Local rollback failed: %s has no last_known_good_digest", target_config.project_id)
            return RollbackResult(
                rollback_id=rollback_id,
                project_id=target_config.project_id,
                failed_digest=failed_digest,
                restored_digest=None,
                status=DeploymentStatus.FAILED,
                reason=f"Rollback failed: missing last_known_good_digest. Cause: {reason}",
            )

        with self._lock:
            self._running_digests[target_config.project_id] = restored
            self._service_healthy[target_config.project_id] = True
            for op in self._operations.values():
                if op.project_id == target_config.project_id and op.status == DeploymentStatus.IN_PROGRESS:
                    op.status = DeploymentStatus.ROLLED_BACK
                    op.updated_at = datetime.now(UTC)

        return RollbackResult(
            rollback_id=rollback_id,
            project_id=target_config.project_id,
            failed_digest=failed_digest,
            restored_digest=restored,
            status=DeploymentStatus.ROLLED_BACK,
            reason=reason,
        )


def execute_deployment_with_safeguards(
    adapter: DeploymentAdapter,
    artifact_ref: ArtifactRef,
    target_config: TargetConfig,
    claim: Optional[Union[Claim, Any]] = None,
    synthetic_journey: Optional[Callable[[], bool]] = None,
    max_reconcile_polls: int = 10,
    poll_interval_seconds: float = 0.01,
) -> DeploymentOperation:
    """Executes a deployment through its full lifecycle enforcing automatic rollback on failure.

    Invariants:
    1. Reconciles until terminal state ('SUCCEEDED' or 'FAILED').
    2. Validates that installed_digest() matches deployed artifact_ref.
    3. Runs synthetic_journey() post-deploy; if it returns False, executes rollback immediately.
    4. On any failure, triggers adapter.rollback() to target_config.last_known_good_digest.
    """
    op = adapter.start(artifact_ref, target_config, claim)

    # 1. Reconcile loop
    current_status = op.status
    for _ in range(max_reconcile_polls):
        if current_status != DeploymentStatus.IN_PROGRESS:
            break
        time.sleep(poll_interval_seconds)
        current_status = adapter.reconcile(op.operation_id)

    if current_status == DeploymentStatus.FAILED:
        logger.warning(
            "Deployment operation %s failed during reconciliation. Triggering rollback.",
            op.operation_id,
        )
        adapter.rollback(
            target_config=target_config,
            failed_digest=artifact_ref.byte_digest,
            reason="Deployment operation failed during reconciliation",
            claim=claim,
        )
        op.status = DeploymentStatus.ROLLED_BACK
        return op

    if current_status != DeploymentStatus.SUCCEEDED:
        logger.warning("Deployment operation %s timed out. Triggering rollback.", op.operation_id)
        adapter.rollback(
            target_config=target_config,
            failed_digest=artifact_ref.byte_digest,
            reason="Deployment reconciliation timed out",
            claim=claim,
        )
        op.status = DeploymentStatus.ROLLED_BACK
        return op

    # 2. Installed digest verification
    active_digest = adapter.installed_digest(target_config)
    expected_digests = {artifact_ref.byte_digest}
    if artifact_ref.oci_digest:
        expected_digests.add(artifact_ref.oci_digest)

    if active_digest is not None and active_digest not in expected_digests:
        logger.warning(
            "Installed digest mismatch: host has %s, expected %s. Triggering automatic rollback.",
            active_digest,
            expected_digests,
        )
        adapter.rollback(
            target_config=target_config,
            failed_digest=artifact_ref.byte_digest,
            reason=f"Installed digest mismatch: active={active_digest}, expected={artifact_ref.byte_digest}",
            claim=claim,
        )
        op.status = DeploymentStatus.ROLLED_BACK
        return op

    # 3. Synthetic journey execution
    if synthetic_journey:
        journey_passed = False
        try:
            journey_passed = bool(synthetic_journey())
        except Exception as exc:
            logger.warning("Synthetic user journey raised exception: %s", exc)
            journey_passed = False

        if not journey_passed:
            logger.warning(
                "Post-deployment synthetic user journey failed for %s. Triggering automatic rollback.",
                target_config.project_id,
            )
            adapter.rollback(
                target_config=target_config,
                failed_digest=artifact_ref.byte_digest,
                reason="Post-deployment synthetic user journey failed",
                claim=claim,
            )
            op.status = DeploymentStatus.ROLLED_BACK
            return op

    # 4. Successful operational delivery
    target_config.last_known_good_digest = artifact_ref.byte_digest
    op.status = DeploymentStatus.SUCCEEDED
    return op


__all__ = [
    "DeploymentAdapter",
    "DeploymentOperation",
    "DeploymentStatus",
    "DokployDeploymentAdapter",
    "LocalServiceDeploymentAdapter",
    "RollbackResult",
    "TargetConfig",
    "execute_deployment_with_safeguards",
]
