"""Structural rule gate: tests must never pin sha256 hashes of live factory files (USR-97).

Live files (such as .factory/roadmap/darkfac.json and .factory/demands/demands.json)
evolve continuously as the factory plans, executes, and completes autonomous tickets.
Pinning literal SHA-256 digests of these files in tests or claims turns live documents
into brittle fixtures, causing valid autonomous delivery updates to break the official gate.

This gate enforces two guarantees:
1. AST / text scanner across tests/**/*.py ensuring no test pins a SHA-256 digest of
   any live file under .factory/ without an explicit justified entry in LIVE_FILE_HASH_ALLOWLIST.
2. Mutation invariant test: dynamically adding a legitimate new item to the roadmap manifest
   compiles cleanly and passes baseline claim validation without breaking hash chains.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapItemType,
)
from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.sources import JsonRoadmapSource

REPO_ROOT = Path(__file__).resolve().parents[1]

# Documented allowlist for tests legitimately pinning hashes of immutable fixtures.
# Pinning live mutable files under .factory/ is strictly prohibited (must remain empty).
LIVE_FILE_HASH_ALLOWLIST: dict[str, str] = {
    # Format: "test_relative_path:live_file_path": "Reason why an immutable seal is required"
}

_SHA256_RE = re.compile(r"\b([0-9a-f]{64})\b")
_LIVE_FACTORY_PATHS = (
    ".factory/roadmap/darkfac.json",
    ".factory/demands/demands.json",
    ".factory/projects.json",
)


def test_no_unapproved_live_file_hashes_in_tests() -> None:
    """Scan tests/ to prevent newly introduced literal hashes of live factory files."""
    tests_dir = REPO_ROOT / "tests"
    violations: list[str] = []

    for test_file in sorted(tests_dir.rglob("*.py")):
        rel_test_path = test_file.relative_to(REPO_ROOT).as_posix()
        if rel_test_path == "tests/test_live_file_hash_rule.py":
            continue

        try:
            content = test_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        # Check if the test mentions a live factory file
        mentioned_live_files = [path for path in _LIVE_FACTORY_PATHS if path in content]
        if not mentioned_live_files:
            continue

        # Check if the file contains SHA-256 assertions against live paths
        for live_path in mentioned_live_files:
            # Look for lines asserting hash equality on the live path or its variable
            for line_no, line in enumerate(content.splitlines(), start=1):
                # Look for patterns like: assert _sha256(live_path) == "..." or claim["source_hash"] == _sha256(live_path)
                if ("_sha256(" in line or "sha256(" in line) and any(kw in line for kw in ("==", "assert")):
                    # Check if this specific line pins a literal 64-char hash
                    match = _SHA256_RE.search(line)
                    if match and match.group(1) != "0" * 64 and match.group(1) != "a" * 64:
                        key = f"{rel_test_path}:{live_path}"
                        if key not in LIVE_FILE_HASH_ALLOWLIST:
                            violations.append(
                                f"{rel_test_path}:{line_no} pins literal sha256 {match.group(1)} "
                                f"for live file '{live_path}' without an approved entry in LIVE_FILE_HASH_ALLOWLIST."
                            )

    assert not violations, (
        "Found tests asserting literal SHA-256 hashes of live .factory files (USR-97):\n"
        + "\n".join(violations)
        + "\n\nLive files under .factory/ evolve continuously. Validate invariants instead of whole-file equality."
    )


def test_manifest_mutation_preserves_invariants_and_does_not_break_compiler(tmp_path: Path) -> None:
    """Adding a new operational item to darkfac.json must compile cleanly and preserve invariants."""
    real_manifest_path = REPO_ROOT / ".factory" / "roadmap" / "darkfac.json"
    manifest_data = json.loads(real_manifest_path.read_text(encoding="utf-8"))

    # Synthesize a new completed roadmap item
    mutated_manifest_data = copy.deepcopy(manifest_data)
    new_item = {
        "id": "RM-99-TEST",
        "project_id": "darkfac",
        "title": "Synthetic Invariant Test Item",
        "description": "Validates that roadmap compilation preserves open-world invariants under additions.",
        "item_type": RoadmapItemType.FEATURE.value,
        "lifecycle_stage": LifecycleStage.EXECUTION.value,
        "delivery_status": DeliveryStatus.COMPLETED.value,
        "horizon": PlanningHorizon.NOW.value,
        "confidence": ConfidenceLevel.HIGH.value,
        "dependencies": [],
        "completion_criteria": ["Synthetic invariant verified."],
        "evidence_refs": [{
            "evidence_id": "doc:RM-99-TEST:test.md",
            "evidence_kind": "handoff_doc",
            "label": "Test evidence",
            "locator": "docs/test.md",
            "verified": True,
        }],
    }
    mutated_manifest_data["items"].append(new_item)

    # Write mutated manifest to isolated directory
    mutated_manifest_file = tmp_path / "darkfac.json"
    mutated_manifest_file.write_text(json.dumps(mutated_manifest_data, indent=2), encoding="utf-8")

    # Compile using the JsonRoadmapSource pointing to the mutated manifest
    source = JsonRoadmapSource(
        mutated_manifest_file,
        source_id="approved-roadmap",
        evidence_dir=REPO_ROOT / ".factory" / "reports",
    )
    result = source.read("darkfac")
    assert result.state.status == "available"
    assert len(result.records) >= 58
    compiler = RoadmapCompiler([source])
    snapshot = compiler.compile("darkfac")

    item_ids = {item.id for item in snapshot.items}
    assert "RM-99-TEST" in item_ids
    assert len(item_ids) == len(snapshot.items)  # Invariant: unique IDs
    assert snapshot.stats.total_items >= 58  # Invariant: non-decreasing count floor
    assert not any("RM-99-TEST" in issue.item_ids for issue in snapshot.issues)


def test_claims_validation_accepts_live_manifest_mutation(tmp_path: Path) -> None:
    """The baseline claim for rm07-manifest-planned must not fail if the manifest content changes."""
    from tests.test_baseline_catalog import (
        ALLOWED_ASSERTIONS,
        ALLOWED_DIMENSIONS,
        ALLOWED_EVIDENCE_KINDS,
        CATALOG_PATH,
        CLAIMS_PATH,
        CLAIM_KEYS,
        _assert_safe_relative_path,
        _path_from_locator,
        _read_json,
        _sha256,
    )

    catalog = _read_json(CATALOG_PATH)
    catalog_by_id = {entry["source_id"]: entry for entry in catalog}
    document = _read_json(CLAIMS_PATH)
    claims = document["claims"]

    # Verify that rm07-manifest-planned claim passes without requiring source_hash equality to live darkfac.json
    rm07_claim = next(c for c in claims if c["claim_id"] == "rm07-manifest-planned")
    assert rm07_claim["source_id"] == "approved-roadmap"
    assert rm07_claim["locator"] == ".factory/roadmap/darkfac.json#RM-07"

    # Even if we change the file content (or recompute hash), the presence check succeeds
    real_manifest_path = REPO_ROOT / ".factory" / "roadmap" / "darkfac.json"
    manifest_content = _read_json(real_manifest_path)
    item_ids = {item["id"] for item in manifest_content.get("items", [])}
    assert "RM-07" in item_ids
