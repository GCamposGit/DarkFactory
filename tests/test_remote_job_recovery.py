"""USR-162: a remote harness job must never become an unexplained silence.

Incident (2026-10-08, T2): the job polled by the runner stopped answering
during ``unit_and_integration_tests_parallel`` while ``/health`` answered
``busy=False``; after >180s the gate fell back to the local run.

Proven root cause (reproduced here, hermetically, with a real uvicorn
server on loopback): the worker kept every job ONLY in process memory. When
the worker process restarted/crashed mid-job, the new process had no record of
the job id, so ``GET /harness/jobs/<id>`` answered HTTP 404 while ``/health``
(served by the new process) answered ``busy=False``. The client treated EVERY
poll failure -- including that definitive 404 -- as "worker silent" and
waited until the USR-141 ceiling (180s) before giving up with the generic
"worker stopped responding" message, discarding the job id and any evidence.

The fix tested here: the worker persists job state per transition and, on
start-up, turns jobs orphaned by a dead predecessor into structured terminal
failures (``error_code=worker_restarted``) that keep their log; the client
treats a 404 as an immediate, structured "job lost" and writes the evidence to
disk, then falls back to local execution as before.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from core.harness import cache as harness_cache
from core.harness import remote_dispatch, remote_worker
from core.harness.remote_worker import (
    ERROR_CODE_JOB_NOT_FOUND,
    ERROR_CODE_WORKER_RESTARTED,
    HarnessJob,
    JobManager,
    create_worker_app,
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "harness_state"))
    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    for key in (
        "CI",
        "DARKFAC_WORKER_TOKEN",
        "DARKFAC_WORKER_SILENCE_FALLBACK_SEC",
        "DARKFAC_HARNESS_WORKER_JOB",
        "DARKFAC_REMOTE_HARNESS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(remote_dispatch, "POLL_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda *a, **k: None)

    # tests/conftest.py swaps urllib.request.urlopen for an offline stub; talk to the
    # loopback worker through a real opener without proxies instead.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _loopback_urlopen(req: object, timeout: float | None = None):
        return opener.open(req, timeout=timeout)

    monkeypatch.setattr(remote_dispatch.urllib.request, "urlopen", _loopback_urlopen)


def _job(job_id: str = "a" * 32) -> HarnessJob:
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


def _running_job(manager: JobManager, log_text: str = "") -> HarnessJob:
    job = _job()
    manager._register(job)
    manager._set_status(job, "running")
    if log_text:
        manager._append_log(job, log_text)
    return job


def _orphan_the_record(manager: JobManager, job_id: str) -> None:
    """Make the persisted record look like it was written by a process that no longer exists."""

    path = manager.state_dir / "jobs" / f"{job_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["owner_started_at"] = 1.0  # pid "reused" by a much younger process => original owner is gone
    path.write_text(json.dumps(record), encoding="utf-8")


# --- worker: persistence + recovery after a restart ---------------------------------


def test_every_transition_is_persisted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    job = _job()
    manager._register(job)
    record_path = manager.state_dir / "jobs" / f"{job.job_id}.json"
    assert json.loads(record_path.read_text(encoding="utf-8"))["status"] == "queued"
    manager._set_status(job, "running")
    assert json.loads(record_path.read_text(encoding="utf-8"))["status"] == "running"
    manager._set_status(
        job, "done", result_json={"candidate_sha": "c" * 40}, returncode=0
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "done"
    assert record["result_json"] == {"candidate_sha": "c" * 40}
    assert record["returncode"] == 0


def test_orphaned_running_job_becomes_a_structured_terminal_failure_after_restart(tmp_path: Path) -> None:
    first = _manager(tmp_path)
    job = _running_job(first, log_text="[STEP_START] unit_and_integration_tests_parallel\n")
    _orphan_the_record(first, job.job_id)

    restarted = _manager(tmp_path)  # new process, same state dir
    payload = restarted.status(job.job_id, 0)

    assert payload is not None, "the restarted worker must still know the job (this was the 404 of the incident)"
    assert payload["status"] == "failed"
    assert payload["error_code"] == ERROR_CODE_WORKER_RESTARTED
    assert payload["job_id"] == job.job_id
    assert payload["worker_instance_id"] == restarted.instance_id != first.instance_id
    assert "unit_and_integration_tests_parallel" in payload["log_chunk"], "log evidence must survive the restart"
    assert "worker_restarted" in payload["log_chunk"] or "restart" in payload["log_chunk"].lower()
    assert payload["result_json"] is None
    assert not restarted.is_busy(), "/health must agree: nothing is running"

    # idempotent: a second restart keeps the same terminal verdict
    again = _manager(tmp_path)
    assert again.status(job.job_id, 0)["error_code"] == ERROR_CODE_WORKER_RESTARTED


def test_orphaned_queued_job_is_also_closed(tmp_path: Path) -> None:
    first = _manager(tmp_path)
    job = _job()
    first._register(job)
    _orphan_the_record(first, job.job_id)
    payload = _manager(tmp_path).status(job.job_id, 0)
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error_code"] == ERROR_CODE_WORKER_RESTARTED


def test_terminal_result_survives_a_restart(tmp_path: Path) -> None:
    first = _manager(tmp_path)
    job = _running_job(first, log_text="line one\n")
    first._set_status(job, "done", result_json={"candidate_sha": "c" * 40}, returncode=0)

    payload = _manager(tmp_path).status(job.job_id, 0)

    assert payload is not None
    assert payload["status"] == "done"
    assert payload["result_json"] == {"candidate_sha": "c" * 40}
    assert payload["log_chunk"] == "line one\n"
    assert payload["next_offset"] == len("line one\n")


def test_a_live_owner_is_never_taken_over(tmp_path: Path) -> None:
    """A second JobManager on the same state dir (a test process, a tool importing
    the module) must not declare a job of a still-running worker dead."""

    live = _manager(tmp_path)
    job = _running_job(live)

    other = _manager(tmp_path)

    assert other.status(job.job_id, 0) is None  # not adopted, not clobbered
    record = json.loads((live.state_dir / "jobs" / f"{job.job_id}.json").read_text(encoding="utf-8"))
    assert record["status"] == "running"
    assert "error_code" not in record or record["error_code"] is None


def test_unknown_job_is_a_structured_404_and_health_exposes_the_instance(tmp_path: Path) -> None:
    app = create_worker_app(project_root=tmp_path, node_id="n1")
    client = TestClient(app)

    response = client.get("/harness/jobs/" + "f" * 32)

    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "job not found"
    assert body["error_code"] == ERROR_CODE_JOB_NOT_FOUND
    assert body["job_id"] == "f" * 32
    health = client.get("/health").json()
    assert body["worker_instance_id"] == health["instance_id"]
    assert health["started_at"] == pytest.approx(body["worker_started_at"])


def test_status_never_waits_for_the_job_log_lock_of_other_jobs(tmp_path: Path) -> None:
    """The query path must stay responsive while a job holds its own lock (a flood of log lines)."""

    manager = _manager(tmp_path)
    busy_job = _running_job(manager, log_text="x\n")
    quiet = _job("b" * 32)
    manager._register(quiet)
    manager._set_status(quiet, "running")
    done: list[Any] = []

    with busy_job.lock:  # a writer stuck mid-append on ANOTHER job
        thread = threading.Thread(target=lambda: done.append(manager.status(quiet.job_id, 0)))
        thread.start()
        thread.join(timeout=2.0)
    assert done and done[0]["status"] == "running", "status of one job must not depend on another job's lock"
    assert manager.is_busy()


def test_restart_endpoint_closes_running_jobs_with_a_cause_and_kills_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_worker_app(project_root=tmp_path, node_id="n1")
    manager: JobManager = app.state.job_manager
    job = _running_job(manager, log_text="partial evidence\n")
    terminated: list[bool] = []

    class _Child:
        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            terminated.append(True)

    job.process = _Child()  # type: ignore[assignment]
    monkeypatch.setattr(remote_worker, "trigger_daemon_restart", lambda *a, **k: None)

    assert TestClient(app).post("/system/restart").status_code == 200

    payload = manager.status(job.job_id, 0)
    assert terminated == [True]
    assert payload["status"] == "failed"
    assert payload["error_code"] == ERROR_CODE_WORKER_RESTARTED
    assert "restart" in payload["error"].lower()
    assert "partial evidence" in payload["log_chunk"]


class _StreamingPopen:
    """Fake Popen whose stdout asserts the log file already holds every line seen so far."""

    log_path: Path
    seen_on_disk: ClassVar[list[str]] = []

    def __init__(self, cmd: list[str], **kwargs: object) -> None:
        self.returncode = 0
        self.pid = 4242

        def _lines():
            for number in range(3):
                yield f"line-{number}\n"
                _StreamingPopen.seen_on_disk.append(_StreamingPopen.log_path.read_text(encoding="utf-8"))

        self.stdout = _lines()

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:
        pass


def test_job_log_is_flushed_per_line_so_a_crash_keeps_the_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    manager = _manager(tmp_path)
    job = _job("job-flush")
    manager._register(job)
    _StreamingPopen.log_path = manager.state_dir / "logs" / "job-flush.log"
    _StreamingPopen.seen_on_disk = []
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        Popen=_StreamingPopen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *a, **k: 30.0)

    manager._run_job(job)

    assert _StreamingPopen.seen_on_disk == ["line-0\n", "line-0\nline-1\n", "line-0\nline-1\nline-2\n"]
    record = json.loads((manager.state_dir / "jobs" / "job-flush.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"  # no HARNESS_RESULT in the fake output
    assert record["returncode"] == 0


# --- client: a 404 is a definitive "job lost", not silence --------------------------


class _Switchable:
    """ASGI shim so the 'worker process' can be replaced under a fixed loopback port."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


