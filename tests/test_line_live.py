"""USR-138: read-only live line projection, Hub endpoint, SSE stream and /live page."""

from __future__ import annotations

import gc
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.line.bindings import LINE_STAGES
from core.line.local_progress import PHASES as LOCAL_PHASES
from core.workflow.line_live import LineLiveSnapshot, LiveRun, read_line_live
from hub.backend import api as hub_api
from hub.backend import service as hub_service_module
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService
from tests.fixtures.line_live_seed import _emit_local_run, seed_line_live_demo, seed_local_runs

NOW = datetime(2026, 10, 4, 15, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_line_live_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every seed here is anchored at NOW; the real clock ages the runs out of the board.

    `RECENT_RUN_WINDOW` is 7 days, so from 2026-10-11 15:00 UTC any test that reads the board through the
    HTTP surface without passing `now=` stopped seeing its runs (date time bomb that turned every PR red).
    """
    monkeypatch.setattr("core.workflow.line_live._utc_now", lambda: NOW)


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db = tmp_path / "control.db"
    ids = seed_line_live_demo(db, NOW)
    return db, ids


def _read(db: Path, **kwargs: object) -> LineLiveSnapshot:
    return read_line_live(db, database_url="", now=kwargs.pop("now", NOW), **kwargs)  # type: ignore[arg-type]


def _run(snapshot: LineLiveSnapshot, run_id: str) -> LiveRun:
    return next(run for run in snapshot.runs if run.run_id == run_id)


def _execute(db: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(sql, params)
        conn.commit()


# ---------------------------------------------------------------- projection


def test_stage_order_marks_unreached_stages_and_keeps_labels(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    snapshot = _read(db)

    assert snapshot.source.model_dump() == {"backend": "sqlite", "status": "ok"}
    assert snapshot.stage_order == list(LINE_STAGES)
    assert snapshot.stage_labels["validation"] == "Validação"
    assert snapshot.stage_labels["target_journey"] == "Jornada-alvo"

    dev = _run(snapshot, ids["dev_live"])
    assert [stage.stage for stage in dev.stages] == list(LINE_STAGES)
    assert [stage.status for stage in dev.stages] == [
        "succeeded",
        "succeeded",
        "running",
        "not_reached",
        "not_reached",
        "not_reached",
        "not_reached",
        "not_reached",
    ]
    assert dev.stages[3].label == "Validação"
    assert dev.stages[3].history == []


def test_extra_stage_with_a_job_is_appended_after_the_line(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    _execute(
        db,
        "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, created_at, updated_at)"
        " VALUES (?, 'darkfac', '1.0', 'target_journey', 0, 'succeeded', 'journey', ?, ?)",
        (ids["done"], NOW.isoformat(), NOW.isoformat()),
    )
    run = _run(_read(db), ids["done"])
    assert [stage.stage for stage in run.stages] == [*LINE_STAGES, "target_journey"]
    assert run.stages[-1].label == "Jornada-alvo"
    # runs without that job do not grow a phantom stage
    other = _run(_read(db), ids["dev_live"])
    assert [stage.stage for stage in other.stages] == list(LINE_STAGES)


def test_stage_status_comes_from_the_highest_iteration(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    validation = _run(_read(db), ids["val_retry"]).stages[3]

    assert validation.status == "running"
    assert validation.attempts == 2
    assert validation.retry_count == 1
    assert [(item.iteration, item.status) for item in validation.history] == [(0, "retry"), (1, "running")]
    assert validation.history[0].cause_code == "tests_red"
    assert validation.cost_usd == pytest.approx(0.09)
    assert validation.worker == "cloud-worker-2"
    assert validation.route == "openai:gpt-5-codex"


def test_run_states_attention_and_stalled(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    snapshot = _read(db)
    states = {key: _run(snapshot, run_id).state for key, run_id in ids.items()}
    assert states == {
        "dev_live": "running",
        "val_retry": "running",
        "grill_wait": "attention",
        "stalled": "running",
        "done": "succeeded",
        "done_old": "succeeded",
        "failed": "failed",
        "queued": "queued",
    }

    waiting = _run(snapshot, ids["grill_wait"])
    assert waiting.current_stage == "grill"
    assert waiting.attention is not None
    assert waiting.attention.stage == "grill"
    assert waiting.attention.cause_code == "grill_pending"
    assert waiting.attention.diagnostic == "Alinhamento Grill pendente com o Owner"
    assert waiting.attention.since is not None

    assert _run(snapshot, ids["dev_live"]).attention is None
    assert _run(snapshot, ids["dev_live"]).stalled is False
    stalled = _run(snapshot, ids["stalled"])
    assert stalled.stalled is True
    assert stalled.stalled_reason is not None and "timeout" in stalled.stalled_reason
    assert [run.run_id for run in snapshot.runs if run.stalled] == [ids["stalled"]]

    failed = _run(snapshot, ids["failed"])
    assert failed.current_stage == "integration"
    assert failed.current_stage_status == "failed"
    assert failed.stages[5].cause_code == "merge_conflict"
    assert failed.completed_at is not None


def test_stalled_detects_expired_lease_idle_running_and_old_queue(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    _execute(db, "UPDATE claims SET expires_at = ? WHERE run_id = ?", ((NOW - timedelta(minutes=3)).isoformat(), ids["dev_live"]))
    reason = _run(_read(db), ids["dev_live"]).stalled_reason
    assert reason is not None and "lease" in reason

    _execute(db, "UPDATE claims SET status = 'released' WHERE run_id = ?", (ids["val_retry"],))
    _execute(
        db,
        "UPDATE jobs SET updated_at = ? WHERE run_id = ? AND stage = 'validation' AND iteration = 1",
        ((NOW - timedelta(minutes=25)).isoformat(), ids["val_retry"]),
    )
    reason = _run(_read(db), ids["val_retry"]).stalled_reason
    assert reason is not None and "sem atividade há 25 min" in reason

    _execute(db, "UPDATE jobs SET updated_at = ? WHERE run_id = ?", ((NOW - timedelta(minutes=45)).isoformat(), ids["queued"]))
    queued = _run(_read(db), ids["queued"])
    assert queued.state == "queued"
    assert queued.stalled_reason == "na fila há 45 min"


def test_long_durations_read_in_days() -> None:
    from core.workflow.line_live import _fmt_duration

    assert _fmt_duration(45) == "45 s"
    assert _fmt_duration(25 * 60) == "25 min"
    assert _fmt_duration(26 * 3600 + 5 * 60) == "26 h 05 min"
    assert _fmt_duration(360 * 3600 + 22 * 60) == "15 d"  # seen in production as "360 h 22 min"
    assert _fmt_duration(50 * 3600) == "2 d 2 h"


def test_stalled_flags_active_run_with_no_successor_scheduled(seeded: tuple[Path, dict[str, str]]) -> None:
    # Production shape (HF-03-08): Grill succeeded, run still active, no next job for days.
    db, ids = seeded
    finished = (NOW - timedelta(hours=2)).isoformat()
    _execute(
        db,
        "UPDATE jobs SET status = 'succeeded', started_at = ?, finished_at = ?, updated_at = ? WHERE run_id = ?",
        (finished, finished, finished, ids["queued"]),
    )
    run = _run(_read(db), ids["queued"])
    assert run.run_status == "active"
    assert run.stalled
    assert run.stalled_reason is not None and run.stalled_reason.startswith("nenhuma etapa agendada há")


def test_running_stage_duration_and_run_age_use_the_given_now(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    snapshot = _read(db)
    dev = _run(snapshot, ids["dev_live"])
    development = dev.stages[2]

    assert development.duration_seconds == pytest.approx(12 * 60)
    assert dev.age_seconds == pytest.approx(50 * 60)
    assert dev.cycle_seconds is None
    assert development.worker == "cloud-worker-1"
    assert development.route == "anthropic:claude-sonnet-5-5"
    assert development.timeout_seconds == 3600
    assert development.lease_expires_at is not None
    assert dev.title == "Esteira ao vivo no DarkHub"
    assert dev.ticket_id == dev.demand_id == "USR-138"  # ticket_id == project_id falls back to demand_id

    later = _read(db, now=NOW + timedelta(minutes=2))
    assert _run(later, ids["dev_live"]).stages[2].duration_seconds == pytest.approx(14 * 60)

    done = _run(snapshot, ids["done"])
    assert done.age_seconds is None
    assert done.cycle_seconds == pytest.approx(120 * 60)
    assert done.stages[0].duration_seconds == pytest.approx(15 * 60)
    assert done.total_cost_usd == pytest.approx(8 * 0.55)
    assert done.iterations_total == 8


def test_runs_are_ordered_attention_running_queued_then_terminal(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    snapshot = _read(db)
    assert [run.run_id for run in snapshot.runs] == [
        ids["grill_wait"],
        ids["stalled"],
        ids["val_retry"],
        ids["dev_live"],
        ids["queued"],
        ids["done"],
        ids["failed"],
        ids["done_old"],
    ]


def test_ticket_id_equal_to_project_id_shows_the_demand_id(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    run = _run(_read(db), ids["grill_wait"])
    assert run.project_id == "darkfac-canary"
    assert run.ticket_id == run.demand_id == "CAN-07"


def test_titles_mapping_is_the_fallback_when_intake_has_no_title(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    _execute(db, "DELETE FROM intake_commands WHERE run_id = ?", (ids["dev_live"],))
    run = _run(_read(db, titles={"USR-138": "Título dos demands"}), ids["dev_live"])
    assert run.title == "Título dos demands"
    assert _run(_read(db), ids["dev_live"]).title == "USR-138"
    # the intake title still wins when present
    assert _run(_read(db, titles={"USR-139": "outro"}), ids["val_retry"]).title == "Backoff do pump de linha"


def test_version_is_stable_changes_with_data_but_not_with_the_clock(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    first = _read(db)
    second = _read(db)
    assert first.version == second.version
    assert len(first.version) == 16

    later = _read(db, now=NOW + timedelta(minutes=2))
    assert later.generated_at != first.generated_at
    assert later.version == first.version

    _execute(
        db,
        "UPDATE jobs SET status = 'succeeded', finished_at = ?, updated_at = ? WHERE run_id = ? AND stage = 'development'",
        (NOW.isoformat(), NOW.isoformat(), ids["dev_live"]),
    )
    changed = _read(db)
    assert changed.version != first.version


def test_events_are_newest_first_capped_and_in_portuguese(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    snapshot = _read(db)
    stamps = [event.at for event in snapshot.events]
    assert stamps == sorted(stamps, reverse=True)
    assert len(snapshot.events) <= 60

    messages = {(event.run_id, event.kind, event.message) for event in snapshot.events}
    assert (ids["dev_live"], "stage_started", "entrou em Desenvolvimento") in messages
    assert (
        ids["grill_wait"],
        "waiting_human",
        "aguarda você: Alinhamento Grill pendente com o Owner",
    ) in messages
    assert any(
        event.run_id == ids["dev_live"] and event.message == "concluiu Planejamento em 14 min"
        for event in snapshot.events
    )
    assert any(event.run_id == ids["val_retry"] and event.kind == "retry" for event in snapshot.events)
    assert any(event.run_id == ids["failed"] and event.kind == "stage_failed" for event in snapshot.events)
    assert any(event.run_id == ids["done"] and event.kind == "run_completed" for event in snapshot.events)
    assert {event.kind for event in snapshot.events} <= {
        "stage_started",
        "stage_succeeded",
        "stage_failed",
        "waiting_human",
        "retry",
        "run_completed",
    }


def test_kpis_include_throughput_with_zeros_and_cycle_percentiles(seeded: tuple[Path, dict[str, str]]) -> None:
    db, _ = seeded
    kpis = _read(db).kpis

    assert kpis.active_runs == 5
    assert kpis.attention == 1
    assert kpis.running_stages == 3
    assert kpis.completed_24h == 1
    assert kpis.failed_24h == 1
    assert kpis.median_cycle_seconds_7d == pytest.approx(8100.0)
    assert kpis.p85_cycle_seconds_7d == pytest.approx(8730.0)

    assert [day.date for day in kpis.throughput_7d] == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
    ]
    assert [day.completed for day in kpis.throughput_7d] == [0, 0, 0, 0, 0, 1, 1]

    with closing(sqlite3.connect(db)) as conn:
        expected = conn.execute(
            "SELECT SUM(actual_cost) FROM jobs WHERE COALESCE(finished_at, updated_at) >= ?",
            ((NOW - timedelta(days=1)).isoformat(),),
        ).fetchone()[0]
    assert kpis.cost_24h_usd == pytest.approx(expected, abs=1e-3)


class _Status(str, Enum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    COMPLETED = "completed"


def _ticket(ticket_id: str, project: str, status: str, horizon: str = "now", title: str = "") -> dict[str, object]:
    return {
        "id": ticket_id,
        "project_id": project,
        "title": title or f"Ticket {ticket_id}",
        "status": _Status(status),
        "horizon": horizon,
        "updated_at": NOW - timedelta(hours=1),
    }


def test_backlog_and_off_line_come_from_tickets(seeded: tuple[Path, dict[str, str]]) -> None:
    db, _ = seeded
    tickets = [
        _ticket("USR-200", "darkfac", "planned", "later"),
        _ticket("USR-201", "darkfac", "planned", "now"),
        _ticket("USR-202", "darkfac", "planned", "next"),
        _ticket("USR-203", "darkfac", "planned", "now"),
        _ticket("USR-204", "darkfac", "planned", "now"),
        _ticket("USR-205", "darkfac", "planned", "now"),
        _ticket("USR-206", "darkfac", "planned", "now"),
        _ticket("USR-138", "darkfac", "implementing"),  # has an active run -> on the line
        _ticket("USR-150", "darkfac", "implementing"),  # no run -> off line
        _ticket("USR-100", "darkfac", "completed"),
        _ticket("CAN-20", "darkfac-canary", "planned", "next"),
    ]
    snapshot = _read(db, tickets=tickets)  # type: ignore[arg-type]

    darkfac = next(project for project in snapshot.backlog if project.project_id == "darkfac")
    assert (darkfac.planned, darkfac.implementing, darkfac.completed, darkfac.total) == (7, 2, 1, 10)
    assert len(darkfac.next) == 5
    assert [item.horizon for item in darkfac.next] == ["now"] * 5
    assert [item.id for item in darkfac.next] == ["USR-201", "USR-203", "USR-204", "USR-205", "USR-206"]
    canary = next(project for project in snapshot.backlog if project.project_id == "darkfac-canary")
    assert canary.planned == 1 and canary.next[0].id == "CAN-20"
    assert snapshot.kpis.planned_backlog == 8

    assert [ticket.id for ticket in snapshot.off_line] == ["USR-150"]
    assert snapshot.off_line[0].status == "implementing"
    assert snapshot.off_line[0].updated_at is not None


def test_missing_source_reports_missing_and_never_creates_files(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "control.db"
    snapshot = read_line_live(db, database_url="", now=NOW)
    assert snapshot.source.model_dump() == {"backend": "sqlite", "status": "missing"}
    assert snapshot.runs == [] and snapshot.events == []
    assert snapshot.stage_order == list(LINE_STAGES)
    assert not db.parent.exists()

    nothing = read_line_live(None, database_url="", now=NOW)
    assert nothing.source.model_dump() == {"backend": "none", "status": "missing"}


def test_corrupted_sqlite_degrades_to_error_without_raising(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    db.write_bytes(b"this is definitely not a sqlite database" * 50)
    snapshot = read_line_live(db, database_url="", now=NOW)
    assert snapshot.source.model_dump() == {"backend": "sqlite", "status": "error"}
    assert snapshot.warnings and snapshot.runs == []


def test_postgres_failure_never_leaks_credentials(tmp_path: Path) -> None:
    snapshot = read_line_live(
        tmp_path / "control.db",
        database_url="postgresql://darkfac:super-secret-pw@127.0.0.1:1/darkfac",
        now=NOW,
    )
    assert snapshot.source.backend == "postgres"
    assert snapshot.source.status == "error"
    assert "super-secret-pw" not in snapshot.model_dump_json()


def test_reading_does_not_write_the_store(seeded: tuple[Path, dict[str, str]]) -> None:
    db, _ = seeded
    # The seeding connections are closed (and may checkpoint) only when collected; settle them first
    # so a late write from the fixture is not blamed on the reader (flaked on the Ubuntu CI by mtime).
    gc.collect()
    wal = db.with_name(db.name + "-wal")
    wal_before = wal.read_bytes() if wal.is_file() else b""
    before = db.read_bytes()
    _read(db)
    gc.collect()
    assert db.read_bytes() == before
    # A read-only WAL reader may create an empty -wal/-shm pair (normal SQLite behaviour), never frames.
    assert (wal.read_bytes() if wal.is_file() else b"") == wal_before


def test_json_contract_field_names_are_frozen(seeded: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded
    data = _read(db).model_dump(mode="json")
    assert set(data) == {
        "generated_at", "version", "source", "warnings", "stage_order", "stage_labels", "runs",
        "events", "kpis", "backlog", "off_line", "fleet", "executable_queue", "executions",
    }
    run = next(item for item in data["runs"] if item["run_id"] == ids["dev_live"])
    assert set(run) == {
        "run_id", "ticket_id", "demand_id", "project_id", "title", "mode", "run_status", "state",
        "current_stage", "current_stage_status", "attention", "stalled", "stalled_reason", "created_at",
        "updated_at", "completed_at", "age_seconds", "cycle_seconds", "total_cost_usd", "iterations_total",
        "stages", "host_id", "reservation_state", "idle_or_blocked_reason", "fixture",
    }
    stage = run["stages"][2]
    assert set(stage) == {
        "stage", "label", "status", "attempts", "retry_count", "max_retries", "started_at", "finished_at",
        "duration_seconds", "cost_usd", "cause_code", "diagnostic", "role", "worker", "route",
        "lease_expires_at", "timeout_seconds", "evidence_refs", "history",
    }
    assert set(stage["history"][0]) == {
        "iteration", "status", "started_at", "finished_at", "duration_seconds", "cost_usd", "cause_code",
        "updated_at",
    }
    assert set(data["events"][0]) == {"at", "run_id", "ticket_id", "title", "project_id", "stage", "kind", "message"}
    assert set(data["kpis"]) == {
        "active_runs", "attention", "running_stages", "completed_24h", "failed_24h",
        "median_cycle_seconds_7d", "p85_cycle_seconds_7d", "cost_24h_usd", "throughput_7d", "planned_backlog",
    }
    waiting = next(item for item in data["runs"] if item["run_id"] == ids["grill_wait"])
    assert set(waiting["attention"]) == {"stage", "cause_code", "diagnostic", "since"}


# ---------------------------------------------------------------- Hub service


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


def test_hub_service_shares_one_read_per_cache_window(
    tmp_path: Path, seeded: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _ = seeded
    service = _make_service(tmp_path, db)
    reads: list[int] = []
    original = hub_service_module.read_line_live

    def counting(*args: object, **kwargs: object) -> LineLiveSnapshot:
        reads.append(1)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(hub_service_module, "read_line_live", counting)
    first = service.get_line_live()
    second = service.get_line_live()
    assert first is second
    assert len(reads) == 1
    assert first.source.status == "ok"

    monkeypatch.setattr(hub_service_module, "LINE_LIVE_CACHE_TTL_SECONDS", 0.0)
    third = service.get_line_live()
    assert third is not first
    assert len(reads) == 2


def test_hub_service_survives_unavailable_demands(
    tmp_path: Path, seeded: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _ = seeded
    service = _make_service(tmp_path, db)

    def boom(*_: object, **__: object) -> list[object]:
        raise RuntimeError("demands store offline")

    monkeypatch.setattr(service.demands_service, "list_tickets", boom)
    snapshot = service.get_line_live()
    assert snapshot.source.status == "ok"
    assert snapshot.runs
    assert any("demands indisponíveis" in warning for warning in snapshot.warnings)


def test_hub_service_preserves_queue_readiness_and_blockers(
    tmp_path: Path, seeded: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.demands.models import UserTicket

    db, _ = seeded
    service = _make_service(tmp_path, db)
    ready = UserTicket(
        id="USR-ready", title="Ready ticket",
        problem_statement="The live queue loses the specified ticket fields before evaluating admission.",
        non_goals=["Do not change admission policy or execute any jobs."],
        acceptance_criteria=["The live queue reports this fully specified ticket as eligible."],
    )
    postponed = ready.model_copy(update={"id": "USR-held", "tags": ["hold"]})
    dependent = ready.model_copy(update={"id": "USR-dependent", "dependencies": ["USR-missing"]})
    monkeypatch.setattr(service.demands_service, "list_tickets", lambda: [ready, postponed, dependent])

    candidates = {c.ticket_id: c for c in service.get_line_live().executable_queue.candidates}

    assert candidates[ready.id].eligible
    assert candidates[postponed.id].reason == "intentional_postponement"
    assert candidates[dependent.id].reason == "waiting_dependencies:USR-missing"


# ---------------------------------------------------------------- HTTP surface


@pytest.fixture()
def client(
    tmp_path: Path, seeded: tuple[Path, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    db, _ = seeded
    # The seed is anchored at NOW; the real clock would age finished runs out of the board over time.
    monkeypatch.setattr("core.workflow.line_live._utc_now", lambda: NOW)
    service = _make_service(tmp_path, db)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_api_line_live_returns_the_validated_snapshot(client: TestClient) -> None:
    response = client.get("/api/line/live")
    assert response.status_code == 200
    snapshot = LineLiveSnapshot.model_validate(response.json())
    assert snapshot.source.status == "ok"
    assert len(snapshot.runs) == 8


def _sse_data(text: str) -> dict[str, object]:
    lines = [line for line in text.splitlines() if line.startswith("data: ")]
    assert len(lines) == 1
    return json.loads(lines[0][len("data: "):])


def test_sse_stream_sends_retry_then_one_snapshot_event(client: TestClient) -> None:
    with client.stream("GET", "/api/line/live/stream", params={"max_events": 1}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["connection"] == "keep-alive"
        body = "".join(response.iter_text())

    assert body.startswith("retry: 5000\n\n")
    assert body.count("event: snapshot") == 1
    payload = _sse_data(body)
    snapshot = LineLiveSnapshot.model_validate(payload)
    assert body.count(f"id: {snapshot.version}\n") == 1
    assert len(snapshot.runs) == 8


def test_sse_stream_skips_first_snapshot_for_matching_last_event_id_and_sends_heartbeats(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = client.get("/api/line/live").json()["version"]
    monkeypatch.setattr(hub_api, "LINE_LIVE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(hub_api, "LINE_LIVE_PING_SECONDS", 0.05)
    monkeypatch.setattr(hub_api, "LINE_LIVE_MAX_STREAM_SECONDS", 0.4)
    with client.stream("GET", "/api/line/live/stream", headers={"Last-Event-ID": version}) as response:
        body = "".join(response.iter_text())

    assert body.startswith("retry: 5000\n\n")
    assert "event: snapshot" not in body
    # Heartbeats are named events (EventSource never surfaces SSE comments to the page), sent at once
    # on a resumed stream and then periodically, carrying the fresh read time of an unchanged board.
    assert body.count("event: heartbeat") >= 2
    heartbeat = json.loads(body.split("event: heartbeat\ndata: ", 1)[1].split("\n", 1)[0])
    assert heartbeat["version"] == version
    assert heartbeat["generated_at"]


def test_sse_stream_answers_a_stale_last_event_id_with_the_current_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_service_module, "LINE_LIVE_CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(hub_api, "LINE_LIVE_POLL_SECONDS", 0.02)
    stale = "0000000000000000"
    with client.stream(
        "GET", "/api/line/live/stream", params={"max_events": 1}, headers={"Last-Event-ID": stale}
    ) as response:
        body = "".join(response.iter_text())
    assert "event: snapshot" in body  # a stale Last-Event-ID is answered with the current snapshot


def test_live_page_is_served_without_caching(client: TestClient) -> None:
    for path in ("/live", "/live/"):
        response = client.get(path)
        assert response.status_code == 200
        assert "line-live-board" in response.text
        assert "no-store" in response.headers["cache-control"]


# ---------------------------------------------------------------- run_ticket runs (USR-140)


@pytest.fixture()
def seeded_local(seeded: tuple[Path, dict[str, str]]) -> tuple[Path, dict[str, str]]:
    db, ids = seeded
    return db, {**ids, **seed_local_runs(db, NOW)}


def test_local_runs_are_projected_with_their_own_phases_worker_and_harness(
    seeded_local: tuple[Path, dict[str, str]],
) -> None:
    db, ids = seeded_local
    snapshot = _read(db)

    assert len(snapshot.runs) == 8 + 4
    live = _run(snapshot, ids["local_live"])
    assert live.mode == "run_ticket"
    assert (live.ticket_id, live.demand_id, live.project_id) == ("USR-140", "USR-140", "darkfac")
    assert live.title == "Esteira ao vivo: run_ticket local"
    assert live.state == "running" and live.current_stage == "agent" and live.current_stage_status == "running"
    assert [stage.stage for stage in live.stages] == list(LOCAL_PHASES)  # no autonomous-line stages
    assert [stage.status for stage in live.stages] == [
        "succeeded", "succeeded", "running", "not_reached", "not_reached",
        "not_reached", "not_reached", "not_reached", "not_reached",
    ]
    agent = live.stages[2]
    assert (agent.worker, agent.route, agent.role) == ("DESKTOP-TEST", "claude", "run_ticket")
    assert agent.evidence_refs == ["desenvolvimento via claude"]
    assert agent.duration_seconds == pytest.approx(12 * 60)
    assert live.stages[1].duration_seconds == pytest.approx(2 * 60)
    assert not live.stalled

    assert snapshot.stage_order == list(LINE_STAGES)  # the line's own columns are untouched
    assert snapshot.stage_labels["gate"] == "Portão" and snapshot.stage_labels["pr"] == "PR"
    assert "entrou em Agente" in [e.message for e in snapshot.events if e.run_id == ids["local_live"]]


def test_local_run_outcomes_done_gate_red_and_abandoned(seeded_local: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded_local
    snapshot = _read(db)

    done = _run(snapshot, ids["local_done"])
    assert done.state == "succeeded" and done.run_status == "completed"
    assert done.cycle_seconds == pytest.approx(38 * 60)
    assert [stage.status for stage in done.stages] == ["succeeded"] * 8 + ["cancelled"]
    assert done.stages[-1].stage == "deploy"  # skipped, not a green deploy

    red = _run(snapshot, ids["local_gate_red"])
    assert red.state == "failed" and red.run_status == "failed"
    gate = next(stage for stage in red.stages if stage.stage == "gate")
    assert gate.status == "failed" and gate.cause_code == "gate_failed"
    assert [stage.status for stage in red.stages if stage.stage in {"pr", "ci", "merge"}] == ["not_reached"] * 3

    gone = _run(snapshot, ids["local_abandoned"])
    assert gone.run_status == "aborted" and gone.state == "cancelled"
    agent = next(stage for stage in gone.stages if stage.stage == "agent")
    assert agent.status == "cancelled" and agent.cause_code == "abandoned"

    assert snapshot.kpis.completed_24h >= 1 and snapshot.kpis.failed_24h >= 1


def test_a_failed_run_without_a_failed_phase_pins_the_reason_on_the_active_phase(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, NOW)
    _emit_local_run(
        db, NOW, run_id="local-USR-150-x", ticket_id="USR-150", title="t", started_min_ago=30,
        steps=[
            (0, "preflight", "succeeded", "rota", None, "claude"),
            (1, "workspace", "succeeded", "ok", None, None),
            (1, "agent", "running", "dev", None, "claude"),
        ],
        finish=(20, False, "run_ticket terminou com codigo 4"),
    )
    run = _run(_read(db), "local-USR-150-x")
    agent = run.stages[2]
    assert run.state == "failed"
    assert agent.status == "failed" and agent.cause_code == "delivery_failed"
    assert agent.evidence_refs == ["run_ticket terminou com codigo 4"]


def test_a_silent_agent_is_not_stalled_until_its_phase_timeout(seeded_local: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded_local
    # 52 min without events: the autonomous line would flag it, a local run publishes only on transitions
    assert not _run(_read(db, now=NOW + timedelta(minutes=40)), ids["local_live"]).stalled
    late = _run(_read(db, now=NOW + timedelta(hours=3)), ids["local_live"])
    assert late.stalled and late.stalled_reason is not None and "timeout" in late.stalled_reason


def test_a_local_run_takes_its_ticket_out_of_off_line(seeded_local: tuple[Path, dict[str, str]]) -> None:
    db, _ = seeded_local
    tickets = [
        {"id": "USR-140", "project_id": "darkfac", "title": "Esteira", "status": "implementing"},
        {"id": "USR-199", "project_id": "darkfac", "title": "Outro", "status": "implementing"},
    ]
    snapshot = _read(db, tickets=tickets)
    assert [item.id for item in snapshot.off_line] == ["USR-199"]


def test_a_store_without_the_progress_table_still_reads_and_a_missing_title_falls_back(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, NOW)
    snapshot = _read(db)
    assert snapshot.source.status == "ok" and len(snapshot.runs) == 8
    assert all(run.mode != "run_ticket" for run in snapshot.runs)

    _emit_local_run(
        db, NOW, run_id="local-USR-151-x", ticket_id="USR-151", title="", started_min_ago=5,
        steps=[(0, "preflight", "running", "x", None, None)],
    )
    run = _run(_read(db, titles={"USR-151": "Titulo do ledger"}), "local-USR-151-x")
    assert run.title == "Titulo do ledger" and run.state == "running"


def test_api_line_live_includes_local_runs(tmp_path: Path, seeded_local: tuple[Path, dict[str, str]]) -> None:
    db, ids = seeded_local
    service = _make_service(tmp_path, db)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        response = TestClient(app).get("/api/line/live")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    snapshot = LineLiveSnapshot.model_validate(response.json())
    live = _run(snapshot, ids["local_live"])
    assert live.mode == "run_ticket" and live.current_stage == "agent"
    assert live.stages[2].worker == "DESKTOP-TEST"


def test_normalize_backfills_resumed_local_stages_and_skipped_middle_phases(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, NOW)
    _emit_local_run(
        db, NOW, run_id="local-res-run", ticket_id="USR-611", title="Resumed running",
        started_min_ago=5, steps=[(1, "gate", "running", "validando", None, "claude")],
    )
    _emit_local_run(
        db, NOW, run_id="local-res-fail", ticket_id="USR-612", title="Resumed failed",
        started_min_ago=8, steps=[(1, "gate", "failed", "falhou no portao", "gate_failed", "claude")],
        finish=(2, False, "gate_failed"),
    )
    _emit_local_run(
        db, NOW, run_id="local-skip-mid", ticket_id="USR-613", title="Skipped middle",
        started_min_ago=12,
        steps=[
            (1, "preflight", "succeeded", "ok", None, "claude"),
            (2, "workspace", "succeeded", "ok", None, "claude"),
            (4, "pr", "succeeded", "pr aberto", None, "claude"),
            (6, "merge", "succeeded", "merge ok", None, "claude"),
        ],
        finish=(7, True, None),
    )

    snap = _read(db)

    resumed = _run(snap, "local-res-run")
    res_map = {st.stage: st.status for st in resumed.stages}
    assert resumed.current_stage == "gate"
    assert resumed.current_stage_status == "running"
    assert [res_map[p] for p in LOCAL_PHASES] == [
        "succeeded", "succeeded", "succeeded", "running",
        "not_reached", "not_reached", "not_reached", "not_reached", "not_reached",
    ]

    failed = _run(snap, "local-res-fail")
    fail_map = {st.stage: st.status for st in failed.stages}
    assert failed.state == "failed"
    assert [fail_map[p] for p in LOCAL_PHASES] == [
        "succeeded", "succeeded", "succeeded", "failed",
        "not_reached", "not_reached", "not_reached", "not_reached", "not_reached",
    ]

    skipped = _run(snap, "local-skip-mid")
    skip_map = {st.stage: st.status for st in skipped.stages}
    assert skipped.state == "succeeded"
    assert [skip_map[p] for p in LOCAL_PHASES] == [
        "succeeded", "succeeded", "succeeded", "succeeded",
        "succeeded", "succeeded", "succeeded", "succeeded", "not_reached",
    ]


def test_normalize_stops_at_failed_autonomous_stage_before_retrospective_and_closes_running(
    seeded: tuple[Path, dict[str, str]],
) -> None:
    db, _ids = seeded
    created = (NOW - timedelta(minutes=20)).isoformat()
    completed = (NOW - timedelta(minutes=1)).isoformat()

    _execute(
        db,
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
        " plan_digest, config_version, created_at, updated_at, completed_at)"
        " VALUES (?, 'darkfac', 'USR-614', 'v1', 'cloud_dbos_postgres', 'autonomous', 'failed', 'd', 'c', ?, ?, ?)",
        ("run-auto-fail-retro", created, completed, completed),
    )
    for stage, st_status, offset in [
        ("grill", "running", 19),
        ("planning", "succeeded", 17),
        ("development", "failed", 15),
        ("retrospective", "succeeded", 2),
    ]:
        ts = (NOW - timedelta(minutes=offset)).isoformat()
        _execute(
            db,
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, started_at, created_at, updated_at)"
            " VALUES (?, 'USR-614', '1.0', ?, 0, ?, 'role', ?, ?, ?)",
            ("run-auto-fail-retro", stage, st_status, ts, ts, ts),
        )

    _execute(
        db,
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
        " plan_digest, config_version, created_at, updated_at, completed_at)"
        " VALUES (?, 'darkfac', 'USR-615', 'v1', 'cloud_dbos_postgres', 'autonomous', 'active', 'd', 'c', ?, ?, NULL)",
        ("run-auto-stale-running", created, (NOW - timedelta(minutes=2)).isoformat()),
    )
    for stage, offset in [("planning", 10), ("development", 2)]:
        ts = (NOW - timedelta(minutes=offset)).isoformat()
        _execute(
            db,
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, started_at, created_at, updated_at)"
            " VALUES (?, 'USR-615', '1.0', ?, 0, 'running', 'role', ?, ?, ?)",
            ("run-auto-stale-running", stage, ts, ts, ts),
        )

    _execute(
        db,
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
        " plan_digest, config_version, created_at, updated_at, completed_at)"
        " VALUES (?, 'darkfac', 'USR-616', 'v1', 'cloud_dbos_postgres', 'autonomous', 'failed', 'd', 'c', ?, ?, ?)",
        ("run-auto-leftover-running", created, completed, completed),
    )
    dev_ts = (NOW - timedelta(minutes=5)).isoformat()
    _execute(
        db,
        "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, started_at, created_at, updated_at)"
        " VALUES (?, 'USR-616', '1.0', 'development', 0, 'running', 'role', ?, ?, ?)",
        ("run-auto-leftover-running", dev_ts, dev_ts, dev_ts),
    )

    snap = _read(db)

    fail_retro = {st.stage: st.status for st in _run(snap, "run-auto-fail-retro").stages}
    assert fail_retro["grill"] == "succeeded"
    assert fail_retro["planning"] == "succeeded"
    assert fail_retro["development"] == "failed"
    assert fail_retro["validation"] == "not_reached"
    assert fail_retro["independent_review"] == "not_reached"
    assert fail_retro["integration"] == "not_reached"
    assert fail_retro["build_deploy"] == "not_reached"
    assert fail_retro["retrospective"] == "succeeded"

    stale = _run(snap, "run-auto-stale-running")
    stale_map = {st.stage: st.status for st in stale.stages}
    assert stale.current_stage == "development"
    assert stale_map["grill"] == "succeeded"
    assert stale_map["planning"] == "succeeded"
    assert stale_map["development"] == "running"

    leftover = {st.stage: st.status for st in _run(snap, "run-auto-leftover-running").stages}
    assert leftover["development"] == "failed"


def test_reconciles_completed_ghost_autonomous_runs_and_keeps_fresh_local_run(
    seeded: tuple[Path, dict[str, str]],
) -> None:
    db, _ids = seeded
    old_ts = (NOW - timedelta(days=10)).isoformat()
    recent_ts = (NOW - timedelta(days=2)).isoformat()

    for run_id, demand_id, ts in [
        ("run-ghost-old", "USR-343", old_ts),
        ("run-ghost-recent", "USR-344", recent_ts),
    ]:
        _execute(
            db,
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at, completed_at)"
            " VALUES (?, 'darkfac', ?, 'v1', 'cloud_dbos_postgres', 'autonomous', 'active', 'd', 'c', ?, ?, NULL)",
            (run_id, demand_id, ts, ts),
        )
        _execute(
            db,
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, cause_code, created_at, updated_at)"
            " VALUES (?, ?, '1.0', 'grill', 0, 'waiting_human', 'grill', 'grill_pending', ?, ?)",
            (run_id, demand_id, ts, ts),
        )

    _emit_local_run(
        db, NOW, run_id="local-USR-500-recent", ticket_id="USR-500", title="Closing deploy",
        started_min_ago=2, steps=[(1, "deploy", "running", "deploy em curso", None, "claude")],
    )

    tickets = [
        {"id": "USR-343", "project_id": "darkfac", "status": "completed", "updated_at": (NOW - timedelta(days=1)).isoformat()},
        {"id": "USR-344", "project_id": "darkfac", "status": "completed", "updated_at": (NOW - timedelta(hours=1)).isoformat()},
        {"id": "USR-500", "project_id": "darkfac", "status": "completed", "updated_at": (NOW - timedelta(minutes=1)).isoformat()},
    ]
    snap = _read(db, tickets=tickets)
    by_id = {r.run_id: r for r in snap.runs}

    assert "run-ghost-old" not in by_id
    recent_ghost = by_id["run-ghost-recent"]
    assert recent_ghost.state == "cancelled"
    assert recent_ghost.run_status == "cancelled"
    assert recent_ghost.stages[0].status == "cancelled"

    local_recent = by_id["local-USR-500-recent"]
    assert local_recent.state == "running"


def test_matches_fleet_reservation_by_ticket_and_detects_local_execution_stall(tmp_path: Path) -> None:
    from core.line.local_progress import SqliteSink
    from core.workflow.control_store import SQLiteControlStore
    from core.workflow.execution_registry import ExecutionRegistry

    db = tmp_path / "control.db"
    seed_line_live_demo(db, NOW)
    store = SQLiteControlStore(db)
    store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-701",
        run_id="fleet-usr-701-other-id",
        host_id="notebook",
        now=NOW - timedelta(seconds=10),
        lease_duration_sec=120,
    )

    _emit_local_run(
        db, NOW, run_id="local-USR-701-abc", ticket_id="USR-701", title="Active with fleet reservation",
        started_min_ago=3, steps=[(1, "gate", "running", "rodando", None, "claude")],
    )
    _emit_local_run(
        db, NOW, run_id="local-USR-702-abc", ticket_id="USR-702", title="Lost execution heartbeat",
        started_min_ago=8, steps=[(1, "agent", "running", "codificando", None, "claude")],
    )
    _emit_local_run(
        db, NOW, run_id="local-USR-703-abc", ticket_id="USR-703", title="Fresh execution heartbeat",
        started_min_ago=3, steps=[(1, "agent", "running", "codificando", None, "claude")],
    )

    lost_clock = [NOW - timedelta(minutes=6)]
    lost_reg = ExecutionRegistry(
        sink=SqliteSink(db), state_dir=tmp_path / "state-lost", clock=lambda: lost_clock[0], hostname="DESKTOP-TEST"
    )
    lost_reg.start("run_ticket", "claude", "agent", "USR-702")

    fresh_clock = [NOW - timedelta(seconds=30)]
    fresh_reg = ExecutionRegistry(
        sink=SqliteSink(db), state_dir=tmp_path / "state-fresh", clock=lambda: fresh_clock[0], hostname="DESKTOP-TEST"
    )
    fresh_reg.start("run_ticket", "claude", "agent", "USR-703")

    snap = _read(db)
    r701 = _run(snap, "local-USR-701-abc")
    assert r701.host_id == "notebook"
    assert r701.reservation_state == "active"
    assert r701.stalled is False

    r702 = _run(snap, "local-USR-702-abc")
    assert r702.stalled is True
    assert "sem batimento há" in (r702.stalled_reason or "")

    r703 = _run(snap, "local-USR-703-abc")
    assert r703.stalled is False


def test_live_js_renders_nine_local_stage_dots_and_eligible_queue_counts() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    live_js = Path(__file__).resolve().parents[1] / "hub" / "frontend" / "live.js"
    syntax = subprocess.run([node, "--check", str(live_js)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function makeNode(tag) {
  const node = {
    nodeType: 1,
    tagName: String(tag || 'div').toUpperCase(),
    className: '',
    hidden: false,
    textContent: '',
    title: '',
    innerHTML: '',
    checked: true,
    value: '',
    disabled: false,
    style: {},
    dataset: {},
    attributes: {},
    children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute(k, v) { this.attributes[String(k)] = String(v); },
    getAttribute(k) { return this.attributes[String(k)] ?? null; },
    removeAttribute(k) { delete this.attributes[String(k)]; },
    addEventListener() {},
    remove() {},
    focus() {},
    append(...items) {
      for (const item of items) {
        if (item == null) continue;
        this.children.push(typeof item === 'string' ? { nodeType: 3, textContent: item, children: [] } : item);
      }
    },
    appendChild(item) { this.append(item); return item; },
    insertBefore(item, ref) {
      const idx = ref ? this.children.indexOf(ref) : -1;
      if (idx >= 0) this.children.splice(idx, 0, item);
      else this.children.push(item);
      return item;
    },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    querySelector() { return makeNode('span'); },
    querySelectorAll() { return []; },
  };
  return node;
}

function collectText(node) {
  if (!node) return '';
  return (node.textContent || '') + (Array.isArray(node.children) ? node.children.map(collectText).join('') : '');
}

const ids = Object.create(null);
const listeners = Object.create(null);
class MockEventSource {
  constructor(url) { this.url = url; }
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }
  close() {}
}

const document = {
  hidden: false,
  visibilityState: 'visible',
  title: '',
  getElementById(id) { return ids[id] || (ids[id] = makeNode('div')); },
  createElement(tag) { return makeNode(tag); },
  createElementNS(_ns, tag) { return makeNode(tag); },
  createTextNode(t) { return { nodeType: 3, textContent: String(t), children: [] }; },
  addEventListener() {},
  querySelector() { return makeNode('div'); },
  querySelectorAll() { return []; },
};
const ctx = {
  document,
  location: { search: '', pathname: '/live' },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
  EventSource: MockEventSource,
  fetch: () => new Promise(() => {}),
  setInterval: () => 1,
  clearInterval() {},
  setTimeout: () => 1,
  clearTimeout() {},
  addEventListener() {},
  AbortController: globalThis.AbortController,
  console,
  URLSearchParams,
  Date,
  Math,
  JSON,
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const emit = listeners.snapshot[0];
const stages = ['preflight', 'workspace', 'agent', 'gate', 'commit', 'pr', 'ci', 'merge', 'deploy'];

emit({
  data: JSON.stringify({
    version: 'v1',
    generated_at: '2026-10-04T15:00:00+00:00',
    source: { backend: 'sqlite', status: 'ok' },
    stage_order: ['grill', 'planning', 'development', 'validation', 'independent_review', 'integration', 'build_deploy', 'retrospective'],
    local_stage_order: stages,
    stage_labels: Object.fromEntries(stages.map((s) => [s, s])),
    kpis: {},
    runs: [{
      run_id: 'local-USR-9-done',
      ticket_id: 'USR-9',
      demand_id: 'USR-9',
      project_id: 'darkfac',
      title: 'Deploy nine stages',
      mode: 'run_ticket',
      run_status: 'completed',
      state: 'succeeded',
      completed_at: '2026-10-04T14:59:00+00:00',
      updated_at: '2026-10-04T14:59:00+00:00',
      stages: stages.map((s) => ({ stage: s, label: s, status: 'succeeded' })),
    }],
    executable_queue: {
      is_empty: false,
      is_blocked: true,
      blocked_count: 1,
      eligible_count: 0,
      candidates: [{ ticket_id: 'USR-10', eligible: false, message: 'aguardando PR #42' }],
    },
    backlog: [],
    executions: { groups: [], lost: [], recent: [] },
    events: [],
    fleet: { hosts: [] },
    off_line: [],
  }),
});

assert.match(collectText(ids['lanes-empty']), /aguardando PR #42/);
const doneDots = ids['done-list'].children[0].children.find((c) => c.className === 'done-extra').children.find((c) => c.className === 'dots');
assert.equal(doneDots.children.length, 9);

emit({
  data: JSON.stringify({
    version: 'v2',
    generated_at: '2026-10-04T15:01:00+00:00',
    source: { backend: 'sqlite', status: 'ok' },
    stage_order: [],
    local_stage_order: stages,
    stage_labels: {},
    kpis: {},
    runs: [],
    executable_queue: { is_empty: false, is_blocked: false, eligible_count: 3, candidates: [] },
    backlog: [],
    executions: { groups: [], lost: [], recent: [] },
    events: [],
    fleet: { hosts: [] },
    off_line: [],
  }),
});
assert.match(collectText(ids['lanes-empty']), /3 ticket\(s\) pronto\(s\)/);
"""
    run = subprocess.run([node, "-e", harness, str(live_js)], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr + run.stdout
