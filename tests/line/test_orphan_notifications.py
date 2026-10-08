"""USR-155: the owner is told, once per run, when the orphan sweeper parks a run on `waiting_human`.

No network, no real Telegram: the notifier is a fake callable and the stores are in-memory fakes or a
temporary SQLite control store.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.line.human import HumanRequest
from core.line.orphan_runs import (
    ORPHAN_NO_SUCCESSOR_CAUSE,
    ORPHAN_NOTIFIED_MARKER,
    build_parked_orphan_request,
    find_unnotified_parked_orphan,
    notify_parked_orphan_runs,
    sweep_orphan_runs,
)
from core.workflow.control_contracts import JobKey
from core.workflow.control_store import RUN_OPEN_JOB_STATUSES, SQLiteControlStore
from tests.fixtures.line_live_seed import seed_line_live_demo

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def _ago(minutes: float) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


class FakeNotifier:
    """Stands in for the owner bot; `fail` makes every send report non-delivery (or raise)."""

    def __init__(self, *, fail: bool = False, raises: bool = False) -> None:
        self.fail = fail
        self.raises = raises
        self.messages: list[str] = []

    def __call__(self, text: str) -> bool:
        self.messages.append(text)
        if self.raises:
            raise RuntimeError("bot down")
        return not self.fail


class FakeStore:
    """Only what the notification step reads/writes; the marker lives in the job like in the real store."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.marker_calls: list[tuple[JobKey, str]] = []
        self.marker_fails = False

    def add_run(self, run_id: str, jobs: list[tuple[str, int, str, str | None]], *, status: str = "active") -> None:
        """jobs: (stage, iteration, status, cause_code)."""
        self.runs[run_id] = {
            "run_id": run_id,
            "status": status,
            "jobs": [
                {
                    "ticket_id": "darkfac",
                    "plan_version": "1.0",
                    "stage": stage,
                    "iteration": iteration,
                    "status": job_status,
                    "created_at": _ago(120),
                    "updated_at": _ago(120),
                    "finished_at": None if job_status in RUN_OPEN_JOB_STATUSES else _ago(120),
                    "cause_code": cause,
                    "output_refs": [],
                    "evidence_refs": [],
                }
                for stage, iteration, job_status, cause in jobs
            ],
        }

    def list_active_run_ids(self) -> list[str]:
        return [rid for rid, run in self.runs.items() if run["status"] == "active"]

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    def add_job_evidence(self, job_key: JobKey, ref: str) -> bool:
        self.marker_calls.append((job_key, ref))
        if self.marker_fails:
            raise RuntimeError("store down")
        for job in self.runs[job_key.run_id]["jobs"]:
            if job["stage"] == job_key.stage and job["iteration"] == job_key.iteration and job["status"] == "waiting_human":
                if ref not in job["evidence_refs"]:
                    job["evidence_refs"].append(ref)
                return True
        return False


def _parked_store() -> FakeStore:
    store = FakeStore()
    store.add_run(
        "run-parked",
        [("development", 0, "cancelled", None), ("validation", 0, "waiting_human", ORPHAN_NO_SUCCESSOR_CAUSE)],
    )
    return store


# ------------------------------------------------------------------------------ message


def test_message_carries_run_stage_and_the_suggested_actions() -> None:
    parked = find_unnotified_parked_orphan(_parked_store().runs["run-parked"])
    assert parked is not None

    request = build_parked_orphan_request(parked)

    assert isinstance(request, HumanRequest)
    assert (request.kind, request.run_id, request.blocking_stage) == ("infra", "run-parked", "validation")
    assert "run-parked" in request.guide_md
    assert "'validation'" in request.guide_md
    assert "/linha darkfac" in request.guide_md
    assert "/cancelar run-parked" in request.guide_md


def test_delivered_text_goes_through_the_human_request_format() -> None:
    notifier = FakeNotifier()
    notify_parked_orphan_runs(_parked_store(), send=notifier)

    (text,) = notifier.messages
    assert text.startswith("[infra] run run-parked bloqueado em 'validation'")
    assert "/linha darkfac" in text and "/cancelar run-parked" in text


# ------------------------------------------------------------------------ once per run


def test_parked_run_is_notified_exactly_once_across_sweeps() -> None:
    store = _parked_store()
    notifier = FakeNotifier()

    first = notify_parked_orphan_runs(store, send=notifier)
    second = notify_parked_orphan_runs(store, send=notifier)
    third = notify_parked_orphan_runs(store, send=notifier)

    assert [(a.run_id, a.action, a.stage) for a in first] == [("run-parked", "notified", "validation")]
    assert second == [] and third == []
    assert len(notifier.messages) == 1
    parked_job = store.runs["run-parked"]["jobs"][-1]
    assert parked_job["evidence_refs"] == [ORPHAN_NOTIFIED_MARKER]


