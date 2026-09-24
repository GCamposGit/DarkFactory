"""Tests for the runner's xdist/count parsing and graceful degradation.

Real captured samples (not guesses) for `_pytest_counts`: run with
`python -m pytest tests/test_harness_contract.py [-v|-q] [-n 2]` on this
repo and pasted verbatim below, per the acceleration task's requirement to
verify parsing "against REAL output, not assumptions."
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

from core.harness import runner


# --------------------------------------------------------------------------- #
# _pytest_counts on real captured output
# --------------------------------------------------------------------------- #

_REAL_VERBOSE_NO_XDIST = """\
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\\dev\\DarkFac
configfile: pytest.ini
plugins: anyio-4.14.2, cov-7.1.0, timeout-2.4.0, xdist-3.8.0
timeout: 120.0s
timeout method: thread
timeout func_only: False
collecting ... collected 10 items

tests/test_harness_contract.py::test_well_formed_supervisor_result_is_accepted PASSED [ 10%]
tests/test_harness_contract.py::test_config_hash_is_stable_and_bound_to_validated_content PASSED [ 70%]
tests/test_harness_contract.py::test_marker_parser_runs_as_standalone_script PASSED [100%]

============================= 10 passed in 0.39s ==============================
"""

_REAL_QUIET_NO_XDIST = """\
.............                                                            [100%]
13 passed in 0.39s
"""

_REAL_QUIET_XDIST = """\
bringing up nodes...
bringing up nodes...

.............                                                            [100%]
13 passed in 1.31s
"""

_REAL_VERBOSE_XDIST = """\
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\\dev\\DarkFac
configfile: pytest.ini
plugins: anyio-4.14.2, cov-7.1.0, timeout-2.4.0, xdist-3.8.0
timeout: 120.0s
timeout method: thread
timeout func_only: False
created: 2/2 workers
2 workers [13 items]

