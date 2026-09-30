"""HF-27-10 -- daily E2E canary + dogfood submitter.

No network, no real git remote, no real Telegram. Stage telemetry and the
smoke check are supplied by fake adapters; the intake path itself runs for
real against a temp-file `SQLiteControlStore`, since idempotent-replay
behavior (no duplicate demand on a same-day retry) is exactly what
`ControlStore.accept` is contractually responsible for and what this suite
must prove end-to-end.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, UTC
from pathlib import Path

import pytest

from core.line import canary, dogfood, store_selection
from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.roadmap.models import (
    ConfidenceLevel,
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapItem,
    RoadmapItemType,
    RoadmapSourceRef,
)
from core.workflow.control_store import SQLiteControlStore


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FixedClock:
    def __init__(self, today: date, now: datetime | None = None) -> None:
        self._today = today
        self._now = now or datetime(today.year, today.month, today.day, 9, 0, tzinfo=UTC)

    def today(self) -> date:
        return self._today

    def now(self) -> datetime:
        return self._now


class FakeObserver:
    """Returns a canned, all-stage telemetry list regardless of run_id."""

    def __init__(self, stages: list[canary.CanaryStageObservation]) -> None:
        self._stages = stages
        self.calls: list[str] = []

    def observe(self, run_id: str) -> list[canary.CanaryStageObservation]:
        self.calls.append(run_id)
        return list(self._stages)


class FakeSmokeClient:
    def __init__(self, ok: bool = True, rollback_required: bool = False) -> None:
        self.ok = ok
        self.rollback_required = rollback_required
        self.calls: list[tuple[str, date, str]] = []

    def check(self, base_url: str, expected_date: date, scenario: str) -> canary.SmokeResult:
        self.calls.append((base_url, expected_date, scenario))
        return canary.SmokeResult(
            ok=self.ok,
            url=f"{base_url}/version",
            expected_date=expected_date.isoformat(),
            observed_body=f'{{"canary_day": "{expected_date.isoformat()}"}}' if self.ok else "{}",
            rollback_required=self.rollback_required,
        )


class RecordingSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, text: str) -> bool:
        self.messages.append(text)
        return True


# Real line stage names (core.line.bindings.LINE_STAGES minus "retrospective"
# == canary.REQUIRED_STAGES), all terminal-succeeded -- a fully released run.
_ALL_STAGES = [
    canary.CanaryStageObservation(stage=stage, status="succeeded", iteration=0, harness="role", actual_cost=0.01)
    for stage in canary.REQUIRED_STAGES
]

# The same run, but stopped partway through -- e.g. only grill/planning have
# reported in so far. Used to prove a non-terminal run is never green.
_PENDING_STAGES = [
    canary.CanaryStageObservation(stage="grill", status="succeeded", iteration=0),
    canary.CanaryStageObservation(stage="planning", status="running", iteration=0),
]


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    return tmp_path / "reports" / "canary"


# --------------------------------------------------------------------------
# Scenario selection (deterministic, no I/O)
# --------------------------------------------------------------------------


def test_scenario_selection_is_deterministic_by_weekday() -> None:
    monday = date(2026, 9, 21)
    tuesday = date(2026, 9, 22)
    thursday = date(2026, 9, 24)
    saturday = date(2026, 9, 26)

    assert monday.weekday() == 0
    assert thursday.weekday() == 3

    assert canary.select_scenario(monday) == "initial_test_fail"
    assert canary.select_scenario(thursday) == "smoke_rollback"
    assert canary.select_scenario(tuesday) == "normal"
    assert canary.select_scenario(saturday) == "normal"

    # Same weekday, different week -> same scenario (pure function of weekday).
    assert canary.select_scenario(monday) == canary.select_scenario(monday + timedelta(days=7))


def test_intake_command_is_deterministic_and_carries_scenario() -> None:
    day = date(2026, 9, 22)
    cmd = canary.build_intake_command(day, "normal")
    assert cmd.project_id == "darkfac-canary"
    assert cmd.channel == "canary"
    assert cmd.external_id == "canary:2026-09-22"
    assert cmd.payload["scenario"] == "normal"
    assert day.isoformat() in cmd.payload["problem"]

    cmd_again = canary.build_intake_command(day, "normal")
    assert cmd.payload_digest == cmd_again.payload_digest

    cmd_other_scenario = canary.build_intake_command(day, "initial_test_fail")
    assert cmd_other_scenario.external_id == cmd.external_id
    assert cmd_other_scenario.payload_digest != cmd.payload_digest


# --------------------------------------------------------------------------
# run_daily: report with all stages
# --------------------------------------------------------------------------


def test_run_daily_produces_report_with_all_stages(store, reports_dir) -> None:
    day = date(2026, 9, 22)  # Tuesday -> "normal" scenario
    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)

    report = canary.run_daily(
        day=day,
        store=store,
        observer=observer,
        smoke_client=smoke,
        clock=FixedClock(day),
        base_url="https://canary.example.test",
        reports_dir=reports_dir,
    )

    assert report.passed is True
    assert report.scenario == "normal"
    assert report.run_id is not None
    assert report.external_id == "canary:2026-09-22"
    observed_stage_names = {s.stage for s in report.stages}
    expected_stage_names = {s.stage for s in _ALL_STAGES}
    assert observed_stage_names == expected_stage_names
    assert report.smoke is not None and report.smoke.ok is True

    report_path = reports_dir / "2026-09-22.json"
    assert report_path.is_file()
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk["run_id"] == report.run_id
    assert len(on_disk["stages"]) == len(_ALL_STAGES)


# --------------------------------------------------------------------------
# Idempotency: second same-day run does not duplicate the demand
# --------------------------------------------------------------------------


def test_second_same_day_run_does_not_duplicate_demand(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)

    first = canary.run_daily(
        day=day, store=store, observer=observer, smoke_client=smoke,
        clock=FixedClock(day), base_url="https://canary.example.test", reports_dir=reports_dir,
    )
    second = canary.run_daily(
        day=day, store=store, observer=observer, smoke_client=smoke,
        clock=FixedClock(day), base_url="https://canary.example.test", reports_dir=reports_dir,
    )

    assert first.run_id == second.run_id
    assert first.demand_id == second.demand_id

    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM intake_commands WHERE channel = 'canary'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT COUNT(*) FROM runs WHERE project_id = 'darkfac-canary'")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()

    # Only one report file for the day; a passed day is sticky (observed once, not resubmitted).
    assert observer.calls == [first.run_id]
    assert second.passed is True
    assert len(list(reports_dir.glob("*.json"))) == 1


# --------------------------------------------------------------------------
# Failure path: notification carries stage + cause_code
# --------------------------------------------------------------------------


def test_failure_path_notifies_with_stage_and_cause_code(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    failing_stages = [
        canary.CanaryStageObservation(stage="grill", status="succeeded", iteration=0),
        canary.CanaryStageObservation(stage="development", status="failed", iteration=2, cause_code="test_failure"),
    ]
    observer = FakeObserver(failing_stages)
    sender = RecordingSender()

    report = canary.run_daily(
        day=day, store=store, observer=observer, clock=FixedClock(day),
        reports_dir=reports_dir, failure_sender=sender, send_weekly=False,
    )

    assert report.outcome == "failed"
    assert report.passed is False
    assert report.failing_stage == "development"
    assert report.cause_code == "test_failure"
    assert len(sender.messages) == 1
    assert "development" in sender.messages[0]
    assert "test_failure" in sender.messages[0]


# --------------------------------------------------------------------------
# HF-27-10 PR #37 review item 1: a non-terminal or unproven run must never
# report passed=True ("false green").
# --------------------------------------------------------------------------


def test_pending_stages_are_not_passed_and_stay_in_progress(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    observer = FakeObserver(_PENDING_STAGES)

    report = canary.run_daily(
        day=day, store=store, observer=observer, clock=FixedClock(day),
        reports_dir=reports_dir, send_weekly=False,
        timeout_seconds=canary.DEFAULT_TIMEOUT_SECONDS,  # far from elapsed -> not a timeout yet
    )

    assert report.outcome == "in_progress"
    assert report.passed is False
    assert report.failing_stage == "planning"


def test_missing_base_url_on_an_otherwise_complete_run_is_not_passed(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    observer = FakeObserver(_ALL_STAGES)

    report = canary.run_daily(
        day=day, store=store, observer=observer, clock=FixedClock(day),
        reports_dir=reports_dir, send_weekly=False,
        # no base_url -> smoke is mandatory and missing, not silently skipped
    )

    assert report.outcome == "failed"
    assert report.passed is False
    assert report.failing_stage == "smoke"
    assert report.cause_code == "canary_base_url_missing"


def test_non_terminal_run_past_the_deadline_times_out_and_notifies(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    observer = FakeObserver(_PENDING_STAGES)
    sender = RecordingSender()

    report = canary.run_daily(
        day=day, store=store, observer=observer, clock=FixedClock(day),
        reports_dir=reports_dir, send_weekly=False, failure_sender=sender,
        timeout_seconds=0,  # deadline is "now" -> any non-terminal run has already timed out
    )

    assert report.outcome == "timeout"
    assert report.passed is False
    assert report.cause_code == "canary_timeout"
    assert report.failing_stage == "planning"
    assert len(sender.messages) == 1
    assert "canary_timeout" in sender.messages[0]
    assert "TIMEOUT" in sender.messages[0]


def test_green_streak_never_counts_in_progress_or_timeout_or_dry_run() -> None:
    base = date(2026, 9, 1)
    reports = [
        canary.CanaryReport(date=base.isoformat(), scenario="normal", external_id="x", outcome="passed"),
        canary.CanaryReport(date=(base + timedelta(days=1)).isoformat(), scenario="normal", external_id="x", outcome="in_progress"),
        canary.CanaryReport(date=(base + timedelta(days=2)).isoformat(), scenario="normal", external_id="x", outcome="passed"),
    ]
    # day 1 (in_progress) breaks any streak that would otherwise bridge day 0 -> day 2
    assert canary.green_streak(reports) == 1

    timeout_reports = [
        canary.CanaryReport(date=base.isoformat(), scenario="normal", external_id="x", outcome="passed"),
        canary.CanaryReport(date=(base + timedelta(days=1)).isoformat(), scenario="normal", external_id="x", outcome="timeout"),
    ]
    assert canary.green_streak(timeout_reports) == 0

    dry_run_reports = [
        canary.CanaryReport(date=base.isoformat(), scenario="normal", external_id="x", outcome="dry_run"),
    ]
    assert canary.green_streak(dry_run_reports) == 0


def test_failing_smoke_check_is_reported_when_stages_all_succeed(store, reports_dir) -> None:
    day = date(2026, 9, 24)  # Thursday -> smoke_rollback scenario
    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=False, rollback_required=True)
    sender = RecordingSender()

    report = canary.run_daily(
        day=day, store=store, observer=observer, smoke_client=smoke, clock=FixedClock(day),
        base_url="https://canary.example.test", reports_dir=reports_dir,
        failure_sender=sender, send_weekly=False,
    )

    assert report.scenario == "smoke_rollback"
    assert report.passed is False
    assert report.failing_stage == "smoke"
    assert report.cause_code == "rollback_required"
    assert len(sender.messages) == 1


def test_dry_run_does_not_submit_intake(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    report = canary.run_daily(day=day, store=store, dry_run=True, reports_dir=reports_dir, clock=FixedClock(day))
    assert report.dry_run is True
    assert report.run_id is None

    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM intake_commands")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Weekly summary
# --------------------------------------------------------------------------


def _write_fake_report(reports_dir: Path, day: date, passed: bool, scenario: str = "normal") -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = canary.CanaryReport(
        date=day.isoformat(),
        scenario=scenario,
        external_id=f"canary:{day.isoformat()}",
        run_id=f"run-{day.isoformat()}",
        demand_id=f"dem-{day.isoformat()}",
        outcome="passed" if passed else "failed",
        failing_stage=None if passed else "development",
        cause_code=None if passed else "test_failure",
    )
    (reports_dir / f"{day.isoformat()}.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")


def test_weekly_summary_includes_streak_and_last_seven(reports_dir) -> None:
    base = date(2026, 9, 16)
    for i in range(7):
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    sender = RecordingSender()
    ok = canary.send_weekly_summary(reports_dir=reports_dir, today=base + timedelta(days=6), sender=sender)

    assert ok is True
    assert len(sender.messages) == 1
    text = sender.messages[0]
    assert "Green streak: 7" in text
    assert "V2 atingido" in text


# --------------------------------------------------------------------------
# green_streak / V2 criterion
# --------------------------------------------------------------------------


def test_green_streak_counts_consecutive_passing_days() -> None:
    base = date(2026, 9, 1)
    reports = [
        canary.CanaryReport(date=(base + timedelta(days=i)).isoformat(), scenario="normal", external_id="x", outcome="passed")
        for i in range(5)
    ]
    assert canary.green_streak(reports) == 5
    assert canary.is_v2_criterion_met(reports) is False


def test_green_streak_stops_at_a_failure() -> None:
    base = date(2026, 9, 1)
    reports = [
        canary.CanaryReport(date=(base + timedelta(days=0)).isoformat(), scenario="normal", external_id="x", outcome="passed"),
        canary.CanaryReport(date=(base + timedelta(days=1)).isoformat(), scenario="normal", external_id="x", outcome="failed"),
        canary.CanaryReport(date=(base + timedelta(days=2)).isoformat(), scenario="normal", external_id="x", outcome="passed"),
        canary.CanaryReport(date=(base + timedelta(days=3)).isoformat(), scenario="normal", external_id="x", outcome="passed"),
    ]
    assert canary.green_streak(reports) == 2


def test_green_streak_stops_at_a_gap_in_calendar_days() -> None:
    base = date(2026, 9, 1)
    reports = [
        canary.CanaryReport(date=base.isoformat(), scenario="normal", external_id="x", outcome="passed"),
        canary.CanaryReport(date=(base + timedelta(days=2)).isoformat(), scenario="normal", external_id="x", outcome="passed"),
    ]
    assert canary.green_streak(reports) == 1


def test_seven_consecutive_green_days_meets_v2() -> None:
    base = date(2026, 9, 1)
    reports = [
        canary.CanaryReport(date=(base + timedelta(days=i)).isoformat(), scenario="normal", external_id="x", outcome="passed")
        for i in range(7)
    ]
    assert canary.green_streak(reports) == 7
    assert canary.is_v2_criterion_met(reports) is True


# --------------------------------------------------------------------------
# Dogfood gating
# --------------------------------------------------------------------------


def _roadmap_item(
    item_id: str,
    *,
    tags: list[str],
    delivery_status: DeliveryStatus = DeliveryStatus.PLANNED,
    description: str = "Some safe description",
    completion_criteria: list[str] | None = None,
    source_locator: str | None = None,
) -> RoadmapItem:
    return RoadmapItem(
        id=item_id,
        project_id="darkfac",
        title=f"Title for {item_id}",
        description=description,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        delivery_status=delivery_status,
        horizon=PlanningHorizon.NOW,
        confidence=ConfidenceLevel.HIGH,
        tags=tags,
        completion_criteria=completion_criteria or ["Do the thing"],
        source_refs=(
            [RoadmapSourceRef(source_id="s", source_kind="document", label="l", locator=source_locator)]
            if source_locator
            else []
        ),
    )


def _write_roadmap(path: Path, items: list[RoadmapItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1",
        "project_id": "darkfac",
        "title": "Test roadmap",
        "items": [json.loads(item.model_dump_json()) for item in items],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_dogfood_gate_closed_when_streak_below_threshold(store, tmp_path, reports_dir) -> None:
    base = date(2026, 9, 1)
    for i in range(3):  # only 3 green days, below the 7 threshold
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(roadmap_path, [_roadmap_item("USR-100", tags=["line-ok"])])

    receipt = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert receipt is None


def test_dogfood_submits_one_eligible_item_when_streak_met(store, tmp_path, reports_dir) -> None:
    base = date(2026, 9, 1)
    for i in range(7):
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(
        roadmap_path,
        [
            _roadmap_item("USR-100", tags=["line-ok"]),
            _roadmap_item("USR-101", tags=["line-ok"]),
        ],
    )

    receipt = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert receipt is not None
    assert receipt.run_id is not None


def test_dogfood_is_one_at_a_time(store, tmp_path, reports_dir) -> None:
    base = date(2026, 9, 1)
    for i in range(7):
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(
        roadmap_path,
        [_roadmap_item("USR-100", tags=["line-ok"]), _roadmap_item("USR-101", tags=["line-ok"])],
    )

    first = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert first is not None

    second = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert second is None  # USR-100's run is still active -> gate stays closed


def test_dogfood_excludes_guard_protected_items(store, tmp_path, reports_dir) -> None:
    base = date(2026, 9, 1)
    for i in range(7):
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(
        roadmap_path,
        [
            _roadmap_item(
                "USR-200",
                tags=["line-ok"],
                description="Update AGENTS.md with the new policy",
            ),
            _roadmap_item("USR-201", tags=["line-ok"], description="Safe change to docs/whatever.md"),
        ],
    )

    receipt = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert receipt is not None
    # The protected item (USR-200) must have been skipped in favor of USR-201.
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT external_id FROM intake_commands WHERE channel = 'dogfood'")
        rows = [r[0] for r in cur.fetchall()]
        assert rows == ["dogfood:USR-201"]
    finally:
        conn.close()


def test_touches_protected_path_detects_governance_mentions() -> None:
    protected = _roadmap_item("X", tags=["line-ok"], description="Please edit FACTORY_RULES.md")
    safe = _roadmap_item("Y", tags=["line-ok"], description="Add a new CLI flag to core/demands/cli.py")
    assert dogfood.touches_protected_path(protected) is True
    assert dogfood.touches_protected_path(safe) is False


def test_pick_candidate_skips_non_planned_and_untagged_items() -> None:
    items = [
        _roadmap_item("A", tags=[], delivery_status=DeliveryStatus.PLANNED),
        _roadmap_item("B", tags=["line-ok"], delivery_status=DeliveryStatus.COMPLETED),
        _roadmap_item("C", tags=["line-ok"], delivery_status=DeliveryStatus.PLANNED),
    ]
    picked = dogfood.pick_candidate(items)
    assert picked is not None
    assert picked.id == "C"


# --------------------------------------------------------------------------
# HF-27-10 PR #37 review item 2: store selection must match the
# coordinator/worker's own Postgres-iff-DATABASE_URL rule.
# --------------------------------------------------------------------------


def test_default_store_is_sqlite_when_no_database_url(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    store = store_selection.default_control_store(sqlite_db_path=tmp_path / "control.db")
    assert isinstance(store, SQLiteControlStore)
    # And it is the persistent file, not an in-memory throwaway.
    assert (tmp_path / "control.db").is_file()


def test_default_store_is_postgres_when_database_url_set(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://darkfac_worker:x@localhost:5432/darkfac")
    store = store_selection.default_control_store(sqlite_db_path=tmp_path / "control.db")
    # psycopg is not installed/reachable in this environment, so
    # PostgresControlStore falls back to its own in-memory mock -- the
    # point of this test is only that we *asked* for Postgres, matching
    # exactly how CloudCoordinator/CloudWorker pick their store.
    assert isinstance(store, PostgresControlStore)


# --------------------------------------------------------------------------
# HF-27-10 PR #37 review item 4b: dogfood wiring is env-gated off by default.
# --------------------------------------------------------------------------


def test_run_daily_does_not_submit_dogfood_by_default_even_when_streak_is_met(
    monkeypatch, store, tmp_path, reports_dir
) -> None:
    monkeypatch.delenv("DARKFAC_DOGFOOD_ENABLED", raising=False)
    base = date(2026, 9, 15)
    for i in range(6):  # 6 prior green days; today's run (below) would be the 7th
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    day = base + timedelta(days=6)
    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(roadmap_path, [_roadmap_item("USR-300", tags=["line-ok"])])
    monkeypatch.setattr(dogfood, "ROADMAP_PATH", roadmap_path)

    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)
    report = canary.run_daily(
        day=day, store=store, observer=observer, smoke_client=smoke, clock=FixedClock(day),
        base_url="https://canary.example.test", reports_dir=reports_dir, send_weekly=False,
    )
    assert report.outcome == "passed"

    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM intake_commands WHERE channel = 'dogfood'")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_run_daily_submits_dogfood_when_enabled_and_streak_met(monkeypatch, store, tmp_path, reports_dir) -> None:
    base = date(2026, 9, 15)
    for i in range(6):
        _write_fake_report(reports_dir, base + timedelta(days=i), passed=True)

    day = base + timedelta(days=6)
    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(roadmap_path, [_roadmap_item("USR-301", tags=["line-ok"])])
    # `submit_dogfood_item` reads the module-level ROADMAP_PATH at call time
    # when no explicit `roadmap_path` is passed through `run_daily`.
    monkeypatch.setattr(dogfood, "ROADMAP_PATH", roadmap_path)

    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)

    report = canary.run_daily(
        day=day, store=store, observer=observer, smoke_client=smoke, clock=FixedClock(day),
        base_url="https://canary.example.test", reports_dir=reports_dir, send_weekly=False,
        run_dogfood=True,  # equivalent to DARKFAC_DOGFOOD_ENABLED=true, without touching env
    )

    assert report.outcome == "passed"

    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT external_id FROM intake_commands WHERE channel = 'dogfood'")
        rows = [r[0] for r in cur.fetchall()]
        assert rows == ["dogfood:USR-301"]
    finally:
        conn.close()


def test_dogfood_enabled_from_env(monkeypatch) -> None:
    monkeypatch.delenv("DARKFAC_DOGFOOD_ENABLED", raising=False)
    assert canary._dogfood_enabled_from_env() is False
    monkeypatch.setenv("DARKFAC_DOGFOOD_ENABLED", "true")
    assert canary._dogfood_enabled_from_env() is True
    monkeypatch.setenv("DARKFAC_DOGFOOD_ENABLED", "0")
    assert canary._dogfood_enabled_from_env() is False


# --------------------------------------------------------------------------
# HF-27-10: loop mode (`run_loop`) -- the canary must actually run
# unattended in the cloud container instead of needing a human/cron to
# invoke it once. A fake sleeper makes the loop run instantly in tests.
# --------------------------------------------------------------------------


class FakeSleeper:
    """Records sleep calls instead of actually waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_run_loop_runs_k_iterations_with_fake_sleeper(store, reports_dir) -> None:
    day = date(2026, 9, 22)
    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)
    sleeper = FakeSleeper()

    reports = canary.run_loop(
        every_seconds=3600,
        sleeper=sleeper,
        max_iterations=3,
        day=day,
        store=store,
        observer=observer,
        smoke_client=smoke,
        clock=FixedClock(day),
        base_url="https://canary.example.test",
        reports_dir=reports_dir,
        send_weekly=False,
    )

    assert len(reports) == 3
    assert all(r.outcome == "passed" for r in reports)
    # Sleeps between iterations, not after the last one.
    assert sleeper.calls == [3600, 3600]


