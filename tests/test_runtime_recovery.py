from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.orchestrator.runtime import OrchestratorRuntime
from core.orchestrator.store import (
    LeaseConflictError,
    OrchestratorStore,
    StaleLeaseError,
)


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_only_one_live_owner_can_claim_a_task(tmp_path: Path) -> None:
    database = tmp_path / "orchestrator.sqlite3"
    store = OrchestratorStore(database)

    def claim(owner: str) -> tuple[str, object]:
        try:
            return ("claimed", store.claim("DF-11", owner=owner, lease_seconds=30))
        except LeaseConflictError as exc:
            return ("rejected", exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ["worker-a", "worker-b"]))

    assert [outcome[0] for outcome in outcomes].count("claimed") == 1
    assert [outcome[0] for outcome in outcomes].count("rejected") == 1


def test_expired_claim_fences_old_owner_and_rejects_stale_completion(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = OrchestratorStore(tmp_path / "orchestrator.sqlite3", clock=clock)

    first = store.claim("DF-11", owner="worker-a", lease_seconds=5)
    clock.advance(6)
    second = store.claim("DF-11", owner="worker-b", lease_seconds=5)

    assert second.fencing_token == first.fencing_token + 1
    assert second.token != first.token
    with pytest.raises(StaleLeaseError, match="stale|expired|fencing"):
        store.complete(first, result={"owner": "worker-a"})

    completed = store.complete(second, result={"owner": "worker-b"})
    assert completed.status.value == "SUCCEEDED"


def test_renewal_extends_a_live_claim(tmp_path: Path) -> None:
    clock = MutableClock()
    store = OrchestratorStore(tmp_path / "orchestrator.sqlite3", clock=clock)

    claim = store.claim("DF-11", owner="worker-a", lease_seconds=5)
    renewed = store.renew(claim, lease_seconds=20)

    assert renewed.expires_at == clock() + timedelta(seconds=20)
    assert renewed.fencing_token == claim.fencing_token


def test_runtime_recovers_from_crash_after_checkpoint(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "orchestrator.sqlite3"
    store = OrchestratorStore(database, clock=clock)
    calls: list[tuple[str, int]] = []
    crash_once = True

    def first_step(context) -> dict[str, str]:
        calls.append(("first", context.step_index))
        return {"checkpoint": "persisted"}

    def second_step(context) -> dict[str, bool]:
        nonlocal crash_once
        calls.append(("second", context.step_index))
        if crash_once:
            crash_once = False
            raise RuntimeError("simulated process crash")
        return {"done": True}

    runtime_a = OrchestratorRuntime(
        store,
        owner="worker-a",
        lease_seconds=5,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        runtime_a.run("DF-11", [first_step, second_step])

    interrupted = store.get_active_run("DF-11")
    assert interrupted is not None
    assert interrupted.step_index == 1
    assert interrupted.checkpoint["outputs"]["0"] == {"checkpoint": "persisted"}

    clock.advance(6)
    runtime_b = OrchestratorRuntime(
        OrchestratorStore(database, clock=clock),
        owner="worker-b",
        lease_seconds=5,
    )
    result = runtime_b.run("DF-11", [first_step, second_step])

    assert result.recovered is True
    assert result.status.value == "SUCCEEDED"
    assert calls == [("first", 0), ("second", 1), ("second", 1)]
    assert result.checkpoint["outputs"]["1"] == {"done": True}

