"""Regression coverage for a Grokbox runner killed by a host-level signal (USR-270, USR-298)."""

from __future__ import annotations

import datetime as dt
import json
import signal
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from core.harness import remote_dispatch, remote_worker, sigterm_probe
from core.harness.remote_worker import HarnessJob, JobManager


def test_signal_killed_is_infrastructure_and_not_a_harness_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    code = remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED
    assert code in remote_dispatch.INFRASTRUCTURE_ERROR_CODES
    assert code in remote_dispatch.RESTART_RETRY_ERROR_CODES

    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._finalize_remote_result(
            {
                "status": "failed",
                "error_code": code,
                "error": "runner subprocess was terminated by signal 15 (return code -15); no test verdict exists",
                "returncode": -15,
                "result_json": None,
                "job_id": "sigterm-job",
            },
            candidate_sha="c" * 40,
            config_hash="a" * 64,
            base_url="http://grokbox:8080",
            health={"hostname": "grokbox"},
            job_id="sigterm-job",
        )
    assert excinfo.value.details["error_code"] == code
    assert "HARNESS_FAIL" not in capsys.readouterr().out


def test_signal_killed_job_is_retried_on_same_worker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Clock:
        now = 0.0

        def sleep(self, seconds: float) -> None:
            self.now += seconds

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    calls: list[str] = []
    outcomes: list[Any] = [
        remote_dispatch.RemoteJobLost(
            "runner received signal",
            {"error_code": remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED, "job_id": "sigterm-job"},
        ),
        (True, None),
    ]

    def dispatch(base_url: str, **_: object) -> Any:
        calls.append(base_url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *_a, **_k: {"busy": False})
    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", dispatch)
    result = remote_dispatch._dispatch_with_restart_retry(
        "http://grokbox:8080",
        health={"busy": True},
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

    assert result == (True, None)
    assert calls == ["http://grokbox:8080", "http://grokbox:8080"]
    assert "[REMOTE_RETRY] worker=http://grokbox:8080" in capsys.readouterr().out


def test_worker_records_negative_runner_exit_without_a_test_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    manager_root = tmp_path / "root"
    manager_root.mkdir()
    manager = JobManager(manager_root, state_dir=tmp_path / "worker_state")
    job = HarnessJob(
        job_id="sigterm-job",
        candidate_sha="c" * 40,
        tree_sha="d" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="test-client",
        ref_name=f"refs/darkfac/validate/{'c' * 40}",
    )
    manager._register(job)

    class Process:
        pid = 4242
        returncode = -15
        stdout: list[str] = []

        def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        if isinstance(command, list) and command[:3] == ["git", "worktree", "add"]:
            Path(command[4]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    fake_subprocess = types.SimpleNamespace(
        run=run,
        Popen=lambda *args, **kwargs: Process(),
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *args, **kwargs: 30.0)
    monkeypatch.setattr(manager, "_sync_requirements", lambda *args, **kwargs: None)

    manager._run_job(job)

    record = (manager.state_dir / "jobs" / "sigterm-job.json").read_text(encoding="utf-8")
    assert job.status == "failed"
    assert job.error_code == remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED
    assert job.returncode == -15
    assert job.result_json is None
    assert "SIGTERM (15)" in record and "return code -15" in record


def _run_fake_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, returncode: int, output: list[str]
) -> HarnessJob:
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    root = tmp_path / "root"
    root.mkdir()
    manager = JobManager(root, state_dir=tmp_path / "worker_state")
    job = HarnessJob(
        job_id="fake-job",
        candidate_sha="c" * 40,
        tree_sha="d" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="test-client",
        ref_name=f"refs/darkfac/validate/{'c' * 40}",
    )
    manager._register(job)

    class Process:
        pid = 4243
        stdout = output

        def __init__(self) -> None:
            self.returncode = returncode

        def wait(self) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        if isinstance(command, list) and command[:3] == ["git", "worktree", "add"]:
            Path(command[4]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    fake_subprocess = types.SimpleNamespace(
        run=run, Popen=lambda *a, **k: Process(), PIPE=subprocess.PIPE, STDOUT=subprocess.STDOUT
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *a, **k: 30.0)
    monkeypatch.setattr(manager, "_sync_requirements", lambda *a, **k: None)
    manager._run_job(job)
    return job


def _harness_result_line(exit_codes: dict[str, int]) -> str:
    return f"{remote_worker.MARKER_HARNESS_RESULT} " + json.dumps({"exit_codes": exit_codes}) + "\n"


def test_step_killed_by_signal_inside_a_normally_ended_runner_is_not_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner exits 1 (a step failed) but that step's own exit code is -15: still infrastructure."""

    output = [
        "[TEST_COUNT] count=0\n",
        _harness_result_line({"syntax": 0, "unit_and_integration_tests_parallel": -15}),
    ]
    job = _run_fake_job(tmp_path, monkeypatch, returncode=1, output=output)

    assert job.status == "failed"
    assert job.error_code == remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED
    assert job.returncode == 1
    assert "unit_and_integration_tests_parallel" in (job.error or "")
    assert "SIGTERM" in (job.error or "") and "-15" in (job.error or "")
    assert "[WORKER worker_signal_killed]" in "".join(job.log_chunks)


def test_short_job_without_any_test_is_treated_as_killed_from_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _run_fake_job(tmp_path, monkeypatch, returncode=1, output=["[TEST_COUNT] count=0\n"])
    assert job.error_code == remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED
    assert "without executing any test" in (job.error or "")


def test_a_real_step_failure_keeps_its_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = ["[TEST_COUNT] count=0\n", _harness_result_line({"syntax": 1})]
    job = _run_fake_job(tmp_path, monkeypatch, returncode=1, output=output)
    assert job.status == "failed"
    assert job.error_code is None
    assert job.result_json is not None


def test_classifier_boundaries() -> None:
    classify = remote_worker.classify_signal_kill
    assert classify(0, None, "[TEST_COUNT] count=120\n", 3.0) is None  # tests ran: not a short-kill
    assert classify(1, {"exit_codes": {"unit": 1}}, "", 3.0) is None
    assert classify(1, None, "", remote_worker.SIGNAL_KILLED_MAX_JOB_SEC + 1) is None
    assert classify(-9, None, "[TEST_COUNT] count=5\n", 400.0) is not None
    assert "SIGTERM (15)" in (classify(-15, None, "", 400.0) or "")
    assert classify(1, {"exit_codes": {"unit": -15}}, "[TEST_COUNT] count=5\n", 400.0) is not None


def test_probe_wrapper_is_posix_only_and_needs_the_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = [sys.executable, "core/harness/runner.py", "--quick", "--local"]
    (tmp_path / "core" / "harness").mkdir(parents=True)
    monkeypatch.delenv(remote_worker.ENV_WORKER_SIGTERM_PROBE, raising=False)

    monkeypatch.setattr(remote_worker.os, "name", "nt")
    assert remote_worker._wrap_with_sigterm_probe(cmd, tmp_path) == cmd

    monkeypatch.setattr(remote_worker.os, "name", "posix")
    monkeypatch.setattr(remote_worker.signal, "sigwaitinfo", lambda *_: None, raising=False)
    assert remote_worker._wrap_with_sigterm_probe(cmd, tmp_path) == cmd  # module missing in the candidate tree

    (tmp_path / "core" / "harness" / "sigterm_probe.py").write_text("", encoding="utf-8")
    wrapped = remote_worker._wrap_with_sigterm_probe(cmd, tmp_path)
    assert wrapped == [sys.executable, "-m", "core.harness.sigterm_probe", "--", *cmd]

    monkeypatch.setenv(remote_worker.ENV_WORKER_SIGTERM_PROBE, "off")
    assert remote_worker._wrap_with_sigterm_probe(cmd, tmp_path) == cmd


def test_probe_line_names_sender_and_time() -> None:
    when = dt.datetime(2030, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    line = sigterm_probe.format_probe_line(15, 1, 0, when, "/sbin/init")
    assert line.startswith(sigterm_probe.PROBE_MARKER)
    assert "SIGTERM (15)" in line and "pid=1" in line and "uid=0" in line
    assert "2030-01-02T03:04:05+00:00" in line and "/sbin/init" in line


@pytest.mark.skipif(not sigterm_probe.probe_supported(), reason="sigwaitinfo exists on POSIX only")
def test_probe_logs_the_sender_and_still_dies_of_sigterm() -> None:
    import os
    import time

    child_code = "import time; print('ready', flush=True); time.sleep(60)"
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "core.harness.sigterm_probe", "--", sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert supervisor.stdout is not None
    assert supervisor.stdout.readline().strip() == "ready"
    time.sleep(0.5)  # let the supervisor reach its sigwaitinfo thread
    supervisor.send_signal(signal.SIGTERM)
    _out, err = supervisor.communicate(timeout=60)
    assert supervisor.returncode == -signal.SIGTERM
    assert f"pid={os.getpid()}" in err


# --- USR-298: the worker must be stable before a job is submitted --------------------


class _Sleeper:
    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _health(instance: str = "i1", started: float = 100.0, **extra: Any) -> dict[str, Any]:
    return {"busy": False, "instance_id": instance, "started_at": started, **extra}


def test_stability_gate_requires_three_identical_health_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, raising=False)
    monkeypatch.delenv(remote_dispatch.ENV_WORKER_STABLE_INTERVAL_SEC, raising=False)
    probes: list[str] = []
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: probes.append("p") or _health())
    monkeypatch.setattr(remote_dispatch, "is_self_dispatch", lambda *a, **k: False)
    sleeper = _Sleeper()

    result = remote_dispatch._await_worker_stable("http://w", _health(), sleep=sleeper)

    assert result == _health()
    assert len(probes) == 2 and sleeper.slept == [20.0, 20.0]


def test_stability_gate_restarts_the_count_when_the_instance_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, raising=False)
    answers = [_health("i2", 200.0), _health("i2", 200.0), _health("i2", 200.0)]
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(remote_dispatch, "is_self_dispatch", lambda *a, **k: False)
    sleeper = _Sleeper()

    result = remote_dispatch._await_worker_stable("http://w", _health("i1", 100.0), sleep=sleeper)

    assert result is not None and result["instance_id"] == "i2"
    assert len(sleeper.slept) == 3  # i1 -> i2 reset the streak; two more matching answers were needed


