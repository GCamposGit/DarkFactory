"""USR-321 slice 3: cross-process ledger lock and delivery classification wiring."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from core.demands import store as store_mod
from core.demands.store import DemandsLockTimeout, _PathMutex
from core.git.ci_checks import classify_delivery


def _hold(path: str, ready, release) -> None:
    m = _PathMutex(Path(path), timeout=5)
    with m:
        ready.set()
        release.wait(10)


def _incr(path: str, counter: str, n: int) -> None:
    m = _PathMutex(Path(path), timeout=20)
    for _ in range(n):
        with m:
            c = Path(counter)
            v = int(c.read_text()) if c.exists() else 0
            time.sleep(0.001)
            c.write_text(str(v + 1))


def test_lockfile_blocks_other_process_with_clear_timeout(tmp_path):
    ledger = tmp_path / "demands.json"
    ready, release = mp.Event(), mp.Event()
    p = mp.Process(target=_hold, args=(str(ledger), ready, release))
    p.start()
    try:
        assert ready.wait(10)
        with pytest.raises(DemandsLockTimeout, match="held by another process"):
            with _PathMutex(ledger, timeout=0.3):
                pass
    finally:
        release.set()
        p.join(10)
    with _PathMutex(ledger, timeout=2):
        pass


def test_lockfile_serializes_read_modify_write_across_processes(tmp_path):
    ledger, counter = tmp_path / "demands.json", tmp_path / "counter"
    procs = [mp.Process(target=_incr, args=(str(ledger), str(counter), 15)) for _ in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    assert int(counter.read_text()) == 45


def test_mutex_is_reentrant(tmp_path):
    m = _PathMutex(tmp_path / "d.json", timeout=1)
    with m:
        with m:
            pass
    assert (tmp_path / "d.json.lock").exists()


def test_store_uses_path_mutex(tmp_path):
    s = store_mod.DemandsStore(tmp_path / "demands.json")
    assert isinstance(s._lock, _PathMutex)
    assert s.list_tickets() == []


def test_classify_delivery_ledger_only_is_not_implementation():
    assert classify_delivery([".factory/demands/demands.json"]) == "ledger_only"
    assert classify_delivery(["core/x.py", ".factory/demands/demands.json"]) == "implementation"
    assert classify_delivery([]) == "empty"
