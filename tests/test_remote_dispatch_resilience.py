"""Resilience of the remote harness dispatch (USR-141): slow worker vs. dead worker,
configurable probe/ceiling envs, and the lowered priority of the worker's job child.

A fake worker (``http.server`` on 127.0.0.1:<free port>, in a thread) stands in
for the Desktop worker: no external network, no real git/pytest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.harness import remote_dispatch, remote_worker


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DARKFAC_WORKER_SILENCE_FALLBACK_SEC",
        "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC",
        "DARKFAC_WORKER_CHILD_PRIORITY",
        "DARKFAC_WORKER_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(remote_dispatch, "POLL_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(remote_dispatch, "SLOW_WORKER_PROBE_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda *a, **k: None)

    # tests/conftest.py replaces urllib.request.urlopen with an "offline" stub for
    # the whole suite. These tests only talk to a fake worker on 127.0.0.1, so route
    # the client through a real opener (no proxies) instead of the stub.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _loopback_urlopen(req: object, timeout: float | None = None):
        return opener.open(req, timeout=timeout)

    monkeypatch.setattr(remote_dispatch.urllib.request, "urlopen", _loopback_urlopen)


class _FakeWorker:
    """Minimal fake of the worker HTTP surface used by ``_stream_job``."""

    def __init__(self, *, poll_fails_for_sec: float | None, health_ok: bool = True) -> None:
        # poll_fails_for_sec: None = job polls fail forever; N = fail for N s then answer "done".
        self.poll_fails_for_sec = poll_fails_for_sec
        self.health_ok = health_ok
        self.health_hits = 0
        self.started = time.monotonic()
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                return

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/health"):
                    owner.health_hits += 1
                    if owner.health_ok:
                        self._send(200, {"ok": True, "busy": True})
                    else:
                        self._send(500, {"ok": False})
                    return
                if self.path.startswith("/harness/jobs/"):
                    elapsed = time.monotonic() - owner.started
                    failing = owner.poll_fails_for_sec is None or elapsed < owner.poll_fails_for_sec
                    if failing:
                        self._send(503, {"detail": "saturated"})
                    else:
                        self._send(200, {"status": "done", "log_chunk": "", "next_offset": 0, "returncode": 0})
                    return
                self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_worker_factory():
    created: list[_FakeWorker] = []

    def _make(**kwargs: object) -> _FakeWorker:
        worker = _FakeWorker(**kwargs)  # type: ignore[arg-type]
        created.append(worker)
        return worker

    yield _make
    for worker in created:
        try:
            worker.stop()
        except Exception:  # noqa: BLE001 - already stopped by the test
            pass


# --- slow vs. dead worker --------------------------------------------------------


def test_slow_but_alive_worker_is_not_abandoned(
    monkeypatch: pytest.MonkeyPatch, fake_worker_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    """Job polls fail for longer than the silence threshold, /health keeps answering
    (busy=true): the runner keeps waiting and gets the final verdict."""

    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.2)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "10")
    worker = fake_worker_factory(poll_fails_for_sec=0.8)

    payload = remote_dispatch._stream_job(worker.url, "job-1", deadline=time.monotonic() + 20)

    assert payload["status"] == "done"
    assert worker.health_hits >= 1
    assert "slow, not dead" in capsys.readouterr().out


def test_alive_worker_is_abandoned_only_after_the_ceiling(monkeypatch: pytest.MonkeyPatch, fake_worker_factory) -> None:
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.1)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "0.8")
    worker = fake_worker_factory(poll_fails_for_sec=None)  # polls never recover, /health ok

    started = time.monotonic()
    with pytest.raises(remote_dispatch._WorkerUnavailable, match="stopped responding"):
        remote_dispatch._stream_job(worker.url, "job-1", deadline=time.monotonic() + 30)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.75, "must keep waiting up to the ceiling while /health answers"
    assert elapsed < 10, "must still give up at the ceiling"


def test_dead_worker_is_abandoned_without_waiting_for_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, fake_worker_factory
) -> None:
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.2)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "60")
    worker = fake_worker_factory(poll_fails_for_sec=None)
    worker.stop()  # port closed: polls AND /health fail

    started = time.monotonic()
    with pytest.raises(remote_dispatch._WorkerUnavailable, match="/health is unreachable"):
        remote_dispatch._stream_job(worker.url, "job-1", deadline=time.monotonic() + 120)

    assert time.monotonic() - started < 15, "a dead worker must not wait for the 60s ceiling"


def test_worker_with_failing_health_endpoint_counts_as_dead(
    monkeypatch: pytest.MonkeyPatch, fake_worker_factory
) -> None:
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.2)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "60")
    worker = fake_worker_factory(poll_fails_for_sec=None, health_ok=False)

    with pytest.raises(remote_dispatch._WorkerUnavailable, match="/health is unreachable"):
        remote_dispatch._stream_job(worker.url, "job-1", deadline=time.monotonic() + 120)


def test_client_deadline_still_wins_over_a_slow_worker(monkeypatch: pytest.MonkeyPatch, fake_worker_factory) -> None:
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.1)
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "600")
    worker = fake_worker_factory(poll_fails_for_sec=None)

    with pytest.raises(remote_dispatch._WorkerUnavailable, match="client-side deadline exceeded"):
        remote_dispatch._stream_job(worker.url, "job-1", deadline=time.monotonic() + 0.6)


# --- env parsing -----------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "abc", "-3", "0", "nan", "inf", " "])
def test_invalid_health_timeout_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_HEALTH_TIMEOUT_SEC", raw)
    assert remote_dispatch.health_probe_timeout_sec() == remote_dispatch.HEALTH_PROBE_TIMEOUT_SEC == 2.0


def test_health_timeout_env_is_honoured_by_probe_health(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake_urlopen(_req: object, timeout: float | None = None):
        seen.append(float(timeout))  # type: ignore[arg-type]
        raise OSError("offline")

    monkeypatch.setattr(remote_dispatch.urllib.request, "urlopen", fake_urlopen)
    assert remote_dispatch.probe_health("http://worker") is None
    monkeypatch.setenv("DARKFAC_WORKER_HEALTH_TIMEOUT_SEC", "7.5")
    assert remote_dispatch.probe_health("http://worker") is None
    assert remote_dispatch.probe_health("http://worker", timeout=1.25) is None
    assert seen == [2.0, 7.5, 1.25]


@pytest.mark.parametrize("raw", ["", "abc", "-1", "0", "nan"])
def test_invalid_silence_ceiling_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", raw)
    assert remote_dispatch.worker_silence_ceiling_sec() == 180.0


def test_silence_ceiling_is_never_below_the_plain_silence_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "5")
    assert remote_dispatch.worker_silence_ceiling_sec() == remote_dispatch.WORKER_SILENCE_FALLBACK_SEC
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "300")
    assert remote_dispatch.worker_silence_ceiling_sec() == 300.0


# --- priority of the job child -----------------------------------------------------


def test_child_priority_defaults_to_below_normal() -> None:
    assert remote_worker.child_priority_mode() == "below_normal"


@pytest.mark.parametrize("raw", ["", "high", "idle", "0", "BELOW-NORMAL"])
def test_invalid_child_priority_env_falls_back_to_below_normal(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_CHILD_PRIORITY", raw)
    assert remote_worker.child_priority_mode() == "below_normal"


def test_child_priority_env_accepts_normal_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_CHILD_PRIORITY", " Normal ")
    assert remote_worker.child_priority_mode() == "normal"
    cmd = [sys.executable, "x.py"]
    assert remote_worker.apply_child_priority(cmd) == (cmd, {})


def test_apply_child_priority_lowers_priority_on_this_platform() -> None:
    cmd = [sys.executable, "core/harness/runner.py", "--quick", "--local"]
    new_cmd, kwargs = remote_worker.apply_child_priority(cmd)
    if os.name == "nt":
        assert new_cmd == cmd
        assert kwargs == {"creationflags": 0x00004000}
    elif shutil.which("nice"):
        assert new_cmd[1:3] == ["-n", "5"]
        assert new_cmd[3:] == cmd
        assert kwargs == {}
    else:  # pragma: no cover - minimal POSIX without nice
        assert (new_cmd, kwargs) == (cmd, {})


class _FakePopen:
    instances: list["_FakePopen"] = []

    def __init__(self, cmd: list[str], **kwargs: object) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.stdout: list[str] = ['[HARNESS_RESULT] {"ok": true}\n']
        self.returncode = 0
        _FakePopen.instances.append(self)

    def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _run_job_with_fake_popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakePopen:
    _FakePopen.instances.clear()
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        Popen=_FakePopen,
        PIPE=subprocess.PIPE,
        STDOUT=subprocess.STDOUT,
    )
    if hasattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS"):
        fake_subprocess.BELOW_NORMAL_PRIORITY_CLASS = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    monkeypatch.setattr(remote_worker, "subprocess", fake_subprocess)
    monkeypatch.setattr(remote_worker, "_job_timeout_sec", lambda *a, **k: 30.0)

    manager = remote_worker.JobManager(tmp_path / "root", state_dir=tmp_path / "worker_state")
    job = remote_worker.HarnessJob(
        job_id="job-priority",
        candidate_sha="a" * 40,
        tree_sha="b" * 40,
        quick=True,
        include_holdout=False,
        requesting_host="test-host",
        ref_name="refs/darkfac/jobs/job-priority",
    )
    manager._run_job(job)
    assert len(_FakePopen.instances) == 1, job.error
    return _FakePopen.instances[0]


def test_job_child_is_started_with_reduced_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    popen = _run_job_with_fake_popen(monkeypatch, tmp_path)
    if os.name == "nt":
        assert popen.kwargs["creationflags"] == 0x00004000
        assert popen.cmd[0] == sys.executable
    elif shutil.which("nice"):
        assert popen.cmd[1:3] == ["-n", "5"]
        assert popen.cmd[3] == sys.executable
    assert popen.cmd[-2:] == ["--quick", "--local"]
    assert popen.kwargs["env"]["DARKFAC_HARNESS_WORKER_JOB"] == "1"


def test_job_child_priority_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    monkeypatch.setenv("DARKFAC_WORKER_CHILD_PRIORITY", "normal")
    popen = _run_job_with_fake_popen(monkeypatch, tmp_path)
    assert "creationflags" not in popen.kwargs
    assert popen.cmd[0] == sys.executable