def test_stability_gate_gives_up_on_a_worker_that_keeps_vanishing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, raising=False)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)
    sleeper = _Sleeper()
    assert remote_dispatch._await_worker_stable("http://w", _health(), sleep=sleeper) is None
    assert len(sleeper.slept) == remote_dispatch.worker_stable_checks() * remote_dispatch.STABLE_ROUNDS_PER_CHECK


def test_stability_gate_is_configurable_and_skippable(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeper = _Sleeper()
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: pytest.fail("must not probe"))
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, "1")
    assert remote_dispatch._await_worker_stable("http://w", _health(), sleep=sleeper) == _health()
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, "3")
    old_worker = {"busy": False}  # reports no identity: nothing to compare, nothing to wait for
    assert remote_dispatch._await_worker_stable("http://w", old_worker, sleep=sleeper) == old_worker
    assert sleeper.slept == []

    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, "garbage")
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_INTERVAL_SEC, "-4")
    assert remote_dispatch.worker_stable_checks() == remote_dispatch.DEFAULT_WORKER_STABLE_CHECKS
    assert remote_dispatch.worker_stable_interval_sec() == remote_dispatch.DEFAULT_WORKER_STABLE_INTERVAL_SEC
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, "2")
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_INTERVAL_SEC, "7.5")
    assert remote_dispatch.worker_stable_checks() == 2
    assert remote_dispatch.worker_stable_interval_sec() == 7.5


def test_signal_kill_retries_twice_on_the_same_worker_then_falls_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def dispatch(base_url: str, **_: object) -> Any:
        calls.append(base_url)
        raise remote_dispatch.RemoteJobLost(
            "killed", {"error_code": remote_worker.ERROR_CODE_WORKER_SIGNAL_KILLED, "job_id": f"j{len(calls)}"}
        )

    sleeper = _Sleeper()
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: _health())
    monkeypatch.setattr(remote_dispatch, "is_self_dispatch", lambda *a, **k: False)
    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", dispatch)
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_STABLE_CHECKS, "1")
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_RESTART_RETRIES, "2")
    monkeypatch.setenv(remote_dispatch.ENV_WORKER_RESTART_GRACE_SEC, "180")

    with pytest.raises(remote_dispatch.RemoteJobLost):
        remote_dispatch._dispatch_with_restart_retry(
            "http://grokbox:8080", health=_health(), sleep=sleeper, monotonic=lambda: 0.0
        )

    out = capsys.readouterr().out
    assert len(calls) == 3  # first try + 2 resubmissions on the same worker
    assert out.count("[REMOTE_RETRY]") == 2
    assert "HARNESS_FAIL" not in out
