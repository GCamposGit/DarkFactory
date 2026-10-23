"""USR-344 (stalled job self-recovery) and USR-253 (suite_lock queue does not eat the job deadline)."""

from __future__ import annotations

from core.harness import remote_worker as rw


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_queue_wait_longer_than_timeout_does_not_expire() -> None:
    clock = _Clock()
    deadline = rw._JobDeadline(100.0, clock=clock, cap_sec=3600.0)
    deadline.extend_for_line("[SUITE_LOCK] waiting: slot busy\n")
    clock.now = 500.0  # far beyond the 100 s timeout, still queued
    assert not deadline.expired()
    clock.now = 600.0
    added = deadline.extend_for_line("[SUITE_LOCK] acquired slot 1 after waiting 600.0s\n")
    assert added == 600.0
    clock.now = 650.0
    assert not deadline.expired()
    clock.now = 701.0
    assert deadline.expired()


def test_queue_wait_is_capped() -> None:
    clock = _Clock()
    deadline = rw._JobDeadline(100.0, clock=clock, cap_sec=200.0)
    deadline.extend_for_line("[SUITE_LOCK] waiting: x\n")
    clock.now = 1000.0
    assert deadline.expired()


def test_deadline_without_lock_lines_still_expires() -> None:
    clock = _Clock()
    deadline = rw._JobDeadline(100.0, clock=clock, cap_sec=3600.0)
    clock.now = 101.0
    assert deadline.expired()


def test_stall_detector_triggers_only_without_output_and_cpu() -> None:
    clock = _Clock()
    det = rw._StallDetector(600.0, clock=clock)
    clock.now = 599.0
    assert not det.check(1.0)
    clock.now = 600.0
    assert det.check(1.0)


def test_stall_detector_healthy_job_is_not_killed() -> None:
    clock = _Clock()
    det = rw._StallDetector(600.0, clock=clock)
    clock.now = 500.0
    assert not det.check(50.0)  # busy CPU resets progress
    clock.now = 1000.0
    assert not det.check(1.0)
    det.note_output()
    clock.now = 1500.0
    assert not det.check(None)  # unknown CPU counts as active
    clock.now = 2200.0
    assert det.check(0.0)


def test_report_stall_marks_job_lost_and_restarts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("core.line.stage_grill.default_telegram_sender", lambda: None)
    manager = rw.JobManager(tmp_path, state_dir=tmp_path / "state")
    restarted: list[bool] = []
    manager.on_stall_restart = lambda: restarted.append(True)
    job = rw.HarnessJob(
        job_id="j1", candidate_sha="a" * 7, tree_sha="b" * 7, quick=True, include_holdout=False,
        requesting_host="h", ref_name="r",
    )
    job.status = "running"
    manager.jobs["j1"] = job
    manager._report_stall(job, "no progress")
    assert job.status == "failed"
    assert job.error_code == rw.ERROR_CODE_JOB_LOST_ON_WORKER
    assert restarted == [True]
    assert "worker_job_stalled" in (tmp_path / "state" / "worker_events.jsonl").read_text(encoding="utf-8")


def test_dispatcher_retries_lost_job() -> None:
    from core.harness import remote_dispatch

    assert "job_lost_on_worker" in remote_dispatch.INFRASTRUCTURE_ERROR_CODES
    assert "job_lost_on_worker" in remote_dispatch.RESTART_RETRY_ERROR_CODES