def test_marker_in_the_store_survives_a_restart_so_a_fresh_process_does_not_resend() -> None:
    store = _parked_store()
    notify_parked_orphan_runs(store, send=FakeNotifier())

    fresh_notifier = FakeNotifier()  # a restarted worker has no memory of the first message
    assert notify_parked_orphan_runs(store, send=fresh_notifier) == []
    assert fresh_notifier.messages == []


def test_a_run_already_marked_on_any_job_is_not_notified_again_after_a_new_parking() -> None:
    store = FakeStore()
    store.add_run(
        "run-again",
        [
            ("validation", 0, "succeeded", ORPHAN_NO_SUCCESSOR_CAUSE),
            ("validation", 1, "waiting_human", ORPHAN_NO_SUCCESSOR_CAUSE),
        ],
    )
    store.runs["run-again"]["jobs"][0]["evidence_refs"] = [ORPHAN_NOTIFIED_MARKER]
    notifier = FakeNotifier()

    assert notify_parked_orphan_runs(store, send=notifier) == []
    assert notifier.messages == []


# ----------------------------------------------------------------------- failed delivery


def test_failed_delivery_keeps_the_park_leaves_no_marker_and_is_retried_next_sweep() -> None:
    store = _parked_store()
    down = FakeNotifier(fail=True)

    first = notify_parked_orphan_runs(store, send=down)

    assert [(a.run_id, a.action) for a in first] == [("run-parked", "notify_failed")]
    assert store.marker_calls == []
    parked_job = store.runs["run-parked"]["jobs"][-1]
    assert parked_job["status"] == "waiting_human" and parked_job["evidence_refs"] == []

    back_up = FakeNotifier()
    second = notify_parked_orphan_runs(store, send=back_up)
    assert [(a.run_id, a.action) for a in second] == [("run-parked", "notified")]
    assert len(back_up.messages) == 1
    assert notify_parked_orphan_runs(store, send=back_up) == []


def test_notifier_that_raises_is_contained_and_retried() -> None:
    store = _parked_store()

    first = notify_parked_orphan_runs(store, send=FakeNotifier(raises=True))

    assert [a.action for a in first] == ["notify_failed"]
    assert store.runs["run-parked"]["jobs"][-1]["evidence_refs"] == []
    assert [a.action for a in notify_parked_orphan_runs(store, send=FakeNotifier())] == ["notified"]


def test_marker_write_failure_never_raises_and_the_message_is_retried_later() -> None:
    store = _parked_store()
    store.marker_fails = True
    notifier = FakeNotifier()

    actions = notify_parked_orphan_runs(store, send=notifier)

    assert [a.action for a in actions] == ["error"]  # contained: nothing propagates
    assert store.runs["run-parked"]["jobs"][-1]["status"] == "waiting_human"
    store.marker_fails = False
    assert [a.action for a in notify_parked_orphan_runs(store, send=notifier)] == ["notified"]
    assert len(notifier.messages) == 2  # at-least-once beats silence


# ----------------------------------------------------------------- runs that must stay silent


def test_runs_that_are_not_parked_by_the_sweeper_never_notify() -> None:
    store = FakeStore()
    store.add_run("run-pending", [("grill", 0, "succeeded", None), ("planning", 0, "pending", None)])
    store.add_run("run-grill", [("grill", 0, "waiting_human", None)])  # a normal owner question
    store.add_run("run-other", [("planning", 0, "waiting_human", "grill_artifacts_missing")])
    store.add_run("run-done", [("validation", 0, "waiting_human", ORPHAN_NO_SUCCESSOR_CAUSE)], status="completed")
    store.add_run("run-empty", [])
    notifier = FakeNotifier()

    assert notify_parked_orphan_runs(store, send=notifier) == []
    assert notifier.messages == []
    assert store.marker_calls == []


def test_without_a_sender_or_a_marker_capable_store_nothing_is_sent() -> None:
    store = _parked_store()
    assert notify_parked_orphan_runs(store, send=None) == []

    class NoMarkerStore:
        def list_active_run_ids(self) -> list[str]:
            return ["run-parked"]

        def get_run_status(self, run_id: str) -> dict[str, Any]:
            return _parked_store().runs["run-parked"]

    notifier = FakeNotifier()
    assert notify_parked_orphan_runs(NoMarkerStore(), send=notifier) == []  # cannot dedupe -> never spam
    assert notifier.messages == []


