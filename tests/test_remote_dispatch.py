"""Tests for remote dispatch of the official validation harness (HF-27-11):
``core/harness/remote_dispatch.py`` (client) and the async job protocol
added to ``core/harness/remote_worker.py`` (server).

Structure:
  - Unit tests exercise ``remote_dispatch`` helpers and ``JobManager`` in
    isolation (mocked HTTP, synthetic git repos) -- fast, no real server.
  - One end-to-end test starts a REAL uvicorn server on 127.0.0.1:<free
    port> in a background thread, seeds a tiny self-contained fixture repo
    (a minimal copy of core/harness/* so the worker's job subprocess --
    `core/harness/runner.py --quick --local` -- can actually import and
    run), and drives the full protocol: bundle transfer, job lifecycle,
    log streaming, client-emitted markers, worker-platform cache storage,
    and worktree cleanup.

Every test is self-contained: no dependency on the real DarkFac test suite
except for the fixture-package copy in the E2E test, which is sourced from
THIS repository so it can never drift out of sync with the real modules.
"""

from __future__ import annotations

import json
import shutil
import socket as socket_module
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.harness import cache as harness_cache
from core.harness import markers
from core.harness import remote_dispatch
from core.harness.models import HarnessResult, HarnessStepConfig
from core.harness.remote_worker import JobManager, create_worker_app

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- fixture-repo helpers (mirrors tests/test_harness_affected.py's pattern) --


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _init_repo(repo: Path, branch: str = "main") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "remote-dispatch-tests@example.com")
    _git(repo, "config", "user.name", "Remote Dispatch Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    # Windows-only gotcha: without this, a machine-wide core.autocrlf=true
    # rewrites line endings on checkout so a brand-new `git worktree add`
    # shows as dirty immediately (git status sees CRLF vs. the committed
    # LF) -- which `_ensure_clean_worktree()` correctly, but confusingly,
    # rejects. Force byte-identical checkouts for these synthetic repos.
    _git(repo, "config", "core.autocrlf", "false")


def _write(repo: Path, rel: str, content: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


#: The exact minimal import closure of `core/harness/runner.py --local`
#: (verified by reading the modules): runner -> cache, remote_dispatch,
#: suite_lock, markers, models; remote_dispatch -> cache, markers, models;
#: cache -> suite_lock (+ core.orchestrator.cloud_db, but only inside an
#: exception path never hit when DARKFAC_HF02_DATABASE_URL is unset).
_HARNESS_PACKAGE_FILES = (
    "core/paths.py",
    "core/harness/runner.py",
    "core/harness/cache.py",
    "core/harness/suite_lock.py",
    "core/harness/markers.py",
    "core/harness/models.py",
    "core/harness/remote_dispatch.py",
)


def _seed_harness_package(repo: Path) -> None:
    """Copy the real core/harness runtime into ``repo`` so a worker job
    subprocess (`core/harness/runner.py --quick --local`) run from a
    worktree of ``repo`` can actually import and execute, without dragging
    in the full DarkFac suite (heavy deps like numpy/soundfile via the
    real tests/conftest.py) as a test fixture dependency."""

    for rel in _HARNESS_PACKAGE_FILES:
        source = REPO_ROOT / rel
        destination = repo / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _seed_minimal_worker_repo(repo: Path) -> None:
    """A tiny, self-contained repo a real worker can fetch a candidate
    into, check out, and run `runner.py --quick --local` against: one
    quick `test` step (a trivial passing pytest file) so the harness's
    success formula (discovered/passed > 0) is satisfiable."""

    _init_repo(repo)
    _seed_harness_package(repo)
    # Byte-compiling core/harness/*.py on import writes __pycache__/*.pyc
    # into the worktree BEFORE `_ensure_clean_worktree()` runs -- without
    # ignoring it (as the real repo's own .gitignore does), that untracked
    # bytecode would make every freshly-checked-out worktree look dirty.
    _write(repo, ".gitignore", "__pycache__/\n*.pyc\n")
    _write(
        repo,
        "harness.config.json",
        json.dumps(
            {
                "steps": [
                    {
                        "name": "unit_tests",
                        "cmd": "python -m pytest tests -q",
                        "quick": True,
                        "kind": "test",
                        "timeout_sec": 60,
                    }
                ]
            }
        ),
    )
    _write(repo, "tests/test_trivial.py", "def test_trivial() -> None:\n    assert 1 + 1 == 2\n")
    _commit_all(repo, "seed fixture repo")


def _config_hash_of(repo: Path) -> str:
    import hashlib

    return hashlib.sha256((repo / "harness.config.json").read_bytes()).hexdigest()


def _free_port() -> int:
    with socket_module.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- shared isolation fixture --------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_remote_dispatch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "harness_state"))
    monkeypatch.setenv("DARKFAC_WORKER_STATE_DIR", str(tmp_path / "worker_state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")
    for key in (
        "CI",
        "DARKFAC_HARNESS_CACHE",
        "DARKFAC_HF02_DATABASE_URL",
        "DARKFAC_TEST_WORKERS",
        "DARKFAC_REMOTE_HARNESS",
        "DARKFAC_WORKER_TOKEN",
        "DARKFAC_REMOTE_BUSY_WAIT_SEC",
        "DARKFAC_REMOTE_DISPATCH_TEST_OVERRIDE",
        "DARKFAC_HARNESS_WORKER_JOB",
    ):
        monkeypatch.delenv(key, raising=False)


def _minimal_steps() -> list[HarnessStepConfig]:
    return [HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check", timeout_sec=30)]


def _noop_emit_cache_hit(record: dict, *, config_path: Path) -> bool:  # pragma: no cover - only if hit
    return True


# =============================================================================
# Env / mode / self-dispatch unit tests
# =============================================================================


def test_worker_urls_defaults_to_desktop_primary() -> None:
    assert remote_dispatch.worker_urls() == [remote_dispatch.DEFAULT_WORKERS]


def test_worker_urls_parses_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://a:1 , http://b:2,http://c:3")
    assert remote_dispatch.worker_urls() == ["http://a:1", "http://b:2", "http://c:3"]


def test_ci_env_disables_remote_without_ever_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://should-not-be-probed:1")

    def _boom(*_args, **_kwargs):
        raise AssertionError("probe_health must never be called when CI is truthy")

    monkeypatch.setattr(remote_dispatch, "probe_health", _boom)

    result = remote_dispatch.maybe_dispatch_remote(
        steps=_minimal_steps(),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=REPO_ROOT,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=True,  # even --remote-required must not raise here
        emit_cache_hit=_noop_emit_cache_hit,
        notify_hub_on_pass=lambda: None,
    )
    assert result is None


def test_local_flag_disables_remote_without_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://should-not-be-probed:1")

    def _boom(*_args, **_kwargs):
        raise AssertionError("probe_health must never be called with --local")

    monkeypatch.setattr(remote_dispatch, "probe_health", _boom)

    result = remote_dispatch.maybe_dispatch_remote(
        steps=_minimal_steps(),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=REPO_ROOT,
        cache_lookup_enabled=False,
        local_only=True,
        remote_required=False,
        emit_cache_hit=_noop_emit_cache_hit,
        notify_hub_on_pass=lambda: None,
    )
    assert result is None


def test_env_off_disables_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://should-not-be-probed:1")
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))

    result = remote_dispatch.maybe_dispatch_remote(
        steps=_minimal_steps(),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=REPO_ROOT,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=_noop_emit_cache_hit,
        notify_hub_on_pass=lambda: None,
    )
    assert result is None


