"""run_ticket.py wires the grokbox wake pre-flight check (USR-310).

The actual probe/email/wait behaviour is unit-tested against `core.line.grokbox_wake`
directly (`tests/line/test_grokbox_wake.py`). This file only checks that `_run()`'s
pre-flight calls `grokbox_wake.preflight_check()`, and that the `PYTEST_CURRENT_TEST`
guard around it (and around the gate-side `_wait_gate_grokbox_if_needed`) does what it
is there for: keep an unrelated test from touching the real checkout's `.factory` state
or sleeping for real while the suite itself runs under pytest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import run_ticket
from core.line import grokbox_wake, routing
from core.line.routing import _HARNESS_TO_PROVIDER
from run_ticket import main

_TICKET = run_ticket.UserTicket(
    id="USR-310-HOOK", project_id="darkfac", title="Ticket de teste", problem_statement="problema de teste"
)


def _quotas() -> dict[str, dict[str, Any]]:
    return {
        harness: {"provider": provider, "headroom": 60.0, "is_critical": False, "status": "SAUDÁVEL"}
        for harness, provider in _HARNESS_TO_PROVIDER.items()
    }


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str):
        return _TICKET if ticket_id == _TICKET.id else None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: _quotas())
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: tmp_path / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "pick", MagicMock(return_value=("claude", "sonnet")))


def test_dry_run_leaves_grokbox_wake_untouched_under_pytest(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(grokbox_wake, "preflight_check", lambda **kw: calls.append(kw) or False)

    assert main(["USR-310-HOOK", "--dry-run"]) == 0

    assert calls == []


def test_dry_run_calls_grokbox_preflight_check_outside_pytest(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    monkeypatch.setattr(grokbox_wake, "preflight_check", lambda **kw: calls.append(kw) or False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    assert main(["USR-310-HOOK", "--dry-run"]) == 0

    assert len(calls) == 1


def test_wait_gate_grokbox_if_needed_skipped_under_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(grokbox_wake, "wait_if_down", lambda **kw: calls.append(kw) or False)

    run_ticket._wait_gate_grokbox_if_needed(False)

    assert calls == []


def test_wait_gate_grokbox_if_needed_calls_wait_if_down_outside_pytest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list = []
    monkeypatch.setattr(grokbox_wake, "wait_if_down", lambda **kw: calls.append(kw) or False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    run_ticket._wait_gate_grokbox_if_needed(True)

    assert len(calls) == 1
    assert calls[0]["announce"] is None  # as_json=True suppresses the stderr announce callback
