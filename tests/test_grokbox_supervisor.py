"""Decisao pura do supervisor do grokbox (USR-315); sem VM, rede ou processos."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture(scope="module")
def sup():
    spec = importlib.util.spec_from_file_location(
        "grokbox_supervisor_under_test", REPO_ROOT / "scripts" / "grokbox_supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def state(sup, **kw):
    base = dict(head_sha=OLD, process_alive=True, launch_sha=OLD, health_sha=OLD, health_busy=False)
    base.update(kw)
    return sup.State(**base)


def test_unchanged_head_is_noop(sup):
    assert sup.decide(state(sup)) == sup.NOOP


def test_head_changed_idle_reloads(sup):
    assert sup.decide(state(sup, head_sha=NEW)) == sup.RELOAD


def test_head_changed_busy_waits(sup):
    assert sup.decide(state(sup, head_sha=NEW, health_busy=True)) == sup.WAIT


def test_head_changed_health_unreachable_never_kills(sup):
    st = state(sup, head_sha=NEW, health_sha=None, health_busy=None)
    assert sup.decide(st) == sup.WAIT


def test_unreachable_health_falls_back_to_launch_sha(sup):
    assert sup.decide(state(sup, health_sha=None, health_busy=None)) == sup.NOOP


def test_worker_dead_launches_even_if_head_same(sup):
    assert sup.decide(state(sup, process_alive=False, health_sha=None, health_busy=None)) == sup.LAUNCH


def test_stop_file_wins_over_everything(sup):
    assert sup.decide(state(sup, process_alive=False, stop_requested=True)) == sup.STOP


def test_unreadable_head_is_noop(sup):
    assert sup.decide(state(sup, head_sha="")) == sup.NOOP


def test_default_interval_is_at_most_15_seconds(sup):
    assert sup.parse_args([]).interval <= 15
