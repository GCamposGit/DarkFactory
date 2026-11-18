"""USR-475 T5: bound default_deploy's wait and cancel a remote gate job durably.

Two independent concerns:

- `core.line.fleet_dispatcher.default_deploy` must never hang the dispatcher's deploy
  slot forever: its subprocess wait is bounded and the whole process tree is killed on
  timeout instead of being abandoned.
- `core.harness.remote_dispatch`'s authenticated DELETE to a worker job was fire-and-
  forget (`with contextlib.suppress(Exception): urlopen(...)`): a failed DELETE left the
  remote job running with nobody left to retry it. `durable_cancel_job` persists an
  intent before the network call, confirms idempotently (including a 404 "already gone"
  response), and `resume_pending_cancellations` retries whatever a crash/restart left
  unconfirmed.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from core.harness import remote_dispatch
from core.line import fleet_dispatcher


# --- default_deploy: bounded wait ------------------------------------------------------


class _HangingProc:
    """Stands in for a `dokploy_redeploy.py` subprocess whose wait never returns on its
    own -- the scenario that previously blocked the dispatcher's deploy slot forever."""

    def __init__(self) -> None:
        self.pid = 4242
        self.wait_calls = 0
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise subprocess.TimeoutExpired(cmd="dokploy_redeploy.py", timeout=timeout or 0.0)
        return 0  # the bounded re-wait after the kill succeeds immediately


def test_default_deploy_ends_wait_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _HangingProc()
    killed_pids: list[int] = []

    monkeypatch.setattr(fleet_dispatcher.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        "core.execution.sandbox.kill_process_tree",
        lambda pid, **k: killed_pids.append(pid),
    )

    rc = fleet_dispatcher.default_deploy(timeout=0.01)

    assert rc == fleet_dispatcher.DEPLOY_TIMEOUT_EXIT_CODE
    assert killed_pids == [proc.pid]
    assert proc.wait_calls == 2  # first call times out, second (post-kill) confirms exit


def test_default_deploy_returns_child_exit_code_within_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FastProc:
        pid = 9
        wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self._wait_calls_bumped = True
            return 0

    proc = _FastProc()
    monkeypatch.setattr(fleet_dispatcher.subprocess, "Popen", lambda *a, **k: proc)

    assert fleet_dispatcher.default_deploy(timeout=5.0) == 0


def test_deploy_timeout_sec_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fleet_dispatcher.ENV_DEPLOY_TIMEOUT_SEC, raising=False)
    assert fleet_dispatcher.deploy_timeout_sec() == fleet_dispatcher.DEFAULT_DEPLOY_TIMEOUT_SEC

    monkeypatch.setenv(fleet_dispatcher.ENV_DEPLOY_TIMEOUT_SEC, "120")
    assert fleet_dispatcher.deploy_timeout_sec() == 120.0

    # Garbage/non-positive values fail closed to the safe default rather than 0/negative
    # (which would make every deploy time out immediately).
    monkeypatch.setenv(fleet_dispatcher.ENV_DEPLOY_TIMEOUT_SEC, "not-a-number")
    assert fleet_dispatcher.deploy_timeout_sec() == fleet_dispatcher.DEFAULT_DEPLOY_TIMEOUT_SEC
    monkeypatch.setenv(fleet_dispatcher.ENV_DEPLOY_TIMEOUT_SEC, "-5")
    assert fleet_dispatcher.deploy_timeout_sec() == fleet_dispatcher.DEFAULT_DEPLOY_TIMEOUT_SEC


# --- remote gate cancellation: durable, idempotent, restart-safe ------------------------


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "harness_state"))
    monkeypatch.setenv("DARKFAC_WORKER_TOKEN", "super-secret-token")


def _intent_dir() -> Path:
    from core.harness import cache as harness_cache

    return harness_cache.state_dir() / remote_dispatch.CANCEL_INTENTS_SUBDIR


