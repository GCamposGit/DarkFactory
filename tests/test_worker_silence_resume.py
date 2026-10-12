"""USR-178 (criterion 2): a Desktop worker that goes silent mid-run is either RESUMED
(a blip: same job, same log offset, no duplicated output) or abandoned CLEANLY after
the 180 s silence ceiling (structured diagnosis, evidence on disk, best-effort cancel
on the worker, `[REMOTE_FALLBACK]` line and the seconds spent recorded for the local
fallback).

Most tests run on a virtual clock (``remote_dispatch.time`` is replaced), so the REAL
default thresholds (60 s silence floor, 180 s ceiling) are exercised in milliseconds.
One test talks to a loopback fake worker over real HTTP for the resume path.
"""

from __future__ import annotations

import json
import threading
import time
import types
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.harness import cache as harness_cache
from core.harness import remote_dispatch
from core.harness.models import HarnessStepConfig


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "state"))
    for name in (
        "DARKFAC_WORKER_SILENCE_FALLBACK_SEC",
        "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC",
        "DARKFAC_WORKER_DEAD_PROBES",
        "DARKFAC_WORKER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    remote_dispatch.DISPATCH_ATTEMPTS.clear()


class _VirtualClock:
    """Stand-in for the ``time`` module inside ``remote_dispatch``: ``sleep`` advances ``monotonic``."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(float(seconds), 0.0)

    def time(self) -> float:
        return 1_700_000_000.0 + self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _VirtualClock:
    virtual = _VirtualClock()
    monkeypatch.setattr(
        remote_dispatch,
        "time",
        types.SimpleNamespace(monotonic=virtual.monotonic, sleep=virtual.sleep, time=virtual.time),
    )
    return virtual


@pytest.fixture
def cancelled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda base, job: calls.append((base, job)))
    return calls


_FIRST_CHUNK = (
    "[STEP_START] syntax_and_types\n"
    "[STEP_TIME] syntax_and_types 1.5s\n"
    "[STEP_START] unit_and_integration_tests_parallel\n"
)


def _poll_then_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """First poll returns the log of a run that is inside the parallel step; every later poll times out."""

    calls = {"n": 0}

    def _poll(_base: str, _job: str, offset: int) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "running", "log_chunk": _FIRST_CHUNK, "next_offset": len(_FIRST_CHUNK)}
        raise OSError("timed out")

    monkeypatch.setattr(remote_dispatch, "_poll_job", _poll)


# --- real default thresholds on a virtual clock ----------------------------------------


def test_default_ceiling_is_180s_and_a_responsive_but_silent_worker_is_lost_just_after_it(
    monkeypatch: pytest.MonkeyPatch, clock: _VirtualClock, cancelled: list, capsys: pytest.CaptureFixture[str]
) -> None:
    """Worker silent on job polls but /health answers: waited for ~180 s, then a structured loss."""

    assert remote_dispatch.worker_silence_ceiling_sec() == 180.0
    _poll_then_silence(monkeypatch)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: {"busy": False, "instance_id": "i1"})

    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job("http://w", "job-180", deadline=clock.now + 10_000, submitted_instance_id="i1")

    details = excinfo.value.details
    assert details["error_code"] == "worker_silent"
    assert 180.0 < details["silent_for_sec"] < 200.0
    assert details["silence_ceiling_sec"] == 180.0
    assert details["job_id"] == "job-180"
    assert details["last_step_started"] == "unit_and_integration_tests_parallel"
    assert details["step_times"] == {"syntax_and_types": "1.5"}
    assert details["remote_elapsed_sec"] >= details["silent_for_sec"]
    assert details["last_offset"] == len(_FIRST_CHUNK)
    assert details["cancel_requested"] is True
    assert cancelled == [("http://w", "job-180")]
    out = capsys.readouterr().out
    assert "[REMOTE_JOB_LOST]" in out
    assert "worker is slow, not dead" in out
    evidence = json.loads((harness_cache.state_dir() / "remote_jobs" / "job-180.json").read_text(encoding="utf-8"))
    assert evidence["details"]["last_step_started"] == "unit_and_integration_tests_parallel"
    assert "[CHILD_STEP_START] unit_and_integration_tests_parallel" in evidence["log_tail"]


def test_dead_worker_needs_three_failed_probes_and_is_abandoned_before_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, clock: _VirtualClock, cancelled: list
) -> None:
    _poll_then_silence(monkeypatch)
    probes: list[float] = []

    def _dead(*_a: object, **_k: object) -> None:
        probes.append(clock.now)
        return None

    monkeypatch.setattr(remote_dispatch, "probe_health", _dead)

    with pytest.raises(remote_dispatch.RemoteJobLost, match="/health is unreachable") as excinfo:
        remote_dispatch._stream_job("http://w", "job-dead", deadline=clock.now + 10_000)

    details = excinfo.value.details
    assert details["error_code"] == "worker_unreachable"
    assert details["failed_health_probes"] == remote_dispatch.DEFAULT_WORKER_DEAD_PROBES == 3
    assert 60.0 < details["silent_for_sec"] < 180.0  # fast fall-through is kept for a truly dead worker
    assert details["last_step_started"] == "unit_and_integration_tests_parallel"
    assert cancelled == [("http://w", "job-dead")]
    # the confirmation probes are back to back, not spread over the 180 s ceiling
    assert len(probes) >= 3 and probes[-1] - probes[-3] < 5.0


def test_a_single_lost_health_probe_is_a_blip_not_a_dead_worker(
    monkeypatch: pytest.MonkeyPatch, clock: _VirtualClock, cancelled: list, capsys: pytest.CaptureFixture[str]
) -> None:
    """Probe #1 fails, probe #2 answers: the job is kept (resumed from the same offset), never cancelled."""

    polls: list[int] = []

    def _poll(_base: str, _job: str, offset: int) -> dict:
        polls.append(offset)
        if len(polls) == 1:
            return {"status": "running", "log_chunk": "alpha\n", "next_offset": 6}
        if clock.now < 1000.0 + 90.0:
            raise OSError("timed out")
        return {"status": "done", "log_chunk": "beta\n", "next_offset": 11, "returncode": 0, "result_json": {"k": 1}}

    probe_results = iter([None, {"busy": True}])
    monkeypatch.setattr(remote_dispatch, "_poll_job", _poll)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: next(probe_results, {"busy": True}))

    payload = remote_dispatch._stream_job("http://w", "job-blip", deadline=clock.now + 10_000)

    assert payload["status"] == "done"
    assert cancelled == []
    assert polls[0] == 0 and polls[-1] == 6, "the resumed poll continues at the last offset, it does not restart the log"
    out = capsys.readouterr().out
    assert "answered again; resuming job job-blip from offset 6" in out
    assert out.count("[REMOTE http://w] alpha") == 1 and out.count("[REMOTE http://w] beta") == 1
    assert "[REMOTE_JOB_LOST]" not in out