def test_run_loop_exception_in_one_iteration_does_not_stop_the_loop(store, reports_dir, monkeypatch) -> None:
    day = date(2026, 9, 22)
    sleeper = FakeSleeper()
    calls = {"n": 0}

    def _flaky_run_daily(*args: object, **kwargs: object) -> canary.CanaryReport:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("transient failure")
        return canary.CanaryReport(
            date=day.isoformat(), scenario="normal", external_id="x", outcome="passed"
        )

    monkeypatch.setattr(canary, "run_daily", _flaky_run_daily)

    reports = canary.run_loop(every_seconds=1, sleeper=sleeper, max_iterations=3)

    # Iteration 2 raised and was swallowed, so only 2 reports were collected,
    # but the loop still ran all 3 iterations (3 calls to run_daily) and slept
    # between each of them, proving the exception did not stop the loop.
    assert calls["n"] == 3
    assert len(reports) == 2
    assert sleeper.calls == [1, 1]


def test_run_loop_one_shot_is_unchanged(store, reports_dir) -> None:
    """max_iterations=1 with every_seconds set behaves like a single run_daily
    call: no sleep happens since there is no next iteration to wait for."""
    day = date(2026, 9, 22)
    observer = FakeObserver(_ALL_STAGES)
    smoke = FakeSmokeClient(ok=True)
    sleeper = FakeSleeper()

    reports = canary.run_loop(
        every_seconds=3600,
        sleeper=sleeper,
        max_iterations=1,
        day=day,
        store=store,
        observer=observer,
        smoke_client=smoke,
        clock=FixedClock(day),
        base_url="https://canary.example.test",
        reports_dir=reports_dir,
        send_weekly=False,
    )

    assert len(reports) == 1
    assert reports[0].outcome == "passed"
    assert sleeper.calls == []


