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


def claim(
    claim_id: str,
    item_id: str,
    dimension: ClaimDimension,
    assertion: ClaimAssertion,
    *,
    evidence_kind: EvidenceKind = EvidenceKind.DOCUMENT,
    source_id: str = "fixture",
    locator: str = "fixture.md#evidence",
    source_hash: str = HASH,
    candidate_sha: str | None = None,
    scope: str = "fixture",
    summary: str = "fixture evidence",
    validation_mode: ValidationMode = ValidationMode.DOCUMENTARY,
) -> EvidenceClaim:
    return EvidenceClaim(
        claim_id=claim_id,
        item_id=item_id,
        dimension=dimension,
        assertion=assertion,
        evidence_kind=evidence_kind,
        source_id=source_id,
        locator=locator,
        source_hash=source_hash,
        candidate_sha=candidate_sha,
        scope=scope,
        summary=summary,
        validation_mode=validation_mode,
    )


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


def test_unattested_remote_git_claim_remains_reported_never_verified() -> None:
    base_sha = "b" * 40
    remote_claim = claim(
        "remote-1",
        "DF-11",
        ClaimDimension.INTEGRATION,
        ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.REMOTE_GIT,
        candidate_sha=base_sha,
    )
    result = reconcile([item("DF-11")], [remote_claim])
    assert result.items[0].integration.value == "reported"
    assert result.items[0].integration.value != "verified"


def test_simulation_probe_remains_reported_and_is_retained_in_snapshot() -> None:
    probe = ProbeObservation(
        probe_id="mock-probe",
        item_id="DF-11",
        origin="http://fixture.invalid",
        status=ProbeStatus.OK,
        status_code=200,
        environment="unit-test",
        validation_mode=ValidationMode.SIMULATION,
    )
    result = reconcile([item("DF-11")], probes=(probe,))
    assert result.items[0].operation.value == "reported"
    assert result.items[0].operation.value != "verified"
    assert "mock-probe" in result.items[0].evidence_ids
    assert len(result.probe_observations) == 1
    assert result.probe_observations[0].probe_id == "mock-probe"
    assert "unit-test" in result.model_dump_json()


def test_positive_and_negative_claims_in_same_scope_yield_contradicted_and_issue() -> None:
    pos = claim("c-pos", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE, scope="same-scope")
    neg = claim("c-neg", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.NEGATIVE, scope="same-scope")
    result = reconcile([item("DF-11")], [pos, neg])
    assert result.items[0].implementation.value == "contradicted"
    issue_codes = {issue.code for issue in result.issues}
    assert "claim_conflict" in issue_codes


def test_positive_and_negative_claims_in_distinct_scopes_yield_partial_without_conflict() -> None:
    pos = claim("c-pos", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE, scope="scope-declared")
    neg = claim("c-neg", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.NEGATIVE, scope="scope-observed")
    result = reconcile([item("DF-11")], [pos, neg])
    assert result.items[0].implementation.value == "partial"
    issue_codes = {issue.code for issue in result.issues}
    assert "claim_conflict" not in issue_codes


def test_claim_with_mismatched_source_hash_or_missing_source_fails_positive_use() -> None:
    stale = claim("c-stale", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE, source_hash="f" * 64)
    missing = claim("c-missing", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE, source_id="absent")
    bad_locator = claim("c-bad-loc", "DF-11", ClaimDimension.IMPLEMENTATION, ClaimAssertion.POSITIVE, locator="wrong.md#claim")

    result_stale = reconcile([item("DF-11")], [stale])
    assert result_stale.items[0].implementation.value == "unknown"
    assert "stale_evidence" in {i.code for i in result_stale.issues}

    result_missing = reconcile([item("DF-11")], [missing])
    assert result_missing.items[0].implementation.value == "unknown"
    assert "claim_source_missing" in {i.code for i in result_missing.issues}

    result_loc = reconcile([item("DF-11")], [bad_locator])
    assert result_loc.items[0].implementation.value == "unknown"
    assert "claim_locator_invalid" in {i.code for i in result_loc.issues}