@pytest.mark.parametrize(
    "raw, expected",
    [("", 3), ("abc", 3), ("1", 1), ("0", 1), ("-4", 1), ("5", 5), ("99", 10)],
)
def test_dead_probe_count_env_is_parsed_defensively(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_DEAD_PROBES", raw)
    assert remote_dispatch.dead_probe_confirmations() == expected


def test_one_confirmation_probe_restores_the_previous_immediate_verdict(
    monkeypatch: pytest.MonkeyPatch, clock: _VirtualClock, cancelled: list
) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_DEAD_PROBES", "1")
    _poll_then_silence(monkeypatch)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)

    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job("http://w", "job-one", deadline=clock.now + 10_000)

    assert excinfo.value.details["failed_health_probes"] == 1


def test_a_vanished_job_is_not_cancelled_it_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, clock: _VirtualClock, cancelled: list
) -> None:
    import io
    import urllib.error

    def _gone(*_a: object, **_k: object) -> dict:
        raise urllib.error.HTTPError(
            "http://w", 404, "Not Found", {}, io.BytesIO(b'{"detail": "job not found", "error_code": "job_not_found"}')  # type: ignore[arg-type]
        )

    monkeypatch.setattr(remote_dispatch, "_poll_job", _gone)
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: {"busy": False})

    with pytest.raises(remote_dispatch.RemoteJobLost) as excinfo:
        remote_dispatch._stream_job("http://w", "job-404", deadline=clock.now + 10_000)

    assert excinfo.value.details["error_code"] == "job_lost_on_worker"
    assert excinfo.value.details["cancel_requested"] is False
    assert cancelled == []


