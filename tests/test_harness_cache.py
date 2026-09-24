"""Tests for the verdict cache and cross-host single-flight (`core/harness/cache.py`)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from core.harness import cache


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DARKFAC_HARNESS_CACHE", raising=False)
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    return state_dir


# --------------------------------------------------------------------------- #
# Cache key
# --------------------------------------------------------------------------- #


def _key(**overrides: Any) -> str:
    base = dict(
        tree_hash="a" * 40,
        config_hash="b" * 64,
        step_names=["unit_and_integration_tests_parallel", "unit_and_integration_tests_serial"],
        quick=True,
        include_holdout=False,
    )
    base.update(overrides)
    return cache.compute_cache_key(**base)


def test_cache_key_is_deterministic() -> None:
    assert _key() == _key()


def test_cache_key_is_insensitive_to_step_order() -> None:
    assert _key(step_names=["a", "b"]) == _key(step_names=["b", "a"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"tree_hash": "c" * 40},
        {"config_hash": "d" * 64},
        {"step_names": ["only_one_step"]},
        {"quick": False},
        {"include_holdout": True},
    ],
)
def test_cache_key_changes_when_any_input_changes(overrides: dict[str, Any]) -> None:
    assert _key(**overrides) != _key()


def test_cache_key_is_bound_to_tree_not_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A rebase/merge that reproduces the same tree must hash identically."""

    same_tree_different_commit_key_a = _key(tree_hash="e" * 40)
    same_tree_different_commit_key_b = _key(tree_hash="e" * 40)
    assert same_tree_different_commit_key_a == same_tree_different_commit_key_b


def test_cache_key_incorporates_platform_and_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "platform_family", lambda: "windows")
    windows_key = _key()
    monkeypatch.setattr(cache, "platform_family", lambda: "posix")
    posix_key = _key()
    assert windows_key != posix_key

    monkeypatch.setattr(cache, "platform_family", lambda: "posix")
    monkeypatch.setattr(cache, "python_version_tag", lambda: "3.12")
    py312_key = _key()
    monkeypatch.setattr(cache, "python_version_tag", lambda: "3.11")
    py311_key = _key()
    assert py312_key != py311_key


