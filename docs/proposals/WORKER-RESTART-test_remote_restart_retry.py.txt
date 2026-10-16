"""Resiliencia do portao a restart/congelamento da VM do worker (15/10/2026, grokbox).

Estes testes SO passam depois de aplicar `docs/proposals/WORKER-RESTART-resilience.patch`
(por isso vivem como .txt; o owner os copia para `tests/test_remote_restart_retry.py` no mesmo
commit do patch). Nada espera de verdade: relogio e sleep sao injetados, o worker e falso e o
`JobManager` roda em diretorios temporarios.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.harness import remote_dispatch
from core.harness.models import HarnessStepConfig
from core.harness.remote_worker import (
    ERROR_CODE_WORKER_RESTARTED,
    HarnessJob,
    JobManager,
)

HEALTHY = {"busy": False, "platform_family": "linux", "python_version": "3.13", "hostname": "grokbox"}


class _Clock:
    """Virtual clock: ``sleep`` advances ``now``; no real waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "harness_state"))
    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    remote_dispatch.DISPATCH_ATTEMPTS.clear()  # module-level list: other tests in the same worker leave entries
    for key in (
        "CI",
        "DARKFAC_WORKER_TOKEN",
        "DARKFAC_HARNESS_WORKER_JOB",
        "DARKFAC_REMOTE_HARNESS",
        "DARKFAC_WORKER_RESTART_GRACE_SEC",
        "DARKFAC_WORKER_RESTART_RETRIES",
    ):
        monkeypatch.delenv(key, raising=False)


def _lost(code: str = "worker_restarted", job_id: str = "j1") -> remote_dispatch.RemoteJobLost:
    return remote_dispatch.RemoteJobLost(f"job {job_id} lost ({code})", {"error_code": code, "job_id": job_id})


def _call_retry(clock: _Clock) -> Any:
    return remote_dispatch._dispatch_with_restart_retry(
        "http://w1:8080",
        health=dict(HEALTHY),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        project_root=Path("."),
        candidate_sha="c" * 40,
        tree_hash=None,
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        total_timeout_sec=60.0,
    )


# --- dispatch ----------------------------------------------------------------------


def test_defaults_are_180s_and_2_retries() -> None:
    assert remote_dispatch.worker_restart_grace_sec() == 180.0
    assert remote_dispatch.worker_restart_retries() == 2


def test_env_overrides_and_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_RESTART_GRACE_SEC", "30")
    monkeypatch.setenv("DARKFAC_WORKER_RESTART_RETRIES", "0")
    assert remote_dispatch.worker_restart_grace_sec() == 30.0
    assert remote_dispatch.worker_restart_retries() == 0
    monkeypatch.setenv("DARKFAC_WORKER_RESTART_GRACE_SEC", "-5")
    monkeypatch.setenv("DARKFAC_WORKER_RESTART_RETRIES", "lixo")
    assert remote_dispatch.worker_restart_grace_sec() == 180.0
    assert remote_dispatch.worker_restart_retries() == 2


def test_job_is_resubmitted_to_the_same_worker_once_it_answers_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    calls: list[str] = []
    outcomes: list[Any] = [_lost("worker_restarted"), (True, None)]

    def _one_job(base_url: str, **_k: object) -> Any:
        calls.append(base_url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    # Fake worker: /health fails on the 1st probe (VM still restoring), answers from the 2nd.
    probes = iter([None, dict(HEALTHY)])
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: next(probes))
    monkeypatch.setattr(remote_dispatch, "is_self_dispatch", lambda *a, **k: False)
    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", _one_job)

    result = _call_retry(clock)

    assert result == (True, None)
    assert calls == ["http://w1:8080", "http://w1:8080"]
    assert clock.slept == [remote_dispatch.RESTART_PROBE_GAP_SEC]  # one wait between the two probes
    out = capsys.readouterr().out
    assert "[REMOTE_RETRY] worker=http://w1:8080 attempt=1/2 reason=worker_restarted" in out
    (attempt,) = remote_dispatch.dispatch_attempts()
    assert attempt["outcome"] == "lost_retrying" and attempt["worker_back"] is True and attempt["retry"] == 1


def test_gives_up_after_two_retries_and_falls_back_to_the_next_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://w1:8080,http://w2:8080")
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    monkeypatch.setattr(remote_dispatch.time, "sleep", clock.sleep)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: dict(HEALTHY))
    monkeypatch.setattr(remote_dispatch, "_local_candidate_sha", lambda root: "c" * 40)
    calls: list[str] = []

    def _one_job(base_url: str, **_k: object) -> Any:
        calls.append(base_url)
        if base_url == "http://w1:8080":
            raise _lost("worker_restarted")
        return (True, None)

    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", _one_job)

    result = remote_dispatch.maybe_dispatch_remote(
        steps=[HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check", timeout_sec=30)],
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=Path(__file__).resolve().parents[1],
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=lambda *a, **k: True,
        notify_hub_on_pass=lambda: None,
    )

    assert result is True
    assert calls == ["http://w1:8080"] * 3 + ["http://w2:8080"]  # 1 attempt + 2 retries, then the next worker
    out = capsys.readouterr().out
    assert "attempt=1/2" in out and "attempt=2/2" in out
    assert "[REMOTE_FALLBACK] worker=http://w1:8080" in out
    assert "[HARNESS_FAIL]" not in out  # a restart is infrastructure, never a verdict


def test_worker_that_never_returns_is_given_up_after_the_grace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = _Clock()
    calls: list[str] = []

    def _one_job(base_url: str, **_k: object) -> Any:
        calls.append(base_url)
        raise _lost("worker_unreachable")

    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)
    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", _one_job)

    with pytest.raises(remote_dispatch.RemoteJobLost):
        _call_retry(clock)

    assert calls == ["http://w1:8080"]  # no resubmission to a worker that never came back
    assert clock.now - 1000.0 == pytest.approx(180.0)  # waited exactly the grace, virtually
    assert "did not return within 180s" in capsys.readouterr().out