def test_is_self_dispatch_true_for_loopback_by_default() -> None:
    assert remote_dispatch.is_self_dispatch("http://127.0.0.1:8080", None) is True
    assert remote_dispatch.is_self_dispatch("http://localhost:8080", None) is True


def test_is_self_dispatch_false_with_test_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    assert remote_dispatch.is_self_dispatch("http://127.0.0.1:8080", None) is False


def test_is_self_dispatch_true_by_reported_hostname() -> None:
    reported = {"hostname": socket_module.gethostname()}
    assert remote_dispatch.is_self_dispatch("http://100.64.1.2:8080", reported) is True


def test_is_self_dispatch_false_for_a_genuinely_different_host() -> None:
    # TEST-NET-1 address and a made-up hostname: using the real Desktop's IP
    # here made this test fail whenever the suite ran ON the Desktop worker.
    assert remote_dispatch.is_self_dispatch("http://192.0.2.10:8080", {"hostname": "not-this-host"}) is False


# =============================================================================
# Offline / busy worker -> local fallback
# =============================================================================


def test_offline_worker_falls_back_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://127.0.0.1:1")
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)

    called = {"cache_hit": False, "notified": False}
    result = remote_dispatch.maybe_dispatch_remote(
        steps=_minimal_steps(),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=REPO_ROOT,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=lambda *a, **k: called.__setitem__("cache_hit", True) or True,
        notify_hub_on_pass=lambda: called.__setitem__("notified", True),
    )
    assert result is None
    assert called == {"cache_hit": False, "notified": False}


