from __future__ import annotations

from core.line.fleet_dispatcher import progress_activity


def test_live_process_without_checkpoint_advancement_expires_after_window() -> None:
    cycle = {
        "heartbeat_epoch": 1_000,
        "resumptions": [{
            "ticket": "USR-475", "status": "running", "pid": 711,
            "stage": "development", "started_at": 100, "heartbeat_epoch": 100,
        }],
    }

    stale, stages, progress = progress_activity(
        cycle, {"USR-475": "development"}, previous_progress_epoch=100,
        now=300, window_sec=120,
    )

    assert stale == "idle"
    assert stages == {"USR-475": "development"}
    assert progress == 100


def test_new_checkpoint_advancement_is_running_then_stalls_after_restart() -> None:
    cycle = {
        "heartbeat_epoch": 1_000,
        "resumptions": [{
            "ticket": "USR-475", "status": "running", "pid": 711,
            "stage": "development", "started_at": 100, "heartbeat_epoch": 450,
        }],
    }

    active, _stages, progress = progress_activity(
        cycle, {"USR-475": "development"}, previous_progress_epoch=100,
        now=500, window_sec=120,
    )
    restarted_stale, _stages, stale_progress = progress_activity(
        cycle, {"USR-475": "development"}, previous_progress_epoch=progress,
        now=600, window_sec=120,
    )

    assert active == "running" and progress == 450
    assert restarted_stale == "idle" and stale_progress == 450


def test_stage_checkpoint_change_survives_dispatcher_restart_once() -> None:
    cycle = {
        "resumptions": [{
            "ticket": "USR-475", "status": "waiting", "stage": "review",
        }],
    }

    active, stages, progress = progress_activity(
        cycle, {"USR-475": "development"}, previous_progress_epoch=None,
        now=500, window_sec=120,
    )
    restarted_stale, _stages, stale_progress = progress_activity(
        cycle, stages, previous_progress_epoch=progress, now=700, window_sec=120,
    )

    assert active == "running" and progress == 500
    assert restarted_stale == "idle" and stale_progress == 500
