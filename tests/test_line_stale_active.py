"""USR-498: detector de ticket `implementing`/`validating` sem vida e reconciliacao para `planned`."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.line import line_lock
from core.line.pending_delivery import PendingDelivery
from core.line.stale_active import (
    ACTIVE_STATUSES,
    DEFAULT_IDLE_LIMIT,
    STALE_ACTIVE_CAUSE,
    StaleActiveTicket,
    collect_liveness,
    find_stale_active,
    main,
    reconcile_stale_active,
    stale_active_evidence,
)
from core.roadmap.models import DeliveryStatus

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def _ticket(
    ticket_id: str = "USR-01",
    *,
    status: str = "implementing",
    updated_at: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": ticket_id, "status": status}
    if updated_at is not None:
        row["updated_at"] = updated_at
    if created_at is not None:
        row["created_at"] = created_at
    return row


def _ago(hours: float, *, base: datetime = NOW) -> str:
    return (base - timedelta(hours=hours)).isoformat()


# --------------------------------------------------------------------------
# find_stale_active: pure filtering
# --------------------------------------------------------------------------


def test_only_implementing_and_validating_are_considered() -> None:
    tickets = [
        _ticket("USR-01", status="planned", updated_at=_ago(72)),
        _ticket("USR-02", status="waiting_human", updated_at=_ago(72)),
        _ticket("USR-03", status="completed", updated_at=_ago(72)),
        _ticket("USR-04", status="cancelled", updated_at=_ago(72)),
        _ticket("USR-05", status="implementing", updated_at=_ago(72)),
        _ticket("USR-06", status="validating", updated_at=_ago(72)),
    ]
    result = find_stale_active(tickets, now=NOW, is_alive=lambda _tid: False)
    assert {item.ticket_id for item in result} == {"USR-05", "USR-06"}
    assert ACTIVE_STATUSES == {"implementing", "validating"}


def test_ticket_with_any_liveness_evidence_never_appears() -> None:
    tickets = [_ticket("USR-07", status="implementing", updated_at=_ago(72))]
    result = find_stale_active(tickets, now=NOW, is_alive=lambda _tid: True)
    assert result == []


def test_idle_at_or_below_limit_is_not_stale() -> None:
    at_limit = _ticket("USR-08", status="implementing", updated_at=(NOW - DEFAULT_IDLE_LIMIT).isoformat())
    just_over = _ticket("USR-09", status="implementing", updated_at=(NOW - DEFAULT_IDLE_LIMIT - timedelta(seconds=1)).isoformat())
    result = find_stale_active([at_limit, just_over], now=NOW, is_alive=lambda _tid: False)
    assert [item.ticket_id for item in result] == ["USR-09"]


def test_age_falls_back_to_created_at_when_updated_at_missing() -> None:
    ticket = _ticket("USR-10", status="validating", created_at=_ago(48))
    result = find_stale_active([ticket], now=NOW, is_alive=lambda _tid: False)
    assert len(result) == 1
    assert result[0].idle == timedelta(hours=48)


def test_ticket_without_any_timestamp_is_ignored() -> None:
    ticket = _ticket("USR-11", status="implementing")
    result = find_stale_active([ticket], now=NOW, is_alive=lambda _tid: False)
    assert result == []


def test_stale_result_shape() -> None:
    ticket = _ticket("USR-12", status="implementing", updated_at=_ago(72))
    [found] = find_stale_active([ticket], now=NOW, is_alive=lambda _tid: False)
    assert isinstance(found, StaleActiveTicket)
    assert found.ticket_id == "USR-12"
    assert found.status == "implementing"
    assert found.idle == timedelta(hours=72)
    assert found.cause


# --------------------------------------------------------------------------
# collect_liveness: fail-closed aggregation of the four probes
# --------------------------------------------------------------------------


def test_collect_liveness_false_when_no_probe_finds_evidence(tmp_path: Path) -> None:
    class EmptyStore:
        def list_active_run_ids(self) -> list[str]:
            return []

        def get_run_status(self, run_id: str) -> dict[str, Any] | None:
            return None

    alive = collect_liveness(
        "USR-20",
        store=EmptyStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )
    assert alive is False


def test_collect_liveness_true_when_process_is_running(tmp_path: Path) -> None:
    alive = collect_liveness(
        "USR-21",
        store=_EmptyStore(),
        running_runs={"USR-21": "notebook"},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )
    assert alive is True


def test_collect_liveness_true_when_line_lock_matches_ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    lock_root = tmp_path / "locks"
    lock = line_lock.LineLock("USR-22", root=lock_root)
    lock.acquire()
    try:
        alive = collect_liveness(
            "USR-22",
            store=_EmptyStore(),
            running_runs={},
            lock_root=lock_root,
            pending_directory=tmp_path / "pending",
        )
    finally:
        lock.release()
    assert alive is True


def test_collect_liveness_false_when_line_lock_belongs_to_another_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    lock_root = tmp_path / "locks"
    lock = line_lock.LineLock("USR-OTHER", root=lock_root)
    lock.acquire()
    try:
        alive = collect_liveness(
            "USR-23",
            store=_EmptyStore(),
            running_runs={},
            lock_root=lock_root,
            pending_directory=tmp_path / "pending",
        )
    finally:
        lock.release()
    assert alive is False


def test_collect_liveness_true_when_pending_delivery_is_running(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True)
    row = PendingDelivery(ticket_id="USR-24", worktree=str(tmp_path), stage="development", status="running", pid=4242)
    (pending_dir / "USR-24.json").write_text(row.model_dump_json(), encoding="utf-8")
    alive = collect_liveness(
        "USR-24",
        store=_EmptyStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=pending_dir,
    )
    assert alive is True


@pytest.mark.parametrize("status", ["waiting", "blocked"])
def test_collect_liveness_false_when_pending_delivery_is_not_running(tmp_path: Path, status: str) -> None:
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True)
    row = PendingDelivery(ticket_id="USR-25", worktree=str(tmp_path), stage="development", status=status)
    (pending_dir / "USR-25.json").write_text(row.model_dump_json(), encoding="utf-8")
    alive = collect_liveness(
        "USR-25",
        store=_EmptyStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=pending_dir,
    )
    assert alive is False


def test_collect_liveness_true_when_control_store_has_an_open_job(tmp_path: Path) -> None:
    class OpenJobStore:
        def list_active_run_ids(self) -> list[str]:
            return ["run-1"]

        def get_run_status(self, run_id: str) -> dict[str, Any] | None:
            return {
                "run_id": "run-1",
                "status": "active",
                "jobs": [{"ticket_id": "USR-26", "stage": "development", "status": "running"}],
            }

    alive = collect_liveness(
        "USR-26",
        store=OpenJobStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )
    assert alive is True


def test_collect_liveness_false_when_control_store_job_is_terminal(tmp_path: Path) -> None:
    class TerminalJobStore:
        def list_active_run_ids(self) -> list[str]:
            return ["run-1"]

        def get_run_status(self, run_id: str) -> dict[str, Any] | None:
            return {
                "run_id": "run-1",
                "status": "active",
                "jobs": [{"ticket_id": "USR-27", "stage": "development", "status": "succeeded"}],
            }

    alive = collect_liveness(
        "USR-27",
        store=TerminalJobStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )
    assert alive is False


def test_collect_liveness_fails_closed_when_a_probe_raises(tmp_path: Path) -> None:
    class BrokenStore:
        def list_active_run_ids(self) -> list[str]:
            raise RuntimeError("control store unreachable")

    alive = collect_liveness(
        "USR-28",
        store=BrokenStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )
    assert alive is True


def test_collect_liveness_fails_closed_when_pending_delivery_file_is_corrupt(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "USR-29.json").write_text("{not valid json", encoding="utf-8")
    alive = collect_liveness(
        "USR-29",
        store=_EmptyStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=pending_dir,
    )
    assert alive is True


def test_collect_liveness_never_propagates_the_probe_exception(tmp_path: Path) -> None:
    class ExplodingStore:
        def list_active_run_ids(self) -> list[str]:
            raise ValueError("boom")

    # Must not raise: a probe failure is swallowed and treated as "alive".
    collect_liveness(
        "USR-30",
        store=ExplodingStore(),
        running_runs={},
        lock_root=tmp_path / "locks",
        pending_directory=tmp_path / "pending",
    )


class _EmptyStore:
    def list_active_run_ids(self) -> list[str]:
        return []

    def get_run_status(self, run_id: str) -> dict[str, Any] | None:
        return None


# --------------------------------------------------------------------------
# reconcile_stale_active (T2): planned, evidence, dry_run, limit, idempotence
# --------------------------------------------------------------------------

OLD = NOW - timedelta(hours=72)
PRIOR_EVIDENCE = "sha-previous"


def _persist(
    store: DemandsStore,
    ticket_id: str,
    *,
    status: str = "implementing",
    evidence: str | None = PRIOR_EVIDENCE,
    updated_at: datetime | None = None,
) -> None:
    moment = OLD if updated_at is None else updated_at
    store.save_ticket(
        UserTicket(
            id=ticket_id,
            title=f"Ticket {ticket_id}",
            status=DeliveryStatus(status),
            delivery_evidence=evidence,
            problem_statement="orig",
            updated_at=moment,
            created_at=moment,
        )
    )


def _ledger(tmp_path: Path) -> tuple[DemandsStore, Path]:
    path = tmp_path / "demands.json"
    return DemandsStore(path), path


def test_reconcile_returns_detected_ticket_to_planned_and_appends_cause(tmp_path: Path) -> None:
    store, _path = _ledger(tmp_path)
    _persist(store, "USR-40", status="validating")
    actions = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False)

    assert len(actions) == 1
    assert actions[0].action == "requeued"
    assert actions[0].previous_status == "validating"
    assert actions[0].cause == STALE_ACTIVE_CAUSE
    reloaded = DemandsStore(store.path).get_ticket("USR-40")
    assert reloaded is not None
    assert reloaded.status == DeliveryStatus.PLANNED
    evidence = reloaded.delivery_evidence or ""
    assert PRIOR_EVIDENCE in evidence
    assert STALE_ACTIVE_CAUSE in evidence
    assert "previous_status=validating" in evidence
    assert evidence.count(stale_active_evidence(cause=STALE_ACTIVE_CAUSE, previous_status="validating")) == 1


def test_second_sweep_is_idempotent_and_does_not_duplicate_evidence(tmp_path: Path) -> None:
    store, path = _ledger(tmp_path)
    _persist(store, "USR-41", status="implementing")
    first = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False)
    after_first = path.read_bytes()
    evidence_once = DemandsStore(path).get_ticket("USR-41")
    assert evidence_once is not None

    second = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False)

    assert len(first) == 1 and second == []
    assert path.read_bytes() == after_first
    again = DemandsStore(path).get_ticket("USR-41")
    assert again is not None
    assert again.status == DeliveryStatus.PLANNED
    assert again.delivery_evidence == evidence_once.delivery_evidence
    assert (again.delivery_evidence or "").count(STALE_ACTIVE_CAUSE) == 1


def test_reconcile_does_not_touch_branch_worktree_or_line_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(line_lock.ENV_INHERITED, raising=False)
    store, _path = _ledger(tmp_path)
    _persist(store, "USR-42", status="implementing")
    lock_root = tmp_path / "state"
    lock = line_lock.LineLock(
        "USR-OTHER",
        root=lock_root,
        fleet_mode=False,
        heartbeat_wait=lambda _interval: True,
    )
    lock.acquire()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    kept = worktree / "kept.txt"
    kept.write_text("preserve-worktree", encoding="utf-8")
    branch = tmp_path / "refs" / "heads" / "ticket" / "usr-42"
    branch.parent.mkdir(parents=True)
    branch.write_text("abc123\n", encoding="utf-8")
    lock_file = line_lock.lock_path(lock_root)
    before = {
        "worktree": kept.read_bytes(),
        "branch": branch.read_bytes(),
        "lock": lock_file.read_bytes(),
    }
    try:
        reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False)
        assert kept.read_bytes() == before["worktree"]
        assert branch.read_bytes() == before["branch"]
        assert lock_file.read_bytes() == before["lock"]
        assert lock_file.is_file()
        info = line_lock.get_lock_info(lock_root)
        assert info is not None and info.ticket_id == "USR-OTHER"
    finally:
        lock.release()
    reloaded = store.get_ticket("USR-42")
    assert reloaded is not None and reloaded.status == DeliveryStatus.PLANNED


def test_dry_run_reports_actions_without_writing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store, path = _ledger(tmp_path)
    _persist(store, "USR-43", status="implementing")
    blob = path.read_bytes()
    caplog.set_level(logging.INFO, logger="core.line.stale_active")

    actions = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False, dry_run=True)

    assert path.read_bytes() == blob
    assert len(actions) == 1
    assert actions[0].action == "would_requeue"
    assert actions[0].ticket_id == "USR-43"
    assert actions[0].previous_status == "implementing"
    assert actions[0].cause == STALE_ACTIVE_CAUSE
    assert actions[0].idle_seconds == int(timedelta(hours=72).total_seconds())
    stayed = store.get_ticket("USR-43")
    assert stayed is not None
    assert stayed.status == DeliveryStatus.IMPLEMENTING
    assert stayed.delivery_evidence == PRIOR_EVIDENCE
    payload = _logged_payload(caplog)
    assert payload["ticket"] == "USR-43"
    assert payload["previous_status"] == "implementing"
    assert payload["cause"] == STALE_ACTIVE_CAUSE
    assert payload["idle_seconds"] == actions[0].idle_seconds
    assert payload["action"] == "would_requeue"


def test_live_ticket_is_not_requeued(tmp_path: Path) -> None:
    store, _path = _ledger(tmp_path)
    _persist(store, "USR-44", status="implementing")
    _persist(store, "USR-45", status="validating")

    actions = reconcile_stale_active(store, now=NOW, is_alive=lambda tid: tid == "USR-44")

    assert [item.ticket_id for item in actions] == ["USR-45"]
    live = store.get_ticket("USR-44")
    assert live is not None
    assert live.status == DeliveryStatus.IMPLEMENTING
    assert live.delivery_evidence == PRIOR_EVIDENCE


def test_max_actions_limits_one_sweep(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store, _path = _ledger(tmp_path)
    for ticket_id in ("USR-46", "USR-47", "USR-48"):
        _persist(store, ticket_id, status="implementing")
    caplog.set_level(logging.INFO, logger="core.line.stale_active")

    actions = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False, max_actions=1)

    assert [item.ticket_id for item in actions] == ["USR-46"]
    assert store.get_ticket("USR-46") is not None
    assert store.get_ticket("USR-46").status == DeliveryStatus.PLANNED  # type: ignore[union-attr]
    assert store.get_ticket("USR-47").status == DeliveryStatus.IMPLEMENTING  # type: ignore[union-attr]
    assert store.get_ticket("USR-48").status == DeliveryStatus.IMPLEMENTING  # type: ignore[union-attr]
    logged = [r for r in caplog.records if r.getMessage().startswith("stale_active_reconciled ")]
    assert len(logged) == 1
    payload = json.loads(logged[0].getMessage().split(" ", 1)[1])
    assert payload["ticket"] == "USR-46"
    assert payload["previous_status"] == "implementing"
    assert payload["cause"] == STALE_ACTIVE_CAUSE
    assert payload["idle_seconds"] > 0


def test_requeue_still_refuses_completed_and_cancelled(tmp_path: Path) -> None:
    store, path = _ledger(tmp_path)
    _persist(store, "USR-49", status="completed", evidence="sha-done")
    _persist(store, "USR-50", status="cancelled", evidence=None)
    blob = path.read_bytes()
    line = stale_active_evidence(cause=STALE_ACTIVE_CAUSE, previous_status="completed")

    with pytest.raises(ValueError, match="refusing to requeue"):
        store.requeue_ticket("USR-49", STALE_ACTIVE_CAUSE, evidence=line)
    with pytest.raises(ValueError, match="refusing to requeue"):
        store.requeue_ticket("USR-50", STALE_ACTIVE_CAUSE, evidence=line)
    assert path.read_bytes() == blob

    actions = reconcile_stale_active(store, now=NOW, is_alive=lambda _tid: False)
    assert actions == []
    assert store.get_ticket("USR-49").status == DeliveryStatus.COMPLETED  # type: ignore[union-attr]
    assert store.get_ticket("USR-50").status == DeliveryStatus.CANCELLED  # type: ignore[union-attr]


def test_reopen_still_clears_evidence_and_ignores_append(tmp_path: Path) -> None:
    store, _path = _ledger(tmp_path)
    _persist(store, "USR-51", status="completed", evidence="sha-done")
    updated, changed = store.requeue_ticket(
        "USR-51",
        "explicit reopen",
        reopen=True,
        evidence="should-not-stick",
    )
    assert changed is True
    assert updated.status == DeliveryStatus.PLANNED
    assert updated.delivery_evidence is None
    assert "explicit reopen" in updated.problem_statement
    assert "should-not-stick" not in (updated.problem_statement or "")


def test_reconcile_records_refusal_when_requeue_rejects(caplog: pytest.LogCaptureFixture) -> None:
    class _Rejecting:
        def list_tickets(self) -> list[dict[str, Any]]:
            return [{"id": "USR-52", "status": "implementing", "updated_at": _ago(48)}]

        def requeue_ticket(self, ticket_id: str, reason: str = "", *, evidence: str | None = None) -> tuple[Any, bool]:
            raise ValueError(f"Ticket {ticket_id} is completed; refusing to requeue")

    caplog.set_level(logging.INFO, logger="core.line.stale_active")
    actions = reconcile_stale_active(_Rejecting(), now=NOW, is_alive=lambda _tid: False)
    assert len(actions) == 1
    assert actions[0].action == "refused"
    assert actions[0].previous_status == "implementing"
    assert "refusing to requeue" in actions[0].detail
    payload = _logged_payload(caplog)
    assert payload["ticket"] == "USR-52"
    assert payload["previous_status"] == "implementing"
    assert payload["cause"] == STALE_ACTIVE_CAUSE
    assert payload["idle_seconds"] == int(timedelta(hours=48).total_seconds())


def test_cli_dry_run_reports_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "demands.json"
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(path))
    store = DemandsStore(path)
    _persist(
        store,
        "USR-53",
        status="implementing",
        updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    blob = path.read_bytes()
    monkeypatch.setattr("core.line.stale_active.collect_liveness", lambda *_args, **_kwargs: False)

    code = main(["--dry-run", "--max-actions", "5"])

    assert code == 0
    assert path.read_bytes() == blob
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["action"] == "would_requeue"
    assert payload[0]["ticket_id"] == "USR-53"
    assert payload[0]["previous_status"] == "implementing"
    assert payload[0]["cause"] == STALE_ACTIVE_CAUSE
    stayed = DemandsStore(path).get_ticket("USR-53")
    assert stayed is not None and stayed.status == DeliveryStatus.IMPLEMENTING


def test_append_does_not_duplicate_an_evidence_line_already_present(tmp_path: Path) -> None:
    store, _path = _ledger(tmp_path)
    line = stale_active_evidence(cause=STALE_ACTIVE_CAUSE, previous_status="implementing")
    _persist(store, "USR-54", status="implementing", evidence=f"{PRIOR_EVIDENCE}\n{line}")
    _updated, changed = store.requeue_ticket("USR-54", STALE_ACTIVE_CAUSE, evidence=line)
    assert changed is True
    reloaded = store.get_ticket("USR-54")
    assert reloaded is not None
    assert reloaded.status == DeliveryStatus.PLANNED
    assert (reloaded.delivery_evidence or "").count(line) == 1
    assert (reloaded.delivery_evidence or "").startswith(PRIOR_EVIDENCE)


def _logged_payload(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("stale_active_reconciled ")
    )
    body = json.loads(message.split(" ", 1)[1])
    assert isinstance(body, dict)
    return body


# --------------------------------------------------------------------------
# T3: o despachante reconcilia antes da fila
# --------------------------------------------------------------------------


class _Poll:
    def poll(self) -> None:
        return None


class _Ledger:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requeued: list[tuple[str, str, str | None]] = []

    def list_tickets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows]

    def requeue_ticket(
        self, ticket_id: str, reason: str = "", *, evidence: str | None = None
    ) -> tuple[dict[str, str], bool]:
        self.requeued.append((ticket_id, reason, evidence))
        return {"id": ticket_id, "status": "planned"}, True


def _queue_row(ticket_id: str, status: str = "planned") -> dict[str, Any]:
    return {
        "id": ticket_id,
        "status": status,
        "tags": [],
        "dependencies": [],
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2020-01-01T00:00:00+00:00",
        "candidate_digest": f"candidate-{ticket_id}",
    }


def _stale_row(ticket_id: str, status: str = "implementing") -> dict[str, Any]:
    return {"id": ticket_id, "status": status, "updated_at": "2020-01-01T00:00:00+00:00"}


def _open_dispatcher(
    tmp_path: Path,
    demands: list[dict[str, Any]],
    store: Any,
    *,
    dry_run: bool = False,
    resume_only: bool = False,
):
    from core.line import fleet_dispatcher as fleet
    from core.line.admission_store import AdmissionStore

    now = datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp()
    launched: list[tuple[str, str]] = []

    def launch(tid: str, host: Any, **_kwargs: Any) -> Any:
        launched.append((tid, host.id))
        return fleet.InFlight(tid, host.id, _Poll())

    dispatcher = fleet.FleetDispatcher(
        fleet.DispatcherConfig(
            hosts=[fleet.HostSpec("grokbox", 1, "http://g"), fleet.HostSpec("notebook", 2, None)],
            state_path=tmp_path / "fleet_dispatcher_state.json",
            deploy_window_sec=100,
            resume_only=resume_only,
        ),
        demands_loader=lambda: demands,
        probe=lambda _host: fleet.HostProbe(True, "ready"),
        launch=launch,
        clock=lambda: now,
        admission_store=AdmissionStore(
            tmp_path / "admission.sqlite3",
            clock=lambda: datetime.fromtimestamp(now, timezone.utc),
        ),
        dry_run=dry_run,
        demands_store_factory=lambda: store,
    )
    return dispatcher, launched


def _saved_cycle(tmp_path: Path) -> dict[str, Any]:
    payload = json.loads((tmp_path / "fleet_dispatcher_state.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_run_cycle_reconciles_before_select_and_dispatches_same_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line import fleet_dispatcher as fleet

    order: list[str] = []
    demand = _queue_row("USR-830", "implementing")
    store = _Ledger([_stale_row("USR-830")])
    original_list = store.list_tickets
    original_requeue = store.requeue_ticket

    def list_tickets() -> list[dict[str, Any]]:
        order.append("reconcile")
        return original_list()

    def requeue_ticket(ticket_id: str, reason: str = "", *, evidence: str | None = None):
        order.append("requeue")
        return original_requeue(ticket_id, reason, evidence=evidence)

    store.list_tickets = list_tickets  # type: ignore[method-assign]
    store.requeue_ticket = requeue_ticket  # type: ignore[method-assign]

    def loader() -> list[dict[str, Any]]:
        order.append("load")
        return [demand]

    original_select = fleet.select_tickets

    def select_tickets(*args: Any, **kwargs: Any):
        order.append("select")
        return original_select(*args, **kwargs)

    monkeypatch.setattr(fleet, "select_tickets", select_tickets)
    monkeypatch.setattr("core.line.stale_active.collect_liveness", lambda _ticket_id, **_kwargs: False)
    dispatcher, launched = _open_dispatcher(tmp_path, [demand], store)
    dispatcher.demands_loader = loader

    cycle = dispatcher.run_cycle()

    assert order.index("reconcile") < order.index("requeue") < order.index("load") < order.index("select")
    assert launched == [("USR-830", "grokbox")]
    assert cycle["dispatched"] == [{"ticket": "USR-830", "host": "grokbox"}]
    assert demand["status"] == "planned"
    assert store.requeued[0][0] == "USR-830"
    assert store.requeued[0][1] == STALE_ACTIVE_CAUSE
    assert "previous_status=implementing" in (store.requeued[0][2] or "")
    action = cycle["reconciled"][0]
    assert action["ticket_id"] == "USR-830"
    assert action["action"] == "requeued"
    assert action["previous_status"] == "implementing"
    assert action["cause"] == STALE_ACTIVE_CAUSE
    assert action["idle_seconds"] > 24 * 3600
    saved = _saved_cycle(tmp_path)
    assert saved["reconciled"] == cycle["reconciled"]
    assert saved["reconciled"][0]["ticket_id"] == "USR-830"


def test_in_flight_and_live_run_are_not_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line import fleet_dispatcher as fleet

    probed: list[str] = []

    def collect(ticket_id: str, **_kwargs: Any) -> bool:
        probed.append(ticket_id)
        assert ticket_id != "USR-81"
        return ticket_id == "USR-LIVE"

    monkeypatch.setattr("core.line.stale_active.collect_liveness", collect)
    demands = [
        _queue_row("USR-81", "implementing"),
        _queue_row("USR-LIVE", "validating"),
        _queue_row("USR-DEAD", "implementing"),
    ]
    store = _Ledger([
        _stale_row("USR-81"),
        _stale_row("USR-LIVE", "validating"),
        _stale_row("USR-DEAD"),
    ])
    dispatcher, launched = _open_dispatcher(tmp_path, demands, store)
    dispatcher.in_flight["grokbox"] = fleet.InFlight("USR-81", "grokbox", _Poll())

    cycle = dispatcher.run_cycle()

    assert store.requeued == [("USR-DEAD", STALE_ACTIVE_CAUSE, store.requeued[0][2])]
    assert "USR-81" not in probed
    assert launched == [("USR-DEAD", "notebook")]
    assert {row["ticket_id"] for row in cycle["reconciled"]} == {"USR-DEAD"}
    assert demands[0]["status"] == "implementing"
    assert demands[1]["status"] == "validating"
    assert demands[2]["status"] == "planned"


def test_dry_run_lists_reconciliation_without_writing_or_dispatching_the_stale_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.line.stale_active.collect_liveness", lambda _ticket_id, **_kwargs: False)
    stale = _queue_row("USR-831", "validating")
    ready = _queue_row("USR-832", "planned")
    store = _Ledger([_stale_row("USR-831", "validating")])
    dispatcher, launched = _open_dispatcher(tmp_path, [stale, ready], store, dry_run=True)

    cycle = dispatcher.run_cycle()

    assert store.requeued == []
    assert launched == []
    assert stale["status"] == "validating"
    assert cycle["dispatched"] == [{"ticket": "USR-832", "host": "grokbox", "dry_run": "1"}]
    assert cycle["reconciled"][0]["action"] == "would_requeue"
    assert cycle["reconciled"][0]["ticket_id"] == "USR-831"
    assert cycle["reconciled"][0]["previous_status"] == "validating"
    assert _saved_cycle(tmp_path)["reconciled"] == cycle["reconciled"]


def test_resume_only_does_not_reconcile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    def explode(*_args: Any, **_kwargs: Any) -> list[Any]:
        called.append("reconcile")
        raise AssertionError("resume_only nao reconcilia")

    monkeypatch.setattr("core.line.stale_active.reconcile_stale_active", explode)
    demand = _queue_row("USR-833", "implementing")
    store = _Ledger([_stale_row("USR-833")])
    dispatcher, launched = _open_dispatcher(tmp_path, [demand], store, resume_only=True)

    cycle = dispatcher.run_cycle()

    assert called == []
    assert store.requeued == []
    assert launched == []
    assert demand["status"] == "implementing"
    assert cycle["mode"] == "resume_only"
    assert cycle["reconciled"] == []
    assert _saved_cycle(tmp_path)["reconciled"] == []


def test_reconciliation_failure_is_recorded_and_cycle_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr("core.line.stale_active.reconcile_stale_active", explode)
    ready = _queue_row("USR-834", "planned")
    dispatcher, launched = _open_dispatcher(tmp_path, [ready], _Ledger([]))
    caplog.set_level(logging.ERROR, logger="darkfac.line.fleet_dispatcher")

    cycle = dispatcher.run_cycle()

    assert launched == [("USR-834", "grokbox")]
    assert cycle["dispatched"] == [{"ticket": "USR-834", "host": "grokbox"}]
    assert cycle["reconciled"] == [{
        "ticket_id": "",
        "previous_status": "",
        "cause": "stale_active_reconcile_failed",
        "idle_seconds": 0,
        "action": "error",
        "detail": "RuntimeError: ledger exploded",
    }]
    assert _saved_cycle(tmp_path)["reconciled"] == cycle["reconciled"]
    assert any("reconciliacao de status ativo sem processo falhou" in record.getMessage() for record in caplog.records)


def test_daemon_factory_is_what_main_uses_to_open_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.line import fleet_dispatcher as fleet

    opened: list[str] = []

    class _Marker:
        def list_tickets(self) -> list[Any]:
            opened.append("list")
            return []

    monkeypatch.setattr("core.demands.store.DemandsStore", lambda *args, **kwargs: _Marker())
    store = fleet.daemon_demands_store()
    assert store.list_tickets() == []
    assert opened == ["list"]

    captured: dict[str, Any] = {}

    def fake_init(self: Any, *_args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        raise RuntimeError("stop-before-cycle")

    monkeypatch.setattr(fleet.FleetDispatcher, "__init__", fake_init)
    monkeypatch.setattr(fleet, "configure_logging", lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="stop-before-cycle"):
        fleet.main(["--once", "--dry-run"])
    assert captured["demands_store_factory"] is fleet.daemon_demands_store