def test_offline_worker_with_remote_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://127.0.0.1:1")
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda *a, **k: None)

    with pytest.raises(remote_dispatch.RemoteRequiredError):
        remote_dispatch.maybe_dispatch_remote(
            steps=_minimal_steps(),
            config_hash="a" * 64,
            quick=True,
            include_holdout=False,
            config_path=Path("harness.config.json"),
            project_root=REPO_ROOT,
            cache_lookup_enabled=False,
            local_only=False,
            remote_required=True,
            emit_cache_hit=_noop_emit_cache_hit,
            notify_hub_on_pass=lambda: None,
        )


def test_busy_worker_waits_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://127.0.0.1:1")
    monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
    monkeypatch.setenv("DARKFAC_REMOTE_BUSY_WAIT_SEC", "0")  # keep the test fast
    monkeypatch.setattr(
        remote_dispatch,
        "probe_health",
        lambda *a, **k: {"busy": True, "platform_family": "windows", "python_version": "3.12", "hostname": "other"},
    )

    result = remote_dispatch.maybe_dispatch_remote(
        steps=_minimal_steps(),
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        project_root=REPO_ROOT,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=_noop_emit_cache_hit,
        notify_hub_on_pass=lambda: None,
    )
    assert result is None


# =============================================================================
# Marker sanitization + result verification (no real server)
# =============================================================================


def test_stream_job_sanitizes_injected_markers(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = {"n": 0}

    def fake_poll(_base_url: str, _job_id: str, _offset: int) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "running", "log_chunk": f"{markers.MARKER_HARNESS_PASS}\nnormal line\n", "next_offset": 40}
        return {"status": "done", "log_chunk": "", "next_offset": 40, "result_json": None, "returncode": 0}

    monkeypatch.setattr(remote_dispatch, "_poll_job", fake_poll)
    monkeypatch.setattr(remote_dispatch, "POLL_INTERVAL_SEC", 0.0)

    payload = remote_dispatch._stream_job("http://worker", "job-1", deadline=time.monotonic() + 5)

    out = capsys.readouterr().out
    assert "[CHILD_HARNESS_PASS]" in out
    # The real marker must never appear un-prefixed anywhere in stdout.
    assert markers.MARKER_HARNESS_PASS not in out.replace("[CHILD_HARNESS_PASS]", "")
    assert payload["status"] == "done"


