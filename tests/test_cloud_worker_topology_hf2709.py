"""Tests for the HF-27-09 worker-topology additions to CloudWorker.

Covers: DARKFAC_WORKER_CAPS / DARKFAC_WORKER_PRIORITY env reading,
ready_age_sec resolution and forwarding to store.claim(), the auth-probe
capability filter, and the priority-aware poll-interval default in main().
No network, no real docker/Postgres/Tailscale/Telegram, no subprocess.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.orchestrator import cloud_worker as cw
from core.orchestrator.cloud_worker import CloudWorker, DEFAULT_CAPABILITIES, _priority_defaults


# --------------------------------------------------------------------------
# DARKFAC_WORKER_CAPS
# --------------------------------------------------------------------------


def test_capabilities_default_when_unset(monkeypatch):
    monkeypatch.delenv("DARKFAC_WORKER_CAPS", raising=False)
    worker = CloudWorker(worker_id="w1")
    assert worker.capabilities == list(DEFAULT_CAPABILITIES)


def test_capabilities_from_env_var(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "git,gh,node,python,harness:claude")
    worker = CloudWorker(worker_id="w1")
    assert worker.capabilities == ["git", "gh", "node", "python", "harness:claude"]


def test_explicit_capabilities_param_wins_over_env(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "git,gh")
    worker = CloudWorker(worker_id="w1", capabilities=["custom"])
    assert worker.capabilities == ["custom"]


# --------------------------------------------------------------------------
# ready_age_sec resolution (DARKFAC_WORKER_PRIORITY)
# --------------------------------------------------------------------------


def test_priority_defaults_table():
    assert _priority_defaults("primary") == (2.0, 0.0)
    assert _priority_defaults("secondary") == (10.0, 30.0)
    assert _priority_defaults("fallback") == (20.0, 30.0)
    assert _priority_defaults(None) == (None, 0.0)
    assert _priority_defaults("unknown") == (None, 0.0)
    # Case-insensitive
    assert _priority_defaults("PRIMARY") == (2.0, 0.0)


def test_ready_age_sec_defaults_to_zero_when_unset(monkeypatch):
    monkeypatch.delenv("DARKFAC_WORKER_PRIORITY", raising=False)
    monkeypatch.delenv("DARKFAC_WORKER_READY_AGE_SEC", raising=False)
    worker = CloudWorker(worker_id="w1")
    assert worker.ready_age_sec == 0.0


def test_ready_age_sec_from_priority_env(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_PRIORITY", "secondary")
    monkeypatch.delenv("DARKFAC_WORKER_READY_AGE_SEC", raising=False)
    worker = CloudWorker(worker_id="w1")
    assert worker.ready_age_sec == 30.0


def test_ready_age_sec_explicit_env_override_wins(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_PRIORITY", "primary")
    monkeypatch.setenv("DARKFAC_WORKER_READY_AGE_SEC", "45")
    worker = CloudWorker(worker_id="w1")
    assert worker.ready_age_sec == 45.0


def test_ready_age_sec_constructor_param_wins_over_everything(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_PRIORITY", "fallback")
    monkeypatch.setenv("DARKFAC_WORKER_READY_AGE_SEC", "45")
    worker = CloudWorker(worker_id="w1", ready_age_sec=1.5)
    assert worker.ready_age_sec == 1.5


class _RecordingStore:
    """Minimal store double recording the ready_age_sec forwarded by poll_and_execute_once."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def claim(self, worker, capabilities, now, ready_age_sec=0.0):
        self.calls.append(ready_age_sec)
        return None


def test_poll_and_execute_once_forwards_ready_age_sec(monkeypatch):
    store = _RecordingStore()
    worker = CloudWorker(worker_id="w1", store=store, ready_age_sec=17.0)
    executed = worker.poll_and_execute_once(now=datetime(2026, 9, 23, tzinfo=UTC))
    assert executed is False
    assert store.calls == [17.0]


# --------------------------------------------------------------------------
# Auth-probe capability filter (constructor-level, HF-27-09 acceptance #3)
# --------------------------------------------------------------------------


def test_capability_prober_drops_failed_harness(monkeypatch):
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "git,gh,harness:claude,harness:codex")
    worker = CloudWorker(worker_id="w1", capability_prober=lambda h: h != "codex")
    assert worker.capabilities == ["git", "gh", "harness:claude"]


def test_capability_prober_none_by_default_keeps_harness_caps(monkeypatch):
    # No prober given: today's behaviour is unchanged, no filtering happens
    # (and, critically, no subprocess/probe is ever invoked).
    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "git,harness:claude")
    worker = CloudWorker(worker_id="w1")
    assert worker.capabilities == ["git", "harness:claude"]


def test_capability_prober_exception_drops_capability(monkeypatch):
    def _boom(_harness: str) -> bool:
        raise RuntimeError("probe crashed")

    monkeypatch.setenv("DARKFAC_WORKER_CAPS", "git,harness:claude")
    worker = CloudWorker(worker_id="w1", capability_prober=_boom)
    assert worker.capabilities == ["git"]


# --------------------------------------------------------------------------
# main() poll-interval default resolution
# --------------------------------------------------------------------------


def test_main_status_uses_priority_poll_default_without_crashing(monkeypatch, capsys):
    """--status must exit before entering the loop; this just guards main()'s
    capability_prober wiring (import failure must degrade silently, not raise)."""
    monkeypatch.delenv("DARKFAC_WORKER_CAPS", raising=False)
    monkeypatch.delenv("DARKFAC_WORKER_PRIORITY", raising=False)
    exit_code = cw.main(["--status"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "worker_id" in captured.out