def test_tree_sha_reads_the_real_git_tree(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    value = cache.tree_sha(project_root)
    assert value is not None
    assert len(value) == 40
    assert all(c in "0123456789abcdef" for c in value)


def test_tree_sha_returns_none_outside_a_git_repository(tmp_path: Path) -> None:
    assert cache.tree_sha(tmp_path) is None


# --------------------------------------------------------------------------- #
# Local cache backend: hit / miss / never-cache-a-failure
# --------------------------------------------------------------------------- #


def _record(**overrides: Any) -> dict[str, Any]:
    base = {
        "tree_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "config_hash": "c" * 64,
        "os_family": "windows",
        "py_version": "3.12",
        "host": "TEST-HOST",
        "result": {
            "required_steps": ["step_a"],
            "config_hash": "c" * 64,
            "candidate_sha": "b" * 40,
            "discovered_count": 10,
            "passed_count": 10,
            "skipped_count": 0,
        },
        "created_at": time.time(),
    }
    base.update(overrides)
    return base


def test_local_cache_miss_returns_none() -> None:
    assert cache.get_local_verdict("nonexistent-key") is None
    assert cache.get_verdict("nonexistent-key") is None


def test_local_cache_hit_roundtrips_after_store() -> None:
    key = "some-key"
    record = _record()
    cache.set_local_verdict(key, record)

    fetched = cache.get_local_verdict(key)
    assert fetched == record
    assert cache.get_verdict(key) == record


def test_local_store_write_is_atomic_and_survives_multiple_keys(tmp_path: Path) -> None:
    cache.set_local_verdict("key-1", _record(host="H1"))
    cache.set_local_verdict("key-2", _record(host="H2"))

    assert cache.get_local_verdict("key-1")["host"] == "H1"
    assert cache.get_local_verdict("key-2")["host"] == "H2"

    store_path = tmp_path / "state" / "verdicts.json"
    assert store_path.exists()
    assert not (tmp_path / "state" / "verdicts.tmp").exists()


def test_build_record_only_happens_on_success_path_by_convention() -> None:
    """`build_record`/`store_verdict` are only ever called by the runner after
    a PASS -- there is no failure record shape at all, which is exactly how
    failures are guaranteed to never be reused."""

    record = cache.build_record(
        tree_hash="a" * 40,
        candidate_sha="b" * 40,
        config_hash="c" * 64,
        result={"discovered_count": 5, "passed_count": 5, "skipped_count": 0, "required_steps": ["x"]},
    )
    assert record["result"]["passed_count"] == 5
    assert "failed" not in record


# --------------------------------------------------------------------------- #
# CI / env / flag disable
# --------------------------------------------------------------------------- #


def test_cache_disabled_by_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    assert cache.cache_disabled() is True


def test_cache_disabled_by_darkfac_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HARNESS_CACHE", "off")
    assert cache.cache_disabled() is True


def test_cache_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DARKFAC_HARNESS_CACHE", raising=False)
    assert cache.cache_disabled() is False


# --------------------------------------------------------------------------- #
# Postgres backend with a fake/stub driver (no real DB)
# --------------------------------------------------------------------------- #


class _FakeCursor:
    def __init__(self, tables: dict[str, dict[str, tuple]]) -> None:
        self._tables = tables
        self._last: Any = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized.startswith("CREATE TABLE"):
            return
        if normalized.startswith("SELECT tree_sha"):
            (key,) = params
            self._last = self._tables["harness_verdicts"].get(key)
            return
        if normalized.startswith("INSERT INTO harness_verdicts"):
            key, tree_sha, candidate_sha, config_hash, os_family, py_version, host, result_json = params
            self._tables["harness_verdicts"][key] = (
                tree_sha,
                candidate_sha,
                config_hash,
                os_family,
                py_version,
                host,
                result_json,
                "2026-01-01T00:00:00+00:00",
            )
            return
        if normalized.startswith("SELECT host, pid, expires_at"):
            (key,) = params
            self._last = self._tables["harness_inflight"].get(key)
            return
        if normalized.startswith("SELECT %s < CURRENT_TIMESTAMP"):
            (expires_at,) = params
            self._last = (expires_at < time.time(),)
            return
        if normalized.startswith("INSERT INTO harness_inflight"):
            key, host, pid, expires_in, _expires_in_again = params
            self._tables["harness_inflight"][key] = (host, pid, time.time() + expires_in)
            self.rowcount = 1
            return
        if normalized.startswith("UPDATE harness_inflight"):
            expires_in, key, host, pid = params
            row = self._tables["harness_inflight"].get(key)
            if row is not None and row[0] == host and row[1] == pid:
                self._tables["harness_inflight"][key] = (host, pid, time.time() + expires_in)
                self.rowcount = 1
            else:
                self.rowcount = 0
            return
        if normalized.startswith("DELETE FROM harness_inflight"):
            key, pid = params
            row = self._tables["harness_inflight"].get(key)
            if row is not None and row[1] == pid:
                del self._tables["harness_inflight"][key]
            return
        raise AssertionError(f"unexpected SQL in fake psycopg: {normalized}")

    def fetchone(self) -> Any:
        return self._last

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeConnection:
    def __init__(self, tables: dict[str, dict[str, tuple]]) -> None:
        self._tables = tables

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._tables)

    def commit(self) -> None:
        pass

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakePsycopgModule:
    def __init__(self) -> None:
        self.tables: dict[str, dict[str, tuple]] = {"harness_verdicts": {}, "harness_inflight": {}}

    def connect(self, url: str, connect_timeout: int | None = None) -> _FakeConnection:
        return _FakeConnection(self.tables)


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch) -> _FakePsycopgModule:
    fake_module = _FakePsycopgModule()
    monkeypatch.setattr(cache, "_import_psycopg", lambda: fake_module)
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://worker:secret@10.0.0.1:5432/darkfac")
    return fake_module


