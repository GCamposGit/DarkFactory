"""Acceptance tests for the pure HF-01 baseline reconciler."""

from datetime import datetime, timezone
from pathlib import Path

from core.planning.baseline_models import ClaimAssertion, ClaimDimension, CollectedBaseline, EvidenceClaim, EvidenceKind, PlannedItem, SourceObservation, SourceStatus, ValidationMode
from core.planning.baseline_probes import ProbeObservation, ProbeStatus
from core.planning.baseline_reconcile import dependency_graph, reconcile_baseline, source_fingerprint

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def item(item_id: str, status: str = "planned", deps: list[str] | None = None) -> PlannedItem:
    return PlannedItem(item_id=item_id, title=f"Title {item_id}", declared_status=status, dependencies=deps or [], source_id="fixture", locator=f"fixture.json#{item_id}")


def observation(source_id: str, status: SourceStatus = SourceStatus.READ, sha256: str | None = HASH, error_code: str | None = None) -> SourceObservation:
    return SourceObservation(source_id=source_id, relative_path=Path(f"{source_id}.md"), sha256=sha256, status=status, observed_at=NOW, error_code=error_code)


def claim(claim_id: str, item_id: str, dimension: ClaimDimension, assertion: ClaimAssertion, *, evidence_kind: EvidenceKind = EvidenceKind.DOCUMENT, candidate_sha: str | None = None, summary: str = "fixture evidence", validation_mode: ValidationMode = ValidationMode.DOCUMENTARY) -> EvidenceClaim:
    return EvidenceClaim(claim_id=claim_id, item_id=item_id, dimension=dimension, assertion=assertion, evidence_kind=evidence_kind, source_id="fixture", locator="fixture.md#evidence", source_hash=HASH, candidate_sha=candidate_sha, scope="fixture", summary=summary, validation_mode=validation_mode)


def reconcile(items: list[PlannedItem], claims: list[EvidenceClaim] | None = None, observations: list[SourceObservation] | None = None, probes: tuple[ProbeObservation, ...] = ()):
    return reconcile_baseline(CollectedBaseline(observations=observations or [observation("fixture")], planned_items=items, claims=[], issues=[]), claims or [], base_sha="b" * 40, snapshot_id="snapshot-fixture", observed_at=NOW, probe_observations=probes)


def test_a1_does_not_promote_unrelated_dimensions() -> None:
    result = reconcile([item("T-A1")], [claim("a1", "T-A1", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE)])
    assert result.items[0].implementation.value == "reported"
    assert result.items[0].integration.value == "unknown"
    assert result.items[0].operation.value == "unknown"


def test_a2_partial_coverage_is_an_issue() -> None:
    result = reconcile([item("DF-23", "completed")], [claim("a2", "DF-23", ClaimDimension.IMPLEMENTATION, ClaimAssertion.PARTIAL, summary="automation coverage is partial")])
    assert result.items[0].implementation.value == "partial"
    assert "partial_ticket_coverage" in {issue.code for issue in result.issues}


def test_a3_conflicting_declarations_are_merged_once() -> None:
    second = item("INFRA-10", "completed").model_copy(update={"source_id": "second-source"})
    result = reconcile([item("INFRA-10"), second])
    assert [entry.item_id for entry in result.items] == ["INFRA-10"]
    assert "source_conflict" in {issue.code for issue in result.issues}


def test_a4_generic_n8n_link_is_not_operation_evidence() -> None:
    result = reconcile([item("n8n", "active")], [claim("a4", "n8n", ClaimDimension.OPERATION, ClaimAssertion.NEGATIVE, summary="link points only to https://n8n.io")])
    assert result.items[0].operation.value == "unknown"
    assert "service_endpoint_missing" in {issue.code for issue in result.issues}


def test_a5_owner_statement_requires_access() -> None:
    result = reconcile([item("PostgreSQL")], [claim("a5", "PostgreSQL", ClaimDimension.OPERATION, ClaimAssertion.POSITIVE, evidence_kind=EvidenceKind.OWNER_STATEMENT, summary="installed, working and tested by owner")])
    assert result.items[0].operation.value == "reported"
    assert "access_required" in {issue.code for issue in result.issues}


def test_a6_404_does_not_erase_implementation_report() -> None:
    probe = ProbeObservation(probe_id="dashboard", item_id="DF-21", origin="https://hub.example.test", status=ProbeStatus.NOT_FOUND, status_code=404, observed_at=NOW, environment="target-vps", validation_mode=ValidationMode.TARGET_ENVIRONMENT)
    result = reconcile([item("DF-21")], [claim("a6", "DF-21", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE)], probes=(probe,))
    assert result.items[0].implementation.value == "reported"
    assert result.items[0].operation.value == "unknown"
    assert "service_endpoint_missing" in {issue.code for issue in result.issues}


def test_a7_dependency_mention_does_not_create_claim() -> None:
    result = reconcile([item("DF-23", deps=["DF-17"])])
    assert [entry.item_id for entry in result.items] == ["DF-23"]
    assert not any(entry.item_id == "DF-17" for entry in result.claims)


def test_a8_stale_remote_evidence_is_unknown() -> None:
    result = reconcile([item("DF-20")], [claim("a8", "DF-20", ClaimDimension.INTEGRATION, ClaimAssertion.POSITIVE, evidence_kind=EvidenceKind.REMOTE_GIT, candidate_sha="1" * 40)])
    assert result.items[0].integration.value == "unknown"
    assert "stale_evidence" in {issue.code for issue in result.issues}


def test_a9_source_failure_categories_remain_distinct() -> None:
    result = reconcile([], observations=[observation("unstable", SourceStatus.UNSTABLE, None, "SOURCE_UNSTABLE"), observation("invalid", SourceStatus.INVALID, None, "SOURCE_INVALID_JSON"), observation("outside", SourceStatus.ACCESS_DENIED, None, "SOURCE_OUTSIDE_ROOT")])
    assert [entry.status.value for entry in result.source_observations] == ["unstable", "invalid", "access_denied"]


def test_a10_fingerprint_and_logic_ignore_timestamp() -> None:
    first = observation("fixture")
    second = first.model_copy(update={"observed_at": NOW.replace(minute=5)})
    assert source_fingerprint([first]) == source_fingerprint([second])
    assert reconcile([item("RM-01")], observations=[first]).items == reconcile([item("RM-01")], observations=[second]).items


def test_dependency_graph_is_stable_and_flags_missing_and_cycles() -> None:
    entries = [item("A", deps=["B"]), item("B", deps=["A"]), item("C", deps=["MISSING"])]
    assert dependency_graph(entries) == {"A": ("B",), "B": ("A",), "C": ("MISSING",)}
    codes = {issue.code for issue in reconcile(entries).issues}
    assert {"dependency_cycle", "dependency_unknown"} <= codes
