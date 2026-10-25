"""USR-304: the backlog feeder is bound to the CloudCoordinator loop through FeederCycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.demands.store import DemandsStore
from core.line import backlog_feeder as bf_module
from core.line import owner_intake
from core.line.backlog_feeder import BacklogFeeder
from core.line.feeder_cycle import FeederCycle
from core.orchestrator.cloud_coordinator import CloudCoordinator
from core.roadmap.models import DeliveryStatus
from core.workflow.control_store import SQLiteControlStore
from tests.test_line_backlog_feeder import _make_ticket


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*owner_intake.DATABASE_URL_ENVS, owner_intake.AUTOSUBMIT_ENV, owner_intake.PROJECTS_ENV):
        monkeypatch.delenv(name, raising=False)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


@pytest.fixture
def demands(tmp_path: Path) -> DemandsStore:
    return DemandsStore(path=tmp_path / "demands.json")


def _cycle(demands, store, clock, *, max_active=1, gate=None) -> FeederCycle:
    feeder = BacklogFeeder(demands_store=demands, control_store=store)
    return FeederCycle(feeder, interval_sec=900, max_active=max_active, quota_gate=gate, clock=clock)


def _coordinator(store, cycle) -> CloudCoordinator:
    return CloudCoordinator(store=store, max_concurrent_slots=1, feeder_cycle=cycle, port=0)


def _finish_runs(monkeypatch: pytest.MonkeyPatch, finished: set[str]) -> None:
    real = owner_intake.run_state

    def fake(store, run_id, **kw):
        return "succeeded" if run_id in finished else real(store, run_id, **kw)

    monkeypatch.setattr(owner_intake, "run_state", fake)
    monkeypatch.setattr(bf_module, "run_state", fake)


def test_loop_feeds_then_frees_slot_for_next(store, demands, monkeypatch) -> None:
    _make_ticket(demands, "USR-901")
    _make_ticket(demands, "USR-902")
    clock = _Clock()
    cycle = _cycle(demands, store, clock)
    coord = _coordinator(store, cycle)

    coord.scan_and_recover_pending()
    first = cycle.last_report
    assert first is not None and first.submitted == 1 and first.free_slots == 1
    assert len(first.submitted_ids) == 1
    run_one = owner_intake._ticket_attempts(store, first.submitted_ids[0])[-1][1]

    # Inside the interval the loop does not run another cycle.
    coord.scan_and_recover_pending()
    assert cycle.cycles_run == 1

    # Next interval: slot is still occupied -> nothing new, observable reason.
    clock.now += 901
    coord.scan_and_recover_pending()
    assert cycle.last_report.submitted == 0
    assert cycle.last_report.in_flight == 1
    assert cycle.last_report.idle_reason == "no_free_slot"

    # Completion frees the slot; the next ticket starts.
    _finish_runs(monkeypatch, {run_one})
    clock.now += 901
    coord.scan_and_recover_pending()
    assert cycle.last_report.submitted == 1
    assert cycle.last_report.submitted_ids != first.submitted_ids


def test_blocked_tickets_are_skipped_with_reason(store, demands) -> None:
    _make_ticket(demands, "USR-910", problem="short", non_goals=[], criteria=[])
    _make_ticket(demands, "USR-911", dependencies=["USR-910"])
    clock = _Clock()
    report = _cycle(demands, store, clock).run_once()
    assert report.submitted == 0
    assert report.skipped.get("g1_ambiguity") == 1
    assert report.skipped.get("unmet_dependencies") == 1
    assert report.idle_reason == "no_eligible_ticket"


def test_quota_floor_blocks_admission(store, demands) -> None:
    _make_ticket(demands, "USR-920")
    cycle = _cycle(demands, store, _Clock(), gate=lambda: (False, "quota_floor: all harnesses <= 15%"))
    report = cycle.run_once()
    assert report.submitted == 0
    assert report.skipped["quota_floor"] == 1
    assert report.idle_reason.startswith("quota_floor")
    assert owner_intake._ticket_attempts(store, "USR-920") == []


def test_replay_and_two_coordinators_do_not_duplicate(store, demands) -> None:
    _make_ticket(demands, "USR-930")
    clock = _Clock()
    c1 = _coordinator(store, _cycle(demands, store, clock, max_active=3))
    c2 = _coordinator(store, _cycle(demands, store, clock, max_active=3))
    c1.scan_and_recover_pending()
    c2.scan_and_recover_pending()
    c1._feeder_cycle._last_run = None
    c1.scan_and_recover_pending()
    assert len(owner_intake._ticket_attempts(store, "USR-930")) == 1
    assert demands.get_ticket("USR-930").status == DeliveryStatus.IMPLEMENTING


def test_store_error_is_reported_not_raised(demands) -> None:
    class Boom:
        def list_candidates(self, **_kw):
            raise RuntimeError("db down")

    cycle = FeederCycle(Boom(), interval_sec=900, clock=_Clock())  # type: ignore[arg-type]
    report = cycle.maybe_run()
    assert report is not None and report.idle_reason == "store_error" and "db down" in report.error


def test_from_env_disabled_and_interval(monkeypatch, store) -> None:
    monkeypatch.setenv("DARKFAC_FEEDER_ENABLED", "0")
    assert FeederCycle.from_env(store) is None
    monkeypatch.setenv("DARKFAC_FEEDER_ENABLED", "1")
    monkeypatch.delenv("DARKFAC_FEEDER_INTERVAL_SEC", raising=False)
    assert FeederCycle.from_env(store).interval_sec == 900.0
    monkeypatch.setenv("DARKFAC_FEEDER_INTERVAL_SEC", "60")
    assert FeederCycle.from_env(store).interval_sec == 60.0


def test_coordinator_without_database_url_has_no_feeder(store) -> None:
    coord = CloudCoordinator(store=store, port=0)
    assert coord._resolve_feeder_cycle() is None


# --- USR-375 S7: entrada unica + um alimentador so + grill de um toque ---------------------------


class _StubDemandsService:
    """Stands in for DemandsService: persists a G1-complete ticket in the durable store."""

    def __init__(self, demands: DemandsStore) -> None:
        self.demands = demands

    def create_ticket_from_input(self, demand, *, force_heuristic=False, timeout=None):
        return _make_ticket(self.demands, "USR-940", title=demand.title, problem=demand.problem_statement)


def test_telegram_submission_survives_redeploy_and_feeder_picks_it(tmp_path, store) -> None:
    durable = tmp_path / "durable" / "demands.json"
    durable.parent.mkdir()
    outcome = owner_intake.submit_owner_demand(
        "Detailed problem description with enough characters to satisfy G1 minimums",
        source="telegram",
        demands_service=_StubDemandsService(DemandsStore(path=durable)),
    )
    assert outcome.ticket_id == "USR-940" and outcome.source == "telegram"

    # "Redeploy": new process objects, fresh ephemeral state dir; only the durable store remains.
    fresh_state = tmp_path / "fresh_state"
    fresh_state.mkdir()
    reborn = DemandsStore(path=durable)
    report = _cycle(reborn, store, _Clock(), max_active=3).run_once()
    assert report.submitted_ids == ["USR-940"]
    assert not list(fresh_state.iterdir())


def test_single_feeder_start_in_cloud_coordinator() -> None:
    src = (Path(__file__).resolve().parents[1] / "core" / "orchestrator" / "cloud_coordinator.py").read_text(
        encoding="utf-8"
    )
    assert src.count(".maybe_run(") == 1
    assert src.count("FeederCycle.from_env(") == 1
    assert "BacklogFeeder(" not in src


def test_one_tap_button_records_the_recommended_option(monkeypatch) -> None:
    from datetime import datetime

    from core.line import human, stage_grill

    question = stage_grill._PendingQuestion(
        id="q1", text="Canal?", kind="technical", options=["email", "telegram"], recommended="telegram"
    )
    button = stage_grill.one_tap_recommended_button("run-1", question)
    assert button is not None and button["callback_data"] == "cb:grill:run-1#q1:1"
    _, rows = stage_grill._build_message("run-1", [question])
    assert any(b.get("callback_data") == button["callback_data"] for row in rows for b in row)

    recorded: dict = {}

    class _Store:
        def find_job(self, run_id, stage, **kw):
            return "job-1"

        def record_grill_answer(self, run_id, qid, choice, at: datetime) -> None:
            recorded[(run_id, qid)] = choice

    monkeypatch.setattr(human, "resume_blocked_job", lambda *a, **k: True)
    tail = button["callback_data"].partition("cb:grill:")[2]
    run_id, rest = tail.split("#", 1)
    qid, idx = rest.split(":", 1)
    assert human.build_telegram_line_grill_handler(_Store())(run_id, qid, idx, 1) == {"resumed": True}
    assert stage_grill.resolve_callback_choice(question, recorded[("run-1", "q1")]) == "telegram"