def test_cli_run_defaults_to_one_shot_without_every_seconds(monkeypatch, reports_dir) -> None:
    """`--every-seconds` defaults to 0, so `python -m core.line.canary run`
    with no flag behaves exactly as it always did: one `run_daily` call, no
    loop, no sleeping."""
    calls: list[dict[str, object]] = []

    def _fake_run_daily(**kwargs: object) -> canary.CanaryReport:
        calls.append(kwargs)
        return canary.CanaryReport(date="2026-09-22", scenario="normal", external_id="x", outcome="passed")

    monkeypatch.setattr(canary, "run_daily", _fake_run_daily)
    loop_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        canary, "run_loop", lambda **kwargs: loop_calls.append(kwargs) or []
    )

    args = canary.main.__globals__["argparse"].Namespace(
        command="run", date=None, base_url=None, dry_run=False,
        timeout_seconds=canary.DEFAULT_TIMEOUT_SECONDS, every_seconds=0,
    )
    exit_code = canary._cli_run(args)

    assert exit_code == 0
    assert len(calls) == 1
    assert loop_calls == []


def test_cli_run_with_every_seconds_dispatches_to_run_loop(monkeypatch) -> None:
    loop_calls: list[dict[str, object]] = []

    def _fake_run_loop(**kwargs: object) -> list[canary.CanaryReport]:
        loop_calls.append(kwargs)
        return []

    monkeypatch.setattr(canary, "run_loop", _fake_run_loop)
    daily_calls: list[dict[str, object]] = []
    monkeypatch.setattr(canary, "run_daily", lambda **kwargs: daily_calls.append(kwargs))

    args = canary.main.__globals__["argparse"].Namespace(
        command="run", date=None, base_url="https://canary.example.test", dry_run=False,
        timeout_seconds=canary.DEFAULT_TIMEOUT_SECONDS, every_seconds=3600,
    )
    exit_code = canary._cli_run(args)

    assert exit_code == 0
    assert len(loop_calls) == 1
    assert loop_calls[0]["every_seconds"] == 3600
    assert daily_calls == []