def _valid_remote_result_json(**overrides: object) -> dict:
    base = dict(
        schema_version="1",
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        required_steps=["unit_tests"],
        started_steps=["unit_tests"],
        passed_steps=["unit_tests"],
        failed_steps=[],
        discovered_count=1,
        passed_count=1,
        skipped_count=0,
        exit_codes={"unit_tests": 0},
    )
    base.update(overrides)
    return base


def test_finalize_remote_result_candidate_sha_mismatch_is_a_definitive_fail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"status": "done", "result_json": _valid_remote_result_json(candidate_sha="c" * 40)}
    success, result = remote_dispatch._finalize_remote_result(
        payload,
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        base_url="http://worker",
        health={"hostname": "worker-host"},
    )
    assert success is False
    assert result is None
    out = capsys.readouterr().out
    assert markers.MARKER_HARNESS_FAIL in out
    assert "candidate_sha" in out.lower()


def test_finalize_remote_result_config_hash_mismatch_is_a_definitive_fail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {"status": "done", "result_json": _valid_remote_result_json(config_hash="d" * 64)}
    success, result = remote_dispatch._finalize_remote_result(
        payload,
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        base_url="http://worker",
        health={"hostname": "worker-host"},
    )
    assert success is False
    assert result is None
    out = capsys.readouterr().out
    assert markers.MARKER_HARNESS_FAIL in out
    assert "config_hash" in out.lower()


def test_finalize_remote_result_missing_result_json_fails(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"status": "failed", "result_json": None}
    success, result = remote_dispatch._finalize_remote_result(
        payload,
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        base_url="http://worker",
        health={},
    )
    assert success is False
    assert result is None
    assert markers.MARKER_HARNESS_FAIL in capsys.readouterr().out


def test_finalize_remote_result_success_sets_executed_on(capsys: pytest.CaptureFixture[str]) -> None:
    payload = {"status": "done", "result_json": _valid_remote_result_json(), "returncode": 0}
    success, result = remote_dispatch._finalize_remote_result(
        payload,
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        base_url="http://worker:8080",
        health={"hostname": "darkfac-desktop", "platform_family": "windows"},
    )
    assert success is True
    assert isinstance(result, HarnessResult)
    assert result.executed_on == {
        "host": "darkfac-desktop",
        "url": "http://worker:8080",
        "mode": "remote",
        "platform_family": "windows",
    }
    out = capsys.readouterr().out
    assert markers.MARKER_HARNESS_PASS in out
    assert markers.MARKER_STEP_PASS in out


def test_finalize_remote_result_cancelled_status_is_worker_unavailable() -> None:
    payload = {"status": "cancelled"}
    with pytest.raises(remote_dispatch._WorkerUnavailable):
        remote_dispatch._finalize_remote_result(
            payload, candidate_sha="a" * 40, config_hash="b" * 64, base_url="http://worker", health={}
        )


# =============================================================================
# Cancellation on client abort (Ctrl+C / timeout)
# =============================================================================


def test_keyboard_interrupt_during_dispatch_cancels_the_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cancelled: list[tuple[str, str]] = []

    def _fake_create_bundle(*_args: object, **_kwargs: object) -> Path:
        bundle_path = tmp_path / "fake.bundle"
        bundle_path.write_bytes(b"x")
        return bundle_path

    monkeypatch.setattr(remote_dispatch, "_create_bundle", _fake_create_bundle)
    monkeypatch.setattr(remote_dispatch, "_submit_job", lambda *a, **k: {"status_code": 200, "body": {"job_id": "job-xyz"}})

    def _raise_interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(remote_dispatch, "_stream_job", _raise_interrupt)
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda base_url, job_id: cancelled.append((base_url, job_id)))

    with pytest.raises(KeyboardInterrupt):
        remote_dispatch._dispatch_one_job(
            "http://worker",
            health={"known_shas": []},
            project_root=REPO_ROOT,
            candidate_sha="a" * 40,
            tree_hash="e" * 40,
            config_hash="b" * 64,
            quick=True,
            include_holdout=False,
            config_path=REPO_ROOT / "harness.config.json",
            total_timeout_sec=60.0,
        )

    assert cancelled == [("http://worker", "job-xyz")]