@pytest.fixture
def loopback_worker(tmp_path: Path):
    """A real uvicorn worker on 127.0.0.1; ``restart()`` swaps in a fresh app (new JobManager,
    same state dir) exactly like a restarted daemon would see it."""

    import socket

    import uvicorn

    (tmp_path / "root").mkdir(exist_ok=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    switch = _Switchable(create_worker_app(project_root=tmp_path / "root", node_id="loopback"))
    # log_config=None: uvicorn's default dictConfig would replace the logging handlers pytest owns.
    server = uvicorn.Server(uvicorn.Config(switch, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    class _Handle:
        url = f"http://127.0.0.1:{port}"

        @property
        def manager(self) -> JobManager:
            return switch.app.state.job_manager

        def restart(self) -> None:
            switch.app = create_worker_app(project_root=tmp_path / "root", node_id="loopback")

    yield _Handle()
    server.should_exit = True
    thread.join(timeout=15)


def test_root_cause_restart_mid_job_used_to_look_like_silence(
    loopback_worker: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-end reproduction of the incident. /health says busy=False, the job is unknown
    to the (restarted) worker; the client must say so at once, with the job id and cause,
    instead of waiting out the silence ceiling."""

    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 30.0)  # the old path would wait this long
    job = _running_job(loopback_worker.manager, log_text="[STEP_START] unit_and_integration_tests_parallel\n")
    pre_restart_health = remote_dispatch.probe_health(loopback_worker.url)
    loopback_worker.restart()
    # the old process is gone AND its job record is no longer retained (pruned / different state dir)
    (loopback_worker.manager.state_dir / "jobs" / f"{job.job_id}.json").unlink()

    health = remote_dispatch.probe_health(loopback_worker.url)
    assert health is not None and health["busy"] is False
    assert health["instance_id"] != pre_restart_health["instance_id"]

    started = time.monotonic()
    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job(
            loopback_worker.url,
            job.job_id,
            deadline=time.monotonic() + 60,
            submitted_instance_id=pre_restart_health["instance_id"],
        )
    assert time.monotonic() - started < 5.0, "a definitive 404 must not be waited out as silence"

    details = excinfo.value.details
    assert details["error_code"] == "job_lost_on_worker"
    assert details["job_id"] == job.job_id
    assert details["worker_url"] == loopback_worker.url
    assert details["worker_restarted_since_submit"] is True
    assert details["health"]["busy"] is False
    assert job.job_id in str(excinfo.value)
    assert "restart" in str(excinfo.value).lower()

    # evidence is kept on disk, not only in the exception
    evidence = json.loads((harness_cache.state_dir() / "remote_jobs" / f"{job.job_id}.json").read_text(encoding="utf-8"))
    assert evidence["details"]["job_id"] == job.job_id
    assert "REMOTE_JOB_LOST" in capsys.readouterr().out


def test_restart_mid_job_with_persistence_returns_a_recoverable_terminal_result(
    loopback_worker: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    job = _running_job(loopback_worker.manager, log_text="[STEP_START] unit_and_integration_tests_parallel\n")
    _orphan_the_record(loopback_worker.manager, job.job_id)
    loopback_worker.restart()

    payload = remote_dispatch._stream_job(loopback_worker.url, job.job_id, deadline=time.monotonic() + 60)

    assert payload["status"] == "failed"
    assert payload["error_code"] == ERROR_CODE_WORKER_RESTARTED
    assert "unit_and_integration_tests_parallel" in capsys.readouterr().out  # evidence streamed to the gate log
    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._finalize_remote_result(
            payload,
            candidate_sha="c" * 40,
            config_hash="e" * 64,
            base_url=loopback_worker.url,
            health={},
            job_id=job.job_id,
        )
    assert excinfo.value.details["error_code"] == "worker_restarted"
    assert excinfo.value.details["job_id"] == job.job_id


def test_infrastructure_failure_is_not_reported_as_a_harness_fail(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"status": "failed", "error_code": ERROR_CODE_WORKER_RESTARTED, "error": "boom", "result_json": None}
    with pytest.raises(remote_dispatch._WorkerUnavailable):
        remote_dispatch._finalize_remote_result(
            payload, candidate_sha="c" * 40, config_hash="e" * 64, base_url="http://w", health={}, job_id="j1"
        )
    assert "HARNESS_FAIL" not in capsys.readouterr().out


def test_plain_404_without_job_not_found_marker_is_still_treated_as_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from a proxy/unrelated route is not proof the job is gone."""

    import io
    import urllib.error

    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.05)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "60")
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)

    def _proxy_404(*_a: object, **_k: object):
        raise urllib.error.HTTPError("http://w", 404, "Not Found", {}, io.BytesIO(b"<html>nginx</html>"))  # type: ignore[arg-type]

    monkeypatch.setattr(remote_dispatch, "_poll_job", _proxy_404)
    with pytest.raises(remote_dispatch._WorkerUnavailable, match="/health is unreachable") as excinfo:
        remote_dispatch._stream_job("http://w", "j1", deadline=time.monotonic() + 5)
    assert excinfo.value.details["error_code"] == "worker_unreachable"  # type: ignore[attr-defined]


def test_rejected_credentials_fail_fast_with_a_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    def _unauthorized(*_a: object, **_k: object):
        raise urllib.error.HTTPError("http://w", 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"Unauthorized"}'))  # type: ignore[arg-type]

    monkeypatch.setattr(remote_dispatch, "_poll_job", _unauthorized)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: {"busy": True})
    started = time.monotonic()
    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job("http://w", "j1", deadline=time.monotonic() + 60)
    assert time.monotonic() - started < 5.0
    assert excinfo.value.details["error_code"] == "worker_rejected_credentials"