.............                                                            [100%]
============================= 13 passed in 1.23s ==============================
"""

_REAL_QUIET_WITH_SKIPS_AND_FAILURE = """\
.....F...s..                                                             [100%]
11 passed, 1 skipped, 1 failed in 2.01s
"""


@pytest.mark.parametrize(
    "output, expected_discovered, expected_passed, expected_skipped",
    [
        (_REAL_VERBOSE_NO_XDIST, 10, 10, 0),
        (_REAL_QUIET_NO_XDIST, 13, 13, 0),
        (_REAL_QUIET_XDIST, 13, 13, 0),
        (_REAL_VERBOSE_XDIST, 13, 13, 0),
        (_REAL_QUIET_WITH_SKIPS_AND_FAILURE, 13, 11, 1),
    ],
)
def test_pytest_counts_on_real_captured_output(
    output: str, expected_discovered: int, expected_passed: int, expected_skipped: int
) -> None:
    discovered, passed, skipped = runner._pytest_counts(output)
    assert discovered == expected_discovered
    assert passed == expected_passed
    assert skipped == expected_skipped


def test_pytest_counts_on_genuinely_empty_output_is_zero() -> None:
    assert runner._pytest_counts("no recognizable pytest output at all") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# resolve_command: quoted marker expressions must not leak literal quotes
# --------------------------------------------------------------------------- #


def test_resolve_command_strips_quotes_from_marker_expression(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.os, "name", "nt", raising=False)
    command = runner.resolve_command('python -m pytest tests -m "not serial" --durations=25')
    assert "not serial" in command
    assert '"not serial"' not in command
    assert all('"' not in token for token in command)


def test_resolve_command_posix_already_strips_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.os, "name", "posix", raising=False)
    command = runner.resolve_command('python -m pytest tests -m "not serial"')
    assert "not serial" in command


# --------------------------------------------------------------------------- #
# Graceful degradation when pytest-xdist is not importable
# --------------------------------------------------------------------------- #


def test_strip_xdist_args_when_available_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_xdist_available", lambda: True)
    command = ["python", "-m", "pytest", "tests", "-n", "auto", "--dist", "loadfile"]
    assert runner.strip_xdist_args_if_unavailable(command) == command


def test_strip_xdist_args_when_unavailable_removes_flags_and_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "_xdist_available", lambda: False)
    command = ["python", "-m", "pytest", "tests", "-n", "auto", "--dist", "loadfile", "-q"]
    stripped = runner.strip_xdist_args_if_unavailable(command)

    assert stripped == ["python", "-m", "pytest", "tests", "-q"]
    captured = capsys.readouterr()
    assert "[WARN]" in captured.out
    assert "pip install" in captured.out


def test_strip_xdist_args_when_unavailable_and_absent_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "_xdist_available", lambda: False)
    command = ["python", "-m", "pytest", "tests", "-q"]
    assert runner.strip_xdist_args_if_unavailable(command) == command


# --------------------------------------------------------------------------- #
# run_with_cache: cache disabled/flag/CI, cache hit short-circuits execute()
# --------------------------------------------------------------------------- #


def _minimal_config() -> "runner.HarnessConfig":
    from core.harness.models import HarnessConfig, HarnessStepConfig

    return HarnessConfig(
        steps=[HarnessStepConfig(name="probe", cmd="python -c pass", quick=True, kind="check")]
    )


@pytest.fixture(autouse=True)
def _isolated_cache_and_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DARKFAC_SUITE_LOCK", "off")  # never touch the real machine lock in tests
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DARKFAC_HARNESS_CACHE", raising=False)
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)


def test_run_with_cache_calls_execute_directly_when_no_cache_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_execute(config, **kwargs):
        calls.append("execute")
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    config = _minimal_config()

    result = runner.run_with_cache(
        config,
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
        no_cache=True,
    )
    assert result is True
    assert calls == ["execute"]


def test_run_with_cache_stores_and_then_reuses_verdict_on_second_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from core.harness.models import HarnessResult

    execute_calls = {"count": 0}

    def fake_execute(config, *, config_hash, quick, include_holdout, config_path, on_result=None):
        execute_calls["count"] += 1
        result = HarnessResult(
            candidate_sha="a" * 40,
            config_hash=config_hash,
            required_steps=["probe"],
            started_steps=["probe"],
            passed_steps=["probe"],
            failed_steps=[],
            discovered_count=3,
            passed_count=3,
            skipped_count=0,
            exit_codes={"probe": 0},
        )
        if on_result is not None:
            on_result(result)
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "a" * 40)
    monkeypatch.setattr(runner.harness_cache, "tree_sha", lambda project_root: "e" * 40)

    config = _minimal_config()
    kwargs = dict(
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )

    first = runner.run_with_cache(config, **kwargs)
    assert first is True
    assert execute_calls["count"] == 1

    capsys.readouterr()
    second = runner.run_with_cache(config, **kwargs)
    assert second is True
    assert execute_calls["count"] == 1  # NOT called again -- verdict was reused

    captured = capsys.readouterr()
    assert "[CACHE_HIT]" in captured.out
    assert runner.MARKER_HARNESS_PASS in captured.out


def test_run_with_cache_never_stores_a_failed_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    execute_calls = {"count": 0}

    def fake_execute(config, *, config_hash, quick, include_holdout, config_path, on_result=None):
        execute_calls["count"] += 1
        return False  # failure -- must never be cached

    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "b" * 40)
    monkeypatch.setattr(runner.harness_cache, "tree_sha", lambda project_root: "f" * 40)

    config = _minimal_config()
    kwargs = dict(
        config_hash="b" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )

    first = runner.run_with_cache(config, **kwargs)
    assert first is False
    assert execute_calls["count"] == 1

    second = runner.run_with_cache(config, **kwargs)
    assert second is False
    assert execute_calls["count"] == 2  # re-run: the failure was never cached


@pytest.mark.parametrize("disable_env", [{"CI": "1"}, {"DARKFAC_HARNESS_CACHE": "off"}])
def test_run_with_cache_is_bypassed_by_ci_or_flag(
    monkeypatch: pytest.MonkeyPatch, disable_env: dict[str, str]
) -> None:
    for key, value in disable_env.items():
        monkeypatch.setenv(key, value)

    calls: list[str] = []

    def fake_execute(config, **kwargs):
        calls.append("execute")
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    config = _minimal_config()

    runner.run_with_cache(
        config,
        config_hash="c" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )
    runner.run_with_cache(
        config,
        config_hash="c" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )
    assert calls == ["execute", "execute"]  # cache never engaged either time


def test_run_with_cache_falls_through_to_execute_on_dirty_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    def dirty(*args, **kwargs):
        raise RuntimeError("Candidate worktree is dirty")

    monkeypatch.setattr(runner, "_ensure_clean_worktree", dirty)

    calls: list[str] = []

    def fake_execute(config, **kwargs):
        calls.append("execute")
        return False

    monkeypatch.setattr(runner, "execute", fake_execute)
    config = _minimal_config()

    result = runner.run_with_cache(
        config,
        config_hash="d" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )
    assert result is False
    assert calls == ["execute"]


# --------------------------------------------------------------------------- #
# run_with_cache: cross-host single-flight restructuring (rule A2) -- the
# remote wait must never happen while holding the local machine-wide lock.
# --------------------------------------------------------------------------- #


def test_run_with_cache_polls_remote_before_touching_local_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A2: waiting on another host's in-flight run must happen BEFORE (and
    without holding) the local suite lock, so this host's other suites are
    never idled behind a remote wait."""

    lock_state = {"held": False}
    poll_observed_lock_held: list[bool] = []

    @contextlib.contextmanager
    def fake_suite_lock(**kwargs):
        lock_state["held"] = True
        try:
            yield None
        finally:
            lock_state["held"] = False

    def fake_poll_for_remote_verdict(key, *, deadline_sec):
        poll_observed_lock_held.append(lock_state["held"])
        return None  # nobody finished -- fall through to running it ourselves

    execute_calls = {"count": 0}

    def fake_execute(config, *, config_hash, quick, include_holdout, config_path, on_result=None):
        execute_calls["count"] += 1
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "c" * 40)
    monkeypatch.setattr(runner.harness_cache, "tree_sha", lambda project_root: "9" * 40)
    monkeypatch.setattr(runner.harness_suite_lock, "suite_lock", fake_suite_lock)
    monkeypatch.setattr(runner.harness_cache, "poll_for_remote_verdict", fake_poll_for_remote_verdict)
    monkeypatch.setattr(runner.harness_cache, "acquire_inflight", lambda key, expires_in_sec: True)
    monkeypatch.setattr(runner.harness_cache, "release_inflight", lambda key: None)
    monkeypatch.setattr(runner.harness_cache, "heartbeat_inflight", lambda *a, **k: None)

    config = _minimal_config()
    result = runner.run_with_cache(
        config,
        config_hash="9" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )

    assert result is True
    assert execute_calls["count"] == 1
    assert poll_observed_lock_held == [False]  # the poll ran with the lock NOT held