def test_worker_vanishing_mid_run_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_stream_job` gives up (raises `_WorkerUnavailable`) once a worker
    stops answering for longer than `WORKER_SILENCE_FALLBACK_SEC`."""

    monkeypatch.setattr(remote_dispatch, "WORKER_SILENCE_FALLBACK_SEC", 0.05)
    monkeypatch.setattr(remote_dispatch, "POLL_INTERVAL_SEC", 0.01)

    def _always_fails(*_args, **_kwargs):
        raise ConnectionError("worker unreachable")

    monkeypatch.setattr(remote_dispatch, "_poll_job", _always_fails)
    monkeypatch.setattr(remote_dispatch, "_cancel_job", lambda *a, **k: None)

    with pytest.raises(remote_dispatch._WorkerUnavailable):
        remote_dispatch._stream_job("http://worker", "job-1", deadline=time.monotonic() + 5)


# =============================================================================
# JobManager: missing-prerequisites -> full-bundle retry contract
# =============================================================================


def test_job_manager_submit_reports_missing_prerequisites_for_a_thin_bundle(tmp_path: Path) -> None:
    client_repo = tmp_path / "client"
    _init_repo(client_repo)
    _write(client_repo, "a.txt", "one\n")
    _commit_all(client_repo, "base")
    base_sha = _git(client_repo, "rev-parse", "HEAD").strip()
    _write(client_repo, "a.txt", "two\n")
    _commit_all(client_repo, "second")
    head_sha = _git(client_repo, "rev-parse", "HEAD").strip()

    thin_bundle = tmp_path / "thin.bundle"
    _git(client_repo, "bundle", "create", str(thin_bundle), "HEAD", "--not", base_sha)

    worker_repo = tmp_path / "worker"
    _init_repo(worker_repo)  # empty: knows nothing about base_sha or head_sha

    manager = JobManager(worker_repo, state_dir=tmp_path / "worker_state")
    status_code, body = manager.submit(thin_bundle.read_bytes(), {"candidate_sha": head_sha})

    assert status_code == 409
    assert body.get("missing_prerequisites") is True

    # And a full bundle (no --not) against the same empty worker succeeds.
    full_bundle = tmp_path / "full.bundle"
    _git(client_repo, "bundle", "create", str(full_bundle), "HEAD")
    status_code2, body2 = manager.submit(full_bundle.read_bytes(), {"candidate_sha": head_sha})
    assert status_code2 == 200
    assert "job_id" in body2


def test_job_manager_submit_rejects_invalid_candidate_sha(tmp_path: Path) -> None:
    worker_repo = tmp_path / "worker"
    _init_repo(worker_repo)
    manager = JobManager(worker_repo, state_dir=tmp_path / "worker_state")
    status_code, body = manager.submit(b"not a real bundle", {"candidate_sha": "not-hex!"})
    assert status_code == 400
    assert "candidate_sha" in body.get("error", "")


# =============================================================================
# Token enforcement on the worker's mutating endpoints
# =============================================================================


def test_health_stays_open_even_with_token_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_TOKEN", "s3cr3t")
    app = create_worker_app(project_root=tmp_path, node_id="token-test")
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "platform_family" in data
    assert "known_shas" in data


def test_harness_jobs_requires_token_when_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKER_TOKEN", "s3cr3t")
    app = create_worker_app(project_root=tmp_path, node_id="token-test")
    client = TestClient(app)

    unauthorized = client.post("/harness/jobs", content=b"bundle-bytes", headers={"X-Job-Meta": ""})
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/harness/jobs",
        content=b"bundle-bytes",
        headers={"Authorization": "Bearer s3cr3t", "X-Job-Meta": ""},
    )
    assert authorized.status_code != 401  # auth passed; it may still 400 on the garbage bundle


def test_harness_jobs_open_without_token_configured(tmp_path: Path) -> None:
    app = create_worker_app(project_root=tmp_path, node_id="token-test")
    client = TestClient(app)
    response = client.post("/harness/jobs", content=b"bundle-bytes", headers={"X-Job-Meta": ""})
    assert response.status_code != 401


# =============================================================================
# End-to-end loopback: real uvicorn server, full protocol
# =============================================================================


@pytest.mark.serial
@pytest.mark.live
def test_end_to_end_loopback_dispatch_real_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Real uvicorn server + real HTTP round-trip, so it needs `--live` +
    `--allow-network` (this repo's convention for opt-in integration tests,
    see tests/conftest.py: `--run-live-audio`/`DARKFAC_RUN_LIVE_AUDIO`
    un-skips `live`-marked tests, `--allow-network`/`DARKFAC_ALLOW_NETWORK`
    un-blocks `urllib.request.urlopen`). Both gates are evaluated once at
    SESSION start by conftest's autouse fixture, so they must be set
    before pytest starts, not via monkeypatch inside the test body:

        DARKFAC_ALLOW_NETWORK=1 python -m pytest tests/test_remote_dispatch.py \\
            --run-live-audio -q

    Without them this test is skipped -- deliberately: `python core/harness
    /runner.py --quick` (the official gate) never sets either, so it never
    tries a real network round-trip. Every other scenario in this module
    (offline/busy/marker-sanitization/token-auth/cancellation/etc.) is a
    real, always-on part of the gate via mocks/TestClient/synthetic repos."""
    import uvicorn

    repo = tmp_path / "fixture_repo"
    _seed_minimal_worker_repo(repo)
    config_hash = _config_hash_of(repo)
    port = _free_port()

    app = create_worker_app(project_root=repo, node_id="loopback-e2e")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn server did not start in time"

        monkeypatch.setenv("DARKFAC_TEST_WORKERS", f"http://127.0.0.1:{port}")
        monkeypatch.setenv(remote_dispatch.ENV_SELF_DISPATCH_TEST_OVERRIDE, "1")
        # The worker's job subprocess is a REAL `runner.py --quick --local`
        # run (inherits this process's env); on PASS it fires its own
        # best-effort hub-refresh POST. Point it at a guaranteed-refused
        # loopback port so the test never makes a real network call and
        # fails fast instead of waiting out the real 4s timeout.
        monkeypatch.setenv("DARKHUB_URL", "http://127.0.0.1:1")

        steps = [HarnessStepConfig(name="unit_tests", cmd="python -m pytest tests -q", quick=True, kind="test", timeout_sec=60)]
        captured = {"notified": False}

        result = remote_dispatch.maybe_dispatch_remote(
            steps=steps,
            config_hash=config_hash,
            quick=True,
            include_holdout=False,
            config_path=repo / "harness.config.json",
            project_root=repo,
            cache_lookup_enabled=True,
            local_only=False,
            remote_required=False,
            emit_cache_hit=_noop_emit_cache_hit,
            notify_hub_on_pass=lambda: captured.__setitem__("notified", True),
        )

        assert result is True
        assert captured["notified"] is True

        out = capsys.readouterr().out
        assert "[REMOTE] dispatched suite to" in out
        assert "[REMOTE 127.0.0.1" in out or f"[REMOTE http://127.0.0.1:{port}]" in out
        assert markers.MARKER_TEST_COUNT in out
        assert markers.MARKER_HARNESS_RESULT in out
        assert markers.MARKER_HARNESS_PASS in out
        assert "executed on" in out

        # Structured evidence carries executed_on and was NOT a cache replay.
        result_line = next(line for line in out.splitlines() if line.startswith(markers.MARKER_HARNESS_RESULT))
        result_payload = json.loads(result_line[len(markers.MARKER_HARNESS_RESULT) + 1 :])
        assert result_payload["executed_on"]["mode"] == "remote"
        assert result_payload["reused_from"] is None
        assert result_payload["passed_count"] >= 1

        # Verdict was cached under the worker's own platform key (local store).
        head_sha = _git(repo, "rev-parse", "HEAD").strip()
        tree_hash = harness_cache.tree_sha(repo)
        worker_key = harness_cache.compute_cache_key(
            tree_hash=tree_hash,
            config_hash=config_hash,
            step_names=["unit_tests"],
            quick=True,
            include_holdout=False,
        )
        cached = harness_cache.get_local_verdict(worker_key)
        assert cached is not None
        assert cached["result"]["candidate_sha"] == head_sha

        # Worktree was cleaned up; at most one job log was kept.
        job_manager: JobManager = app.state.job_manager
        worktrees_dir = job_manager.state_dir / "worktrees"
        assert list(worktrees_dir.iterdir()) == []
        logs_dir = job_manager.state_dir / "logs"
        assert len(list(logs_dir.glob("*.log"))) == 1

        # A second dispatch for the SAME (unchanged) tree hits the
        # worker-platform cache instead of running the suite again.
        replay_calls = {"count": 0}

        def _counting_emit_cache_hit(record: dict, *, config_path: Path) -> bool:
            replay_calls["count"] += 1
            return True

        second = remote_dispatch.maybe_dispatch_remote(
            steps=steps,
            config_hash=config_hash,
            quick=True,
            include_holdout=False,
            config_path=repo / "harness.config.json",
            project_root=repo,
            cache_lookup_enabled=True,
            local_only=False,
            remote_required=False,
            emit_cache_hit=_counting_emit_cache_hit,
            notify_hub_on_pass=lambda: None,
        )
        assert second is True
        assert replay_calls["count"] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def test_worker_redirects_missing_console_streams_to_log(tmp_path, monkeypatch):
    """pythonw / S4U tasks start with stdout/stderr = None; uvicorn's logging
    config then crashes the worker before it binds (exit code 1)."""
    from core.harness import remote_worker

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    log_path = tmp_path / "worker" / "daemon.log"

    assert remote_worker.ensure_console_streams(log_path) == log_path
    try:
        print("hello from a console-less worker")
        assert sys.stdout is sys.stderr
        assert "hello from a console-less worker" in log_path.read_text(encoding="utf-8")
    finally:
        sys.stdout.close()


def test_worker_keeps_real_console_streams(tmp_path):
    from core.harness import remote_worker

    assert remote_worker.ensure_console_streams(tmp_path / "daemon.log") is None
    assert not (tmp_path / "daemon.log").exists()


def test_bundle_when_worker_already_has_head(tmp_path):
    """The worker usually has the exact commit being validated (its own
    origin/main); `HEAD --not HEAD` is empty and git refuses it, which made
    the client silently skip the Desktop and run locally."""
    from core.harness import remote_dispatch

    repo = tmp_path / "repo"
    _init_repo(repo)
    _write(repo, "a.txt", "one\n")
    _commit_all(repo, "first")
    _write(repo, "a.txt", "two\n")
    _commit_all(repo, "second")
    head = _git(repo, "rev-parse", "HEAD").strip()
    parent = _git(repo, "rev-parse", "HEAD~1").strip()

    bundle = remote_dispatch._create_bundle(repo, base_shas=[head])
    try:
        heads = _git(repo, "bundle", "list-heads", str(bundle))
        assert head in heads
        # Only the HEAD commit is shipped; its parent is a prerequisite the
        # worker is known to hold because it already has HEAD.
        verify = subprocess.run(
            ["git", "bundle", "verify", str(bundle)],
            cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        assert verify.returncode == 0, verify.stderr
        assert parent in (verify.stdout + verify.stderr)
    finally:
        bundle.unlink()
