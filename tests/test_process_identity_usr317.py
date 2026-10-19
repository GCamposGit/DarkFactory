"""USR-317: process identity independent of boot_time drift, lazy worker ``app``.

Skipped until the owner applies ``docs/proposals/USR-317-process-identity.patch``
(``core/harness`` is protected governance); green before and after.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from core.harness import remote_worker, suite_lock

pi = pytest.importorskip("core.harness.process_identity", reason="USR-317 patch not applied yet")
psutil = pytest.importorskip("psutil")

pytestmark = pytest.mark.skipif(
    not hasattr(remote_worker, "_THIS_PROCESS_START_TICKS"), reason="USR-317 patch not applied yet"
)

DRIFT_SEC = 600.0  # VM freeze/restore moves boot_time (and create_time) by minutes


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _fixed_ticks(monkeypatch: pytest.MonkeyPatch, value: int | None) -> None:
    monkeypatch.setattr(pi, "proc_start_ticks", lambda pid=None: value)
    monkeypatch.setattr(remote_worker, "proc_start_ticks", lambda pid=None: value)


def test_parse_proc_stat_handles_parentheses_and_spaces_in_comm() -> None:
    fields_after = ["S"] + ["0"] * 18 + ["424242"] + ["0"] * 5
    line = "123 (we ) ird (name) " + " ".join(fields_after)
    assert pi.parse_proc_stat_start_ticks(line) == 424242
    assert pi.parse_proc_stat_start_ticks("garbage") is None
    assert pi.parse_proc_stat_start_ticks("1 (x) S 0") is None


def test_proc_start_ticks_is_none_off_linux_and_int_on_linux() -> None:
    value = pi.proc_start_ticks()
    if sys.platform.startswith("linux"):
        assert isinstance(value, int) and value > 0
        assert value == pi.proc_start_ticks(os.getpid())
    else:
        assert value is None


def test_process_alive_survives_boot_time_drift(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 5000)
    recorded_create_time = psutil.Process(sleeper.pid).create_time() - DRIFT_SEC
    assert remote_worker._process_alive(sleeper.pid, recorded_create_time, 5000) is True


def test_process_alive_rejects_recycled_pid(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 6000)
    create_time = psutil.Process(sleeper.pid).create_time()
    assert remote_worker._process_alive(sleeper.pid, create_time, 5000) is False


def test_process_alive_without_ticks_keeps_create_time_tolerance(sleeper) -> None:
    create_time = psutil.Process(sleeper.pid).create_time()
    assert remote_worker._process_alive(sleeper.pid, create_time, None) is True
    assert remote_worker._process_alive(sleeper.pid, create_time - DRIFT_SEC, None) is False


def test_process_tree_keeps_root_under_drift_and_kills_it(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 7000)
    drifted = psutil.Process(sleeper.pid).create_time() - DRIFT_SEC
    tree = remote_worker._ProcessTree(sleeper.pid, drifted, 7000)
    tree.refresh()
    assert sleeper.pid in tree._known
    assert tree.kill(force=True) >= 1
    assert sleeper.wait(timeout=15) is not None


def test_process_tree_ignores_recycled_root(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 8000)
    tree = remote_worker._ProcessTree(sleeper.pid, time.time(), 7999)
    tree.refresh()
    assert tree._known == {}


def test_terminate_process_tree_spares_recycled_pid(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 9000)
    remote_worker._terminate_process_tree(sleeper.pid, time.time(), force=True, start_ticks=1)
    assert sleeper.poll() is None


def test_sample_holder_tree_survives_drift_and_rejects_recycled(sleeper, monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_ticks(monkeypatch, 4000)
    # drift: create_time looks far younger than the sidecar's started_at (would exceed the old 5 s rule)
    older_started_at = psutil.Process(sleeper.pid).create_time() - DRIFT_SEC
    assert suite_lock._sample_holder_tree(sleeper.pid, older_started_at, 4000) is not None
    # recycled pid: another starttime
    assert suite_lock._sample_holder_tree(sleeper.pid, older_started_at, 3999) is None


def test_sidecar_records_start_ticks(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suite_lock, "proc_start_ticks", lambda pid=None: 31337)
    lock_path = tmp_path / "suite.lock"
    suite_lock._write_sidecar(lock_path)
    assert suite_lock._read_sidecar(lock_path)["start_ticks"] == 31337


def test_job_record_roundtrips_start_ticks() -> None:
    job = remote_worker.HarnessJob.from_record({"job_id": "j1"})
    job.child_start_ticks = 77
    clone = remote_worker.HarnessJob.from_record(job.to_record())
    assert clone.owner_start_ticks == job.owner_start_ticks
    assert clone.child_start_ticks == 77


def test_import_does_not_build_app_or_job_manager() -> None:
    code = (
        "import core.harness.remote_worker as m; "
        "assert m._LAZY_APP is None, 'app built on import'; "
        "app = m.app; assert app is m.app and m._LAZY_APP is app; print('LAZY_OK')"
    )
    env = {**os.environ, "DARKFAC_WORKER_STATE_DIR": os.path.join(os.environ.get("TEMP", "."), "usr317_state_probe")}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert "LAZY_OK" in result.stdout, result.stdout + result.stderr


def test_unknown_module_attribute_still_raises() -> None:
    with pytest.raises(AttributeError):
        remote_worker.definitely_missing_attribute  # noqa: B018