def test_durable_cancel_persists_intent_before_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The intent file must exist (unconfirmed) even if the DELETE never returns."""

    call_order: list[str] = []

    def _never_returns(base_url: str, job_id: str) -> dict[str, Any]:
        call_order.append("network")
        # Simulate a network failure: the caller must already have persisted the
        # intent by the time this runs.
        assert (_intent_dir() / "job-1.json").exists(), "intent must be persisted before the DELETE"
        return {"outcome": "error", "status_code": None, "detail": "connection refused"}

    monkeypatch.setattr(remote_dispatch, "_cancel_request", _never_returns)

    confirmed = remote_dispatch.durable_cancel_job("http://worker:8080", "job-1", reason="client_deadline_exceeded")

    assert confirmed is False
    assert call_order == ["network"]
    record = json.loads((_intent_dir() / "job-1.json").read_text(encoding="utf-8"))
    assert record["confirmed"] is False
    assert record["job_id"] == "job-1"
    assert record["reason"] == "client_deadline_exceeded"
    assert record["remote_outcome"] == "error"
    assert "idempotency_key" in record


def test_durable_cancel_treats_404_as_already_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        remote_dispatch, "_cancel_request",
        lambda base_url, job_id: {"outcome": "already_terminal", "status_code": 404},
    )

    confirmed = remote_dispatch.durable_cancel_job("http://worker:8080", "job-2", reason="worker_silent")

    assert confirmed is True
    record = json.loads((_intent_dir() / "job-2.json").read_text(encoding="utf-8"))
    assert record["confirmed"] is True
    assert record["remote_outcome"] == "already_terminal"
    assert "confirmed_at" in record


def test_durable_cancel_is_idempotent_once_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _count(base_url: str, job_id: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"outcome": "confirmed", "status_code": 200}

    monkeypatch.setattr(remote_dispatch, "_cancel_request", _count)

    first = remote_dispatch.durable_cancel_job("http://worker:8080", "job-3", reason="keyboard_interrupt")
    second = remote_dispatch.durable_cancel_job("http://worker:8080", "job-3", reason="keyboard_interrupt")

    assert first is True
    assert second is True
    assert calls["n"] == 1, "a confirmed job must never trigger a second DELETE"


def test_resume_pending_cancellations_retries_unconfirmed_intents(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: dict[str, int] = {"job-a": 0, "job-b": 0}

    def _fake_request(base_url: str, job_id: str) -> dict[str, Any]:
        attempts[job_id] += 1
        if job_id == "job-a":
            return {"outcome": "confirmed", "status_code": 200}
        return {"outcome": "error", "status_code": None, "detail": "still unreachable"}

    monkeypatch.setattr(remote_dispatch, "_cancel_request", lambda *a, **k: {"outcome": "error", "status_code": None})
    # Persist two unconfirmed intents, as if the process crashed right after writing
    # them but before (or during) the original DELETE attempt.
    remote_dispatch.durable_cancel_job("http://worker:8080", "job-a", reason="client_deadline_exceeded")
    remote_dispatch.durable_cancel_job("http://worker:8080", "job-b", reason="worker_unreachable")
    assert json.loads((_intent_dir() / "job-a.json").read_text(encoding="utf-8"))["confirmed"] is False
    assert json.loads((_intent_dir() / "job-b.json").read_text(encoding="utf-8"))["confirmed"] is False

    monkeypatch.setattr(remote_dispatch, "_cancel_request", _fake_request)
    still_pending = remote_dispatch.resume_pending_cancellations()

    assert still_pending == 1
    assert json.loads((_intent_dir() / "job-a.json").read_text(encoding="utf-8"))["confirmed"] is True
    assert json.loads((_intent_dir() / "job-b.json").read_text(encoding="utf-8"))["confirmed"] is False
    assert attempts == {"job-a": 1, "job-b": 1}

    # A second sweep only retries what is still unconfirmed.
    monkeypatch.setattr(
        remote_dispatch, "_cancel_request",
        lambda base_url, job_id: {"outcome": "confirmed", "status_code": 200},
    )
    still_pending_2 = remote_dispatch.resume_pending_cancellations()
    assert still_pending_2 == 0
    assert attempts["job-a"] == 1  # untouched: it was already confirmed


def test_resume_pending_cancellations_is_noop_without_state_dir() -> None:
    assert remote_dispatch.resume_pending_cancellations() == 0


def test_cancellation_record_never_contains_the_worker_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        remote_dispatch, "_cancel_request",
        lambda base_url, job_id: {"outcome": "error", "status_code": 401, "detail": "unauthorized"},
    )

    remote_dispatch.durable_cancel_job("http://worker:8080", "job-secret", reason="client_deadline_exceeded")

    record_text = (_intent_dir() / "job-secret.json").read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "super-secret-token" not in record_text
    assert "super-secret-token" not in printed
    assert remote_dispatch.MARKER_REMOTE_JOB_CANCELLED in printed


def test_cancel_request_sanitizes_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_401(*args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError(
            url="http://worker:8080/harness/jobs/job-x", code=401, msg="Unauthorized",
            hdrs=None, fp=__import__("io").BytesIO(b"token=super-secret-token"),
        )

    monkeypatch.setattr(remote_dispatch.urllib.request, "urlopen", _raise_401)

    outcome = remote_dispatch._cancel_request("http://worker:8080", "job-x")

    assert outcome["outcome"] == "error"
    assert outcome["status_code"] == 401


def test_cancel_request_404_is_already_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_404(*args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError(
            url="http://worker:8080/harness/jobs/job-y", code=404, msg="Not Found",
            hdrs=None, fp=__import__("io").BytesIO(b"{}"),
        )

    monkeypatch.setattr(remote_dispatch.urllib.request, "urlopen", _raise_404)

    outcome = remote_dispatch._cancel_request("http://worker:8080", "job-y")

    assert outcome == {"outcome": "already_terminal", "status_code": 404}


def test_legacy_cancel_job_wrapper_still_void_and_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing call sites (and tests) use `_cancel_job(base_url, job_id)` as a
    fire-and-forget void call; it must still persist durably underneath."""

    monkeypatch.setattr(
        remote_dispatch, "_cancel_request",
        lambda base_url, job_id: {"outcome": "confirmed", "status_code": 200},
    )

    result = remote_dispatch._cancel_job("http://worker:8080", "job-legacy")

    assert result is None
    record = json.loads((_intent_dir() / "job-legacy.json").read_text(encoding="utf-8"))
    assert record["confirmed"] is True


def test_maybe_dispatch_remote_resumes_pending_cancellations_first(monkeypatch: pytest.MonkeyPatch) -> None:
    resumed = {"called": False}
    monkeypatch.setattr(remote_dispatch, "resume_pending_cancellations", lambda: resumed.__setitem__("called", True) or 0)
    monkeypatch.setattr(remote_dispatch, "_effective_mode", lambda remote_required: "off")

    result = remote_dispatch.maybe_dispatch_remote(
        steps=[], config_hash="x", quick=True, include_holdout=False,
        config_path=Path("x"), project_root=Path("."), cache_lookup_enabled=False,
        local_only=False, remote_required=False,
        emit_cache_hit=lambda *a, **k: True, notify_hub_on_pass=lambda: None,
    )

    assert result is None  # disabled mode: no remote work, but the sweep still ran
    assert resumed["called"] is True