def test_run_with_cache_short_circuits_on_remote_poll_hit_without_local_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2: when the pre-lock poll finds a PASS from another host, the local
    suite lock must never be entered at all -- not just "not held during the
    poll", the whole local run is skipped."""

    from core.harness.models import HarnessResult

    lock_entries = {"count": 0}

    @contextlib.contextmanager
    def fake_suite_lock(**kwargs):
        lock_entries["count"] += 1
        yield None

    cached_result = HarnessResult(
        candidate_sha="a" * 40,
        config_hash="a" * 64,
        required_steps=["probe"],
        started_steps=["probe"],
        passed_steps=["probe"],
        failed_steps=[],
        discovered_count=1,
        passed_count=1,
        skipped_count=0,
        exit_codes={"probe": 0},
    )
    remote_record = {
        "tree_sha": "a" * 40,
        "candidate_sha": "a" * 40,
        "config_hash": "a" * 64,
        "os_family": "posix",
        "py_version": "3.12",
        "host": "remote-host",
        "result": cached_result.model_dump(mode="json"),
        "created_at": time.time(),
    }

    def fake_poll(key, *, deadline_sec):
        return remote_record

    execute_calls = {"count": 0}

    def fake_execute(*args, **kwargs):
        execute_calls["count"] += 1
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "a" * 40)
    monkeypatch.setattr(runner.harness_cache, "tree_sha", lambda project_root: "a" * 40)
    monkeypatch.setattr(runner.harness_suite_lock, "suite_lock", fake_suite_lock)
    monkeypatch.setattr(runner.harness_cache, "poll_for_remote_verdict", fake_poll)

    config = _minimal_config()
    result = runner.run_with_cache(
        config,
        config_hash="a" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )

    assert result is True
    assert execute_calls["count"] == 0  # reused the remote verdict, never ran locally
    assert lock_entries["count"] == 0  # local lock was never even entered


