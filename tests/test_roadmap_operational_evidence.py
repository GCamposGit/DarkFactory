"""Operational evidence, readiness and plan enrichment tests for HF-13-01."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.paths import project_root
from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    DependencyType,
    LifecycleStage,
    PlanningHorizon,
    RoadmapCandidate,
    RoadmapDependency,
    RoadmapEvidenceRef,
    RoadmapItemType,
    RoadmapSourceRef,
    RoadmapSourceState,
)
from core.roadmap.service import build_repository_roadmap_service
from core.roadmap.sources import (
    ContinuousAutonomyPlanSource,
    JsonRoadmapSource,
    RoadmapSourceResult,
    is_report_for_ticket,
)
from core.workflow.contracts import SanitizedIdentity
from core.workflow.verification import EvidenceReceipt, EvidenceResult, ValidationMode


def _make_candidate(
    item_id: str,
    *,
    project_id: str = "darkfac",
    status: DeliveryStatus = DeliveryStatus.PLANNED,
    dependencies: list[RoadmapDependency] | None = None,
    evidence_refs: list[RoadmapEvidenceRef] | None = None,
    source_id: str = "fixture",
) -> RoadmapCandidate:
    return RoadmapCandidate(
        id=item_id,
        project_id=project_id,
        title=f"Test Item {item_id}",
        description="Fixture item for DAG resilience testing.",
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        delivery_status=status,
        horizon=PlanningHorizon.NOW,
        confidence=ConfidenceLevel.HIGH,
        dependencies=dependencies or [],
        evidence_refs=evidence_refs or [],
        source_id=source_id,
        source_priority=10,
        source_refs=[
            RoadmapSourceRef(
                source_id=source_id,
                source_kind="fixture",
                label=f"Fixture {source_id}",
                locator="memory://fixture",
            )
        ],
    )


def test_plan_enrichment_by_full_id_with_status_fields(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    plan_data = {
        "schema_version": "1.0",
        "units": [
            {
                "ticket_id": "HF-13-01",
                "parent_id": "HF-13",
                "title": "Prontidão e evidência no roadmap",
                "planning_status": "waiting_dependency",
                "implementation_status": "not_started",
                "operational_status": "not_verified",
                "depends_on": ["HF-26-01"],
                "oracle": "Relatório falso/prefixo pai-filho não concluem.",
            },
            {
                "ticket_id": "HF-26-01",
                "parent_id": "HF-26",
                "title": "Resolvedor puro de política efetiva",
                "planning_status": "ready_for_handoff",
                "implementation_status": "not_started",
                "operational_status": "not_verified",
                "depends_on": [],
                "oracle": "Grant exato autoriza.",
            },
        ],
    }
    plan_file.write_text(json.dumps(plan_data), encoding="utf-8")

    source = ContinuousAutonomyPlanSource(plan_file)
    result = source.read("darkfac")

    assert result.state.status == "available"
    assert len(result.records) == 2
    records_by_id = {rec.id: rec for rec in result.records}

    # Verify full ID keying (never truncated to parent HF-13 or HF-26)
    assert "HF-13-01" in records_by_id
    assert "HF-26-01" in records_by_id
    assert "HF-13" not in records_by_id
    assert "HF-26" not in records_by_id

    hf13_01 = records_by_id["HF-13-01"]
    assert hf13_01.id == "HF-13-01"
    assert hf13_01.parent_id == "HF-13"
    assert hf13_01.planning_status == "waiting_dependency"
    assert hf13_01.implementation_status == "not_started"
    assert hf13_01.operational_status == "not_verified"
    assert any(dep.item_id == "HF-26-01" for dep in hf13_01.dependencies)

    hf26_01 = records_by_id["HF-26-01"]
    assert hf26_01.id == "HF-26-01"
    assert hf26_01.parent_id == "HF-26"
    assert hf26_01.planning_status == "ready_for_handoff"
    assert hf26_01.implementation_status == "not_started"
    assert hf26_01.operational_status == "not_verified"

    # Integration test with build_repository_roadmap_service
    service = build_repository_roadmap_service(project_root())
    repo_hf13_01 = service.get_item("darkfac", "HF-13-01")
    assert repo_hf13_01 is not None
    assert repo_hf13_01.id == "HF-13-01"
    assert repo_hf13_01.parent_id == "HF-13"
    assert repo_hf13_01.planning_status == "ready_for_handoff"
    assert repo_hf13_01.implementation_status == "implemented"
    assert repo_hf13_01.operational_status == "verified"


def test_documentary_markdown_glob_does_not_conclude_ticket_without_verified_receipt(
    tmp_path: Path,
) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({
            "schema_version": "1.0",
            "units": [
                {
                    "ticket_id": "HF-13-01",
                    "parent_id": "HF-13",
                    "title": "Prontidão e evidência no roadmap",
                    "planning_status": "waiting_dependency",
                    "implementation_status": "not_started",
                    "operational_status": "not_verified",
                    "depends_on": [],
                }
            ],
        }),
        encoding="utf-8",
    )

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    doc_report = reports_dir / "hf-13-01-readiness-report.md"
    doc_report.write_text("Markdown report content for HF-13-01", encoding="utf-8")

    # 1. Read with globbed markdown report ONLY
    source = ContinuousAutonomyPlanSource(plan_file, evidence_dir=reports_dir)
    result = source.read("darkfac")
    candidate = result.records[0]

    # Contra-prova: Documentary report MUST be documentary_unverified, verified=False, and NEVER completed
    assert len(candidate.evidence_refs) == 1
    evidence = candidate.evidence_refs[0]
    assert evidence.evidence_kind == "documentary_unverified"
    assert evidence.verified is False
    assert candidate.delivery_status == DeliveryStatus.PLANNED
    assert candidate.delivery_status != DeliveryStatus.COMPLETED

    # 2. Supply verified receipt for HF-13-01
    receipt = EvidenceReceipt(
        receipt_id="rcpt-hf-13-01-pass",
        producer=SanitizedIdentity(subject="agent-tester", role="reviewer", host="local"),
        subject="HF-13-01",
        requirement="HF-13-01",
        artifact_hash="83e5298eb231599076811802dceac8575c7f6feb",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=datetime.now(timezone.utc),
    )

    verified_source = ContinuousAutonomyPlanSource(
        plan_file, evidence_dir=reports_dir, receipts=[receipt]
    )
    verified_result = verified_source.read("darkfac")
    verified_candidate = verified_result.records[0]

    # Completion strictly requires verified receipt
    assert any(ref.verified for ref in verified_candidate.evidence_refs)
    assert verified_candidate.delivery_status == DeliveryStatus.COMPLETED


def test_child_evidence_isolation_never_concludes_parent(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({
            "schema_version": "1.0",
            "units": [
                {
                    "ticket_id": "HF-05",
                    "title": "Núcleo de Controle da Fábrica",
                    "planning_status": "ready",
                    "depends_on": [],
                },
                {
                    "ticket_id": "HF-05-02",
                    "parent_id": "HF-05",
                    "title": "Binding do controle cloud e ownership",
                    "planning_status": "waiting_dependency",
                    "depends_on": ["HF-05"],
                },
            ],
        }),
        encoding="utf-8",
    )

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    # Report belonging strictly to child HF-05-02
    (reports_dir / "hf-05-02-runtime-report.md").write_text("Child report", encoding="utf-8")

    # Receipt belonging strictly to child HF-05-02
    child_receipt = EvidenceReceipt(
        receipt_id="rcpt-hf-05-02",
        producer=SanitizedIdentity(subject="agent-worker", role="reviewer", host="local"),
        subject="HF-05-02",
        requirement="HF-05-02",
        artifact_hash="cafebabe00112233445566778899aabbccddeeff00112233445566778899aabb",
        result=EvidenceResult.PASSED,
        mode=ValidationMode.TARGET_ENVIRONMENT,
        observed_at=datetime.now(timezone.utc),
    )

    source = ContinuousAutonomyPlanSource(
        plan_file, evidence_dir=reports_dir, receipts=[child_receipt]
    )
    result = source.read("darkfac")
    records_by_id = {rec.id: rec for rec in result.records}

    parent = records_by_id["HF-05"]
    child = records_by_id["HF-05-02"]

    # Child ticket HF-05-02 receives evidence and is completed
    assert child.delivery_status == DeliveryStatus.COMPLETED
    assert any(ref.verified for ref in child.evidence_refs)

    # Parent ticket HF-05 MUST NOT receive child's evidence or be concluded
    assert parent.delivery_status == DeliveryStatus.PLANNED
    assert parent.delivery_status != DeliveryStatus.COMPLETED
    assert len(parent.evidence_refs) == 0
    assert not any("hf-05-02" in ref.evidence_id.lower() for ref in parent.evidence_refs)

    # Unit-level contra-proof test for is_report_for_ticket
    assert is_report_for_ticket("hf-05-02-runtime-report.md", "HF-05") is False
    assert is_report_for_ticket("hf-05-02-runtime-report.md", "HF-05-02") is True
    assert is_report_for_ticket("hf-05-report.md", "HF-05") is True
    assert is_report_for_ticket("hf-05-report.md", "HF-05-02") is False
    assert is_report_for_ticket("hf-05-01-local-runtime-report.md", "HF-05") is False


def test_offline_or_error_source_marked_stale_or_unavailable_preserves_dag(
    tmp_path: Path,
) -> None:
    missing_plan = tmp_path / "nonexistent" / "plan.json"
    source = ContinuousAutonomyPlanSource(missing_plan)

    # 1. Offline / missing source returns unavailable without exception
    result = source.read("darkfac")
    assert result.state.status == "unavailable"
    assert result.records == []
    assert "file not found" in (result.state.error or "")

    # 2. Integration in compiler with another source preserves DAG without truncating
    class StubValidSource:
        source_id = "stub-valid"
        priority = 10

        def read(self, project_id: str) -> RoadmapSourceResult:
            item_b = _make_candidate("TASK-B", project_id=project_id)
            item_a = _make_candidate(
                "TASK-A",
                project_id=project_id,
                dependencies=[
                    RoadmapDependency(item_id="TASK-B", type=DependencyType.REQUIRES)
                ],
            )
            state = RoadmapSourceState(
                source_id=self.source_id,
                label=self.source_id,
                source_kind="fixture",
                locator="memory://stub",
                revision="v1",
                content_hash="hash-v1",
            )
            return RoadmapSourceResult(state=state, records=[item_a, item_b])

    compiler = RoadmapCompiler([StubValidSource(), source])
    snapshot = compiler.compile("darkfac")

    # DAG items and relations are completely preserved
    assert {item.id for item in snapshot.items} == {"TASK-A", "TASK-B"}
    task_a = next(it for it in snapshot.items if it.id == "TASK-A")
    assert any(dep.item_id == "TASK-B" for dep in task_a.dependencies)
    assert snapshot.sources_unavailable == ["continuous-autonomy-plan"]

    # 3. Test stale caching resilience
    plan_file = tmp_path / "valid_plan.json"
    plan_file.write_text(
        json.dumps({
            "schema_version": "1.0",
            "units": [
                {
                    "ticket_id": "HF-13-01",
                    "title": "Test Cached",
                    "planning_status": "waiting_dependency",
                }
            ],
        }),
        encoding="utf-8",
    )

    caching_source = ContinuousAutonomyPlanSource(plan_file)
    initial_result = caching_source.read("darkfac")
    assert initial_result.state.status == "available"
    assert len(initial_result.records) == 1

    # Remove the file to simulate transient offline condition
    plan_file.unlink()
    stale_result = caching_source.read("darkfac")

    # Stale fail-safe: marks stale and preserves cached records without crashing DAG
    assert stale_result.state.status == "stale"
    assert len(stale_result.records) == 1
    assert stale_result.records[0].id == "HF-13-01"


def test_repository_service_deterministic_snapshot_hash() -> None:
    service = build_repository_roadmap_service(project_root())
    first = service.get_snapshot("darkfac")
    second = service.get_snapshot("darkfac")

    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_id == second.snapshot_id

    # Verify HF-13-01 in compiled snapshot
    hf13_01 = next((item for item in first.items if item.id == "HF-13-01"), None)
    assert hf13_01 is not None
    assert hf13_01.planning_status == "ready_for_handoff"
    assert hf13_01.implementation_status == "implemented"
    assert hf13_01.operational_status == "verified"
    assert hf13_01.parent_id == "HF-13"


def test_json_manifest_raw_evidence_refs_do_not_drop_source(tmp_path: Path) -> None:
    """Raw dict evidence_refs in the manifest must be coerced, not crash read()."""
    def _item(item_id: str, evidence_refs: list[dict] | None = None) -> dict:
        item = {
            "id": item_id,
            "project_id": "darkfac",
            "title": f"Item {item_id}",
            "description": "Fixture item with raw manifest evidence.",
            "item_type": "feature",
            "lifecycle_stage": "execution",
            "delivery_status": "planned",
            "horizon": "now",
            "confidence": "high",
            "dependencies": [],
        }
        if evidence_refs is not None:
            item["evidence_refs"] = evidence_refs
        return item

    manifest = tmp_path / "roadmap.json"
    manifest.write_text(
        json.dumps({
            "project_id": "darkfac",
            "items": [
                _item(
                    "HF-99-01",
                    [
                        {
                            "evidence_id": "pr-99",
                            "evidence_kind": "pull_request",
                            "label": "PR #99 merged",
                            "locator": "https://example.invalid/pull/99",
                            "verified": True,
                        }
                    ],
                ),
                _item("HF-99-02"),
            ],
        }),
        encoding="utf-8",
    )

    result = JsonRoadmapSource(manifest).read("darkfac")

    assert result.state.status == "available", result.state.error
    records_by_id = {rec.id: rec for rec in result.records}
    assert set(records_by_id) == {"HF-99-01", "HF-99-02"}
    with_evidence = records_by_id["HF-99-01"]
    assert len(with_evidence.evidence_refs) == 1
    assert isinstance(with_evidence.evidence_refs[0], RoadmapEvidenceRef)
    assert with_evidence.evidence_refs[0].evidence_id == "pr-99"
    assert with_evidence.delivery_status == DeliveryStatus.COMPLETED

    snapshot = RoadmapCompiler([JsonRoadmapSource(manifest)]).compile("darkfac")
    assert {"HF-99-01", "HF-99-02"} <= {item.id for item in snapshot.items}