def test_only_run_ids_and_max_actions_are_honoured() -> None:
    store = FakeStore()
    for index in range(3):
        store.add_run(f"run-{index}", [("validation", 0, "waiting_human", ORPHAN_NO_SUCCESSOR_CAUSE)])
    notifier = FakeNotifier()

    assert [a.run_id for a in notify_parked_orphan_runs(store, send=notifier, only_run_ids=["run-1"])] == ["run-1"]
    assert len(notify_parked_orphan_runs(store, send=notifier, max_actions=1)) == 1
    assert len(notifier.messages) == 2


# ----------------------------------------------------- real SQLite store, end to end with the sweeper


def _execute(db: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(sql, params)
        conn.commit()


def test_sweeper_parks_then_notifies_once_on_the_real_store_and_a_failed_send_does_not_unpark(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    run_id = seed_line_live_demo(db, NOW)["queued"]
    old = (NOW - timedelta(hours=3)).isoformat()
    _execute(db, "UPDATE jobs SET status = 'cancelled', finished_at = ?, updated_at = ? WHERE run_id = ?", (old, old, run_id))
    store = SQLiteControlStore(db_path=db)

    assert [a.action for a in sweep_orphan_runs(store, now=NOW)] == ["parked"]

    down = FakeNotifier(fail=True)
    assert [a.action for a in notify_parked_orphan_runs(store, send=down)] == ["notify_failed"]
    status = store.get_run_status(run_id)
    assert status is not None and status["status"] == "active"
    parked = [j for j in status["jobs"] if j["status"] == "waiting_human"]
    assert [j["cause_code"] for j in parked] == [ORPHAN_NO_SUCCESSOR_CAUSE]
    assert ORPHAN_NOTIFIED_MARKER not in parked[0]["evidence_refs"]

    up = FakeNotifier()
    assert [(a.run_id, a.action) for a in notify_parked_orphan_runs(store, send=up)] == [(run_id, "notified")]
    assert notify_parked_orphan_runs(store, send=up) == []
    assert len(up.messages) == 1 and run_id in up.messages[0]

    after = store.get_run_status(run_id)
    assert after is not None
    parked_after = [j for j in after["jobs"] if j["status"] == "waiting_human"]
    assert parked_after[0]["evidence_refs"] == [ORPHAN_NOTIFIED_MARKER]
    assert sweep_orphan_runs(store, now=NOW) == []  # the sweeper still ignores the parked run


def test_add_job_evidence_is_idempotent_and_only_touches_waiting_human_jobs(tmp_path: Path) -> None:
    db = tmp_path / "control.db"
    run_id = seed_line_live_demo(db, NOW)["queued"]
    store = SQLiteControlStore(db_path=db)
    status = store.get_run_status(run_id)
    assert status is not None
    job = status["jobs"][0]
    key = JobKey(
        run_id=run_id, ticket_id=job["ticket_id"], plan_version=job["plan_version"], stage=job["stage"],
        iteration=job["iteration"],
    )
    assert job["status"] != "waiting_human"
    assert store.add_job_evidence(key, "x:1") is False  # not parked -> untouched

    _execute(db, "UPDATE jobs SET status = 'waiting_human' WHERE run_id = ?", (run_id,))
    assert store.add_job_evidence(key, "x:1") is True
    assert store.add_job_evidence(key, "x:1") is True
    refreshed = store.get_run_status(run_id)
    assert refreshed is not None
    assert next(j for j in refreshed["jobs"] if j["stage"] == key.stage)["evidence_refs"].count("x:1") == 1


# -------------------------------------------------------------------------- worker wiring


class _IdleStore:
    def claim(self, **_: Any) -> None:
        return None

    def list_waiting_jobs(self, **_: Any) -> list:
        return []

    def get_run_status(self, run_id: str) -> None:
        return None


def test_cloud_worker_notifies_after_the_sweep_and_a_notifier_failure_never_blocks_it() -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    sent: list[Any] = []

    def notifier(store: Any, *, send: Any) -> list[Any]:
        sent.append(send)
        return []

    def exploding(store: Any, *, send: Any) -> list[Any]:
        raise RuntimeError("notifier exploded")

    def make(notify: Any) -> CloudWorker:
        return CloudWorker(
            worker_id="w",
            max_slots=1,
            store=_IdleStore(),  # type: ignore[arg-type]
            capabilities=[],
            orphan_run_sweeper=lambda store, *, now: ["swept"],
            parked_orphan_notifier=notify,
            workspace_sweeper=lambda **_: [],
        )

    assert make(notifier).sweep_orphan_runs(now=NOW) == ["swept"]
    assert len(sent) == 1 and callable(sent[0])
    assert make(exploding).sweep_orphan_runs(now=NOW) == ["swept"]
