"""Durable artifact storage and reference tracking for cloud workflows.

Governed by HF-03-05 / ADR-HF-001.
Stores large workflow artifacts (reports, dumps, logs) on volume storage,
emitting deterministic SHA-256 digests and safe relative artifact references
to avoid database bloat and ensure verifiable provenance.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union
from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACTS_ROOT = PROJECT_ROOT / ".factory" / "artifacts"


class ArtifactReference(BaseModel):
    """Immutable metadata describing a stored artifact."""

    model_config = ConfigDict(frozen=True)

    workflow_id: str
    relative_path: str = Field(description="Normalized POSIX relative path")
    sha256: str
    byte_size: int
    content_type: str = "application/octet-stream"


class CloudArtifactStore:
    """Manages volume-backed artifact persistence with integrity guarantees."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self.root_dir = (root_dir or DEFAULT_ARTIFACTS_ROOT).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _safe_target_path(self, workflow_id: str, filename: str) -> Path:
        """Resolve target path ensuring no directory traversal escapes root."""
        # Normalize and sanitize
        clean_wf = Path(workflow_id).name
        clean_file = Path(filename).name
        if not clean_wf or not clean_file:
            raise ValueError("Invalid workflow_id or filename for artifact storage")
        target_dir = self.root_dir / clean_wf
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / clean_file

    def store_artifact(
        self,
        workflow_id: str,
        filename: str,
        content: Union[bytes, str],
        content_type: str = "application/octet-stream",
    ) -> ArtifactReference:
        """Store content on volume storage and compute deterministic SHA-256."""
        target_path = self._safe_target_path(workflow_id, filename)
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(raw_bytes).hexdigest()

        target_path.write_bytes(raw_bytes)

        rel_path = target_path.relative_to(self.root_dir).as_posix()
        return ArtifactReference(
            workflow_id=workflow_id,
            relative_path=rel_path,
            sha256=digest,
            byte_size=len(raw_bytes),
            content_type=content_type,
        )

    def read_artifact(self, reference: ArtifactReference) -> bytes:
        """Read artifact verifying content matches reference SHA-256."""
        target_path = self.root_dir / reference.relative_path
        if not target_path.is_file():
            raise FileNotFoundError(f"Artifact not found: {reference.relative_path}")
        data = target_path.read_bytes()
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != reference.sha256:
            raise ValueError(
                f"Artifact integrity violation for {reference.relative_path}: expected {reference.sha256}, got {actual_digest}"
            )
        return data

    def verify_integrity(self, reference: ArtifactReference) -> bool:
        """Check whether persisted artifact exists and matches its recorded hash."""
        target_path = self.root_dir / reference.relative_path
        if not target_path.is_file():
            return False
        return hashlib.sha256(target_path.read_bytes()).hexdigest() == reference.sha256
