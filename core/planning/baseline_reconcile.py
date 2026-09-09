"""Deterministic, side-effect-free reconciliation for the HF-01 baseline."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence

from .baseline_models import (
    AssessmentDimension,
    BaselineIssue,
    BaselineSnapshot,
    CapabilityAssessment,
    ClaimAssertion,
    ClaimDimension,
    CollectedBaseline,
    CompletenessStatus,
    EvidenceClaim,
    EvidenceKind,
    HF02Readiness,
    IssueSeverity,
    PlannedItem,
    SourceObservation,
    SourceStatus,
    ValidationMode,
    utc_now,
)
from .baseline_probes import ProbeObservation, ProbeStatus


def source_fingerprint(observations: Iterable[SourceObservation]) -> str:
    rows = [{"source_id": item.source_id, "relative_path": item.relative_path.as_posix(), "sha256": item.sha256, "status": item.status.value, "error_code": item.error_code} for item in observations]
    payload = json.dumps(sorted(rows, key=lambda row: row["source_id"]), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dependency_graph(items: Iterable[PlannedItem]) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for item in items:
        grouped[item.item_id].update(item.dependencies)
    return {item_id: tuple(sorted(dependencies)) for item_id, dependencies in sorted(grouped.items())}


def reconcile_baseline(collected: CollectedBaseline, claims: Sequence[EvidenceClaim], *, base_sha: str, snapshot_id: str, observed_at: datetime | None = None, probe_observations: Sequence[ProbeObservation] = ()) -> BaselineSnapshot:
    observations = tuple(collected.observations)
    merged_claims = _dedupe_claims([*collected.claims, *claims])
    items_by_id = _merge_items(collected.planned_items, merged_claims)
    issues = _dedupe_issues(collected.issues)
    issues.extend(_declaration_conflict_issues(collected.planned_items))
    issues.extend(_assessment_issues(merged_claims, base_sha, probe_observations))
    issues.extend(_dependency_issues(items_by_id.values()))
    issues.extend(_probe_issues(probe_observations))
    assessments = [_assessment_for(item, merged_claims, base_sha, probe_observations, issues) for item in sorted(items_by_id.values(), key=lambda value: value.item_id)]
    issues = _dedupe_issues(issues)
    readiness, readiness_issues = _hf02_readiness(observations, assessments)
    issues = _dedupe_issues([*issues, *readiness_issues])
    statuses = [observation.status for observation in observations]
    if any(status in {SourceStatus.INVALID, SourceStatus.ACCESS_DENIED} for status in statuses):
        completeness = CompletenessStatus.INVALID
    elif all(status == SourceStatus.READ for status in statuses):
        completeness = CompletenessStatus.COMPLETE
    else:
        completeness = CompletenessStatus.PARTIAL
    blocker_codes = sorted({issue.code for issue in issues if issue.severity == IssueSeverity.ERROR})
    return BaselineSnapshot(snapshot_id=snapshot_id, observed_at=observed_at or utc_now(), base_sha=base_sha.strip().lower(), source_fingerprint=source_fingerprint(observations), source_observations=list(observations), items=assessments, claims=list(merged_claims), issues=issues, completeness=completeness, hf02_readiness=readiness, blocker_codes=blocker_codes)


def _dedupe_claims(claims: Sequence[EvidenceClaim]) -> list[EvidenceClaim]:
    unique = {claim.claim_id: claim for claim in claims}
    return [unique[key] for key in sorted(unique)]


def _merge_items(items: Sequence[PlannedItem], claims: Sequence[EvidenceClaim]) -> dict[str, PlannedItem]:
    grouped: dict[str, list[PlannedItem]] = defaultdict(list)
    for item in items:
        grouped[item.item_id].append(item)
    for claim in claims:
        if claim.item_id not in grouped:
            grouped[claim.item_id].append(PlannedItem(item_id=claim.item_id, title=claim.item_id, declared_status="unknown", dependencies=[], source_id=claim.source_id, locator=claim.locator))
    result: dict[str, PlannedItem] = {}
    for item_id, declarations in grouped.items():
        statuses = sorted({item.declared_status for item in declarations})
        titles = sorted({item.title for item in declarations})
        dependencies = sorted({dependency for item in declarations for dependency in item.dependencies})
        result[item_id] = declarations[0].model_copy(update={"title": titles[0], "declared_status": statuses[0], "dependencies": dependencies})
    return result


def _issue(code: str, *, item_ids: Sequence[str] = (), source_ids: Sequence[str] = (), severity: IssueSeverity = IssueSeverity.WARNING, required_action: str, target_package: str | None = "HF-01") -> BaselineIssue:
    identity = ":".join([code, *sorted(item_ids), *sorted(source_ids)])
    return BaselineIssue(issue_id=identity, code=code, severity=severity, item_ids=sorted(set(item_ids)), source_ids=sorted(set(source_ids)), required_action=required_action, target_package=target_package)


def _dedupe_issues(issues: Sequence[BaselineIssue]) -> list[BaselineIssue]:
    unique = {issue.issue_id: issue for issue in issues}
    return sorted(unique.values(), key=lambda issue: issue.issue_id)


def _declaration_conflict_issues(items: Sequence[PlannedItem]) -> list[BaselineIssue]:
    statuses: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)
    for item in items:
        statuses[item.item_id].add(item.declared_status.strip().lower())
        sources[item.item_id].add(item.source_id)
    return [_issue("source_conflict", item_ids=[item_id], source_ids=sorted(sources[item_id]), required_action="Review the competing declarations; do not choose a winner silently.") for item_id in sorted(statuses) if len(statuses[item_id]) > 1]


def _assessment_issues(claims: Sequence[EvidenceClaim], base_sha: str, probes: Sequence[ProbeObservation]) -> list[BaselineIssue]:
    issues: list[BaselineIssue] = []
    for claim in claims:
        if claim.assertion == ClaimAssertion.PARTIAL and claim.dimension == ClaimDimension.IMPLEMENTATION:
            issues.append(_issue("partial_ticket_coverage", item_ids=[claim.item_id], source_ids=[claim.source_id], required_action="Complete the missing acceptance surface before declaring the ticket delivered."))
        if claim.dimension == ClaimDimension.INTEGRATION and claim.evidence_kind == EvidenceKind.REMOTE_GIT and claim.candidate_sha and claim.candidate_sha.lower() != base_sha.strip().lower():
            issues.append(_issue("stale_evidence", item_ids=[claim.item_id], source_ids=[claim.source_id], required_action="Collect the remote receipt for the candidate SHA currently under review."))
        if claim.dimension == ClaimDimension.OPERATION and claim.evidence_kind == EvidenceKind.OWNER_STATEMENT:
            issues.append(_issue("access_required", item_ids=[claim.item_id], source_ids=[claim.source_id], required_action="Use the guided access reference and repeat an authorized target probe; do not infer availability."))
        if claim.item_id.lower() == "n8n" and "n8n.io" in claim.summary.lower():
            issues.append(_issue("service_endpoint_missing", item_ids=[claim.item_id], source_ids=[claim.source_id], required_action="Record the real instance origin and probe it from the intended environment."))
    return issues


def _probe_issues(probes: Sequence[ProbeObservation]) -> list[BaselineIssue]:
    issues: list[BaselineIssue] = []
    for probe in probes:
        if probe.status in {ProbeStatus.NOT_FOUND, ProbeStatus.REDIRECT}:
            issues.append(_issue("service_endpoint_missing", item_ids=[probe.item_id], required_action="Confirm the target service origin and repeat the probe from its declared environment."))
        elif probe.status == ProbeStatus.UNAUTHORIZED:
            issues.append(_issue("access_required", item_ids=[probe.item_id], required_action="Provide access through the configured secret reference, then repeat the read-only probe."))
        elif probe.status != ProbeStatus.OK:
            issues.append(_issue("service_probe_failed", item_ids=[probe.item_id], required_action="Resolve the probe failure or record the dependency with a concrete next probe."))
    return issues


def _assessment_for(item: PlannedItem, claims: Sequence[EvidenceClaim], base_sha: str, probes: Sequence[ProbeObservation], issues: Sequence[BaselineIssue]) -> CapabilityAssessment:
    item_claims = [claim for claim in claims if claim.item_id == item.item_id]
    values: dict[ClaimDimension, AssessmentDimension] = {}
    for dimension in ClaimDimension:
        dimension_claims = [claim for claim in item_claims if claim.dimension == dimension]
        usable = [claim for claim in dimension_claims if not (dimension == ClaimDimension.INTEGRATION and claim.evidence_kind == EvidenceKind.REMOTE_GIT and claim.candidate_sha and claim.candidate_sha.lower() != base_sha.strip().lower())]
        if any(claim.assertion == ClaimAssertion.PARTIAL for claim in usable):
            values[dimension] = AssessmentDimension.PARTIAL
        elif any(claim.assertion == ClaimAssertion.POSITIVE for claim in usable):
            if dimension == ClaimDimension.INTEGRATION and any(claim.evidence_kind == EvidenceKind.REMOTE_GIT and claim.candidate_sha for claim in usable):
                values[dimension] = AssessmentDimension.VERIFIED
            elif dimension == ClaimDimension.OPERATION and any(claim.validation_mode == ValidationMode.TARGET_ENVIRONMENT for claim in usable):
                values[dimension] = AssessmentDimension.VERIFIED
            else:
                values[dimension] = AssessmentDimension.REPORTED
        else:
            values[dimension] = AssessmentDimension.UNKNOWN
    if any(probe.item_id == item.item_id and probe.status == ProbeStatus.OK for probe in probes):
        values[ClaimDimension.OPERATION] = AssessmentDimension.VERIFIED
    return CapabilityAssessment(item_id=item.item_id, title=item.title, declared_status=item.declared_status, implementation=values[ClaimDimension.IMPLEMENTATION], integration=values[ClaimDimension.INTEGRATION], operation=values[ClaimDimension.OPERATION], evidence_ids=sorted(claim.claim_id for claim in item_claims), issue_ids=sorted(issue.issue_id for issue in issues if item.item_id in issue.item_ids), dependencies=sorted(item.dependencies))


def _dependency_issues(items: Iterable[PlannedItem]) -> list[BaselineIssue]:
    graph = dependency_graph(items)
    issues: list[BaselineIssue] = []
    for item_id, dependencies in graph.items():
        missing = [dependency for dependency in dependencies if dependency not in graph]
        if missing:
            issues.append(_issue("dependency_unknown", item_ids=[item_id, *missing], required_action="Resolve the dependency in a canonical source before scheduling the item."))
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
        for dependency in graph.get(node, ()):
            if dependency in graph:
                visit(dependency)
                if dependency in cycle_nodes:
                    cycle_nodes.add(node)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node)
    if cycle_nodes:
        issues.append(_issue("dependency_cycle", item_ids=sorted(cycle_nodes), severity=IssueSeverity.ERROR, required_action="Break the causal cycle in the source graph before dispatching work."))
    return issues


def _hf02_readiness(observations: Sequence[SourceObservation], assessments: Sequence[CapabilityAssessment]) -> tuple[HF02Readiness, list[BaselineIssue]]:
    required_sources = {"agent-contract", "autonomy-policy", "runtime-decision", "runtime-core", "runtime-store", "report-df-11-runtime-recovery"}
    by_id = {observation.source_id: observation for observation in observations}
    missing = sorted(source_id for source_id in required_sources if by_id.get(source_id) is None or by_id[source_id].status != SourceStatus.READ)
    has_df11 = any(assessment.item_id == "DF-11" for assessment in assessments)
    if missing or not has_df11:
        return HF02Readiness.BLOCKED, [_issue("HF02_RUNTIME_EVIDENCE_INCOMPLETE", source_ids=missing, severity=IssueSeverity.ERROR, required_action="Complete the runtime/store/DF-11 evidence and keep the base SHA identifiable before starting HF-02.", target_package="HF-02")]
    return HF02Readiness.READY, []


__all__ = ["dependency_graph", "reconcile_baseline", "source_fingerprint"]