# --------------------------------------------------------------------------
# Alert dedup across restarts + cause_code never lost / never "None"
# (production: the same CRITICAL alert arrived at 12:18 and 14:54 UTC, one per
# container restart, saying cause_code=None)
# --------------------------------------------------------------------------

_GRILL_FAILED = [
    canary.CanaryStageObservation(stage="grill", status="failed", iteration=2, cause_code="handler_error:X"),
]
_DEV_FAILED = [
    canary.CanaryStageObservation(stage="grill", status="succeeded", iteration=0),
    canary.CanaryStageObservation(stage="development", status="failed", iteration=1, cause_code="test_failure"),
]


def _run(store, reports_dir, observer_stages, sender, day=date(2026, 9, 22), **kwargs):
    kwargs.setdefault("max_attempts_per_day", 1)  # these tests pin single-attempt alert dedup
    return canary.run_daily(
        day=day, store=store, observer=FakeObserver(observer_stages), clock=FixedClock(day),
        reports_dir=reports_dir, failure_sender=sender, send_weekly=False, **kwargs,
    )


def test_same_failure_is_alerted_once_even_across_restarts(store, reports_dir) -> None:
    sender = RecordingSender()

    first = _run(store, reports_dir, _GRILL_FAILED, sender)
    # "container restart": brand-new call, nothing in memory, only the report volume survives.
    second = _run(store, reports_dir, _GRILL_FAILED, sender)
    third = _run(store, reports_dir, _GRILL_FAILED, sender)

    assert len(sender.messages) == 1
    assert first.notified is not None and first.notified.failing_stage == "grill"
    assert second.notified == first.notified == third.notified  # state carried forward
    on_disk = json.loads((reports_dir / "2026-09-22.json").read_text(encoding="utf-8"))
    assert on_disk["notified"]["outcome"] == "failed"
    assert on_disk["notified"]["failing_stage"] == "grill"
    assert on_disk["notified"]["run_id"] == first.run_id
    assert on_disk["notified"]["at"]


