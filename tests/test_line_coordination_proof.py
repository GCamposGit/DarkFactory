import json
from datetime import UTC, datetime

from core.line.coordination_proof import (
    CRITERIA,
    collect_coordination_proof,
    evaluate_coordination_proof,
)


def test_coordination_proof_keeps_missing_evidence_pending_and_degraded() -> None:
    report = evaluate_coordination_proof(
        {"priority": ["priority-event.json"]},
        now=datetime(2026, 11, 21, tzinfo=UTC),
        degraded_sources=["quota_repair:telemetry_unavailable"],
    )

    assert report.criterion_met is False
    assert report.criteria[0].verified is True
    assert next(item for item in report.criteria if item.id == "quota_repair").verified is False
    assert len(report.missing) == len(CRITERIA) - 1


def test_coordination_cli_collector_reads_four_harness_refs_and_missing_items_are_pending(tmp_path) -> None:
    (tmp_path / "harness_entry.json").write_text(
        json.dumps({"evidence_refs": ["claude.json", "codex.json", "grok.json", "antigravity.json"]}),
        encoding="utf-8",
    )
    (tmp_path / "continuity.json").write_text("{invalid", encoding="utf-8")
    report = collect_coordination_proof(evidence_dir=tmp_path, now=datetime(2026, 11, 21, tzinfo=UTC))

    assert next(item for item in report.criteria if item.id == "harness_entry").verified is True
    assert "continuity:evidence_read_failed" in report.degraded_sources
    assert next(item for item in report.criteria if item.id == "continuity").verified is False

