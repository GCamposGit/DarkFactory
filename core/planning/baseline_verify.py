"""Deterministic integrity and semantic replay verifier for HF-01 baseline snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Sequence

from .baseline_models import (
    AssessmentDimension,
    BaselineIssue,
    BaselineManifest,
    BaselineSnapshot,
    CapabilityAssessment,
    CompletenessStatus,
    EvidenceClaim,
    HF02Readiness,
    IssueSeverity,
    PlannedItem,
    SourceObservation,
    SourceStatus,
    VerificationReport,
)
from .baseline_reconcile import dependency_graph, reconcile_baseline, source_fingerprint
from .baseline_sources import collect_sources, load_catalog


def load_claims(path: Path) -> list[EvidenceClaim]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"CLAIMS_UNREADABLE: {exc}") from exc
    raw_claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(raw_claims, list):
        raise ValueError("CLAIMS_SCHEMA_CHANGED: 'claims' list missing")
    return [EvidenceClaim.model_validate(claim) for claim in raw_claims]


def verify_snapshot(
    snapshot: BaselineSnapshot,
    *,
    root: Path | None = None,
    manifest: BaselineManifest | None = None,
) -> VerificationReport:
    """Verify structural integrity, evidence references, and recompute baseline to detect tampering."""
    errors: list[str] = []
    exit_code = 0

    def add_error(code: int, msg: str) -> None:
        nonlocal exit_code
        errors.append(msg)
        if exit_code < code:
            exit_code = code

    # Phase 1: Structural and relational integrity
    calc_fingerprint = source_fingerprint(snapshot.source_observations)
    if snapshot.source_fingerprint != calc_fingerprint:
        add_error(3, f"SNAPSHOT_FINGERPRINT_MISMATCH: Snapshot source fingerprint '{snapshot.source_fingerprint}' does not match calculated '{calc_fingerprint}'")

    source_ids = [obs.source_id for obs in snapshot.source_observations]
    if len(source_ids) != len(set(source_ids)):
        add_error(3, "SNAPSHOT_DUPLICATE_SOURCE: Duplicate source IDs found in source_observations")

    item_ids = [item.item_id for item in snapshot.items]
    if len(item_ids) != len(set(item_ids)):
        add_error(3, "SNAPSHOT_DUPLICATE_ITEM: Duplicate item IDs found in items")

    claim_ids = [claim.claim_id for claim in snapshot.claims]
    if len(claim_ids) != len(set(claim_ids)):
        add_error(3, "SNAPSHOT_DUPLICATE_CLAIM: Duplicate claim IDs found in claims")

    # Check evidence references in items
    known_evidence_ids = set(claim_ids) | {p.probe_id for p in snapshot.probe_observations}
    for item in snapshot.items:
        for eid in item.evidence_ids:
            if eid not in known_evidence_ids:
                add_error(3, f"EVIDENCE_REFERENCE_INVALID: Item '{item.item_id}' references unknown evidence ID '{eid}'")

    # Check issue references in items
    known_issue_ids = {i.issue_id for i in snapshot.issues}
    for item in snapshot.items:
        for iid in item.issue_ids:
            if iid not in known_issue_ids:
                add_error(3, f"ISSUE_REFERENCE_INVALID: Item '{item.item_id}' references unknown issue ID '{iid}'")

    # Check dependency graph & cycles
    graph = dependency_graph([PlannedItem(item_id=it.item_id, title=it.title, declared_status=it.declared_status, dependencies=it.dependencies, source_id="check", locator="check") for it in snapshot.items])
    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_nodes: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            cycle_nodes.add(node)
            return
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, ()):
            if dep in graph:
                visit(dep)
                if dep in cycle_nodes:
                    cycle_nodes.add(node)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)

    if cycle_nodes:
        if "dependency_cycle" not in snapshot.blocker_codes:
            add_error(3, f"DEPENDENCY_CYCLE_UNRESOLVED: Dependency cycle detected in items {sorted(cycle_nodes)} but 'dependency_cycle' is missing from blocker_codes")
        if not any(i.code == "dependency_cycle" for i in snapshot.issues):
            add_error(3, f"DEPENDENCY_CYCLE_UNRESOLVED: Dependency cycle detected in items {sorted(cycle_nodes)} but issue 'dependency_cycle' is missing from snapshot.issues")

    # Phase 2: Root and Manifest presence
    if root is None or manifest is None:
        if errors:
            return VerificationReport(
                valid=False,
                snapshot_id=snapshot.snapshot_id,
                errors=errors,
                replayed=False,
                exit_code=exit_code,
                source_count=len(snapshot.source_observations),
                item_count=len(snapshot.items),
            )
        return VerificationReport(
            valid=False,
            snapshot_id=snapshot.snapshot_id,
            errors=["REPLAY_DATA_REQUIRED: Full baseline verification requires --root and a valid BaselineManifest"],
            replayed=False,
            exit_code=2,
            source_count=len(snapshot.source_observations),
            item_count=len(snapshot.items),
        )

    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        add_error(2, f"ROOT_MISSING: Root directory '{root}' does not exist")
        return VerificationReport(
            valid=False,
            snapshot_id=snapshot.snapshot_id,
            errors=errors,
            replayed=False,
            exit_code=exit_code,
            source_count=len(snapshot.source_observations),
            item_count=len(snapshot.items),
        )

    # Check manifest against snapshot
    if manifest.schema_version != "1" or manifest.policy_version != "1":
        add_error(3, f"MANIFEST_POLICY_VERSION_UNSUPPORTED: Unsupported manifest policy version '{manifest.policy_version}'")
    if snapshot.schema_version != "1":
        add_error(3, f"SNAPSHOT_SCHEMA_VERSION_UNSUPPORTED: Unsupported snapshot schema version '{snapshot.schema_version}'")
    if manifest.snapshot_id != snapshot.snapshot_id:
        add_error(3, f"MANIFEST_SNAPSHOT_MISMATCH: Manifest snapshot_id '{manifest.snapshot_id}' does not match snapshot '{snapshot.snapshot_id}'")
    if manifest.base_sha.lower() != snapshot.base_sha.lower():
        add_error(3, f"MANIFEST_BASE_SHA_MISMATCH: Manifest base_sha '{manifest.base_sha}' does not match snapshot '{snapshot.base_sha}'")
    if manifest.source_fingerprint != snapshot.source_fingerprint:
        add_error(3, f"MANIFEST_FINGERPRINT_MISMATCH: Manifest fingerprint '{manifest.source_fingerprint}' does not match snapshot '{snapshot.source_fingerprint}'")

    snap_source_paths = [obs.relative_path for obs in snapshot.source_observations]
    if manifest.source_relative_paths != snap_source_paths:
        add_error(3, "MANIFEST_SOURCE_PATHS_MISMATCH: Manifest source_relative_paths do not match snapshot source observations")

    # Check Catalog on disk
    cat_path = (resolved_root / manifest.catalog_relative_path).resolve()
    if not cat_path.is_relative_to(resolved_root) or not cat_path.is_file():
        add_error(2, f"CATALOG_MISSING: Catalog file '{manifest.catalog_relative_path}' not found in root")
    else:
        actual_cat_hash = hashlib.sha256(cat_path.read_bytes()).hexdigest()
        if actual_cat_hash != manifest.catalog_sha256:
            add_error(3, f"CATALOG_HASH_MISMATCH: Catalog file hash '{actual_cat_hash}' does not match manifest hash '{manifest.catalog_sha256}'")

    # Check Claims on disk
    claims_path = (resolved_root / manifest.claims_relative_path).resolve()
    if not claims_path.is_relative_to(resolved_root) or not claims_path.is_file():
        add_error(2, f"CLAIMS_MISSING: Claims file '{manifest.claims_relative_path}' not found in root")
    else:
        actual_claims_hash = hashlib.sha256(claims_path.read_bytes()).hexdigest()
        if actual_claims_hash != manifest.claims_sha256:
            add_error(3, f"CLAIMS_HASH_MISMATCH: Claims file hash '{actual_claims_hash}' does not match manifest hash '{manifest.claims_sha256}'")

    # Check optional Probe Config on disk
    if manifest.probe_config_relative_path:
        probe_cfg_path = (resolved_root / manifest.probe_config_relative_path).resolve()
        if not probe_cfg_path.is_relative_to(resolved_root) or not probe_cfg_path.is_file():
            add_error(2, f"PROBE_CONFIG_MISSING: Probe config file '{manifest.probe_config_relative_path}' not found in root")
        else:
            actual_probe_hash = hashlib.sha256(probe_cfg_path.read_bytes()).hexdigest()
            if actual_probe_hash != manifest.probe_config_sha256:
                add_error(3, f"PROBE_CONFIG_HASH_MISMATCH: Probe config hash does not match manifest")

    # Check Source files on disk
    for obs in snapshot.source_observations:
        if obs.status == SourceStatus.READ:
            source_path = (resolved_root / obs.relative_path).resolve()
            if not source_path.is_relative_to(resolved_root) or not source_path.is_file():
                add_error(2, f"SOURCE_NOT_REPLAYABLE: Source file '{obs.relative_path.as_posix()}' not found in root")
            else:
                actual_source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                if actual_source_hash != obs.sha256:
                    add_error(3, f"SOURCE_HASH_CHANGED: Source file '{obs.relative_path.as_posix()}' hash changed from '{obs.sha256}' to '{actual_source_hash}'")

    catalog_specs = None
    if cat_path.is_file():
        try:
            catalog_specs = load_catalog(cat_path)
            obs_by_id = {obs.source_id: obs for obs in snapshot.source_observations}
            for spec in catalog_specs:
                if spec.required:
                    obs = obs_by_id.get(spec.source_id)
                    if obs is None or obs.status != SourceStatus.READ:
                        status_str = obs.status.value if obs else "absent"
                        add_error(2, f"REQUIRED_SOURCE_MISSING: Required source '{spec.source_id}' has status '{status_str}'")
        except Exception:
            pass

    has_missing_inputs = any(
        err.startswith(("CATALOG_MISSING", "CLAIMS_MISSING", "PROBE_CONFIG_MISSING", "SOURCE_NOT_REPLAYABLE", "REQUIRED_SOURCE_MISSING"))
        for err in errors
    )
    if has_missing_inputs:
        return VerificationReport(
            valid=False,
            snapshot_id=snapshot.snapshot_id,
            errors=errors,
            replayed=False,
            exit_code=exit_code,
            source_count=len(snapshot.source_observations),
            item_count=len(snapshot.items),
        )

    # Phase 3: Semantic Replay (recomputes baseline without network calls)
    if cat_path.is_file() and claims_path.is_file():
        try:
            catalog = catalog_specs if catalog_specs is not None else load_catalog(cat_path)
            collected = collect_sources(resolved_root, catalog)
            claims = load_claims(claims_path)

            snap_claims_dump = [c.model_dump(mode="json") for c in snapshot.claims]
            disk_claims_dump = [c.model_dump(mode="json") for c in claims]
            if snap_claims_dump != disk_claims_dump:
                add_error(3, f"CLAIMS_MISMATCH: Snapshot claims do not match claims from '{manifest.claims_relative_path.as_posix()}'")

            recomputed = reconcile_baseline(
                collected,
                claims,
                base_sha=snapshot.base_sha,
                snapshot_id=snapshot.snapshot_id,
                observed_at=snapshot.observed_at,
                probe_observations=snapshot.probe_observations,
            )

            if snapshot.source_fingerprint != recomputed.source_fingerprint:
                add_error(3, f"SNAPSHOT_FINGERPRINT_MISMATCH: Recomputed fingerprint '{recomputed.source_fingerprint}' != snapshot '{snapshot.source_fingerprint}'")

            if snapshot.completeness != recomputed.completeness:
                add_error(3, f"COMPLETENESS_MISMATCH: Snapshot completeness '{snapshot.completeness.value}' != recomputed '{recomputed.completeness.value}'")

            if snapshot.hf02_readiness != recomputed.hf02_readiness:
                add_error(3, f"READINESS_MISMATCH: Snapshot hf02_readiness '{snapshot.hf02_readiness.value}' != recomputed '{recomputed.hf02_readiness.value}'")

            if sorted(snapshot.blocker_codes) != sorted(recomputed.blocker_codes):
                add_error(3, f"BLOCKER_MISMATCH: Snapshot blocker_codes {snapshot.blocker_codes} != recomputed {recomputed.blocker_codes}")

            snap_items = {it.item_id: it for it in snapshot.items}
            recomp_items = {it.item_id: it for it in recomputed.items}
            if set(snap_items) != set(recomp_items):
                add_error(3, "ITEM_SET_MISMATCH: Snapshot items do not match recomputed items")

            for item_id in sorted(snap_items):
                if item_id in recomp_items:
                    s_it = snap_items[item_id]
                    r_it = recomp_items[item_id]
                    if s_it.implementation != r_it.implementation:
                        add_error(3, f"ITEM_DIMENSION_MISMATCH: Item '{item_id}' implementation is '{s_it.implementation.value}', expected '{r_it.implementation.value}'")
                    if s_it.integration != r_it.integration:
                        add_error(3, f"ITEM_DIMENSION_MISMATCH: Item '{item_id}' integration is '{s_it.integration.value}', expected '{r_it.integration.value}'")
                    if s_it.operation != r_it.operation:
                        add_error(3, f"ITEM_DIMENSION_MISMATCH: Item '{item_id}' operation is '{s_it.operation.value}', expected '{r_it.operation.value}'")
                    if s_it.declared_status != r_it.declared_status:
                        add_error(3, f"ITEM_STATUS_MISMATCH: Item '{item_id}' declared_status is '{s_it.declared_status}', expected '{r_it.declared_status}'")
                    if sorted(s_it.dependencies) != sorted(r_it.dependencies):
                        add_error(3, f"ITEM_DEPENDENCY_MISMATCH: Item '{item_id}' dependencies {s_it.dependencies} != expected {r_it.dependencies}")
                    if sorted(s_it.evidence_ids) != sorted(r_it.evidence_ids):
                        add_error(3, f"ITEM_EVIDENCE_MISMATCH: Item '{item_id}' evidence_ids {s_it.evidence_ids} != expected {r_it.evidence_ids}")
                    if sorted(s_it.issue_ids) != sorted(r_it.issue_ids):
                        add_error(3, f"ITEM_ISSUE_MISMATCH: Item '{item_id}' issue_ids {s_it.issue_ids} != expected {r_it.issue_ids}")

            snap_issues = {iss.issue_id: iss for iss in snapshot.issues}
            recomp_issues = {iss.issue_id: iss for iss in recomputed.issues}
            if set(snap_issues) != set(recomp_issues):
                add_error(3, f"ISSUES_MISMATCH: Snapshot issue IDs {sorted(snap_issues)} != recomputed {sorted(recomp_issues)}")

            for iid in sorted(snap_issues):
                if iid in recomp_issues:
                    s_iss = snap_issues[iid]
                    r_iss = recomp_issues[iid]
                    if (
                        s_iss.code != r_iss.code
                        or s_iss.severity != r_iss.severity
                        or s_iss.target_package != r_iss.target_package
                        or sorted(s_iss.item_ids) != sorted(r_iss.item_ids)
                        or sorted(s_iss.source_ids) != sorted(r_iss.source_ids)
                    ):
                        add_error(3, f"ISSUE_DETAIL_MISMATCH: Issue '{iid}' details differ from recomputed issue")

        except Exception as exc:
            add_error(2, f"RECOMPUTATION_FAILED: {exc}")

    is_valid = len(errors) == 0
    return VerificationReport(
        valid=is_valid,
        snapshot_id=snapshot.snapshot_id,
        errors=errors,
        replayed=True,
        exit_code=exit_code if not is_valid else 0,
        source_count=len(snapshot.source_observations),
        item_count=len(snapshot.items),
    )


__all__ = ["load_claims", "verify_snapshot"]
