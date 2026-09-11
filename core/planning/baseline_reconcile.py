"""Deterministic, side-effect-free reconciliation for the HF-01 baseline."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
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
    ProbeObservation,
    ProbeStatus,
    SourceObservation,
    SourceStatus,
    ValidationMode,
    utc_now,
)


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
    obs_by_id = {obs.source_id: obs for obs in observations}
    all_probes = _dedupe_probes([*collected.probe_observations, *probe_observations])
    merged_claims = _dedupe_claims([*collected.claims, *claims])
    items_by_id = _merge_items(collected.planned_items, merged_claims, all_probes)
    issues = _dedupe_issues(collected.issues)
    issues.extend(_declaration_conflict_issues(collected.planned_items))
    issues.extend(_claim_validation_issues(merged_claims, obs_by_id))
    issues.extend(_assessment_issues(merged_claims, base_sha, all_probes))
    issues.extend(_claim_conflict_issues(merged_claims, obs_by_id, base_sha))
    issues.extend(_dependency_issues(items_by_id.values()))
    issues.extend(_probe_issues(all_probes))
    assessments = [_assessment_for(item, merged_claims, base_sha, all_probes, issues, obs_by_id) for item in sorted(items_by_id.values(), key=lambda value: value.item_id)]
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
    return BaselineSnapshot(snapshot_id=snapshot_id, observed_at=observed_at or utc_now(), base_sha=base_sha.strip().lower(), source_fingerprint=source_fingerprint(observations), source_observations=list(observations), items=assessments, claims=list(merged_claims), issues=issues, probe_observations=list(all_probes), completeness=completeness, hf02_readiness=readiness, blocker_codes=blocker_codes)


def _dedupe_probes(probes: Sequence[ProbeObservation]) -> list[ProbeObservation]:
    unique = {probe.probe_id: probe for probe in probes}
    return [unique[key] for key in sorted(unique)]


def _dedupe_claims(claims: Sequence[EvidenceClaim]) -> list[EvidenceClaim]:
    unique = {claim.claim_id: claim for claim in claims}
    return [unique[key] for key in sorted(unique)]


def _merge_items(items: Sequence[PlannedItem], claims: Sequence[EvidenceClaim], probes: Sequence[ProbeObservation] = ()) -> dict[str, PlannedItem]:
    grouped: dict[str, list[PlannedItem]] = defaultdict(list)
    for item in items:
        grouped[item.item_id].append(item)
    for claim in claims:
        if claim.item_id not in grouped:
            grouped[claim.item_id].append(PlannedItem(item_id=claim.item_id, title=claim.item_id, declared_status="unknown", dependencies=[], source_id=claim.source_id, locator=claim.locator))
    for probe in probes:
        if probe.item_id not in grouped:
            grouped[probe.item_id].append(PlannedItem(item_id=probe.item_id, title=probe.item_id, declared_status="unknown", dependencies=[], source_id=probe.probe_id, locator=probe.origin))
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


def _is_locator_matching(locator: str, relative_path: Path) -> bool:
    raw_path = locator.split("#", 1)[0].strip()
    if not raw_path:
        return False
    return Path(raw_path).as_posix() == relative_path.as_posix()


def _is_claim_valid(claim: EvidenceClaim, obs_by_id: dict[str, SourceObservation], base_sha: str) -> bool:
    obs = obs_by_id.get(claim.source_id)
    if obs is None:
        return False
    if obs.status != SourceStatus.READ:
        return False
    if obs.sha256 != claim.source_hash:
        return False
    if not _is_locator_matching(claim.locator, obs.relative_path):
        return False
    if (
        claim.dimension == ClaimDimension.INTEGRATION
        and claim.evidence_kind == EvidenceKind.REMOTE_GIT
        and claim.candidate_sha
        and claim.candidate_sha.lower() != base_sha.strip().lower()
    ):
        return False
    return True


def _claim_validation_issues(claims: Sequence[EvidenceClaim], obs_by_id: dict[str, SourceObservation]) -> list[BaselineIssue]:
    issues: list[BaselineIssue] = []
    for claim in claims:
        obs = obs_by_id.get(claim.source_id)
        if obs is None:
            issues.append(
                _issue(
                    "claim_source_missing",
                    item_ids=[claim.item_id],
                    source_ids=[claim.source_id],
                    required_action="Ensure the claim references an authorized, catalogued source observation.",
                )
            )
        else:
            if obs.status != SourceStatus.READ:
                issues.append(
                    _issue(
                        "claim_source_unreadable",
                        item_ids=[claim.item_id],
                        source_ids=[claim.source_id],
                        required_action=f"Source observation is '{obs.status.value}'; ensure it is readable before relying on its claims.",
                    )
                )
            if obs.sha256 != claim.source_hash:
                issues.append(
                    _issue(
                        "stale_evidence",
                        item_ids=[claim.item_id],
                        source_ids=[claim.source_id],
                        required_action="Collect fresh evidence; the observed source hash does not match the claim source hash.",
                    )
                )
            if not _is_locator_matching(claim.locator, obs.relative_path):
                issues.append(
                    _issue(
                        "claim_locator_invalid",
                        item_ids=[claim.item_id],
                        source_ids=[claim.source_id],
                        required_action="Align the claim locator with the source observation relative path.",
                    )
                )
    return issues


def _claim_conflict_issues(
    claims: Sequence[EvidenceClaim],
    obs_by_id: dict[str, SourceObservation],
    base_sha: str,
) -> list[BaselineIssue]:
    issues: list[BaselineIssue] = []
    grouped: dict[tuple[str, ClaimDimension, str], list[EvidenceClaim]] = defaultdict(list)
    for claim in claims:
        if _is_claim_valid(claim, obs_by_id, base_sha):
            grouped[(claim.item_id, claim.dimension, claim.scope)].append(claim)

    for (item_id, dimension, scope), group in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1].value, x[0][2])):
        has_pos = any(c.assertion == ClaimAssertion.POSITIVE for c in group)
        has_neg = any(c.assertion == ClaimAssertion.NEGATIVE for c in group)
        if has_pos and has_neg:
            source_ids = sorted({c.source_id for c in group})
            issues.append(
                _issue(
                    "claim_conflict",
                    item_ids=[item_id],
                    source_ids=source_ids,
                    required_action=f"Resolve contradicting positive and negative evidence claims in scope '{scope}'.",
                )
            )
    return issues


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


def _assessment_for(
    item: PlannedItem,
    claims: Sequence[EvidenceClaim],
    base_sha: str,
    probes: Sequence[ProbeObservation],
    issues: Sequence[BaselineIssue],
    obs_by_id: dict[str, SourceObservation],
) -> CapabilityAssessment:
    item_claims = [claim for claim in claims if claim.item_id == item.item_id]
    values: dict[ClaimDimension, AssessmentDimension] = {}
    for dimension in ClaimDimension:
        dimension_claims = [claim for claim in item_claims if claim.dimension == dimension]
        usable = [claim for claim in dimension_claims if _is_claim_valid(claim, obs_by_id, base_sha)]

        scope_groups: dict[str, list[EvidenceClaim]] = defaultdict(list)
        for claim in usable:
            scope_groups[claim.scope].append(claim)

        has_same_scope_conflict = any(
            any(c.assertion == ClaimAssertion.POSITIVE for c in group)
            and any(c.assertion == ClaimAssertion.NEGATIVE for c in group)
            for group in scope_groups.values()
        )

        if has_same_scope_conflict:
            values[dimension] = AssessmentDimension.CONTRADICTED
        else:
            has_pos = any(c.assertion == ClaimAssertion.POSITIVE for c in usable)
            has_neg = any(c.assertion == ClaimAssertion.NEGATIVE for c in usable)
            has_partial = any(c.assertion == ClaimAssertion.PARTIAL for c in usable)

            if has_partial or (has_pos and has_neg):
                values[dimension] = AssessmentDimension.PARTIAL
            elif has_pos:
                values[dimension] = AssessmentDimension.REPORTED
            else:
                values[dimension] = AssessmentDimension.UNKNOWN

    item_probes = [probe for probe in probes if probe.item_id == item.item_id]
    if any(probe.status == ProbeStatus.OK for probe in item_probes):
        if values[ClaimDimension.OPERATION] == AssessmentDimension.UNKNOWN:
            values[ClaimDimension.OPERATION] = AssessmentDimension.REPORTED

    evidence_ids = sorted(set([claim.claim_id for claim in item_claims] + [probe.probe_id for probe in item_probes]))
    issue_ids = sorted(issue.issue_id for issue in issues if item.item_id in issue.item_ids)

    return CapabilityAssessment(
        item_id=item.item_id,
        title=item.title,
        declared_status=item.declared_status,
        implementation=values[ClaimDimension.IMPLEMENTATION],
        integration=values[ClaimDimension.INTEGRATION],
        operation=values[ClaimDimension.OPERATION],
        evidence_ids=evidence_ids,
        issue_ids=issue_ids,
        dependencies=sorted(item.dependencies),
    )


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