def test_alert_is_resent_when_the_failing_stage_or_outcome_changes(store, reports_dir) -> None:
    sender = RecordingSender()
    _run(store, reports_dir, _GRILL_FAILED, sender)
    _run(store, reports_dir, _DEV_FAILED, sender)  # different failing stage -> new alert
    _run(store, reports_dir, _DEV_FAILED, sender)  # same again -> silent
    assert len(sender.messages) == 2
    assert "grill" in sender.messages[0] and "development" in sender.messages[1]

    # A non-terminal run past its deadline is a different outcome (timeout) -> alerts too.
    _run(store, reports_dir, _PENDING_STAGES, sender, timeout_seconds=0)
    assert len(sender.messages) == 3 and "TIMEOUT" in sender.messages[2]


def test_failed_then_in_progress_then_failed_again_does_not_realert(store, reports_dir) -> None:
    sender = RecordingSender()
    _run(store, reports_dir, _GRILL_FAILED, sender)
    mid = _run(store, reports_dir, _PENDING_STAGES, sender)
    assert mid.outcome == "in_progress" and mid.notified is not None  # marker survives non-failure iterations
    _run(store, reports_dir, _GRILL_FAILED, sender)
    assert len(sender.messages) == 1


def test_undelivered_alert_is_not_recorded_and_is_retried(store, reports_dir) -> None:
    class FlakySender:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, text: str) -> bool:
            self.calls += 1
            return self.calls > 1  # first attempt is not delivered

    sender = FlakySender()
    first = _run(store, reports_dir, _GRILL_FAILED, sender)
    assert first.notified is None
    second = _run(store, reports_dir, _GRILL_FAILED, sender)
    assert second.notified is not None
    _run(store, reports_dir, _GRILL_FAILED, sender)
    assert sender.calls == 2