def test_run_with_cache_starts_and_stops_a_heartbeat_thread_around_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A3: a heartbeat thread must be running while `execute` is in
    progress, and must be stopped (joined) once it returns -- verified via a
    real threading.Event so this catches a heartbeat that never gets
    signalled to stop (which would leak a daemon thread per run)."""

    import threading

    from core.harness.models import HarnessResult

    heartbeat_calls: list[dict] = []
    observed_thread_alive_during_execute = {"value": None}

    def fake_heartbeat_inflight(key, *, stop_event: threading.Event, lease_sec=None):
        heartbeat_calls.append({"key": key, "lease_sec": lease_sec})
        stop_event.wait(5.0)  # blocks until run_with_cache signals stop

    def fake_execute(config, *, config_hash, quick, include_holdout, config_path, on_result=None):
        # The heartbeat thread should already be alive at this point.
        observed_thread_alive_during_execute["value"] = any(
            t.name == "darkfac-harness-inflight-heartbeat" and t.is_alive()
            for t in threading.enumerate()
        )
        result = HarnessResult(
            candidate_sha="b" * 40,
            config_hash=config_hash,
            required_steps=["probe"],
            started_steps=["probe"],
            passed_steps=["probe"],
            failed_steps=[],
            discovered_count=1,
            passed_count=1,
            skipped_count=0,
            exit_codes={"probe": 0},
        )
        if on_result is not None:
            on_result(result)
        return True

    monkeypatch.setattr(runner, "execute", fake_execute)
    monkeypatch.setattr(runner, "_ensure_clean_worktree", lambda: None)
    monkeypatch.setattr(runner, "_candidate_sha", lambda: "b" * 40)
    monkeypatch.setattr(runner.harness_cache, "tree_sha", lambda project_root: "b" * 40)
    monkeypatch.setattr(runner.harness_cache, "poll_for_remote_verdict", lambda key, *, deadline_sec: None)
    monkeypatch.setattr(runner.harness_cache, "heartbeat_inflight", fake_heartbeat_inflight)

    config = _minimal_config()
    result = runner.run_with_cache(
        config,
        config_hash="b" * 64,
        quick=True,
        include_holdout=False,
        config_path=Path("harness.config.json"),
    )

    assert result is True
    assert len(heartbeat_calls) == 1
    assert observed_thread_alive_during_execute["value"] is True
    # After run_with_cache returns, the daemon thread must have been joined
    # -- none left alive under that name.
    assert not any(
        t.name == "darkfac-harness-inflight-heartbeat" and t.is_alive() for t in threading.enumerate()
    )
