"""USR-42: the DarkHub task board reads the canonical HF-05 control store."""

from __future__ import annotations

import json
from pathlib import Path

from core.workflow.control_store import SQLiteControlStore
from core.workflow.job_board import fold_job_rows, read_job_board, read_sqlite_job_board
from hub.backend.service import HubService

NOW = "2026-09-23T10:00:00+00:00"


def _seed_control_db(path: Path) -> None:
    store = SQLiteControlStore(db_path=path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-1', 'darkfac', 'USR-42', 'v1', 'hf05_sqlite', 'autonomous', 'active', 'd', 'c', ?, ?)",
            (NOW, NOW),
        )
        jobs = [
            ("grill", 0, "succeeded", "grill_engine", None, 0.10, "2026-09-23T10:00:00+00:00"),
            ("planning", 0, "succeeded", "planner", None, 0.40, "2026-09-23T10:05:00+00:00"),
            ("development", 0, "waiting_human", "developer", "missing_secret", 1.25, "2026-09-23T10:09:00+00:00"),
        ]
        for stage, iteration, status, role, cause, cost, updated in jobs:
            conn.execute(
                "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code,"
                " actual_cost, evidence_refs, created_at, updated_at)"
                " VALUES ('run-1', 'USR-42', 'p1', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stage, iteration, status, role, cause, cost, json.dumps([f"artifact://{stage}"]), updated, updated),
            )
        conn.commit()


def _make_service(tmp_path: Path, control_db: Path) -> HubService:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=Path.cwd(),
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=control_db,
        control_database_url="",
    )


def test_fold_keeps_latest_stage_and_sums_cost() -> None:
    rows = [
        {"run_id": "r", "ticket_id": "T", "stage": "validation", "status": "failed", "actual_cost": 0.5,
         "cause_code": "tests_red", "updated_at": "2", "evidence_refs": "[]"},
        {"run_id": "r", "ticket_id": "T", "stage": "development", "status": "succeeded", "actual_cost": 1.0,
         "updated_at": "1", "evidence_refs": None},
    ]
    [entry] = fold_job_rows(rows)
    assert entry.stage == "validation"
    assert entry.status == "failed"
    assert entry.needs_attention
    assert entry.total_cost_usd == 1.5
    assert entry.stages_seen == ["development", "validation"]


def test_sqlite_board_reads_without_writing(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    _seed_control_db(db)
    before = db.stat().st_mtime_ns

    snapshot = read_sqlite_job_board(db)

    assert snapshot.source == "ok"
    assert snapshot.backend == "sqlite"
    [entry] = snapshot.entries
    assert (entry.ticket_id, entry.stage, entry.status) == ("USR-42", "development", "waiting_human")
    assert entry.total_cost_usd == 1.75
    assert db.stat().st_mtime_ns == before


def test_missing_sqlite_is_not_created(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "control.db"
    snapshot = read_job_board(db, database_url="")
    assert snapshot.source == "missing"
    assert not db.parent.exists()


def test_postgres_failure_never_leaks_credentials(tmp_path: Path) -> None:
    snapshot = read_job_board(
        tmp_path / "control.db",
        database_url="postgresql://darkfac:super-secret-pw@127.0.0.1:1/darkfac",
    )
    assert snapshot.backend == "postgres"
    assert snapshot.source == "error"
    assert "super-secret-pw" not in json.dumps(snapshot.model_dump())


def test_task_dashboard_surfaces_control_jobs_first_when_waiting_human(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    _seed_control_db(db)
    service = _make_service(tmp_path, db)

    report = service.get_task_dashboard()

    assert report.sources["control"] == "sqlite:ok"
    first = report.queue[0]
    assert first.task_id == "USR-42"
    assert first.status == "WAITING_HUMAN"
    assert first.stage == "development"
    assert first.run_id == "run-1"
    assert first.cost_usd == 1.75
    assert first.exceptions and "missing_secret" in first.exceptions[0]
    assert {item.label for item in first.evidence} >= {"projeto", "papel", "etapas", "evidência"}
    assert report.exception_count == 1