def test_remote_verdict_roundtrip_with_fake_driver(fake_postgres: _FakePsycopgModule) -> None:
    key = "remote-key"
    assert cache.get_remote_verdict(key) is None

    record = _record()
    cache.set_remote_verdict(key, record)

    fetched = cache.get_remote_verdict(key)
    assert fetched is not None
    assert fetched["tree_sha"] == record["tree_sha"]
    assert fetched["result"] == record["result"]


def test_get_verdict_checks_local_then_remote_and_backfills_local(fake_postgres: _FakePsycopgModule) -> None:
    key = "backfill-key"
    record = _record()
    cache.set_remote_verdict(key, record)

    assert cache.get_local_verdict(key) is None  # not local yet
    fetched = cache.get_verdict(key)
    assert fetched is not None
    assert cache.get_local_verdict(key) is not None  # now backfilled locally


def test_store_verdict_writes_both_backends(fake_postgres: _FakePsycopgModule) -> None:
    key = "dual-write-key"
    record = _record()
    cache.store_verdict(key, record)

    assert cache.get_local_verdict(key) is not None
    assert cache.get_remote_verdict(key) is not None


def test_inflight_single_flight_second_claimant_is_rejected(fake_postgres: _FakePsycopgModule) -> None:
    key = "inflight-key"
    assert cache.acquire_inflight(key, expires_in_sec=60.0) is True
    assert cache.acquire_inflight(key, expires_in_sec=60.0) is False  # still live; rejected


def test_inflight_expired_claim_can_be_taken_over(fake_postgres: _FakePsycopgModule) -> None:
    key = "inflight-expired"
    assert cache.acquire_inflight(key, expires_in_sec=-1.0) is True  # already "expired"
    assert cache.acquire_inflight(key, expires_in_sec=60.0) is True  # takeover allowed


def test_inflight_release_only_removes_own_pid_claim(fake_postgres: _FakePsycopgModule) -> None:
    key = "inflight-release"
    cache.acquire_inflight(key, expires_in_sec=60.0)
    cache.release_inflight(key)
    # Released -> a fresh claim succeeds again.
    assert cache.acquire_inflight(key, expires_in_sec=60.0) is True


# --------------------------------------------------------------------------- #
# get_inflight_claim / refresh_inflight / heartbeat (cross-host single-flight
# defect fixes A1/A2/A3)
# --------------------------------------------------------------------------- #


def test_get_inflight_claim_is_none_when_no_row_exists(fake_postgres: _FakePsycopgModule) -> None:
    assert cache.get_inflight_claim("no-such-key") is None


def test_get_inflight_claim_returns_live_claim(fake_postgres: _FakePsycopgModule) -> None:
    key = "live-claim"
    cache.acquire_inflight(key, expires_in_sec=60.0)

    claim = cache.get_inflight_claim(key)
    assert claim is not None
    assert claim["pid"] == __import__("os").getpid()


def test_get_inflight_claim_is_none_once_expired(fake_postgres: _FakePsycopgModule) -> None:
    key = "expired-claim"
    cache.acquire_inflight(key, expires_in_sec=-1.0)  # already expired

    assert cache.get_inflight_claim(key) is None


def test_poll_for_remote_verdict_stops_fast_when_holder_finished_without_a_verdict(
    fake_postgres: _FakePsycopgModule,
) -> None:
    """A1: the other host claimed the key, then finished with a FAILURE (so
    no verdict was ever stored -- failures are never cached) and deleted its
    inflight row. The waiter must notice on its very next poll and return,
    not idle out the full deadline."""

    key = "poll-fast-on-finished-failure"
    cache.acquire_inflight(key, expires_in_sec=60.0)
    cache.release_inflight(key)  # simulates the holder finishing (failure path)

    started = time.monotonic()
    result = cache.poll_for_remote_verdict(key, deadline_sec=30.0)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 2.0  # nowhere near the 30s deadline


