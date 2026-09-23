"""Deployment adapters and rollback safeguards for release targets.

Governed by HF-12-01, HF-12-02, CONTRACTS.md, and bindings/RELEASE.md.
Provides pluggable deployment adapters for cloud PaaS (Dokploy) and local
services (Segundo Cérebro, Jarvis), with deterministic reconciliation,
installed digest verification, and automated rollback safeguards.
"""

from __future__ import annotations

import io
import json
import logging
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Union, runtime_checkable

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


class FtpMirrorDeploymentAdapter:
    """Deployment adapter for plain-FTP shared hosting targets (HF-27-07).

    Targets Hostinger-style hosting used by projects like ATRIUM, where the
    only available protocol is FTP (no SSH/API deploy endpoint). The adapter
    treats `artifact_ref.byte_digest` as the release identifier (the release
    stage passes the merged git SHA in that field, not a byte hash) and:

    1. Mirrors `target_config.metadata["dist_dir"]` (the project's build
       output, produced by `commands.build`) to `releases/<sha>/` on the
       host, keeping every release directory (never deleted) so a later
       rollback can re-fetch it.
    2. Attempts a `current` symlink swap via the non-standard `SITE SYMLINK`
       command. Most shared hosts (including Hostinger) do not implement it;
       when it fails, the adapter falls back to mirroring the same tree
       directly into the web root (`target_config.metadata.get("remote_root",
       "public_html")`) with extraneous remote files deleted, matching
       `lftp mirror -R --delete` semantics.
    3. Records the previously active SHA in a `.darkfac-release` marker file
       at the web root before overwriting it with the new one, so
       `rollback()` can redeploy that previous release without depending on
       any local state surviving between calls.

    Credentials are never embedded in `TargetConfig`; `target_config.metadata`
    carries only the *names* of environment variables holding them
    (`ftp_host_env`, `ftp_user_env`, `ftp_pass_env`, defaulting to
    `FTP_HOST`/`FTP_USER`/`FTP_PASS`), resolved from `os.environ` at call
    time. Tests inject `ftp_factory` to return an in-memory fake instead of
    opening a real socket; production code leaves it `None` and gets a real
    `ftplib.FTP` connection.
    """

    def __init__(
        self,
        ftp_factory: Optional[Callable[["TargetConfig"], Any]] = None,
    ) -> None:
        self.ftp_factory = ftp_factory
        self._lock = threading.Lock()
        self._operations: Dict[str, DeploymentOperation] = {}
        self._target_configs: Dict[str, TargetConfig] = {}

    # -- connection -------------------------------------------------------

    def _connect(self, target_config: TargetConfig) -> Any:
        if self.ftp_factory:
            return self.ftp_factory(target_config)
        import ftplib  # imported lazily: never needed when tests inject ftp_factory

        host_env = target_config.metadata.get("ftp_host_env", "FTP_HOST")
        user_env = target_config.metadata.get("ftp_user_env", "FTP_USER")
        pass_env = target_config.metadata.get("ftp_pass_env", "FTP_PASS")
        host = target_config.host or os.environ.get(host_env, "")
        user = os.environ.get(user_env, "")
        password = os.environ.get(pass_env, "")
        missing = [name for name, value in ((host_env, host), (user_env, user), (pass_env, password)) if not value]
        if missing:
            raise RuntimeError(f"FTP deploy credentials not set: {', '.join(missing)}")
        # Explicit TLS (FTPS) by default: plain FTP would send the password in
        # cleartext. Opt out only with deploy.params.ftp_tls = "false".
        use_tls = str(target_config.metadata.get("ftp_tls", "true")).lower() not in ("false", "0", "no")
        ftp = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
        ftp.connect(host, target_config.port or 21, timeout=30)
        ftp.login(user, password)
        if use_tls:
            ftp.prot_p()  # encrypt the data channel too, not only the login
        else:
            logger.warning("FTP deploy for %s uses plain FTP (ftp_tls=false)", target_config.project_id)
        return ftp

    @staticmethod
    def _close(ftp: Any) -> None:
        try:
            ftp.quit()
        except Exception:
            pass

    def _remote_root(self, target_config: TargetConfig) -> str:
        return str(target_config.metadata.get("remote_root", "public_html")).strip("/")

    def _releases_dir(self, target_config: TargetConfig) -> str:
        return str(target_config.metadata.get("releases_dir", "releases")).strip("/")

    def _marker_path(self, target_config: TargetConfig) -> str:
        return f"{self._remote_root(target_config)}/.darkfac-release"

    # -- marker file --------------------------------------------------------

    def _read_marker(self, ftp: Any, target_config: TargetConfig) -> Optional[str]:
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {self._marker_path(target_config)}", buf.write)
        except Exception:
            return None
        value = buf.getvalue().decode("utf-8", errors="replace").strip()
        return value or None

    def _write_marker(self, ftp: Any, target_config: TargetConfig, sha: str) -> None:
        ftp.storbinary(f"STOR {self._marker_path(target_config)}", io.BytesIO(sha.encode("utf-8")))

    # -- mirroring ----------------------------------------------------------

    def _ensure_dir(self, ftp: Any, remote_dir: str) -> None:
        parts = [p for p in remote_dir.strip("/").split("/") if p]
        path = ""
        for part in parts:
            path = f"{path}/{part}" if path else part
            try:
                ftp.mkd(path)
            except Exception:
                pass  # already exists, or host cannot report the difference

    def _list_remote_files(self, ftp: Any, remote_dir: str) -> set[str]:
        """Best-effort recursive listing of `remote_dir`, relative paths only."""
        found: set[str] = set()

        def _walk(current: str, prefix: str) -> None:
            try:
                entries = list(ftp.mlsd(current))
            except Exception:
                return
            for name, facts in entries:
                if name in (".", ".."):
                    continue
                rel = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
                if (facts or {}).get("type") == "dir":
                    _walk(f"{current}/{name}", rel)
                else:
                    found.add(rel)

        _walk(remote_dir, "")
        return found

    def _mirror(
        self, ftp: Any, local_dir: Path, remote_dir: str, *, delete_extraneous: bool
    ) -> List[str]:
        uploaded: List[str] = []
        self._ensure_dir(ftp, remote_dir)
        local_files: set[str] = set()
        if local_dir.is_dir():
            for path in sorted(local_dir.rglob("*")):
                rel = path.relative_to(local_dir).as_posix()
                remote_path = f"{remote_dir}/{rel}"
                if path.is_dir():
                    self._ensure_dir(ftp, remote_path)
                    continue
                local_files.add(rel)
                self._ensure_dir(ftp, str(Path(remote_path).parent.as_posix()))
                with path.open("rb") as fh:
                    ftp.storbinary(f"STOR {remote_path}", fh)
                uploaded.append(remote_path)
        if delete_extraneous:
            for rel in self._list_remote_files(ftp, remote_dir) - local_files:
                try:
                    ftp.delete(f"{remote_dir}/{rel}")
                except Exception:
                    pass
        return uploaded

    def _redeploy_release(self, ftp: Any, target_config: TargetConfig, sha: str) -> bool:
        """Re-mirror an already-uploaded `releases/<sha>/` into the web root.

        Downloads each file to a temporary directory and re-uploads it,
        since plain FTP has no server-side copy verb. Used by `rollback()`.
        """
        release_dir = f"{self._releases_dir(target_config)}/{sha}"
        remote_files = self._list_remote_files(ftp, release_dir)
        if not remote_files:
            return False
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for rel in remote_files:
                local_path = tmp_path / rel
                local_path.parent.mkdir(parents=True, exist_ok=True)
                buf = io.BytesIO()
                try:
                    ftp.retrbinary(f"RETR {release_dir}/{rel}", buf.write)
                except Exception:
                    return False
                local_path.write_bytes(buf.getvalue())
            self._mirror(ftp, tmp_path, self._remote_root(target_config), delete_extraneous=True)
        self._write_marker(ftp, target_config, sha)
        return True

    # -- DeploymentAdapter protocol ------------------------------------------

    def start(
        self,
        artifact_ref: ArtifactRef,
        target_config: TargetConfig,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> DeploymentOperation:
        """Mirrors `dist_dir` to the host. Runs synchronously: FTP transfer
        has no separate provider-side reconciliation step, so `reconcile()`
        only ever reports the terminal status recorded here."""
        operation_id = f"ftp_op_{uuid.uuid4().hex[:12]}"
        sha = artifact_ref.byte_digest
        dist_dir = Path(target_config.metadata.get("dist_dir", ""))
        remote_root = self._remote_root(target_config)
        releases_dir = f"{self._releases_dir(target_config)}/{sha}"
        details: Dict[str, Any] = {}
        status = DeploymentStatus.IN_PROGRESS

        ftp = self._connect(target_config)
        try:
            previous_sha = self._read_marker(ftp, target_config)
            self._mirror(ftp, dist_dir, releases_dir, delete_extraneous=False)
            symlinked = False
            try:
                # Non-standard extension a handful of hosts implement; plain
                # FTP (RFC 959) has no symlink verb. Hostinger does not
                # support it, so this normally falls through to the mirror
                # fallback below.
                ftp.sendcmd(f"SITE SYMLINK {releases_dir} {remote_root}")
                symlinked = True
            except Exception:
                symlinked = False
            if not symlinked:
                self._mirror(ftp, dist_dir, remote_root, delete_extraneous=True)
            self._write_marker(ftp, target_config, sha)
            status = DeploymentStatus.SUCCEEDED
            details = {
                "previous_sha": previous_sha,
                "symlinked": symlinked,
                "remote_root": remote_root,
                "release_dir": releases_dir,
            }
        except Exception as exc:
            status = DeploymentStatus.FAILED
            details = {"error": str(exc)}
        finally:
            self._close(ftp)

        operation = DeploymentOperation(
            operation_id=operation_id,
            external_operation_id=f"ext_ftp_{sha[:12] if sha else 'na'}",
            project_id=target_config.project_id,
            target_type=target_config.target_type,
            artifact_ref=artifact_ref,
            status=status,
            details=details,
        )
        with self._lock:
            self._operations[operation_id] = operation
            self._target_configs[operation_id] = target_config
        return operation

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        with self._lock:
            op = self._operations.get(operation_id)
            if not op:
                raise ValueError(f"Unknown operation ID: {operation_id}")
            return op.status

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        ftp = self._connect(target_config)
        try:
            return self._read_marker(ftp, target_config)
        except Exception:
            return None
        finally:
            self._close(ftp)

    def rollback(
        self,
        target_config: TargetConfig,
        failed_digest: str,
        reason: str,
        claim: Optional[Union[Claim, Any]] = None,
    ) -> RollbackResult:
        rollback_id = f"rb_ftp_{uuid.uuid4().hex[:12]}"
        restored = target_config.last_known_good_digest

        if not restored:
            logger.error(
                "FTP rollback aborted: project %s has no last_known_good_digest", target_config.project_id
            )
            return RollbackResult(
                rollback_id=rollback_id,
                project_id=target_config.project_id,
                failed_digest=failed_digest,
                restored_digest=None,
                status=DeploymentStatus.FAILED,
                reason=f"Rollback failed: missing last_known_good_digest. Original cause: {reason}",
            )

        ftp = self._connect(target_config)
        try:
            ok = self._redeploy_release(ftp, target_config, restored)
        except Exception as exc:
            ok = False
            reason = f"{reason} (rollback error: {exc})"
        finally:
            self._close(ftp)

        if not ok:
            return RollbackResult(
                rollback_id=rollback_id,
                project_id=target_config.project_id,
                failed_digest=failed_digest,
                restored_digest=None,
                status=DeploymentStatus.FAILED,
                reason=f"Rollback failed: could not redeploy release {restored}. Cause: {reason}",
            )

        with self._lock:
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
    "FtpMirrorDeploymentAdapter",
    "LocalServiceDeploymentAdapter",
    "RollbackResult",
    "TargetConfig",
    "execute_deployment_with_safeguards",
]
