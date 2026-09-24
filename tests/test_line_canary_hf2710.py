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

    # Only one report file for the day, observed (not resubmitted) twice.
    assert observer.calls == [first.run_id, second.run_id]
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