def test_poll_for_remote_verdict_stops_fast_when_nobody_is_inflight(
    fake_postgres: _FakePsycopgModule,
) -> None:
    """A2 support: calling the poll when no one holds the key at all (the
    common case for the pre-lock check) must return near-instantly."""

    started = time.monotonic()
    result = cache.poll_for_remote_verdict("never-claimed-key", deadline_sec=30.0)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 2.0


def test_poll_for_remote_verdict_returns_verdict_once_stored(
    fake_postgres: _FakePsycopgModule,
) -> None:
    key = "poll-hit-key"
    cache.acquire_inflight(key, expires_in_sec=60.0)
    cache.set_remote_verdict(key, _record())

    result = cache.poll_for_remote_verdict(key, deadline_sec=30.0)
    assert result is not None
    assert result["result"] == _record()["result"]


def test_inflight_lease_expires_without_heartbeat_and_can_be_taken_over(
    fake_postgres: _FakePsycopgModule,
) -> None:
    """A3: a short lease with no heartbeat lapses quickly and another host's
    claim attempt then succeeds (the abandoned-lease takeover path)."""

    key = "lease-no-heartbeat"
    assert cache.acquire_inflight(key, expires_in_sec=0.05) is True
    time.sleep(0.15)

    assert cache.get_inflight_claim(key) is None  # treated as abandoned
    assert cache.acquire_inflight(key, expires_in_sec=60.0) is True  # takeover allowed


def test_heartbeat_inflight_keeps_the_lease_alive_past_its_original_expiry(
    fake_postgres: _FakePsycopgModule,
) -> None:
    """A3: while a heartbeat thread is running, the lease must stay alive far
    longer than its own short duration would otherwise allow."""

    import threading

    key = "heartbeat-alive"
    lease_sec = 0.3
    assert cache.acquire_inflight(key, expires_in_sec=lease_sec) is True

    stop_event = threading.Event()
    thread = threading.Thread(
        target=cache.heartbeat_inflight,
        args=(key,),
        kwargs={"stop_event": stop_event, "lease_sec": lease_sec},
        daemon=True,
    )
    thread.start()
    try:
        # Without the heartbeat this lease (0.3s) would already have lapsed.
        time.sleep(lease_sec * 2.5)
        assert cache.get_inflight_claim(key) is not None
    finally:
        stop_event.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()

    # Once the heartbeat has stopped, the (short) lease lapses again and the
    # claim is abandoned like any other orphan.
    time.sleep(lease_sec * 5)
    assert cache.get_inflight_claim(key) is None


def test_refresh_inflight_fails_once_another_host_has_taken_over(
    fake_postgres: _FakePsycopgModule,
) -> None:
    key = "refresh-lost-ownership"
    cache.acquire_inflight(key, expires_in_sec=-1.0)  # our claim, already expired
    # Another host/pid takes over by mutating the table directly (as a real
    # second acquire_inflight call from a different process would).
    fake_postgres.tables["harness_inflight"][key] = ("other-host", 999999, time.time() + 60.0)

    assert cache.refresh_inflight(key, expires_in_sec=60.0) is False


def test_refresh_inflight_succeeds_while_still_owned(fake_postgres: _FakePsycopgModule) -> None:
    key = "refresh-still-owned"
    cache.acquire_inflight(key, expires_in_sec=1.0)

    assert cache.refresh_inflight(key, expires_in_sec=60.0) is True
    claim = cache.get_inflight_claim(key)
    assert claim is not None  # extended well past the original 1s lease


def test_inflight_lease_sec_default_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_HARNESS_INFLIGHT_LEASE_SEC", raising=False)
    assert cache.inflight_lease_sec() == 90.0

    monkeypatch.setenv("DARKFAC_HARNESS_INFLIGHT_LEASE_SEC", "45")
    assert cache.inflight_lease_sec() == 45.0

    monkeypatch.setenv("DARKFAC_HARNESS_INFLIGHT_LEASE_SEC", "not-a-number")
    assert cache.inflight_lease_sec() == 90.0  # invalid -> falls back to default


