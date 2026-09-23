"""DH-16 (USR-50): Tests for retirement of legacy state.json and orchestrator.sqlite3 sources.

Verifies:
  (a) HubService.get_task_dashboard() relies purely on the canonical control store;
  (b) Legacy methods (_load_task_records, _load_task_runs) are retired;
  (c) Legacy sources ('state', 'runs') are not present in report.sources;
  (d) Even if state.json or orchestrator.sqlite3 exist on disk, they are ignored by the task dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.workflow.control_store import SQLiteControlStore
from hub.backend.service import HubService


def test_legacy_loader_methods_are_retired():
    """Verify that legacy helper methods were removed from HubService."""
    assert not hasattr(HubService, "_load_task_records"), "_load_task_records must be retired"
    assert not hasattr(HubService, "_load_task_runs"), "_load_task_runs must be retired"
    assert not hasattr(HubService, "_dashboard_evidence"), "_dashboard_evidence must be retired"
    assert not hasattr(HubService, "_dashboard_exceptions"), "_dashboard_exceptions must be retired"


def test_dashboard_sources_contain_only_control_and_usage(tmp_path: Path):
    """Verify report.sources only includes canonical control and usage, not legacy ledgers."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")

    service = HubService(
        data_dir=data_dir,
        roadmap_root=Path.cwd(),
        usage_dir=tmp_path / "usage",
        control_db_path=tmp_path / "control.db",
        control_database_url="",
    )

    report = service.get_task_dashboard()
    assert "control" in report.sources
    assert "usage" in report.sources
    assert "state" not in report.sources, "legacy 'state' source must be retired in DH-16"
    assert "runs" not in report.sources, "legacy 'runs' source must be retired in DH-16"


def test_legacy_files_on_disk_are_strictly_ignored(tmp_path: Path):
    """Verify that if state.json or orchestrator.sqlite3 exist, they are ignored in favor of control store."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")

    # Create dummy legacy files that would have caused items to appear if read
    legacy_state = tmp_path / "state.json"
    legacy_state.write_text(
        json.dumps({
            "tasks": {
                "legacy-ghost-task": {
                    "id": "legacy-ghost-task",
                    "status": "RUNNING",
                    "metadata": {"title": "Ghost Task from legacy file"},
                }
            }
        }),
        encoding="utf-8",
    )

    control_db = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=control_db)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-canon', 'darkfac', 'canonical-task', 'v1', 'hf05_sqlite', 'autonomous', 'active', 'd', 'c', '2026-09-23T10:00:00Z', '2026-09-23T10:00:00Z')"
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code, actual_cost, evidence_refs, created_at, updated_at)"
            " VALUES ('run-canon', 'canonical-task', 'p1', 'development', 0, 'succeeded', 'worker', NULL, 0.15, '[]', '2026-09-23T10:00:00Z', '2026-09-23T10:00:00Z')"
        )
        conn.commit()

    service = HubService(
        data_dir=data_dir,
        roadmap_root=Path.cwd(),
        usage_dir=tmp_path / "usage",
        control_db_path=control_db,
        control_database_url="",
    )

    report = service.get_task_dashboard()

    task_ids = [item.task_id for item in report.queue]
    assert "canonical-task" in task_ids
    assert "legacy-ghost-task" not in task_ids, "legacy state.json tasks must never leak into the queue"
    assert report.queued_count == 1