def test_alert_text_never_contains_none(store, reports_dir) -> None:
    sender = RecordingSender()
    stages = [canary.CanaryStageObservation(stage="grill", status="failed", iteration=0)]  # no cause anywhere
    report = _run(store, reports_dir, stages, sender)
    assert report.failing_stage == "grill"
    assert "None" not in sender.messages[0]
    assert "cause_code=unknown" in sender.messages[0]


class _StatusStore:
    """Store double exposing only get_run_status, like the real observers' contract."""

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = jobs

    def get_run_status(self, run_id: str) -> dict:
        return {"run_id": run_id, "jobs": self.jobs}


def _job(stage: str, status: str, iteration: int, cause: str | None) -> dict:
    return {"stage": stage, "status": status, "iteration": iteration, "cause_code": cause, "role": "grill_engine"}


def test_observer_keeps_the_failing_jobs_own_cause_code() -> None:
    obs = canary.ControlStoreLineObserver(_StatusStore([_job("grill", "failed", 0, "handler_error:X")])).observe("r")
    assert obs[0].cause_code == "handler_error:X"


def test_observer_falls_back_to_last_retry_cause_when_final_failed_row_has_none() -> None:
    jobs = [
        _job("grill", "retry", 0, "no_authenticated_harness"),
        _job("grill", "retry", 1, "empty_output"),
        _job("grill", "failed", 2, None),  # loop/retry cap: final row carries no cause
    ]
    failed = [o for o in canary.ControlStoreLineObserver(_StatusStore(jobs)).observe("r") if o.status == "failed"]
    assert failed[0].cause_code == "empty_output"


def test_observer_uses_retry_cap_exhausted_when_no_earlier_cause_exists() -> None:
    jobs = [_job("grill", "retry", 0, None), _job("grill", "failed", 1, None)]
    failed = [o for o in canary.ControlStoreLineObserver(_StatusStore(jobs)).observe("r") if o.status == "failed"]
    assert failed[0].cause_code == "retry_cap_exhausted"

    lone = canary.ControlStoreLineObserver(_StatusStore([_job("grill", "failed", 0, None)])).observe("r")
    assert lone[0].cause_code is None  # nothing to say: alert text falls back to "unknown"


def test_postgres_get_run_status_returns_job_cause_code_and_retry_count() -> None:
    """Where cause_code was lost: the Postgres get_run_status SELECT never included
    jobs.cause_code (nor retry_count), so the canary always saw None."""
    from core.orchestrator.adapters.control_postgres import PostgresControlStore
    from core.workflow.control_contracts import RuntimeOwner

    now = datetime(2026, 9, 29, 11, 49, tzinfo=UTC)
    run_row = ("run-1", "darkfac-canary", "d", "1", "cloud_dbos_postgres", "autonomous", "active", "p" * 64, "1.0", now, now, None)
    job_row = (
        "darkfac-canary", "1.0", "grill", 2, "failed", "grill_engine", 3, None, 0.5, [], [],
        now, now, now, now, "handler_error:ValidationError~ab12cd34", 3,
    )
    executed: list[str] = []

    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): executed.append(sql)
        def fetchone(self): return run_row
        def fetchall(self): return [job_row]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def cursor(self): return _Cursor()

    class _Psycopg:
        @staticmethod
        def connect(url): return _Conn()

    store = PostgresControlStore(mock_mode=True, runtime_owner=RuntimeOwner.HF05_SQLITE.value)
    store.mock_mode = False
    store._psycopg = _Psycopg
    store.raw_url = "postgresql://fake"

    status = store.get_run_status("run-1")
    assert status is not None
    job = status["jobs"][0]
    assert job["cause_code"] == "handler_error:ValidationError~ab12cd34"
    assert job["retry_count"] == 3
    assert any("cause_code" in sql for sql in executed)


# --------------------------------------------------------------------------
# Same-day retry (line-autonomy unblock)
# --------------------------------------------------------------------------


class RunAwareObserver:
    """Per-run canned telemetry, so each attempt's run can have its own fate."""

    def __init__(self, default: list[canary.CanaryStageObservation] | None = None) -> None:
        self.by_run: dict[str, list[canary.CanaryStageObservation]] = {}
        self.default = default or []
        self.calls: list[str] = []

    def observe(self, run_id: str) -> list[canary.CanaryStageObservation]:
        self.calls.append(run_id)
        return list(self.by_run.get(run_id, self.default))


def _retry_run(store, reports_dir, observer, sender, **kwargs):
    day = kwargs.pop("day", date(2026, 9, 22))
    kwargs.setdefault("max_attempts_per_day", 4)
    return canary.run_daily(
        day=day, store=store, observer=observer, clock=FixedClock(day), smoke_client=FakeSmokeClient(ok=True),
        base_url="https://canary.example.test", reports_dir=reports_dir, failure_sender=sender,
        send_weekly=False, **kwargs,
    )


def _intake_count(store) -> int:
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM intake_commands WHERE channel = 'canary'")
        return cur.fetchone()[0]
    finally:
        conn.close()


def test_attempt_external_ids_and_attempt_one_payload_is_unchanged() -> None:
    day = date(2026, 9, 22)
    assert canary.attempt_external_id(day, 1) == "canary:2026-09-22"
    assert canary.attempt_external_id(day, 2) == "canary:2026-09-22:a2"
    first = canary.build_intake_command(day, "normal")
    assert first.external_id == "canary:2026-09-22"
    assert "attempt" not in first.payload  # byte-identical to the pre-retry payload
    assert canary.build_intake_command(day, "normal", attempt=1).payload_digest == first.payload_digest
    third = canary.build_intake_command(day, "normal", attempt=3)
    assert third.external_id == "canary:2026-09-22:a3"
    assert third.payload["attempt"] == 3
    assert third.payload_digest != first.payload_digest


