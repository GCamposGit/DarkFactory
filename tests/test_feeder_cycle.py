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
