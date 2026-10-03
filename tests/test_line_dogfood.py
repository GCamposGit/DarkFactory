"""The dogfood consumer reads approved tickets without duplicating line runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line.dogfood import eligible_ticket_candidates, submit_dogfood_item
from core.roadmap.models import DeliveryStatus, PlanningHorizon
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