def test_terminal_state_is_recovered_when_the_client_is_about_to_give_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence ceiling reached, but a last read of the job (full log) finds the terminal verdict."""

    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.0)
    monkeypatch.setattr(remote_dispatch, "worker_silence_ceiling_sec", lambda: 0.0)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: {"busy": False})
    calls: list[int] = []

    def _poll(_base: str, _job: str, offset: int) -> dict:
        calls.append(offset)
        if len(calls) == 1:
            raise OSError("timed out")
        return {"status": "done", "log_chunk": "final\n", "next_offset": 6, "result_json": {"k": 1}, "returncode": 0}

    monkeypatch.setattr(remote_dispatch, "_poll_job", _poll)

    payload = remote_dispatch._stream_job("http://w", "j1", deadline=time.monotonic() + 30)

    assert payload["status"] == "done"
    assert payload["result_json"] == {"k": 1}
    assert calls == [0, 0]


def test_give_up_without_recovery_keeps_job_id_and_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.0)
    monkeypatch.setattr(remote_dispatch, "worker_silence_ceiling_sec", lambda: 0.0)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: {"busy": False, "instance_id": "inst-2"})
    calls = {"n": 0}

    def _poll(_base: str, _job: str, offset: int) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "running", "log_chunk": "step output\n", "next_offset": 12}
        raise OSError("timed out")

    monkeypatch.setattr(remote_dispatch, "_poll_job", _poll)

    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job(
            "http://w", "job-77", deadline=time.monotonic() + 30, submitted_instance_id="inst-1"
        )

    details = excinfo.value.details
    assert details["error_code"] == "worker_silent"
    assert details["job_id"] == "job-77"
    assert details["last_offset"] == 12
    assert details["worker_restarted_since_submit"] is True
    assert details["last_status"] == "running"
    assert "job-77" in str(excinfo.value)
    evidence = json.loads((harness_cache.state_dir() / "remote_jobs" / "job-77.json").read_text(encoding="utf-8"))
    assert "step output" in evidence["log_tail"]
    assert "REMOTE_JOB_LOST" in capsys.readouterr().out


def test_lost_job_falls_back_to_local_through_maybe_dispatch_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from core.harness.models import HarnessStepConfig

    repo_root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://127.0.0.1:1")
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    monkeypatch.setattr(
        remote_dispatch,
        "probe_health",
        lambda *a, **k: {"busy": False, "platform_family": "windows", "python_version": "3.12", "hostname": "other"},
    )
    monkeypatch.setattr(remote_dispatch, "_local_candidate_sha", lambda root: "c" * 40)

    def _lost(*_a: object, **_k: object):
        raise remote_dispatch.RemoteJobLost(
            "job j9 lost on http://127.0.0.1:1: worker restarted",
            {"error_code": "job_lost_on_worker", "job_id": "j9"},
        )

    monkeypatch.setattr(remote_dispatch, "_dispatch_one_job", _lost)

    result = remote_dispatch.maybe_dispatch_remote(
        steps=[HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check", timeout_sec=30)],
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=repo_root,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=lambda *a, **k: True,
        notify_hub_on_pass=lambda: None,
    )

    assert result is None, "None means: the caller runs the suite locally (fallback preserved)"
    out = capsys.readouterr().out
    assert "j9" in out
    assert "running the suite locally" in out
    assert sys.platform  # keep the import used on every platform
