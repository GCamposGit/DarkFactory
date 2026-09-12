"""Test suite for HF-03-05: Durable Artifact Storage.

Tests:
- Artifact storage computes correct SHA-256 and byte size.
- References use normalized POSIX paths.
- Integrity verification succeeds for unmodified files and detects alterations.
- Directory traversal attempts are sanitized/prevented.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import pytest
from core.orchestrator.cloud_artifacts import CloudArtifactStore, ArtifactReference


def test_artifact_storage_computes_accurate_sha256_and_posix_path(tmp_path: Path) -> None:
    store = CloudArtifactStore(root_dir=tmp_path)
    sample_content = b"Deterministic report content\nLine 2\n"
    expected_sha = hashlib.sha256(sample_content).hexdigest()

    ref = store.store_artifact(
        workflow_id="wf-2026-001",
        filename="report.md",
        content=sample_content,
        content_type="text/markdown",
    )

    assert isinstance(ref, ArtifactReference)
    assert ref.sha256 == expected_sha
    assert ref.byte_size == len(sample_content)
    assert ref.relative_path == "wf-2026-001/report.md"
    assert "\\" not in ref.relative_path
    assert store.verify_integrity(ref) is True


def test_artifact_detects_content_tampering(tmp_path: Path) -> None:
    store = CloudArtifactStore(root_dir=tmp_path)
    ref = store.store_artifact("wf-2026-002", "data.json", '{"clean": true}')
    assert store.verify_integrity(ref) is True

    # Tamper with file directly
    actual_file = tmp_path / "wf-2026-002" / "data.json"
    actual_file.write_text('{"tampered": true}', encoding="utf-8")

    assert store.verify_integrity(ref) is False
    with pytest.raises(ValueError, match="Artifact integrity violation"):
        store.read_artifact(ref)


def test_directory_traversal_is_sanitized(tmp_path: Path) -> None:
    store = CloudArtifactStore(root_dir=tmp_path)
    ref = store.store_artifact("../../bad_wf", "../evil.txt", "payload")
    # Path should be sanitized to basename only
    assert ref.workflow_id == "../../bad_wf"
    assert ".." not in ref.relative_path
    assert ref.relative_path == "bad_wf/evil.txt"