@pytest.mark.parametrize("code", ["worker_silent", "worker_internal_error", "client_deadline_exceeded"])
def test_other_losses_are_not_retried(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    clock = _Clock()
    calls: list[str] = []

    def _one_job(base_url: str, **_k: object) -> Any:
        calls.append(base_url)
        raise _lost(code)

    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: dict(HEALTHY))
    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", _one_job)

    with pytest.raises(remote_dispatch.RemoteJobLost):
        _call_retry(clock)

    assert calls == ["http://w1:8080"]


def test_worker_restarted_payload_is_infrastructure_not_a_harness_fail(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"status": "failed", "error_code": "worker_restarted", "error": "worker restarting", "job_id": "j1"}
    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._finalize_remote_result(
            payload,
            candidate_sha="c" * 40,
            config_hash="a" * 64,
            base_url="http://w1:8080",
            health=dict(HEALTHY),
            job_id="j1",
        )
    assert excinfo.value.details["error_code"] == "worker_restarted"
    assert "[HARNESS_FAIL]" not in capsys.readouterr().out
    assert "worker_restarted" in remote_dispatch.RESTART_RETRY_ERROR_CODES


# --- worker ------------------------------------------------------------------------


def _job(job_id: str) -> HarnessJob:
    return HarnessJob(
        job_id=job_id,
        candidate_sha="c" * 40,
        tree_sha="d" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="client-host",
        ref_name=f"refs/darkfac/validate/{'c' * 40}",
    )


def _manager(tmp_path: Path) -> JobManager:
    (tmp_path / "root").mkdir(exist_ok=True)
    return JobManager(tmp_path / "root", state_dir=tmp_path / "worker_state")


def _orphan_the_record(manager: JobManager, job_id: str, **extra: Any) -> None:
    path = manager.state_dir / "jobs" / f"{job_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["owner_started_at"] = 1.0  # original owner is gone
    record.update(extra)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_recovery_does_not_delete_the_worktree_of_an_orphaned_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _manager(tmp_path)
    job = _job("a" * 32)
    worktree = first.state_dir / "worktrees" / job.job_id
    (worktree / "core").mkdir(parents=True)
    (worktree / "core" / "marker.py").write_text("x = 1\n", encoding="utf-8")
    job.worktree_path = worktree
    first._register(job)
    first._set_status(job, "running")
    _orphan_the_record(first, job.job_id)

    removed: list[str] = []
    monkeypatch.setattr(JobManager, "_cleanup_worktree", lambda self, j: removed.append(j.job_id))
    second = JobManager(first.root_path, state_dir=first.state_dir)

    recovered = second.jobs[job.job_id]
    assert recovered.status == "failed" and recovered.error_code == ERROR_CODE_WORKER_RESTARTED
    assert removed == []  # nothing was cleaned
    assert (worktree / "core" / "marker.py").exists()  # the files the live owner may still be using


def test_recovery_only_cleans_worktrees_of_old_terminal_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _manager(tmp_path)
    old, fresh = _job("b" * 32), _job("c" * 32)
    for job in (old, fresh):
        worktree = first.state_dir / "worktrees" / job.job_id
        worktree.mkdir(parents=True)
        job.worktree_path = worktree
        first._register(job)
        first._set_status(job, "done", returncode=0)
    import time

    _orphan_the_record(first, old.job_id, updated_at=time.time() - 2 * 3600)

    removed: list[str] = []
    monkeypatch.setattr(JobManager, "_cleanup_worktree", lambda self, j: removed.append(j.job_id))
    JobManager(first.root_path, state_dir=first.state_dir)

    assert removed == [old.job_id]  # the 2 h old terminal job only; the fresh one is kept


def test_job_whose_worktree_vanished_ends_as_worker_restarted_not_as_test_failures(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job = _job("d" * 32)
    manager._register(job)
    missing = manager.state_dir / "worktrees" / job.job_id  # never created / removed by a successor
    reason = manager._lost_underfoot_reason(job, missing)
    assert reason is not None and "disappeared" in reason


def test_job_closed_as_worker_restarted_on_disk_by_a_successor_is_detected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job = _job("e" * 32)
    worktree = manager.state_dir / "worktrees" / job.job_id
    worktree.mkdir(parents=True)
    job.worktree_path = worktree
    manager._register(job)
    manager._set_status(job, "running")
    assert manager._lost_underfoot_reason(job, worktree) is None  # healthy job: no false positive

    _orphan_the_record(manager, job.job_id, status="failed", error_code=ERROR_CODE_WORKER_RESTARTED)
    assert "worker_restarted" in (manager._lost_underfoot_reason(job, worktree) or "")
