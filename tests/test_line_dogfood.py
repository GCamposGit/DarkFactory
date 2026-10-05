"""The dogfood consumer reads approved tickets without duplicating line runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.dogfood import eligible_ticket_candidates, pick_candidate, submit_dogfood_item
from core.line.owner_intake import submit_ticket_to_line
from core.roadmap.models import DeliveryStatus, PlanningHorizon, RoadmapItem
from core.workflow.control_store import SQLiteControlStore


def _ticket(
    ticket_id: str,
    *,
    tags: list[str] | None = None,
    horizon: PlanningHorizon = PlanningHorizon.NOW,
    dependencies: list[str] | None = None,
    suggested_files: list[str] | None = None,
) -> UserTicket:
    return UserTicket(
        id=ticket_id,
        title=f"Implement {ticket_id}",
        tags=tags or [],
        horizon=horizon,
        dependencies=dependencies or [],
        suggested_files=suggested_files or [],
        acceptance_criteria=["The feature works"],
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def _stores(tmp_path: Path) -> tuple[DemandsStore, SQLiteControlStore]:
    return DemandsStore(tmp_path / "demands.json"), SQLiteControlStore(db_path=tmp_path / "control.db")


def _empty_roadmap(tmp_path: Path) -> Path:
    path = tmp_path / "roadmap.json"
    path.write_text(json.dumps({"items": []}), encoding="utf-8")
    return path


def _intake_ids(store: SQLiteControlStore) -> list[str]:
    with store._connect() as conn:
        return [row[0] for row in conn.execute("SELECT external_id FROM intake_commands ORDER BY external_id")]


def _fail_run(store: SQLiteControlStore, run_id: str) -> None:
    conn = store._connect()
    try:
        conn.execute("UPDATE jobs SET status = 'failed' WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()


def test_ticket_selection_requires_line_ok_resolved_deps_and_unprotected_paths() -> None:
    completed = _ticket("USR-1").model_copy(update={"status": DeliveryStatus.COMPLETED})
    candidates = eligible_ticket_candidates([
        _ticket("USR-2"),
        _ticket("USR-3", tags=["line-ok"], dependencies=["USR-9"]),
        _ticket("USR-4", tags=["line-ok"], suggested_files=["AGENTS.md"]),
        _ticket("USR-5", tags=["line-ok"], dependencies=["USR-1"], horizon=PlanningHorizon.NEXT),
        _ticket("USR-6", tags=["line-ok"], dependencies=["USR-1"]),
        completed,
    ])
    assert [ticket.id for ticket in candidates] == ["USR-6", "USR-5"]


def test_dogfood_uses_ledger_first_and_does_not_duplicate_an_active_ticket(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-10", tags=["line-ok"]))
    roadmap = tmp_path / "roadmap.json"
    roadmap.write_text(json.dumps({"items": [{
        "id": "RM-1", "project_id": "darkfac", "title": "Roadmap candidate",
        "item_type": "feature", "lifecycle_stage": "execution",
        "delivery_status": "planned", "horizon": "now", "confidence": "high",
        "tags": ["line-ok"], "completion_criteria": ["Works"],
    }]}), encoding="utf-8")

    first = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert first is not None and first.run_id
    assert _intake_ids(store) == ["ticket:USR-10"]
    assert demands.get_ticket("USR-10").status == DeliveryStatus.IMPLEMENTING

    second = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert second is None
    assert _intake_ids(store) == ["ticket:USR-10"]


def test_roadmap_cannot_bypass_ledger_tag_for_same_id(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-20"))
    roadmap = tmp_path / "roadmap.json"
    roadmap.write_text(json.dumps({"items": [{
        "id": "USR-20", "project_id": "darkfac", "title": "Same item",
        "item_type": "feature", "lifecycle_stage": "execution",
        "delivery_status": "planned", "horizon": "now", "confidence": "high",
        "tags": ["line-ok"], "completion_criteria": ["Works"],
    }]}), encoding="utf-8")

    receipt = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert receipt is None
    assert _intake_ids(store) == []


def test_failed_ticket_run_gets_a_new_attempt_without_human_resubmission(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-30", tags=["line-ok"]))
    roadmap = _empty_roadmap(tmp_path)
    first = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert first is not None and first.run_id
    _fail_run(store, first.run_id)

    second = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert second is not None and second.run_id != first.run_id
    assert _intake_ids(store) == ["ticket:USR-30", "ticket:USR-30:a2"]


def _complete_run(
    store: SQLiteControlStore,
    run_id: str,
    ticket_id: str,
    *,
    pr_url: str = "https://github.com/GCamposGit/DarkFactory/pull/199",
    merge_sha: str = "abc1234567890abcdef1234567890abcdef12345",
) -> None:
    from core.line.bindings import LINE_STAGES

    with store._connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE run_id = ?", (run_id,))
        for stage in LINE_STAGES:
            if stage in ("grill", "retrospective"):
                continue
            output_refs = json.dumps([pr_url, merge_sha]) if stage == "integration" else "[]"
            conn.execute(
                """
                INSERT OR REPLACE INTO jobs (
                    run_id, ticket_id, plan_version, stage, iteration, status,
                    role, required_capabilities, fencing_token, timeout_seconds,
                    retry_count, max_retries, actual_cost, output_refs, evidence_refs,
                    created_at, updated_at, ready_at
                ) VALUES (?, ?, '1.0', ?, 0, 'succeeded', 'worker', '[]', 0, 1800, 0, 3, 0.0, ?, '[]', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
                """,
                (run_id, ticket_id, stage, output_refs),
            )
        conn.commit()


def test_ticket_without_line_ok_is_never_submitted(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-50", tags=["other-tag"]))
    roadmap = _empty_roadmap(tmp_path)
    receipt = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert receipt is None
    assert _intake_ids(store) == []


def test_ticket_with_open_dependency_is_not_submitted(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-51", tags=["line-ok"], dependencies=["USR-999"]))
    roadmap = _empty_roadmap(tmp_path)
    receipt = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert receipt is None
    assert _intake_ids(store) == []


def test_completed_ticket_run_updates_status_with_delivery_evidence(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-60", tags=["line-ok"]))
    roadmap = _empty_roadmap(tmp_path)

    submission = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert submission is not None and submission.run_id
    ticket = demands.get_ticket("USR-60")
    assert ticket.status == DeliveryStatus.IMPLEMENTING

    _complete_run(store, submission.run_id, "USR-60")

    # Next dogfood sweep discovers the delivered run and updates demands.json
    res = submit_dogfood_item(
        store=store, demands_store=demands, roadmap_path=roadmap,
        reports_dir=tmp_path / "reports", min_green_streak=0,
    )
    assert res is None  # no new items to submit

    updated_ticket = demands.get_ticket("USR-60")
    assert updated_ticket.status == DeliveryStatus.COMPLETED
    assert "https://github.com/GCamposGit/DarkFactory/pull/199" in updated_ticket.delivery_evidence
    assert "abc12345" in updated_ticket.delivery_evidence


def test_replayed_succeeded_ticket_submission_marks_completed(tmp_path: Path) -> None:
    demands, store = _stores(tmp_path)
    demands.save_ticket(_ticket("USR-65", tags=["line-ok"]))
    roadmap = _empty_roadmap(tmp_path)

    submission = submit_ticket_to_line("USR-65", demands_store=demands, store=store)
    assert submission.ok and submission.run_id
    assert demands.get_ticket("USR-65").status == DeliveryStatus.IMPLEMENTING

    _complete_run(store, submission.run_id, "USR-65")

    # Re-submitting the same ticket detects delivery and updates status with evidence
    replayed = submit_ticket_to_line("USR-65", demands_store=demands, store=store)
    assert replayed.ok
    assert replayed.state == "delivered"

    updated_ticket = demands.get_ticket("USR-65")
    assert updated_ticket.status == DeliveryStatus.COMPLETED
    assert "https://github.com/GCamposGit/DarkFactory/pull/199" in updated_ticket.delivery_evidence


def test_pick_candidate_prioritizes_tickets_over_roadmap() -> None:
    ticket = _ticket("USR-70", tags=["line-ok"], horizon=PlanningHorizon.NOW)
    items = [
        RoadmapItem(
            id="RM-70",
            project_id="darkfac",
            title="Roadmap RM-70",
            item_type="feature",
            lifecycle_stage="execution",
            delivery_status=DeliveryStatus.PLANNED,
            horizon=PlanningHorizon.NOW,
            confidence="high",
            tags=["line-ok"],
            completion_criteria=["Done"],
        )
    ]
    # When both are passed, ticket takes precedence
    candidate = pick_candidate(items=items, tickets=[ticket])
    assert isinstance(candidate, UserTicket)
    assert candidate.id == "USR-70"

    # When ticket is excluded, roadmap item wins
    candidate2 = pick_candidate(items=items, tickets=[ticket], exclude_ids=frozenset(["USR-70"]))
    assert isinstance(candidate2, RoadmapItem)
    assert candidate2.id == "RM-70"


def test_retrospective_stage_handler_reconciles_delivered_ticket(tmp_path: Path) -> None:
    from core.line.bindings import RetrospectiveStageHandler
    from core.workflow.control_contracts import Claim, JobKey, StageContext

    project_dir = tmp_path / "project"
    demands_dir = project_dir / ".factory" / "demands"
    demands_dir.mkdir(parents=True, exist_ok=True)
    demands = DemandsStore(demands_dir / "demands.json")
    demands.save_ticket(_ticket("USR-80", tags=["line-ok"]))

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    sub = submit_ticket_to_line("USR-80", demands_store=demands, store=store)
    assert sub.ok and sub.run_id

    _complete_run(store, sub.run_id, "USR-80")

    class FakeProject:
        def resolve_path(self):
            return project_dir

    handler = RetrospectiveStageHandler(store=store, project_resolver=lambda _: FakeProject())
    jk = JobKey(run_id=sub.run_id, ticket_id="darkfac", plan_version="1.0", stage="retrospective", iteration=0)
    claim = Claim(
        job_key=jk,
        lease_id="lease-retro-test",
        owner="worker-1",
        fencing_token=1,
        expires_at=datetime.now(UTC).isoformat(),
    )
    context = StageContext(
        claim=claim,
        plan_ref="plan://line",
        plan_digest="a" * 64,
        config_version="1.0",
        environment_ref="env",
        identity="worker-1",
        route_ref="route",
        memory_version="1.0",
    )
    result = handler.handle(context)
    assert result.outcome == "success"

    updated = demands.get_ticket("USR-80")
    assert updated.status == DeliveryStatus.COMPLETED
    assert "https://github.com/GCamposGit/DarkFactory/pull/199" in updated.delivery_evidence

