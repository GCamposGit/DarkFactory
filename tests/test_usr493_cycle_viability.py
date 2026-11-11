"""USR-493: cycle quota, candidate priority, no-merge pause and the shared split."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.line.admission import (
    CODE_ADMITTED,
    CODE_CYCLE_QUOTA_SHORT,
    GateObservation,
    GateWorkerObservation,
    admit,
)
from core.line.admission_store import AdmissionStore
from core.line.fleet_dispatcher import FleetDispatcher, DispatcherConfig, HostProbe, HostSpec, InFlight
from core.orchestrator.cycle_viability import (
    estimate_from_records,
    fallback_estimate,
    guard_build_start,
    load_cycle_estimate,
)
from core.orchestrator.merge_quota import aggregate_merge_quota, load_merge_quota_lines
from core.orchestrator.no_merge_pause import ConsumptionCompleted, NoMergeBuildPause
from core.orchestrator.work_admission import prioritize_fleet_queue
from core.usage.reservation import QuotaReservationManager
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


def _event(event_id: str, *, merged: bool = False, work_id: str = "USR-1") -> ConsumptionCompleted:
    return ConsumptionCompleted(
        event_id=event_id,
        work_id=work_id,
        stage="BUILD",
        produced_own_merge=merged,
        completed_at="2026-11-11T00:00:00+00:00",
    )


def _routes() -> dict[str, tuple[str, None]]:
    return {"planning": ("claude", None), "development": ("codex", None), "review": ("grok", None)}


def test_history_estimate_uses_median_and_configured_fallback() -> None:
    fallback = fallback_estimate(
        {
            "ORCHESTRATOR_CYCLE_FALLBACK_PLAN": "1.5",
            "ORCHESTRATOR_CYCLE_FALLBACK_BUILD": "9",
            "ORCHESTRATOR_CYCLE_FALLBACK_CORRECTION": "2",
            "ORCHESTRATOR_CYCLE_FALLBACK_REVIEW": "3",
        }
    )
    estimate = estimate_from_records(
        [
            {"stage": "BUILD", "delta_headroom": 2},
            {"stage": "development", "weekly_delta": 8},
            {"stage": "BUILD", "delta_headroom": 4},
            {"stage": "REVIEW", "weekly_delta": 3},
            {"stage": "REVIEW", "delta_headroom": 5},
            {"stage": "", "delta_headroom": 100},
        ],
        fallback=fallback,
    )
    assert estimate.build == 4
    assert estimate.review == 4
    assert estimate.plan == 1.5
    assert estimate.correction == 2
    assert estimate.sources["build"] == "history"
    assert estimate.sources["plan"] == "fallback"
    assert estimate.total == 11.5


def _open_gate(now: datetime) -> GateObservation:
    return GateObservation(
        workers=[
            GateWorkerObservation(
                worker_id="local",
                evidence_ref="gate://ok",
                workers=1,
                busy=False,
                observed_at=now,
                evidenced_fields=("workers", "busy", "observed_at"),
            )
        ],
        observed_at=now,
        evidence_ref="gate://ok",
    )


def test_admission_waits_when_cycle_quota_is_short_and_does_not_reserve_on_pick() -> None:
    seen: list[bool] = []

    def pick_route(stage: str, caps: list[str], reserve_quota: bool = False, **_kwargs: object) -> object:
        seen.append(reserve_quota)
        return _routes().get(stage)

    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    estimate = fallback_estimate({})
    decision = admit(
        build_caps=["harness:codex"],
        read_caps=["harness:claude", "harness:codex", "harness:grok"],
        execution_context="headless",
        pick_route=pick_route,
        earliest=lambda *_a, **_k: now,
        observe_gate=lambda: _open_gate(now),
        clock=lambda: now,
        cycle_headroom={"codex": estimate.total - 0.1},
        cycle_estimate=estimate,
    )
    assert decision.outcome == "wait"
    assert decision.code == CODE_CYCLE_QUOTA_SHORT
    assert seen and all(flag is False for flag in seen)


def test_admission_refuses_short_and_unknown_headroom_without_reserving_pick_quota() -> None:
    now = datetime(2026, 11, 11, tzinfo=timezone.utc)
    flags: list[bool] = []

    def pick_route(stage: str, _caps: list[str], reserve_quota: bool = False, **_kwargs: object) -> object:
        flags.append(reserve_quota)
        return _routes().get(stage)

    gate = _open_gate(now)
    estimate = fallback_estimate({})
    short = admit(
        build_caps=["harness:codex"],
        read_caps=["harness:claude", "harness:codex", "harness:grok"],
        execution_context="headless",
        pick_route=pick_route,
        earliest=lambda *_a, **_k: now,
        observe_gate=lambda: gate,
        clock=lambda: now,
        cycle_headroom={"codex": estimate.total - 0.1},
        cycle_estimate=estimate,
    )
    assert short.code == CODE_CYCLE_QUOTA_SHORT
    assert short.stage == "build"
    assert flags and all(flag is False for flag in flags)

    unknown = admit(
        build_caps=["harness:codex"],
        read_caps=["harness:claude", "harness:codex", "harness:grok"],
        execution_context="headless",
        pick_route=pick_route,
        earliest=lambda *_a, **_k: now,
        observe_gate=lambda: gate,
        clock=lambda: now,
        cycle_headroom={"codex": None},
        cycle_estimate=estimate,
    )
    assert unknown.code == CODE_CYCLE_QUOTA_SHORT
    assert unknown.evidence_ref.endswith("unknown_headroom")

    covered = admit(
        build_caps=["harness:codex"],
        read_caps=["harness:claude", "harness:codex", "harness:grok"],
        execution_context="headless",
        pick_route=pick_route,
        earliest=lambda *_a, **_k: now,
        observe_gate=lambda: gate,
        clock=lambda: now,
        cycle_headroom={"codex": estimate.total},
        cycle_estimate=estimate,
    )
    assert covered.code == CODE_ADMITTED


def test_launcher_admission_passes_the_cycle_view(monkeypatch: pytest.MonkeyPatch) -> None:
    import run_ticket

    captured: dict[str, object] = {}

    def spy(**kwargs: object) -> object:
        captured.update(kwargs)
        from core.line.admission import allow_decision

        return allow_decision(datetime(2026, 11, 11, tzinfo=timezone.utc))

    monkeypatch.setattr(run_ticket.admission, "admit", spy)
    monkeypatch.setattr(
        run_ticket,
        "inspect_quotas",
        lambda: {"codex": {"headroom": 10.0}, "claude": {"headroom": None}},
    )
    ticket = SimpleNamespace(id="USR-493", project_id="darkfac", estimated_complexity="medium")
    run_ticket._admission_impl(argparse.Namespace(fleet=True, harness=""), ticket)
    assert captured["cycle_headroom"] == {"codex": 10.0, "claude": None}
    assert captured["cycle_estimate"].total == load_cycle_estimate([]).total  # type: ignore[attr-defined]


def test_review_reservation_is_atomic_idempotent_and_refuses_the_next_ticket(tmp_path: Path) -> None:
    manager = QuotaReservationManager(path=tmp_path / "reservations.json")
    estimate = fallback_estimate({})
    first = guard_build_start(
        ticket_id="USR-1",
        harness="codex",
        headroom=estimate.total,
        estimate=estimate,
        manager=manager,
        provider_id="openai",
    )
    again = guard_build_start(
        ticket_id="USR-1",
        harness="codex",
        headroom=estimate.total,
        estimate=estimate,
        manager=manager,
        provider_id="openai",
    )
    second = guard_build_start(
        ticket_id="USR-2",
        harness="codex",
        headroom=estimate.total,
        estimate=estimate,
        manager=manager,
        provider_id="openai",
    )
    assert first.admitted and again.admitted
    assert first.reservation_id == again.reservation_id
    assert second.admitted is False
    assert second.reason == "insufficient_quota"
    rows = manager.active_rows()
    assert len(rows) == 1
    assert rows[0]["purpose"] == "review"
    assert rows[0]["percent"] == estimate.review


def test_ready_candidate_keeps_quota_and_host_ahead_of_a_new_build() -> None:
    ordered, skipped = prioritize_fleet_queue(
        ["USR-NEW", "USR-READY"],
        [
            {"id": "USR-NEW", "work_kind": "new_build"},
            {"id": "USR-READY", "work_kind": "ready_candidate"},
        ],
        quota_available=6,
        cycle_cost=4,
        review_cost=2,
        max_unmerged_candidates=3,
    )
    assert ordered == ["USR-READY", "USR-NEW"]
    assert skipped == {}

    tight, refused = prioritize_fleet_queue(
        ["USR-NEW", "USR-READY"],
        [
            {"id": "USR-NEW", "work_kind": "new_build"},
            {"id": "USR-READY", "work_kind": "ready_candidate"},
        ],
        quota_available=3,
        cycle_cost=4,
        review_cost=2,
    )
    assert tight == ["USR-READY"]
    assert refused["USR-NEW"] == "quota"


def test_candidate_limit_and_pause_keep_resumptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_MAX_UNMERGED_CANDIDATES", "1")
    ordered, skipped = prioritize_fleet_queue(
        ["USR-NEW", "USR-READY", "USR-RESUME"],
        [
            {"id": "USR-NEW", "work_kind": "new_build"},
            {"id": "USR-READY", "work_kind": "ready_candidate"},
            {"id": "USR-RESUME", "work_kind": "resume"},
        ],
    )
    assert ordered == ["USR-READY", "USR-RESUME"]
    assert skipped["USR-NEW"] == "candidate_limit"

    paused, reasons = prioritize_fleet_queue(
        ["USR-NEW", "USR-RESUME"],
        [
            {"id": "USR-NEW", "work_kind": "new_build"},
            {"id": "USR-RESUME", "work_kind": "resume"},
        ],
        new_builds_paused=True,
        pause_reason="new_build_paused_after_no_merge_sequence",
    )
    assert paused == ["USR-RESUME"]
    assert reasons["USR-NEW"] == "new_build_paused_after_no_merge_sequence"


def test_fleet_dispatches_the_ready_candidate_on_the_first_host(tmp_path: Path) -> None:
    launched: list[str] = []

    def launch(tid: str, host: HostSpec, **_kwargs: object) -> InFlight:
        launched.append(tid)
        return InFlight(tid, host.id, SimpleNamespace(poll=lambda: None))

    demands = [
        {"id": "USR-NEW", "status": "planned", "created_at": "2026-01-01", "tags": [], "dependencies": [], "work_kind": "new_build"},
        {"id": "USR-READY", "status": "planned", "created_at": "2026-01-02", "tags": [], "dependencies": [], "work_kind": "ready_candidate"},
    ]
    dispatcher = FleetDispatcher(
        DispatcherConfig(
            hosts=[HostSpec("grokbox", 1, None), HostSpec("notebook", 2, None)],
            state_path=tmp_path / "state.json",
        ),
        demands_loader=lambda: demands,
        probe=lambda _host: HostProbe(True, "ready"),
        launch=launch,
        clock=lambda: 1000.0,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        quota_lookup=lambda: {"codex": 100.0},
    )
    cycle = dispatcher.run_cycle()
    assert launched[0] == "USR-READY"
    assert {row["ticket"] for row in cycle["dispatched"]} == {"USR-READY", "USR-NEW"}


def test_fleet_pause_blocks_new_build_and_keeps_resumptions(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    path = tmp_path / "no_merge_pause.json"
    brake = NoMergeBuildPause(path, threshold=1, sentinel=lambda ctx: calls.append(dict(ctx)))
    brake.record_consumption(_event("spent-1"))
    assert calls and calls[0]["pause_episode"] == 1

    launched: list[str] = []
    dispatcher = FleetDispatcher(
        DispatcherConfig(hosts=[HostSpec("grokbox", 1, None)], state_path=tmp_path / "state.json"),
        demands_loader=lambda: [
            {"id": "USR-NEW", "status": "planned", "created_at": "2026-01-01", "tags": [], "dependencies": [], "work_kind": "new_build"},
            {"id": "USR-FIX", "status": "planned", "created_at": "2026-01-02", "tags": [], "dependencies": [], "work_kind": "resume"},
        ],
        probe=lambda _host: HostProbe(True, "ready"),
        launch=lambda tid, host, **_kwargs: launched.append(tid) or InFlight(tid, host.id, SimpleNamespace(poll=lambda: None)),
        clock=lambda: 1000.0,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        resume_source=lambda: [{"ticket": "USR-RESUME", "status": "running"}],
    )
    dispatcher._no_merge_brake = brake
    cycle = dispatcher.run_cycle()
    assert launched == ["USR-FIX"]
    assert cycle["skipped"]["USR-NEW"] == "new_build_paused_after_no_merge_sequence"
    assert cycle["resumptions"][0]["ticket"] == "USR-RESUME"
    assert brake.admission_decision("RESUME").admitted is True
    assert brake.admission_decision("BUILD", already_started=True).admitted is True


def test_pause_notifies_once_retries_a_lost_delivery_and_restores_state(tmp_path: Path) -> None:
    path = tmp_path / "pause.json"
    calls: list[int] = []

    def boom(_ctx: dict[str, object]) -> None:
        raise RuntimeError("sentinel down")

    failed = NoMergeBuildPause(path, threshold=2, sentinel=boom)
    failed.record_consumption(_event("e1"))
    update = failed.record_consumption(_event("e2"))
    assert update.pause_started and update.sentinel_triggered is False
    assert failed.snapshot()["sentinel_pending"]["pause_episode"] == 1
    assert failed.snapshot()["sentinel_episode"] == 0

    restored = NoMergeBuildPause(path, threshold=2, sentinel=lambda ctx: calls.append(int(ctx["pause_episode"])))
    assert restored.paused
    assert calls == [1]
    assert restored.snapshot()["sentinel_pending"] is None
    assert restored.snapshot()["sentinel_episode"] == 1

    duplicate = restored.record_consumption(_event("e2"))
    assert duplicate.duplicate is True
    assert calls == [1]
    quiet = NoMergeBuildPause(path, threshold=2, sentinel=lambda ctx: calls.append(int(ctx["pause_episode"])))
    assert quiet.snapshot()["paused"] is True
    assert calls == [1]


def test_processed_events_stay_bounded_and_recent_ids_remain_idempotent(tmp_path: Path) -> None:
    brake = NoMergeBuildPause(tmp_path / "pause.json", threshold=1000, sentinel=None)
    for index in range(300):
        brake.record_consumption(_event(f"e{index}"))
    stored = brake.snapshot()["processed_event_ids"]
    assert len(stored) <= 256
    assert "e299" in stored
    assert "e0" not in stored
    again = brake.record_consumption(_event("e299"))
    assert again.duplicate is True
    assert brake.no_merge_sequence == 300


def test_hub_and_briefing_share_the_merge_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"ticket_id": "USR-MERGED", "delta_headroom": 5, "stage": "BUILD"},
        {"ticket_id": "USR-OPEN", "weekly_delta": 2, "stage": "REVIEW"},
        {"ticket_id": "", "delta_headroom": 1},
    ]
    demands = [
        {"id": "USR-MERGED", "status": "completed", "delivery_evidence": "abc"},
        {"id": "USR-OPEN", "status": "planned"},
    ]
    report = aggregate_merge_quota(records, ["USR-MERGED"])
    lines = load_merge_quota_lines(records, demands)
    assert report["quota_spent_with_merge"] == 5
    assert report["quota_spent_without_merge"] == 3
    assert lines[0] == "Cota gasta com merge: 5.0"
    assert lines[1] == "Cota gasta sem merge: 3.0"

    service = HubService(data_dir=tmp_path)
    monkeypatch.setattr(service, "_merge_quota_records", lambda: records)
    monkeypatch.setattr(service, "_merge_quota_demands", lambda: demands)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        body = TestClient(app).get("/api/usage/merge-quota").json()
    finally:
        app.dependency_overrides.clear()
    assert body["quota_spent_with_merge"] == report["quota_spent_with_merge"]
    assert body["quota_spent_without_merge"] == report["quota_spent_without_merge"]


def test_merge_resets_the_pause(tmp_path: Path) -> None:
    calls: list[int] = []
    brake = NoMergeBuildPause(tmp_path / "pause.json", threshold=1, sentinel=lambda ctx: calls.append(1))
    brake.record_consumption(_event("e1"))
    assert brake.paused
    released = brake.record_consumption(_event("e2", merged=True))
    assert released.pause_released
    assert brake.paused is False
    assert brake.admission_decision("BUILD").admitted is True
    assert calls == [1]
