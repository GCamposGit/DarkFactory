"""Execution registry contract and /live projection with mixed sources (USR-208)."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line.local_progress import LocalRunEvent, ProgressPublisher, SqliteSink
from core.workflow import execution_registry as er
from core.workflow.line_live import read_line_live
from scripts import execution as execution_cli
from tests.fixtures.line_live_seed import seed_line_live_demo


class Clock:
    """Injected, advanceable clock (no wall-clock dependence)."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FlakySink:
    """Wraps a real sink; ``online`` toggles reachability."""

    def __init__(self, inner: SqliteSink) -> None:
        self.inner = inner
        self.online = True
        self.writes: list[LocalRunEvent] = []

    def write(self, event: LocalRunEvent) -> None:
        if not self.online:
            raise OSError("connection refused")
        self.inner.write(event)
        self.writes.append(event)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "control.db"


def _registry(db: Path, tmp_path: Path, clock: Clock, sink: object | None = None) -> er.ExecutionRegistry:
    return er.ExecutionRegistry(
        sink=sink if sink is not None else SqliteSink(db),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        clock=clock,
        hostname="test-host",
        timeout_s=2.0,
    )


def _events(db: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM local_run_events ORDER BY event_id")]


def _fold(db: Path, clock: Clock) -> list[er.ExecutionRecord]:
    return er.fold_executions(_events(db), clock.now)


# ------------------------------------------------------------------ registry contract


def test_ids_are_unique_per_start_even_with_the_same_arguments(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    ids = {reg.start("run_ticket", "claude", "development", "USR-1") for _ in range(25)}
    assert len(ids) == 25
    assert all(i.startswith("exec-") for i in ids)


def test_start_heartbeat_finish_round_trip(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    parent = reg.start("run_ticket", "claude", "ticket", "USR-9", model="sonnet")
    child = reg.start("run_ticket", "claude", "agent", "USR-9", parent, model="sonnet")
    clock.advance(seconds=60)
    reg.heartbeat(child)
    clock.advance(seconds=30)
    reg.finish(child, "ok", {"pr": 12})

    by_id = {r.execution_id: r for r in _fold(db, clock)}
    assert by_id[parent].state == "active" and by_id[parent].parent_execution_id is None
    done = by_id[child]
    assert (done.state, done.source, done.stage, done.ticket_id) == ("succeeded", "run_ticket", "agent", "USR-9")
    assert done.parent_execution_id == parent and done.harness == "claude" and done.model == "sonnet"
    assert json.loads(done.evidence) == {"pr": 12}
    assert done.last_heartbeat_at == (clock.now).isoformat()
    assert done.started_at < done.last_heartbeat_at
    assert done.host == "test-host"


def test_finish_status_is_normalized(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    states = {}
    for word in ("succeeded", "ok", "failed", "error", "cancelled"):
        eid = reg.start("telegram", "gateway", "command")
        reg.finish(eid, word)
        states[word] = next(r for r in _fold(db, clock) if r.execution_id == eid).state
    assert states == {
        "succeeded": "succeeded", "ok": "succeeded", "failed": "failed", "error": "failed", "cancelled": "cancelled",
    }


def test_no_heartbeat_for_more_than_three_minutes_becomes_lost(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    eid = reg.start("claude_session", "claude", "development", "USR-5")
    clock.advance(seconds=er.LOST_AFTER_SECONDS)
    assert next(r for r in _fold(db, clock) if r.execution_id == eid).state == "active"  # boundary: still alive
    clock.advance(seconds=1)
    assert next(r for r in _fold(db, clock) if r.execution_id == eid).state == "lost"
    reg.heartbeat(eid)  # a late sign of life revives it
    assert next(r for r in _fold(db, clock) if r.execution_id == eid).state == "active"


def test_lost_executions_leave_the_active_groups(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    reg.start("subagent", "claude", "development", "USR-5")
    clock.advance(minutes=10)
    alive = reg.start("subagent", "claude", "development", "USR-6")
    records = _fold(db, clock)
    groups = er.group_active(records)
    assert [e.execution_id for g in groups for e in g.executions] == [alive]
    assert [r.state for r in records if r.execution_id != alive] == ["lost"]


def test_offline_events_are_queued_and_resent_in_order_with_original_time(
    db: Path, tmp_path: Path, clock: Clock
) -> None:
    sink = FlakySink(SqliteSink(db))
    reg = _registry(db, tmp_path, clock, sink)
    sink.online = False
    eid = reg.start("run_ticket", "codex", "ticket", "USR-7")  # does not raise, still returns an id
    started = clock.now
    clock.advance(seconds=60)
    reg.heartbeat(eid)
    outbox = tmp_path / "state" / er.OUTBOX_NAME
    assert len(outbox.read_text(encoding="utf-8").splitlines()) == 2
    assert sink.writes == []

    sink.online = True
    clock.advance(seconds=30)
    reg.finish(eid, "succeeded", "PR #1")
    assert not outbox.exists() or outbox.read_text(encoding="utf-8").strip() == ""
    kinds = [e.cause_code for e in sink.writes]
    assert kinds == [er.KIND_START, er.KIND_BEAT, er.KIND_END]
    assert sink.writes[0].at == started  # the original timestamp, not the resend time
    record = next(r for r in _fold(db, clock) if r.execution_id == eid)
    assert record.state == "succeeded" and record.started_at == started.isoformat()


def test_still_offline_keeps_the_queue_and_never_raises(db: Path, tmp_path: Path, clock: Clock) -> None:
    sink = FlakySink(SqliteSink(db))
    sink.online = False
    reg = _registry(db, tmp_path, clock, sink)
    eid = reg.start("telegram", "gateway", "command")
    reg.heartbeat(eid)
    reg.finish(eid, "failed", "boom")
    lines = (tmp_path / "state" / er.OUTBOX_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_registry_never_raises_without_sink_or_state_dir(clock: Clock) -> None:
    reg = er.ExecutionRegistry(sink=None, state_dir=None, clock=clock)
    eid = reg.start("telegram", "gateway", "command")
    reg.heartbeat(eid)
    reg.finish(eid, "ok")
    assert eid.startswith("exec-")

    class Exploding:
        def write(self, event: LocalRunEvent) -> None:
            raise RuntimeError("driver exploded")

    reg2 = er.ExecutionRegistry(sink=Exploding(), state_dir=None, clock=clock)  # type: ignore[arg-type]
    reg2.finish(reg2.start("x", None, "y"), "ok")


def test_message_with_huge_evidence_still_decodes(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    eid = reg.start("run_ticket", "claude", "ticket", "USR-1", title="t" * 500)
    reg.finish(eid, "failed", "x" * 5000)
    record = next(r for r in _fold(db, clock) if r.execution_id == eid)
    assert record.state == "failed" and 0 < len(record.evidence) <= 160


def test_tracked_execution_context_manager_and_heartbeat_thread(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    with er.TrackedExecution("vps_line", "claude", "validation", "USR-3", registry=reg, heartbeat_interval=0.01) as t:
        beats = threading.Event()

        def seen() -> bool:
            return sum(1 for e in _events(db) if e["cause_code"] == er.KIND_BEAT) >= 1

        for _ in range(200):
            if seen():
                beats.set()
                break
            threading.Event().wait(0.01)
        assert beats.is_set()
    with pytest.raises(ValueError):
        with er.TrackedExecution("vps_line", "claude", "validation", "USR-3", registry=reg, heartbeat_interval=0):
            raise ValueError("stage blew up")
    states = sorted(r.state for r in _fold(db, clock))
    assert states == ["failed", "succeeded"]
    assert t.execution_id


def test_progress_tracker_mirrors_run_ticket_phases(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    publisher = ProgressPublisher(ticket_id="USR-8", sink=None, run_id="run-8", clock=clock)
    tracker = er.ProgressExecutionTracker(
        "USR-8", harness="grok", model="m1", registry=reg, heartbeat_interval=0
    )
    publisher.add_observer(tracker.observe)
    publisher.phase("agent", "running", "dev")
    publisher.phase("gate", "running", "gate")  # starting gate closes agent as succeeded
    publisher.phase("gate", "failed", "red", cause="gate_red")
    publisher.finish(False, "falhou", cause="gate_red")

    records = {(r.stage): r for r in _fold(db, clock)}
    assert set(records) == {"ticket", "agent", "gate"}
    assert records["agent"].state == "succeeded" and records["gate"].state == "failed"
    assert records["ticket"].state == "failed"
    assert records["agent"].parent_execution_id == records["ticket"].execution_id == tracker.root_id
    assert records["gate"].harness == "grok" and records["gate"].model == "m1"


def test_child_process_nests_under_inherited_parent(db: Path, tmp_path: Path, clock: Clock) -> None:
    reg = _registry(db, tmp_path, clock)
    parent = reg.start("run_ticket", "claude", "ticket", "USR-2")
    publisher = ProgressPublisher(ticket_id="USR-2", sink=None, run_id="run-2", clock=clock, owns_run=False)
    tracker = er.ProgressExecutionTracker(
        "USR-2", registry=reg, parent_execution_id=parent, heartbeat_interval=0
    )
    publisher.add_observer(tracker.observe)
    publisher.phase("pr", "running", "abrindo PR")
    publisher.phase("pr", "succeeded", "ok")
    publisher.phase("run", "succeeded", "")
    records = _fold(db, clock)
    assert [r.stage for r in records if r.execution_id != parent] == ["pr"]
    assert next(r for r in records if r.stage == "pr").parent_execution_id == parent
    assert next(r for r in records if r.execution_id == parent).state == "active"  # the parent owns its own close


# ------------------------------------------------------------------ CLI


def test_cli_start_heartbeat_finish(db: Path, tmp_path: Path, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    reg = _registry(db, tmp_path, clock)
    assert execution_cli.main(
        ["start", "--source", "claude_session", "--harness", "claude", "--stage", "development", "--ticket", "USR-4", "--json"],
        registry=reg,
    ) == 0
    eid = json.loads(capsys.readouterr().out)["execution_id"]
    clock.advance(seconds=10)
    assert execution_cli.main(["heartbeat", eid], registry=reg) == 0
    assert execution_cli.main(["finish", eid, "--status", "failed", "--evidence", "gate"], registry=reg) == 0
    record = next(r for r in _fold(db, clock) if r.execution_id == eid)
    assert (record.source, record.stage, record.ticket_id, record.state, record.evidence) == (
        "claude_session", "development", "USR-4", "failed", "gate",
    )
    assert execution_cli.main(["start"], registry=reg) == 2  # malformed command line only


def test_cli_exits_zero_when_the_store_is_down(tmp_path: Path, clock: Clock, capsys: pytest.CaptureFixture[str]) -> None:
    sink = FlakySink(SqliteSink(tmp_path / "x.db"))
    sink.online = False
    reg = _registry(tmp_path / "x.db", tmp_path, clock, sink)
    assert execution_cli.main(["start", "--source", "codex", "--stage", "planning"], registry=reg) == 0
    assert capsys.readouterr().out.strip().startswith("exec-")


# ------------------------------------------------------------------ /live projection with mixed sources


def test_live_snapshot_projects_mixed_sources(tmp_path: Path, clock: Clock) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, clock.now)
    reg = _registry(db, tmp_path, clock)

    # finished real deliveries (inside and outside the 7-day window)
    old = reg.start("claude_session", "claude", "development", "USR-OLD")
    reg.finish(old, "succeeded", "PR #1")
    clock.advance(days=8)
    delivered = reg.start("run_ticket", "claude", "ticket", "USR-77", model="sonnet")
    stage = reg.start("run_ticket", "claude", "agent", "USR-77", delivered)
    reg.finish(stage, "succeeded")
    reg.finish(delivered, "succeeded", "merged")
    chatter = reg.start("telegram", "gateway", "command")
    reg.finish(chatter, "succeeded", "tickets")
    failed = reg.start("subagent", "grok", "development", "USR-78")
    reg.finish(failed, "failed", "gate_red")
    # fixtures and canaries
    fixture = reg.start("vps_line", "claude", "validation", "USR-TESTE", title="Teste da esteira")
    canary = reg.start("canary", "claude", "development", "USR-79")
    # active across sources
    clock.advance(minutes=1)
    live_tg = reg.start("telegram", "gateway", "command")
    live_vps = reg.start("vps_line", "codex", "development", "USR-80", model="gpt")
    live_vps2 = reg.start("vps_line", "grok", "development", "USR-81")
    live_session = reg.start("claude_session", "claude", "planning", "USR-82")
    # lost: started long ago, never heartbeated
    clock.advance(minutes=10)
    for eid in (live_tg, live_vps, live_vps2):
        reg.heartbeat(eid)
    snapshot = read_line_live(db, database_url="", now=clock.now)
    ex = snapshot.executions

    # active grouped by source and stage, with the fields the Hub shows
    groups = {(g.source, g.stage): [e.execution_id for e in g.executions] for g in ex.active}
    assert groups[("vps_line", "development")] == sorted([live_vps, live_vps2], reverse=True) or set(
        groups[("vps_line", "development")]
    ) == {live_vps, live_vps2}
    assert groups[("telegram", "command")] == [live_tg]
    assert ("claude_session", "planning") not in groups  # no heartbeat for 10 min: lost, not in progress
    vps = next(e for g in ex.active for e in g.executions if e.execution_id == live_vps)
    assert (vps.ticket_id, vps.harness, vps.model, vps.state) == ("USR-80", "codex", "gpt", "active")
    assert vps.started_at and vps.last_heartbeat_at > vps.started_at

    lost_ids = {e.execution_id for e in ex.lost}
    assert live_session in lost_ids and fixture in lost_ids  # nobody beat them
    # fixtures/canaries flagged so the Hub can hide them by default
    flagged = {e.execution_id for e in ex.lost if e.fixture}
    assert fixture in flagged and canary in flagged
    assert ex.fixtures_hidden >= 2

    # recently completed: real deliveries of the last 7 days only
    recent_ids = {e.execution_id for e in ex.recent}
    assert delivered in recent_ids
    assert stage not in recent_ids  # nested phase, not a delivery
    assert chatter not in recent_ids  # Telegram chatter is not a delivery
    assert failed not in recent_ids and old not in recent_ids  # failed / older than 7 days

    # execution rows never leak into the run_ticket runs projection
    assert not any(run.run_id.startswith("exec-") for run in snapshot.runs)


def test_fixture_runs_are_flagged_in_the_runs_projection(tmp_path: Path, clock: Clock) -> None:
    db = tmp_path / "control.db"
    seed_line_live_demo(db, clock.now)
    publisher = ProgressPublisher(
        ticket_id="USR-TESTE", title="Teste da esteira", run_id="local-fixture", sink=SqliteSink(db),
        worker="h", clock=clock,
    )
    publisher.phase("agent", "running", "x")
    snapshot = read_line_live(db, database_url="", now=clock.now)
    fixture_run = next(r for r in snapshot.runs if r.run_id == "local-fixture")
    assert fixture_run.fixture is True
    flagged = {r.run_id for r in snapshot.runs if r.fixture}
    assert flagged == {"local-fixture"} | {r.run_id for r in snapshot.runs if r.project_id == "darkfac-canary"}
    assert all(not r.fixture for r in snapshot.runs if r.project_id == "darkfac" and r.run_id != "local-fixture")


def test_is_fixture_heuristics() -> None:
    assert er.is_fixture("USR-TESTE") and er.is_fixture("TESTE-1") and er.is_fixture("USR-CANARY-3")
    assert er.is_fixture("USR-1", "canary") and er.is_fixture("USR-1", None, "Teste da esteira")
    assert not er.is_fixture("USR-208") and not er.is_fixture(None, "run_ticket", "Registro universal")


def test_stage_handler_wrapper_tracks_and_stays_transparent(db: Path, tmp_path: Path, clock: Clock) -> None:
    from core.line.bindings import TrackedStageHandler
    from core.workflow.control_contracts import StageResult

    seen: list[object] = []

    class Inner:
        route_waiter = "shared-waiter"

        def handle(self, context: object) -> StageResult:
            seen.append(context)
            return StageResult(outcome="failed", cause_code="gate_red")

    class Key:
        run_id = "run-1"
        ticket_id = "USR-12"

    class Claim:
        job_key = Key()
        route_ref = "codex:gpt"

    class Ctx:
        claim = Claim()
        route_ref = "codex:gpt"

    reg = _registry(db, tmp_path, clock)
    er.set_registry(reg)
    try:
        wrapped = TrackedStageHandler("validation", Inner())
        assert wrapped.route_waiter == "shared-waiter"  # attribute access falls through
        result = wrapped.handle(Ctx())  # type: ignore[arg-type]
    finally:
        er.set_registry(None)
    assert result.outcome == "failed" and len(seen) == 1
    record = _fold(db, clock)[0]
    assert (record.source, record.stage, record.ticket_id, record.state) == ("vps_line", "validation", "USR-12", "failed")
    assert (record.harness, record.model, record.evidence) == ("codex", "gpt", "gate_red")


def test_frontend_derive_view_hides_fixtures_by_default() -> None:
    node = pytest.importorskip("shutil").which("node")
    if node is None:
        pytest.skip("node not available")
    import subprocess

    script = r"""
const LL = require(process.argv[1]);
const rec = (id, fixture) => ({execution_id: id, source: 'telegram', stage: 'command', state: 'active', fixture});
const snap = {runs: [{run_id: 'a', project_id: 'darkfac', state: 'running', fixture: false, ticket_id: 'USR-1', title: 't'},
                     {run_id: 'b', project_id: 'darkfac', state: 'running', fixture: true, ticket_id: 'USR-TESTE', title: 't'}],
  executions: {active: [{source: 'telegram', stage: 'command', executions: [rec('e1', false), rec('e2', true)]}],
               lost: [], recent: [], fixtures_hidden: 1}};
const hidden = LL.deriveView(snap, {});
const shown = LL.deriveView(snap, {showFixtures: true});
console.log(JSON.stringify([hidden.active.length, hidden.execGroups[0].executions.length, shown.active.length, shown.execGroups[0].executions.length]));
"""
    path = Path(__file__).resolve().parents[1] / "hub" / "frontend" / "live.js"
    out = subprocess.run([node, "-e", script, str(path)], capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(out.stdout.strip()) == [1, 1, 2, 2]



def _dead_pid() -> int:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _queue_offline_event(db: Path, tmp_path: Path, clock: Clock) -> Path:
    sink = FlakySink(SqliteSink(db))
    sink.online = False
    reg = _registry(db, tmp_path, clock, sink)
    reg.start("run_ticket", "codex", "ticket", "USR-9")
    return tmp_path / "state" / er.OUTBOX_NAME


def test_orphan_sending_of_dead_pid_is_resent_and_removed(db: Path, tmp_path: Path, clock: Clock) -> None:
    outbox = _queue_offline_event(db, tmp_path, clock)
    orphan = outbox.with_name(f"{er.OUTBOX_NAME}.{_dead_pid()}.abc123.sending")
    outbox.replace(orphan)
    sink = FlakySink(SqliteSink(db))
    reg = _registry(db, tmp_path, clock, sink)
    assert reg._flush_outbox() is True
    assert [e.cause_code for e in sink.writes] == [er.KIND_START]
    assert not orphan.exists()


def test_recent_sending_of_live_pid_is_left_alone(db: Path, tmp_path: Path, clock: Clock) -> None:
    import os

    outbox = _queue_offline_event(db, tmp_path, clock)
    live = outbox.with_name(f"{er.OUTBOX_NAME}.{os.getpid()}.abc123.sending")
    outbox.replace(live)
    sink = FlakySink(SqliteSink(db))
    reg = _registry(db, tmp_path, clock, sink)
    assert reg._flush_outbox() is True
    assert sink.writes == [] and live.exists()