def test_terminal_failure_is_retried_as_a_new_attempt_on_the_next_iteration(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)

    first = _retry_run(store, reports_dir, observer, sender)
    assert first.attempt == 1 and first.outcome == "failed"
    assert _intake_count(store) == 1  # the failure iteration itself never submits

    second = _retry_run(store, reports_dir, observer, sender)
    assert second.attempt == 2
    assert second.external_id == "canary:2026-09-22:a2"
    assert second.run_id != first.run_id
    assert _intake_count(store) == 2
    assert [a.attempt for a in second.attempts] == [1, 2]
    assert second.attempts[0].outcome == "failed"


def test_attempt_is_never_submitted_while_the_previous_is_in_flight(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_PENDING_STAGES)

    for _ in range(4):
        report = _retry_run(store, reports_dir, observer, sender)
        assert report.attempt == 1 and report.outcome == "in_progress"
    assert _intake_count(store) == 1


def test_failed_attempt_whose_run_resumed_is_not_replaced(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    first = _retry_run(store, reports_dir, observer, sender)
    observer.by_run[first.run_id] = _PENDING_STAGES  # the line resumed the same run
    again = _retry_run(store, reports_dir, observer, sender)
    assert again.attempt == 1 and again.outcome == "in_progress"
    assert _intake_count(store) == 1


def test_retries_are_capped_by_max_attempts_per_day(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    reports = [_retry_run(store, reports_dir, observer, sender, max_attempts_per_day=3) for _ in range(8)]
    assert _intake_count(store) == 3
    assert reports[-1].attempt == 3 and reports[-1].outcome == "failed"
    assert [a.attempt for a in reports[-1].attempts] == [1, 2, 3]


def test_max_attempts_default_and_env(monkeypatch) -> None:
    monkeypatch.delenv(canary.MAX_ATTEMPTS_ENV, raising=False)
    assert canary.max_attempts_from_env() == 4
    monkeypatch.setenv(canary.MAX_ATTEMPTS_ENV, "2")
    assert canary.max_attempts_from_env() == 2
    for bad in ("0", "-3", "abc", ""):
        monkeypatch.setenv(canary.MAX_ATTEMPTS_ENV, bad)
        assert canary.max_attempts_from_env() == 4


def test_env_cap_is_honoured_by_run_daily(monkeypatch, store, reports_dir) -> None:
    monkeypatch.setenv(canary.MAX_ATTEMPTS_ENV, "2")
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    for _ in range(6):
        canary.run_daily(
            day=date(2026, 9, 22), store=store, observer=observer, clock=FixedClock(date(2026, 9, 22)),
            reports_dir=reports_dir, failure_sender=sender, send_weekly=False,
        )
    assert _intake_count(store) == 2


def test_day_is_green_when_a_later_attempt_passes_and_then_stays_green(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    first = _retry_run(store, reports_dir, observer, sender)
    second = _retry_run(store, reports_dir, observer, sender)
    observer.by_run[second.run_id] = _ALL_STAGES  # attempt 2 goes green
    third = _retry_run(store, reports_dir, observer, sender)
    assert first.run_id != second.run_id
    assert third.attempt == 2 and third.passed is True
    assert [a.outcome for a in third.attempts] == ["failed", "passed"]

    # Sticky: later iterations submit nothing and keep the day green.
    for _ in range(3):
        again = _retry_run(store, reports_dir, observer, sender)
        assert again.passed is True and again.attempt == 2
    assert _intake_count(store) == 2

    on_disk = canary.load_reports(reports_dir)
    assert len(on_disk) == 1 and on_disk[0].passed is True
    assert canary.green_streak(on_disk) == 1


def test_alert_dedup_is_per_attempt_so_a_new_attempt_failing_alike_alerts_once(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    _retry_run(store, reports_dir, observer, sender, max_attempts_per_day=2)  # a1 fails -> alert
    _retry_run(store, reports_dir, observer, sender, max_attempts_per_day=2)  # a2 fails alike -> alert again
    assert len(sender.messages) == 2
    assert "tentativa=1" in sender.messages[0] and "tentativa=2" in sender.messages[1]
    # Cap reached: re-observing the same failed attempt never re-alerts.
    for _ in range(3):
        _retry_run(store, reports_dir, observer, sender, max_attempts_per_day=2)
    assert len(sender.messages) == 2


def test_legacy_report_without_attempts_is_adopted_as_attempt_one(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_DEV_FAILED)
    first = _retry_run(store, reports_dir, observer, sender, max_attempts_per_day=1)
    path = reports_dir / "2026-09-22.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    for key in ("attempt", "attempts", "weekly_summary_sent_at"):
        legacy.pop(key)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    second = _retry_run(store, reports_dir, observer, sender)
    assert second.attempt == 2 and second.external_id == "canary:2026-09-22:a2"
    assert second.attempts[0].run_id == first.run_id
    # Legacy marker adopted: attempt 1 is not re-alerted (only attempt 2's own failure alerts).
    assert len(sender.messages) == 2
    assert sum("tentativa=1" in m for m in sender.messages) == 1


def test_timeout_attempt_is_retried_too(store, reports_dir) -> None:
    sender = RecordingSender()
    observer = RunAwareObserver(default=_PENDING_STAGES)
    first = _retry_run(store, reports_dir, observer, sender, timeout_seconds=0)
    assert first.outcome == "timeout"
    second = _retry_run(store, reports_dir, observer, sender, timeout_seconds=0)
    assert second.attempt == 2 and _intake_count(store) == 2


def test_weekly_summary_is_sent_once_per_monday_even_when_looping(store, reports_dir) -> None:
    monday = date(2026, 9, 21)
    summaries = RecordingSender()
    observer = RunAwareObserver(default=_ALL_STAGES)
    for _ in range(4):
        canary.run_daily(
            day=monday, store=store, observer=observer, clock=FixedClock(monday),
            smoke_client=FakeSmokeClient(ok=True), base_url="https://canary.example.test",
            reports_dir=reports_dir, failure_sender=RecordingSender(), summary_sender=summaries,
        )
    assert len(summaries.messages) == 1


def test_dogfood_min_streak_env(monkeypatch) -> None:
    monkeypatch.delenv(dogfood.MIN_STREAK_ENV, raising=False)
    assert dogfood.min_green_streak_from_env() == 7
    monkeypatch.setenv(dogfood.MIN_STREAK_ENV, "1")
    assert dogfood.min_green_streak_from_env() == 1
    for bad in ("0", "-1", "x", " "):
        monkeypatch.setenv(dogfood.MIN_STREAK_ENV, bad)
        assert dogfood.min_green_streak_from_env() == 7


def test_dogfood_gate_follows_env_min_streak(monkeypatch, store, tmp_path, reports_dir) -> None:
    _write_fake_report(reports_dir, date(2026, 9, 1), passed=True)
    roadmap_path = tmp_path / "roadmap.json"
    _write_roadmap(roadmap_path, [_roadmap_item("USR-100", tags=["line-ok"])])

    monkeypatch.delenv(dogfood.MIN_STREAK_ENV, raising=False)
    assert dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir) is None

    monkeypatch.setenv(dogfood.MIN_STREAK_ENV, "1")
    receipt = dogfood.submit_dogfood_item(store=store, roadmap_path=roadmap_path, reports_dir=reports_dir)
    assert receipt is not None

    # An explicit argument still wins over the env.
    assert dogfood.submit_dogfood_item(
        store=store, roadmap_path=roadmap_path, reports_dir=reports_dir, min_green_streak=5
    ) is None


# --------------------------------------------------------------------------
# Unambiguous demand text + replay of attempts accepted with the legacy text
# --------------------------------------------------------------------------


def test_demand_text_states_that_canary_day_is_a_fixed_literal_and_keeps_sha() -> None:
    day = date(2026, 9, 30)
    text = canary._demand_text(day)
    assert "`2026-09-30`" in text
    assert "string literal fixa" in text and "constante" in text and "NAO calculada" in text
    assert "`sha`" in text  # the existing field is kept
    assert "data de hoje" not in text  # the ambiguous legacy wording is gone


def test_intake_payload_carries_the_auto_grill_policy_but_the_legacy_one_does_not() -> None:
    day = date(2026, 9, 30)
    current = canary.build_intake_command(day, "normal")
    legacy = canary.build_intake_command(day, "normal", legacy_text=True)
    assert current.payload["grill_policy"] == "auto"
    assert "grill_policy" not in legacy.payload
    assert legacy.payload["problem"] == (
        "Adicione ao /version o campo `canary_day` com a data de hoje `2026-09-30` e um teste."
    )
    assert current.payload_digest != legacy.payload_digest
    assert current.external_id == legacy.external_id
    assert canary.build_intake_command(day, "normal", 2).external_id == "canary:2026-09-30:a2"


def _accept_legacy(store, day: date, attempt: int):
    from datetime import datetime as _dt

    from core.demands.autonomous_intake import AutonomousIntakeService

    command = canary.build_intake_command(day, canary.select_scenario(day), attempt, legacy_text=True)
    return AutonomousIntakeService(store=store).accept(command, _dt(day.year, day.month, day.day, 8, tzinfo=UTC))


def test_attempt_accepted_with_the_legacy_text_is_replayed_not_conflicted(store, reports_dir) -> None:
    day = date(2026, 9, 30)  # Wednesday -> "normal"
    legacy_receipt = _accept_legacy(store, day, 2)
    observer = RunAwareObserver(default=_PENDING_STAGES)
    # attempt 1 was terminal in a previous iteration, attempt 2 was accepted with the old text
    canary._write_report(
        reports_dir, day,
        canary.CanaryReport(
            date=day.isoformat(), scenario="normal", external_id="canary:2026-09-30:a2",
            run_id=legacy_receipt.run_id, demand_id=legacy_receipt.demand_id, outcome="in_progress",
            attempt=2,
            attempts=[
                canary.CanaryAttempt(attempt=1, external_id="canary:2026-09-30", outcome="timeout"),
                canary.CanaryAttempt(
                    attempt=2, external_id="canary:2026-09-30:a2", run_id=legacy_receipt.run_id, outcome="in_progress"
                ),
            ],
        ),
    )

    for _ in range(3):
        report = _retry_run(store, reports_dir, observer, RecordingSender(), day=day)
        assert report.attempt == 2
        assert report.run_id == legacy_receipt.run_id  # the legacy run is still the one observed
        assert report.outcome == "in_progress" and report.cause_code != "idempotency_conflict"
    assert observer.calls == [legacy_receipt.run_id] * 3
    assert _intake_count(store) == 1  # nothing new was created


def test_legacy_attempt_that_later_passes_is_observed_to_green(store, reports_dir) -> None:
    day = date(2026, 9, 30)
    receipt = _accept_legacy(store, day, 1)
    observer = RunAwareObserver(default=_ALL_STAGES)
    report = _retry_run(store, reports_dir, observer, RecordingSender(), day=day)
    assert report.attempt == 1 and report.run_id == receipt.run_id and report.passed is True


def test_new_attempts_are_submitted_with_the_new_text_and_replay_cleanly(store, reports_dir) -> None:
    day = date(2026, 9, 30)
    observer = RunAwareObserver(default=_PENDING_STAGES)
    first = _retry_run(store, reports_dir, observer, RecordingSender(), day=day)
    again = _retry_run(store, reports_dir, observer, RecordingSender(), day=day)
    assert first.run_id == again.run_id and _intake_count(store) == 1
    payload = store.get_run_payload(first.run_id)
    assert payload["grill_policy"] == "auto" and "string literal fixa" in payload["problem"]


def test_a_truly_conflicting_payload_still_fails_closed(store, reports_dir) -> None:
    from datetime import datetime as _dt

    from core.demands.autonomous_intake import AutonomousIntakeService
    from core.workflow.control_contracts import IntakeCommand

    day = date(2026, 9, 30)
    bogus = canary.build_intake_command(day, "normal")
    other = IntakeCommand(
        project_id=bogus.project_id, channel=bogus.channel, external_id=bogus.external_id,
        mode="autonomous", policy_ref=bogus.policy_ref, payload={**bogus.payload, "problem": "algo totalmente diferente"},
    )
    AutonomousIntakeService(store=store).accept(other, _dt(2026, 9, 30, 8, tzinfo=UTC))
    report = _retry_run(store, reports_dir, RunAwareObserver(), RecordingSender(), day=day, max_attempts_per_day=1)
    assert report.outcome == "failed" and report.cause_code == "idempotency_conflict"