# --- clean fall-through to local, with the remote time accounted -----------------------


def test_lost_job_is_announced_and_its_seconds_are_recorded_for_the_local_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "job j9 lost",
            {"error_code": "worker_unreachable", "job_id": "j9", "silent_for_sec": 75.2, "last_step_started": "parallel"},
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

    assert result is None, "None means: the caller runs the suite locally"
    out = capsys.readouterr().out
    assert "[REMOTE_FALLBACK] worker=http://127.0.0.1:1 job=j9 code=worker_unreachable" in out
    assert "running the suite locally" in out
    (attempt,) = remote_dispatch.dispatch_attempts()
    assert attempt["outcome"] == "lost"
    assert attempt["error_code"] == "worker_unreachable"
    assert attempt["job_id"] == "j9"
    assert attempt["last_step_started"] == "parallel"
    assert attempt["elapsed_sec"] >= 0


def test_attempt_log_is_reset_on_every_dispatch_call(monkeypatch: pytest.MonkeyPatch) -> None:
    remote_dispatch.DISPATCH_ATTEMPTS.append({"outcome": "lost", "elapsed_sec": 1.0})
    assert remote_dispatch.maybe_dispatch_remote(
        steps=[HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check", timeout_sec=30)],
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=Path(__file__).resolve().parents[1],
        cache_lookup_enabled=False,
        local_only=True,
        remote_required=False,
        emit_cache_hit=lambda *a, **k: True,
        notify_hub_on_pass=lambda: None,
    ) is None
    assert remote_dispatch.dispatch_attempts() == []


# --- resume over real HTTP --------------------------------------------------------------


class _BlippingWorker:
    """Loopback worker: serves a log, then answers 503 (and a failing /health) for a window, then finishes."""

    LOG = "alpha\nbeta\n"

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.health_hits = 0
        self.poll_offsets: list[int] = []
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
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
                    # first two probes lost, third one answers: a blip shorter than 3 probes
                    if owner.health_hits <= 2:
                        self._send(500, {"ok": False})
                    else:
                        self._send(200, {"ok": True, "busy": True})
                    return
                offset = int(self.path.split("offset=")[1]) if "offset=" in self.path else 0
                owner.poll_offsets.append(offset)
                elapsed = time.monotonic() - owner.started
                if elapsed < 0.2:
                    self._send(200, {"status": "running", "log_chunk": owner.LOG[offset:6], "next_offset": 6})
                elif elapsed < 1.0:
                    self._send(503, {"detail": "saturated"})
                else:
                    self._send(200, {"status": "done", "log_chunk": owner.LOG[offset:], "next_offset": len(owner.LOG), "returncode": 0})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_worker_that_blips_mid_run_is_resumed_over_http_without_duplicating_the_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    monkeypatch.setattr(
        remote_dispatch.urllib.request, "urlopen", lambda req, timeout=None: opener.open(req, timeout=timeout)
    )
    monkeypatch.setattr(remote_dispatch, "POLL_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(remote_dispatch, "SLOW_WORKER_PROBE_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.1)
    monkeypatch.setattr(remote_dispatch, "DEAD_PROBE_GAP_SEC", 0.02)
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda *a, **k: pytest.fail("a blip must not cancel the job"))
    monkeypatch.setenv("DARKFAC_WORKER_SILENCE_FALLBACK_SEC", "30")
    worker = _BlippingWorker()
    try:
        payload = remote_dispatch._stream_job(worker.url, "job-http", deadline=time.monotonic() + 30)
    finally:
        worker.stop()

    assert payload["status"] == "done"
    assert worker.health_hits >= 3
    out = capsys.readouterr().out
    assert "resuming job job-http from offset 6" in out
    assert out.count("] alpha") == 1 and out.count("] beta") == 1
    assert "[REMOTE_JOB_LOST]" not in out
    assert worker.poll_offsets[0] == 0 and worker.poll_offsets[-1] == 6
