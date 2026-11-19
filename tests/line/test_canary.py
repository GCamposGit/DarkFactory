"""Behavioral tests for core.line.canary preflight, circuit-breaker and PR reconciliation (USR-470)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.line import canary
from core.line.canary import (
    CanaryStageObservation,
    SmokeResult,
    _reconcile_canary_open_prs,
    run_daily,
)
from core.workflow.control_contracts import IntakeReceipt


class _FixedClock:
    def __init__(self, current: datetime) -> None:
        self._now = current

    def today(self) -> date:
        return self._now.date()

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: Any) -> None:
        self._now += timedelta(**delta)


class _StubStore:
    def __init__(
        self,
        *,
        active_runs: dict[str, dict[str, Any]] | None = None,
        run_statuses: dict[str, dict[str, Any]] | None = None,
        reserved_runs: set[str] | None = None,
    ) -> None:
        self.active_runs = active_runs or {}
        self.run_statuses = run_statuses or {}
        self.reserved_runs = reserved_runs or set()
        self.accept_calls: list[str] = []

    def list_active_run_ids(self) -> list[str]:
        return list(self.active_runs.keys())

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        return self.run_statuses.get(run_id) or self.active_runs.get(run_id)

    def get_active_reservation_for_run(self, run_id: str) -> dict[str, Any] | None:
        return {"run_id": run_id} if run_id in self.reserved_runs else None

    def get_run_created_at(self, _run_id: str) -> str | None:
        return None


def _patch_accept(monkeypatch: pytest.MonkeyPatch, store: _StubStore, run_id: str = "run-new") -> None:
    def fake_accept(self: Any, command: Any, now: Any) -> IntakeReceipt:
        store.accept_calls.append(command.external_id)
        return IntakeReceipt(
            demand_id="DEM-1",
            demand_version="1.0",
            run_id=run_id,
            initial_job_id="job-1",
            mode="autonomous",
            committed_at=now.isoformat(),
        )

    monkeypatch.setattr(canary.AutonomousIntakeService, "accept", fake_accept)


def test_non_retryable_failure_does_not_submit_extra_attempts_same_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _StubStore()
    _patch_accept(monkeypatch, store, run_id="run-fail-pr")
    clock = _FixedClock(datetime(2026, 11, 19, 10, 0, tzinfo=UTC))
    observer = SimpleNamespace(
        observe=lambda _rid: [
            CanaryStageObservation(
                stage="development", status="failed", iteration=0, cause_code="active_pull_request"
            )
        ]
    )
    alerts: list[str] = []

    first = run_daily(
        day=clock.today(), store=store, observer=observer, clock=clock,
        failure_sender=lambda msg: alerts.append(msg) or True,
        reports_dir=tmp_path, send_weekly=False, max_attempts_per_day=4,
        pr_reconciler=lambda _s: [],
    )
    clock.advance(minutes=15)
    second = run_daily(
        day=clock.today(), store=store, observer=observer, clock=clock,
        failure_sender=lambda msg: alerts.append(msg) or True,
        reports_dir=tmp_path, send_weekly=False, max_attempts_per_day=4,
        pr_reconciler=lambda _s: [],
    )

    assert first.outcome == "failed" and first.cause_code == "active_pull_request"
    assert len(second.attempts) == 1
    assert store.accept_calls == ["canary:2026-11-19", "canary:2026-11-19"]
    assert len(alerts) == 1


def test_single_flight_skips_intake_when_previous_canary_run_is_still_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yesterday = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)
    store = _StubStore(
        active_runs={
            "run-prev-active": {
                "project_id": canary.CANARY_PROJECT_ID,
                "created_at": yesterday.isoformat(),
                "jobs": [{"stage": "development", "status": "claimed"}],
            }
        }
    )
    _patch_accept(monkeypatch, store)
    clock = _FixedClock(datetime(2026, 11, 19, 0, 15, tzinfo=UTC))
    alerts: list[str] = []

    report = run_daily(
        day=clock.today(), store=store, clock=clock,
        failure_sender=lambda msg: alerts.append(msg) or True,
        reports_dir=tmp_path, send_weekly=False,
        pr_reconciler=lambda _s: [],
    )

    assert report.outcome == "in_progress"
    assert report.failing_stage == "preflight"
    assert "run-prev-active" in report.notes
    assert store.accept_calls == []
    assert alerts == []


def test_reconcile_canary_open_prs_closes_terminal_pr_without_deleting_branch_and_keeps_active() -> None:
    store = _StubStore(
        run_statuses={
            "run-b86124167e66": {
                "project_id": canary.CANARY_PROJECT_ID,
                "jobs": [
                    {"stage": "grill", "status": "succeeded"},
                    {"stage": "integration", "status": "failed"},
                ],
            },
            "run-abc123def": {
                "project_id": canary.CANARY_PROJECT_ID,
                "jobs": [{"stage": "development", "status": "claimed"}],
            },
        }
    )
    registry = SimpleNamespace(
        get_project=lambda _pid: SimpleNamespace(repo_url="https://github.com/GCamposGit/darkfac-canary.git")
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            payload = [
                {
                    "number": 46,
                    "url": "https://github.com/GCamposGit/darkfac-canary/pull/46",
                    "title": "Canario T1",
                    "headRefName": "df/run-b86124167e66",
                    "body": "T1",
                },
                {
                    "number": 47,
                    "url": "https://github.com/GCamposGit/darkfac-canary/pull/47",
                    "title": "Active T1",
                    "headRefName": "df/run-abc123def",
                    "body": "T1",
                },
            ]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    blocking = _reconcile_canary_open_prs(store, project_registry=registry, run=fake_run)

    close_cmds = [c for c in calls if c[:3] == ["gh", "pr", "close"]]
    assert len(close_cmds) == 1
    assert "46" in close_cmds[0]
    assert "--delete-branch" not in close_cmds[0]
    assert [b["number"] for b in blocking] == [47]


def test_preflight_blocking_pr_skips_intake_and_reports_active_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _StubStore()
    _patch_accept(monkeypatch, store)
    clock = _FixedClock(datetime(2026, 11, 19, 8, 0, tzinfo=UTC))
    alerts: list[str] = []

    report = run_daily(
        day=clock.today(), store=store, clock=clock,
        failure_sender=lambda msg: alerts.append(msg) or True,
        reports_dir=tmp_path, send_weekly=False,
        pr_reconciler=lambda _s: [{"number": 46, "head_ref": "df/run-b86124167e66", "url": "u", "title": "t"}],
    )

    assert report.outcome == "failed"
    assert report.failing_stage == "preflight"
    assert report.cause_code == "active_pull_request"
    assert report.notes == "blocked_pr=#46:df/run-b86124167e66"
    assert store.accept_calls == []
    assert len(alerts) == 1


def test_structural_alert_deduplicates_across_days_and_resets_after_green_or_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _StubStore()
    _patch_accept(monkeypatch, store, run_id="run-green")
    alerts: list[str] = []
    send = lambda msg: alerts.append(msg) or True  # noqa: E731
    same_pr = lambda _s: [{"number": 46, "head_ref": "df/run-b86124167e66", "url": "u", "title": "t"}]  # noqa: E731
    new_pr = lambda _s: [{"number": 48, "head_ref": "df/run-other", "url": "u2", "title": "t2"}]  # noqa: E731

    run_daily(day=date(2026, 11, 18), store=store, clock=_FixedClock(datetime(2026, 11, 18, 9, 0, tzinfo=UTC)),
              failure_sender=send, reports_dir=tmp_path, send_weekly=False, pr_reconciler=same_pr)
    # Next day with identical blocker: suppressed
    run_daily(day=date(2026, 11, 19), store=store, clock=_FixedClock(datetime(2026, 11, 19, 9, 0, tzinfo=UTC)),
              failure_sender=send, reports_dir=tmp_path, send_weekly=False, pr_reconciler=same_pr)
    assert len(alerts) == 1

    # Different blocker on 2026-11-20: alerts again
    run_daily(day=date(2026, 11, 20), store=store, clock=_FixedClock(datetime(2026, 11, 20, 9, 0, tzinfo=UTC)),
              failure_sender=send, reports_dir=tmp_path, send_weekly=False, pr_reconciler=new_pr)
    assert len(alerts) == 2

    # Green day on 2026-11-21 clears structural dedup chain
    sha = "a" * 40
    green_obs = SimpleNamespace(
        observe=lambda rid: [
            CanaryStageObservation(stage=s, status="succeeded", output_refs=([sha] if s == "integration" else ([f"smoke:{rid}:{sha[:12]}"] if s == "build_deploy" else [])))
            for s in canary.REQUIRED_STAGES
        ]
    )
    green_smoke = SimpleNamespace(
        check=lambda url, exp_date, _sc, exp_sha: SmokeResult(
            ok=True, url=url, expected_date=exp_date.isoformat(), expected_sha=exp_sha, observed_sha=exp_sha
        )
    )
    green = run_daily(
        day=date(2026, 11, 21), store=store, observer=green_obs, smoke_client=green_smoke,
        clock=_FixedClock(datetime(2026, 11, 21, 9, 0, tzinfo=UTC)),
        base_url="https://canary.example.invalid", failure_sender=send,
        reports_dir=tmp_path, send_weekly=False, pr_reconciler=lambda _s: [],
    )
    assert green.passed is True

    # New failure after green day alerts again
    run_daily(day=date(2026, 11, 22), store=store, clock=_FixedClock(datetime(2026, 11, 22, 9, 0, tzinfo=UTC)),
              failure_sender=send, reports_dir=tmp_path, send_weekly=False, pr_reconciler=new_pr)
    assert len(alerts) == 3