# --------------------------------------------------------------------------- #
# Degradation when psycopg (or the configured URL) is unavailable
# --------------------------------------------------------------------------- #


def test_no_database_url_configured_is_local_only_silently() -> None:
    assert cache.get_remote_verdict("k") is None
    cache.set_remote_verdict("k", _record())  # must not raise
    assert cache.acquire_inflight("k", expires_in_sec=10.0) is True  # proceed locally
    cache.release_inflight("k")  # must not raise
    assert cache.get_inflight_claim("k") is None  # nothing to see without coordination
    assert cache.refresh_inflight("k", expires_in_sec=10.0) is True  # nothing to refresh, proceed
    assert cache.poll_for_remote_verdict("k", deadline_sec=30.0) is None


def test_psycopg_not_importable_degrades_to_local_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://worker:secret@10.0.0.1:5432/darkfac")
    monkeypatch.setattr(cache, "_import_psycopg", lambda: None)

    assert cache.get_remote_verdict("k") is None
    cache.set_remote_verdict("k", _record())  # must not raise
    assert cache.acquire_inflight("k", expires_in_sec=10.0) is True
    cache.release_inflight("k")
    assert cache.get_inflight_claim("k") is None
    assert cache.refresh_inflight("k", expires_in_sec=10.0) is True


def test_remote_backend_exception_degrades_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _ExplodingModule:
        def connect(self, url: str, connect_timeout: int | None = None) -> Any:
            raise ConnectionError("simulated network failure to 10.0.0.1:5432")

    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://worker:supersecret@10.0.0.1:5432/darkfac")
    monkeypatch.setattr(cache, "_import_psycopg", lambda: _ExplodingModule())

    result = cache.get_remote_verdict("k")
    assert result is None

    captured = capsys.readouterr()
    assert "[WARN]" in captured.out
    assert "supersecret" not in captured.out  # credentials never leak into logs


def test_refresh_inflight_backend_exception_degrades_with_warning_and_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A3: a transient DB failure during a heartbeat tick must never crash
    the run -- it degrades to "lost ownership" (``False``) with one
    sanitized warning, and the caller (heartbeat_inflight) just logs once
    and keeps going or stops, but never raises."""

    class _ExplodingModule:
        def connect(self, url: str, connect_timeout: int | None = None) -> Any:
            raise ConnectionError("simulated network failure to 10.0.0.1:5432")

    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://worker:supersecret@10.0.0.1:5432/darkfac")
    monkeypatch.setattr(cache, "_import_psycopg", lambda: _ExplodingModule())

    result = cache.refresh_inflight("k", expires_in_sec=60.0)
    assert result is False

    captured = capsys.readouterr()
    assert "[WARN]" in captured.out
    assert "supersecret" not in captured.out  # credentials never leak into logs


def test_heartbeat_inflight_survives_refresh_exceptions_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A3: heartbeat_inflight must never propagate a refresh failure -- it
    logs once and keeps ticking (a transient outage may recover)."""

    import threading

    call_count = {"n": 0}

    def flaky_refresh(key: str, *, expires_in_sec: float) -> bool:
        call_count["n"] += 1
        raise ConnectionError("simulated transient outage")

    monkeypatch.setattr(cache, "refresh_inflight", flaky_refresh)

    stop_event = threading.Event()
    thread = threading.Thread(
        target=cache.heartbeat_inflight,
        args=("k",),
        kwargs={"stop_event": stop_event, "lease_sec": 0.1},
        daemon=True,
    )
    thread.start()
    time.sleep(0.35)  # several heartbeat ticks at lease_sec/3 ~= 0.033s
    stop_event.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()  # never crashed the thread, just kept looping
    assert call_count["n"] >= 2  # it really did keep retrying past the first failure

    captured = capsys.readouterr()
    assert captured.out.count("[WARN]") == 1  # warned exactly once, not per-tick
