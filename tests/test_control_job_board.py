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


def _seed_intake_command(
    path: Path,
    *,
    run_id: str,
    payload: str,
    demand_id: str,
    channel: str = "cli",
    external_id: str = "ext-1",
    project_id: str = "darkfac",
    committed_at: str = NOW,
) -> None:
    """Seed one raw intake_commands row, as the intake pipeline would have written it."""
    store = SQLiteControlStore(db_path=path)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO intake_commands (channel, external_id, project_id, payload_digest, payload, mode,"
            " policy_ref, demand_id, demand_version, run_id, initial_job_id, committed_at)"
            " VALUES (?, ?, ?, 'digest', ?, 'autonomous', 'policy-1', ?, 'v1', ?, 'job-1', ?)",
            (channel, external_id, project_id, payload, demand_id, run_id, committed_at),
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


def test_job_board_extracts_title_from_intake_payload(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    _seed_control_db(db)
    _seed_intake_command(
        db,
        run_id="run-1",
        demand_id="USR-42",
        payload=json.dumps(
            {
                "title": "Painel de vendas Q4",
                "problem": "p",
                "journey": "j",
                "non_goals": "n",
                "criteria": "c",
            }
        ),
    )

    snapshot = read_sqlite_job_board(db)

    [entry] = snapshot.entries
    assert entry.title == "Painel de vendas Q4"


def test_job_board_falls_back_to_none_on_malformed_intake_payload(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    _seed_control_db(db)
    _seed_intake_command(db, run_id="run-1", demand_id="USR-42", payload="not-json")

    snapshot = read_sqlite_job_board(db)

    [entry] = snapshot.entries
    assert entry.title is None


def test_task_dashboard_title_prefers_intake_payload_over_demand_id(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    _seed_control_db(db)
    _seed_intake_command(
        db,
        run_id="run-1",
        demand_id="USR-42",
        payload=json.dumps(
            {
                "title": "Painel de vendas Q4",
                "problem": "p",
                "journey": "j",
                "non_goals": "n",
                "criteria": "c",
            }
        ),
    )
    service = _make_service(tmp_path, db)

    report = service.get_task_dashboard()

    first = report.queue[0]
    assert first.title == "Painel de vendas Q4"


def test_task_dashboard_uses_demand_id_as_task_id_when_ticket_equals_project(tmp_path: Path) -> None:
    """Real cloud rows carry ticket_id = project_id ("darkfac"); demand_id is the meaningful id."""
    db = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at)"
            " VALUES ('run-2', 'darkfac', 'dem-331d68d8836f', 'v1', 'hf05_sqlite', 'autonomous', 'active',"
            " 'd', 'c', ?, ?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, actual_cost,"
            " evidence_refs, created_at, updated_at)"
            " VALUES ('run-2', 'darkfac', 'p1', 'development', 0, 'running', 'developer', 0.10, '[]', ?, ?)",
            (NOW, NOW),
        )
        conn.commit()
    service = _make_service(tmp_path, db)

    report = service.get_task_dashboard()

    entry = next(item for item in report.queue if item.run_id == "run-2")
    assert entry.task_id == "dem-331d68d8836f"
