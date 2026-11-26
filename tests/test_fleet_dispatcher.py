from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line import failure_alerts as failure_alerts_mod
from core.line import fleet_dispatcher as fd
from core.line.admission_store import AdmissionStore

_REAL_DETERMINISTIC_ALERT = failure_alerts_mod.notify_deterministic_failure

HOSTS = [fd.HostSpec("notebook", 1, None), fd.HostSpec("grokbox", 2, "http://g"), fd.HostSpec("desktop", 3, "http://d")]


def _d(i, status="planned", tags=(), deps=()):
    return {"id": i, "status": status, "tags": list(tags), "dependencies": list(deps),
            "created_at": f"2026-01-{int(i[-2:]):02d}", "candidate_digest": f"candidate-{i}"}


class Handle:
    def __init__(self):
        self.rc = None

    def poll(self):
        return self.rc


class Env:
    def __init__(self, tmp, demands, free=("grokbox", "notebook", "desktop")):
        self.now = datetime.now(timezone.utc).timestamp()
        self.tmp = tmp
        self.launched: list[tuple[str, str]] = []
        self.handles: dict[str, Handle] = {}
        self.deploys = 0
        self.free = set(free)
        cfg = fd.DispatcherConfig(hosts=HOSTS, state_path=tmp / "state.json", deploy_window_sec=100)
        self.d = fd.FleetDispatcher(cfg, demands_loader=lambda: demands,
                                    probe=lambda h: fd.HostProbe(h.id in self.free, "x"),
                                    launch=self._launch, deploy=self._deploy, clock=lambda: self.now,
                                    admission_store=AdmissionStore(tmp / "admission.sqlite3", clock=lambda: datetime.fromtimestamp(self.now, timezone.utc)))

    def _launch(self, tid, host, *, run_id=None, recovery=False, candidate_digest="unknown"):
        self.launched.append((tid, host.id))
        h = self.handles[tid] = Handle()
        return fd.InFlight(tid, host.id, h, run_id=run_id)

    def mark_delivered(self, ticket_id):
        path = self.tmp / f"{ticket_id}.log"
        host_id = next(host for host, job in self.d.in_flight.items() if job.ticket_id == ticket_id)
        job = self.d.in_flight[host_id]
        stamp = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        stages = {
            "gate": {"passed": True, "marker": "[HARNESS_PASS]"},
            "review": {"approved": True, "independent": True, "reviewer_family": "claude", "implementer_family": "codex"},
            "ci": {"green": True},
            "merge": {"sha": "a" * 40, "reachable": True},
            "deploy": {"deployed": True, "sha": "a" * 40},
            "smoke": {"http_status": 200},
            "learning_pack": {"indexed": True},
        }
        for stage, evidence in stages.items():
            evidence.update(candidate_digest=job.candidate_digest, observed_at=stamp, evidence_ref=f"{stage}://{ticket_id}")
        receipt = {"schema_version": 1, "simulated": False, "candidate_digest": job.candidate_digest,
                   "run_id": job.run_id, "implementer_family": "codex", **stages}
        import json
        path.write_text("[DELIVERY_MERGED]\n[RECOVERY_RECEIPT] " + json.dumps(receipt), encoding="utf-8")
        job.log_path = path

    def _deploy(self):
        self.deploys += 1
        return 0


def _main_thread_sleep(record):
    """``fd.time`` is the real ``time`` module, so the patch replaces ``sleep`` for the whole process.

    On POSIX ``subprocess`` polls a child with short sleeps on this same thread, and other threads
    sleep too. Only the call made by the dispatcher loop itself is the event under test.
    """
    import sys

    def fake_sleep(seconds):
        if sys._getframe(1).f_code.co_name == "run_forever":
            record(seconds)

    return fake_sleep


def test_selection_filters():
    dem = [_d("USR-01"), _d("USR-02", tags=["congelado-v0"]), _d("USR-03", tags=["waiting_human"]),
           _d("USR-04", deps=["USR-09"]), _d("USR-05", deps=["USR-06"]), _d("USR-06", "completed"), _d("USR-07")]
    q, sk = fd.select_tickets(dem, busy_ids={"USR-07"})
    assert q == ["USR-01", "USR-05"]
    assert sk["USR-02"].startswith("tag:") and sk["USR-04"] == "dependencia_aberta" and sk["USR-07"] == "em_voo"


def test_selection_orders_eligible_tickets_by_priority_then_stable_creation_and_id():
    demands = [
        {**_d("USR-04"), "priority": 2, "created_at": "2026-01-01"},
        {**_d("USR-03"), "priority": 2, "created_at": "2026-01-01"},
        {**_d("USR-02"), "priority": 5, "created_at": "2026-01-03"},
        {**_d("USR-01"), "priority": 0, "created_at": "2026-01-01"},
        {**_d("USR-05", deps=["USR-99"]), "priority": 99},
        {**_d("USR-06"), "priority": 100},
    ]

    queue, skipped = fd.select_tickets(demands, busy_ids={"USR-06"})

    assert queue == ["USR-02", "USR-03", "USR-04", "USR-01"]
    assert skipped == {"USR-05": "dependencia_aberta", "USR-06": "em_voo"}


def test_active_canonical_reservation_blocks_dispatch_until_lease_expires(tmp_path):
    from core.workflow.control_store import SQLiteControlStore

    now = datetime.now(timezone.utc)
    now_epoch = [now.timestamp()]
    control_store = SQLiteControlStore(db_path=tmp_path / "control.db")
    external = control_store.reserve_cycle(
        project_id="darkfac",
        ticket_id="USR-449",
        run_id="external-run-449",
        host_id="grokbox",
        now=now,
        lease_duration_sec=2,
    )
    launched = []

    def launch(ticket_id, host, **kwargs):
        launched.append((ticket_id, host.id))
        return fd.InFlight(ticket_id, host.id, Handle(), run_id=kwargs.get("run_id"))

    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(
            hosts=[fd.HostSpec("grokbox", 1, "http://g")],
            state_path=tmp_path / "fleet.json",
        ),
        demands_loader=lambda: [_d("USR-453")],
        probe=lambda host: fd.HostProbe(True, "ready"),
        launch=launch,
        clock=lambda: now_epoch[0],
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        control_store_factory=lambda: control_store,
    )

    occupied = dispatcher.run_cycle()
    assert launched == []
    assert occupied["free_hosts"] == []
    assert occupied["idle_reasons"]["grokbox"] == "host_reserved:USR-449"
    assert occupied["canonical_reservations"]["grokbox"]["run_id"] == "external-run-449"
    assert control_store.get_reservation(external.reservation_id).status == "active"

    now_epoch[0] = (now + timedelta(seconds=3)).timestamp()
    expired = dispatcher.run_cycle()
    assert launched == [("USR-453", "grokbox")]
    assert control_store.get_reservation(external.reservation_id).status == "expired"
    assert expired["dispatched"] == [{"ticket": "USR-453", "host": "grokbox"}]


def test_reservation_created_by_probe_is_revalidated_before_launch_and_not_renewed(tmp_path):
    from core.workflow.control_store import SQLiteControlStore

    now = datetime.now(timezone.utc)
    control_store = SQLiteControlStore(db_path=tmp_path / "control.db")
    external_reservation = None
    launched = []

    def probe(host):
        nonlocal external_reservation
        if external_reservation is None:
            external_reservation = control_store.reserve_cycle(
                project_id="darkfac",
                ticket_id="USR-449",
                run_id="external-run-449",
                host_id=host.id,
                now=now,
                lease_duration_sec=300,
            )
        return fd.HostProbe(True, "ready")

    def launch(ticket_id, host, **kwargs):
        launched.append((ticket_id, host.id))
        return fd.InFlight(ticket_id, host.id, Handle(), run_id=kwargs.get("run_id"))

    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(
            hosts=[fd.HostSpec("grokbox", 1, "http://g")],
            state_path=tmp_path / "fleet.json",
        ),
        demands_loader=lambda: [_d("USR-453")],
        probe=probe,
        launch=launch,
        clock=lambda: now.timestamp(),
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        control_store_factory=lambda: control_store,
    )

    cycle = dispatcher.run_cycle()

    persisted = control_store.get_reservation(external_reservation.reservation_id)
    assert launched == []
    assert cycle["dispatched"] == []
    assert cycle["free_hosts"] == []
    assert cycle["idle_reasons"]["grokbox"] == "host_reserved:USR-449"
    assert persisted.status == external_reservation.status == "active"
    assert persisted.expires_at == external_reservation.expires_at


@pytest.mark.parametrize("failure_point", ["factory", "list_active_reservations"])
def test_canonical_reservation_read_failure_fails_closed(tmp_path, failure_point):
    launched = []
    probed = []

    class BrokenControlStore:
        def reconcile_fleet_reservations(self, now):
            return None

        def list_active_reservations(self):
            raise OSError("canonical store unavailable")

    def control_store_factory():
        if failure_point == "factory":
            raise OSError("canonical store unavailable")
        return BrokenControlStore()

    def probe(host):
        probed.append(host.id)
        return fd.HostProbe(True, "ready")

    def launch(ticket_id, host, **kwargs):
        launched.append((ticket_id, host.id))
        return fd.InFlight(ticket_id, host.id, Handle(), run_id=kwargs.get("run_id"))

    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(
            hosts=[fd.HostSpec("grokbox", 1, "http://g")],
            state_path=tmp_path / "fleet.json",
        ),
        demands_loader=lambda: [_d("USR-453")],
        probe=probe,
        launch=launch,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        control_store_factory=control_store_factory,
    )

    cycle = dispatcher.run_cycle()

    assert probed == []
    assert launched == []
    assert cycle["dispatched"] == []
    assert cycle["free_hosts"] == []
    assert cycle["idle_reasons"]["grokbox"] == "canonical_reservations_unavailable"
    assert cycle["reservation_error"] == "OSError"


def test_canonical_reservation_failure_during_prelaunch_revalidation_fails_closed(tmp_path):
    launched = []
    list_calls = 0

    class FailingSecondReadStore:
        def reconcile_fleet_reservations(self, now):
            return None

        def list_active_reservations(self):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 2:
                raise OSError("canonical store unavailable")
            return []

    def launch(ticket_id, host, **kwargs):
        launched.append((ticket_id, host.id))
        return fd.InFlight(ticket_id, host.id, Handle(), run_id=kwargs.get("run_id"))

    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(
            hosts=[fd.HostSpec("grokbox", 1, "http://g")],
            state_path=tmp_path / "fleet.json",
        ),
        demands_loader=lambda: [_d("USR-453")],
        probe=lambda host: fd.HostProbe(True, "ready"),
        launch=launch,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        control_store_factory=FailingSecondReadStore,
    )

    cycle = dispatcher.run_cycle()

    assert list_calls == 2
    assert launched == []
    assert cycle["dispatched"] == []
    assert cycle["free_hosts"] == []
    assert cycle["idle_reasons"]["grokbox"] == "canonical_reservations_unavailable"
    assert cycle["reservation_error"] == "OSError"


def test_infra_backoff_survives_dispatcher_restart_then_retries(tmp_path, monkeypatch):
    e = Env(tmp_path, [_d("USR-01")], free=("notebook",))
    for index in range(5):
        e.d.run_cycle()
        e.handles["USR-01"].rc = 75
        cycle = e.d.run_cycle()
        assert cycle["skipped"]["USR-01"] == "backoff_infra"
        budget = e.d.admission_store.budget("darkfac", "USR-01")
        assert budget["candidate_failures"] == 0 and not e.d.admission_store.ticket_holds()
        e.d = fd.FleetDispatcher(e.d.config, demands_loader=lambda: [_d("USR-01")],
                                 probe=lambda h: fd.HostProbe(h.id in e.free, "ready"), launch=e._launch,
                                 clock=lambda: e.now, admission_store=AdmissionStore(
                                     tmp_path / "admission.sqlite3",
                                     clock=lambda: datetime.fromtimestamp(e.now, timezone.utc)))
        assert not e.d.run_cycle()["dispatched"]
        e.now = budget["next_attempt_at"] + 1
    assert e.d.run_cycle()["dispatched"] == [{"ticket": "USR-01", "host": "notebook"}]


def test_structured_outcome_beats_banner_and_other_run_is_ignored(tmp_path, monkeypatch):
    import json
    from core.line import run_outcome

    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    job = e.d.in_flight["grokbox"]
    log = tmp_path / "run.log"
    log.write_text("[ERRO NA ENTREGA] causa: gate_failed; erro: stale\nRESUMO DAS COTAS", encoding="utf-8")
    job.log_path = log
    path = run_outcome.outcome_path(job.ticket_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(ticket=job.ticket_id, run_id=job.run_id, exit_code=75, cause="gate_timeout",
                   cause_class="infra", stage="gate", detail="worker timed out", ts="now")
    path.write_text(json.dumps(payload), encoding="utf-8")
    e.handles["USR-01"].rc = 1
    e.d.run_cycle()
    assert e.d.failures[-1]["cause"] == "gate_timeout"
    assert e.d.failures[-1]["reason"] == "worker timed out"
    assert e.d.admission_store.budget("darkfac", "USR-01")["candidate_failures"] == 0
    job.run_id = "another-run"
    e.d._record_failure(job, 1, "failure", log.read_text(encoding="utf-8"))
    assert e.d.admission_store.budget("darkfac", "USR-01")["candidate_failures"] == 1
    banner = "Telemetria de Cotas em Tempo Real\nQuota summary: headroom 80%"
    assert run_outcome.error_detail(banner, "diagnostico indisponivel") == "diagnostico indisponivel"
    assert run_outcome.diagnostic_cause(banner, 1) == "unknown"


def test_suite_lock_progress_does_not_replace_pytest_failure_or_lock_timeout(tmp_path, monkeypatch, caplog):
    import json
    from core.line import run_outcome

    failure = "FAILED tests/test_gate.py::test_contract - AssertionError: expected value"
    output = ("[SUITE_LOCK] acquired slot 0 after waiting 450.8s\n"
              "[TEST_PHASE] collection files=23 timeout_sec=120\n" + failure)
    assert run_outcome.error_detail(output) == failure
    assert run_outcome.error_detail(
        "[SUITE_LOCK] acquired slot 0 after waiting 450.8s\n"
        "[TEST_PHASE] collection files=23 timeout_sec=350\n"
        "[STEP_TIMEOUT] affected tests phase=collection",
        "diagnostico indisponivel",
    ) == "[STEP_TIMEOUT] affected tests phase=collection"
    assert run_outcome.error_detail("[SUITE_LOCK] timed out after 120s") == "[SUITE_LOCK] timed out after 120s"
    assert run_outcome.error_detail("[SUITE_LOCK] waiting for slot; timed out after 120s") == \
        "[SUITE_LOCK] waiting for slot; timed out after 120s"
    assert run_outcome.actionable_failure("Quota summary: timeout exceeded") == ""
    assert run_outcome.actionable_failure("timeout: 120.0s") == ""
    timeout_log = (
        "[SUITE_LOCK] acquired slot 0 after waiting 450.8s\n"
        "[TEST_PHASE] collection files=23 timeout_sec=350\n"
        "[STEP_TIMEOUT] affected tests phase=collection"
    )
    assert run_outcome.actionable_failure(timeout_log) == "[STEP_TIMEOUT] affected tests phase=collection"

    notified = []
    monkeypatch.setattr("core.line.failure_alerts.notify_ticket_hold", lambda *args: notified.append(args))
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    for attempt in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest=f"patch-{attempt}",
                          context_digest="ctx", run_id=f"run-{attempt}")
        outcome_path = run_outcome.outcome_path(job.ticket_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({
            "ticket": job.ticket_id, "run_id": job.run_id, "exit_code": 1,
            "cause": "gate_failed", "cause_class": "candidate", "stage": "gate",
            "detail": "[SUITE_LOCK] acquired slot 0 after waiting 450.8s", "ts": "now",
        }), encoding="utf-8")
        tail = ("[ERRO NA ENTREGA] causa: gate_failed; erro: [SUITE_LOCK] acquired slot 0\n" + output)
        job.log_path = tmp_path / f"{attempt}.log"
        job.log_path.write_text(tail, encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="core.line.fleet_dispatcher"):
            e.d._record_failure(job, 1, "failure", tail)

    assert e.d.failures[-1]["reason"] == failure
    assert notified and notified[-1][2] == failure
    assert all(f"motivo={failure}" in record.message for record in caplog.records[-3:])
    assert e.d.admission_store.ticket_hold(project_id="darkfac", ticket_id="USR-01")["cause"] == "gate_failed"


def test_collection_timeout_log_overrides_stale_outcome_detail(tmp_path, monkeypatch):
    import json
    from core.line import run_outcome

    notified = []
    monkeypatch.setattr("core.line.failure_alerts.notify_ticket_hold", lambda *args: notified.append(args))
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    tail = (
        "[SUITE_LOCK] acquired slot 0 after waiting 450.8s\n"
        "[TEST_PHASE] collection files=23 timeout_sec=350\n"
        "[STEP_TIMEOUT] affected tests phase=collection"
    )
    expected = "[STEP_TIMEOUT] affected tests phase=collection"
    for attempt in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest=f"candidate-{attempt}",
                          context_digest="ctx", run_id=f"collection-timeout-{attempt}")
        outcome_path = run_outcome.outcome_path(job.ticket_id)
        outcome_path.parent.mkdir(parents=True, exist_ok=True)
        outcome_path.write_text(json.dumps({
            "ticket": job.ticket_id, "run_id": job.run_id, "exit_code": 1,
            "cause": "gate_failed", "cause_class": "candidate", "stage": "gate",
            "detail": "old informative outcome detail", "ts": "now",
        }), encoding="utf-8")
        e.d._record_failure(job, 1, "failure", tail)
        assert e.d.failures[-1]["reason"] == expected
    assert e.d.admission_store.ticket_hold(project_id="darkfac", ticket_id="USR-01")["cause"] == "gate_failed"
    assert notified and notified[-1][2] == expected


def test_host_permission_failure_before_launch_is_infra(tmp_path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    def denied(*args, **kwargs):
        raise PermissionError("host ACL refused")
    e.d.launch = denied
    e.d.run_cycle()
    budget = e.d.admission_store.budget("darkfac", "USR-01")
    assert budget["candidate_failures"] == 0 and budget["infra_streak"] == 1
    assert e.d.failures[-1]["cause"] == "host_permission"


def test_candidate_failures_across_hosts_and_fingerprints_hold_and_notify_once(tmp_path, monkeypatch):
    from core.line import failure_alerts
    alerts = []
    monkeypatch.setattr(failure_alerts, "notify_ticket_hold", lambda *args: alerts.append(args))
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))
    for index in range(4):
        job = fd.InFlight("USR-01", f"host-{index}", Handle(), candidate_digest=str(index),
                          resource_version=str(index), run_id=f"run-{index}")
        e.d._record_failure(job, 1, "failure", "[ERRO NA ENTREGA] causa: gate_failed; erro: assertion failed")
    assert len(alerts) == 1 and "assertion failed" in alerts[0][2]
    cycle = e.d.run_cycle()
    assert cycle["skipped"]["USR-01"] == "ticket_hold:gate_failed"
    assert e.launched == [("USR-02", "grokbox")]


@pytest.mark.parametrize("rc", [0, 1, 75])
def test_pending_child_uses_durable_budget_and_zero_exit_waits(tmp_path, monkeypatch, rc):
    from core.line import pending_delivery as pd, failure_alerts
    from core.paths import state_root
    from types import SimpleNamespace

    alerts = []
    monkeypatch.setattr(failure_alerts, "notify_ticket_hold", lambda *args: alerts.append(args))
    monkeypatch.setattr(pd.run_outcome, "main_sha", lambda _repo: "a" * 40)
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    path = tmp_path / "pending" / "USR-99.json"
    now = 10000.0
    for index in range(3):
        row = pd.PendingDelivery(ticket_id="USR-99", worktree=str(tmp_path), stage="development",
                                 status="running", pid=991, run_id=f"pending-{index}")
        pd._save(path, row)
        monkeypatch.setattr(pd, "_children", {991: SimpleNamespace(poll=lambda: rc)})
        result = pd._tick_row(path, tmp_path, now)
        budget = AdmissionStore(state_root() / "admission.sqlite3").budget("darkfac", "USR-99")
        assert budget["candidate_failures"] == (index + 1 if rc == 1 else 0)
        assert result["status"] == ("blocked" if rc == 1 and index == 2 else "waiting")
        if rc == 75:
            assert result["next_check"] == now + 600 * 2 ** index
        else:
            assert result["next_check"] == now + pd.RECHECK_SECONDS
        if rc == 0:
            # No relaunch in the same tick or in the immediately following tick.
            assert pd._tick_row(path, tmp_path, now + 1)["status"] == "waiting"
        now = result["next_check"] + 1
    assert len(alerts) == int(rc == 1)


def test_new_main_releases_pending_infra_delay_without_erasing_candidate_debt(tmp_path, monkeypatch):
    from core.line import pending_delivery as pd
    from core.paths import state_root

    store = pd._budget_store(10000)
    store.record_attempt("darkfac", "USR-99", "gate_failed", run_id="candidate", main_sha="a")
    budget = store.record_attempt("darkfac", "USR-99", "gate_timeout", run_id="infra", main_sha="a")
    row = pd.PendingDelivery(ticket_id="USR-99", worktree=str(tmp_path), stage="development",
                             next_check=budget["next_attempt_at"])
    path = tmp_path / "USR-99.json"
    pd._save(path, row)
    monkeypatch.setattr(pd.run_outcome, "main_sha", lambda _repo: "b")
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: True)
    assert pd._tick_row(path, tmp_path, 10001)["next_check"] == 0
    budget = AdmissionStore(state_root() / "admission.sqlite3").budget("darkfac", "USR-99")
    assert budget["candidate_failures"] == 1 and budget["infra_streak"] == 0


def test_policy_config_is_reloaded_each_cycle_and_exclusion_is_published(tmp_path, monkeypatch):
    import json
    from core.line import fleet_policy

    path = tmp_path / "line_fleet.json"
    config = {
        "max_fleet_slots": 3,
        "disabled_hosts": [],
        "hosts": [
            {"id": "desktop", "name": "Desktop", "priority": 1, "endpoint": "http://worker", "enabled": True,
             "max_workers": 1, "min_free_memory_gib": 0},
        ],
    }
    path.write_text(json.dumps(config), encoding="utf-8")
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("STATE_ROOT", str(runtime_root))
    cfg = fd.DispatcherConfig(hosts=[], policy_path=path, state_path=tmp_path / "published.json")
    dispatcher = fd.FleetDispatcher(
        cfg, demands_loader=lambda: [], probe=lambda _host: pytest.fail("excluded/idle policy must not probe"),
        deploy=lambda: 0, admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
    )

    dispatcher.run_cycle()
    snapshot_path = fleet_policy.snapshot_path()
    first_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert first_snapshot["origin"]["configuration"] == str(path)
    assert first_snapshot["origin"]["resource_readings"] == "dispatcher_probe_results"
    assert first_snapshot["updated_at"] == dispatcher.last_cycle["policy_snapshot"]["updated_at"]
    assert dispatcher.last_cycle["host_policy"][0]["config"]["endpoint"] == "http://worker"
    config["disabled_hosts"] = ["desktop"]
    path.write_text(json.dumps(config), encoding="utf-8")
    dispatcher.run_cycle()

    row = dispatcher.last_cycle["host_policy"][0]
    persisted_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert row["eligible"] is False
    assert row["reason"] == "excluded"
    assert row["exclusion_sources"] == ["config.disabled_hosts"]
    assert persisted_snapshot["hosts"][0]["reason"] == "excluded"
    assert persisted_snapshot["origin"]["exclusions"]["config"] == ["desktop"]
    assert "desktop" not in dispatcher.last_cycle["free_hosts"]


def test_dispatcher_does_not_probe_or_launch_a_persistently_excluded_host(tmp_path, monkeypatch):
    import json

    from core.line import fleet_policy

    path = tmp_path / "line_fleet.json"
    path.write_text(json.dumps({
        "max_fleet_slots": 3,
        "disabled_hosts": ["desktop"],
        "hosts": [{
            "id": "desktop", "name": "Desktop", "priority": 1,
            "endpoint": "http://desktop", "enabled": True,
            "max_workers": 1, "min_free_memory_gib": 0,
        }],
    }), encoding="utf-8")
    monkeypatch.setenv("STATE_ROOT", str(tmp_path / "state"))
    launches: list[str] = []
    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(
            hosts=[], policy_path=path, state_path=tmp_path / "dispatcher.json",
        ),
        demands_loader=lambda: [_d("USR-91")],
        probe=lambda _host: pytest.fail("an excluded host must not be probed"),
        launch=lambda ticket, host, **_kwargs: launches.append(ticket),
        deploy=lambda: 0,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
    )

    cycle = dispatcher.run_cycle()
    snapshot = json.loads(fleet_policy.snapshot_path().read_text(encoding="utf-8"))

    assert launches == []
    assert cycle["idle_reasons"]["desktop"] == "excluded_by_config"
    assert cycle["host_policy"][0]["reason"] == "excluded"
    assert snapshot["hosts"][0]["reason"] == "excluded"


def test_canonical_host_reservation_survives_restart_until_real_expiry(tmp_path):
    from datetime import timedelta

    now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    store = AdmissionStore(tmp_path / "admission.sqlite3", clock=lambda: now, reservation_ttl_sec=30)
    token = store.reserve(
        project_id="darkfac", ticket_id="USR-471", candidate_digest="candidate",
        context_digest="context", resource_version="resources", host_id="desktop",
    )
    assert token
    assert store.active_host_reservations() == [{"host_id": "desktop", "expires_at": (now + timedelta(seconds=30)).isoformat()}]
    restarted_store = AdmissionStore(tmp_path / "admission.sqlite3", clock=lambda: now + timedelta(seconds=31))
    assert restarted_store.active_host_reservations() == []


@pytest.mark.parametrize("loop_args", [[], ["--loop"]])
def test_main_rejects_split_recovery_ticket_arguments_before_running(loop_args, monkeypatch):
    calls = []

    class Dispatcher:
        def __init__(self, *_args, **_kwargs):
            calls.append("init")

        def run_cycle(self):
            calls.append("run_cycle")

        def run_forever(self):
            calls.append("run_forever")

    monkeypatch.setattr(fd, "FleetDispatcher", Dispatcher)
    monkeypatch.setattr(fd, "configure_logging", lambda *_args, **_kwargs: None)

    with pytest.raises(SystemExit) as exc:
        fd.main(["--recovery-tickets", "USR-1", "USR-2", *loop_args])

    assert exc.value.code == 2
    assert calls == []


def test_main_rejects_repeated_recovery_option_before_running(monkeypatch):
    calls = []

    class Dispatcher:
        def __init__(self, *_args, **_kwargs):
            calls.append("init")

    monkeypatch.setattr(fd, "FleetDispatcher", Dispatcher)
    monkeypatch.setattr(fd, "configure_logging", lambda *_args, **_kwargs: calls.append("logging"))

    with pytest.raises(SystemExit) as exc:
        fd.main(["--recovery-tickets", "USR-1", "--recovery-tickets", "USR-2"])

    assert exc.value.code == 2
    assert calls == []


def test_main_passes_comma_separated_recovery_tickets_in_order(monkeypatch, capsys):
    captured = {}

    class Dispatcher:
        def __init__(self, config, **_kwargs):
            captured["config"] = config

        def run_cycle(self):
            return {"ok": True}

    monkeypatch.setattr(fd, "FleetDispatcher", Dispatcher)
    monkeypatch.setattr(fd, "configure_logging", lambda *_args, **_kwargs: None)

    assert fd.main(["--recovery-tickets", " USR-1, , USR-2 ,, "]) == 0
    assert captured["config"].recovery_tickets == ("USR-1", "USR-2")
    assert capsys.readouterr().out.strip() == '{\n  "ok": true\n}'


def test_main_help_documents_csv_format_without_operational_side_effects(monkeypatch, capsys):
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("operational")
        raise AssertionError("help must not initialize operational services")

    monkeypatch.setattr(fd, "FleetDispatcher", forbidden)
    monkeypatch.setattr(fd, "configure_logging", forbidden)

    with pytest.raises(SystemExit) as exc:
        fd.main(["--help"])

    assert exc.value.code == 0
    assert "ID1,ID2" in capsys.readouterr().out
    assert calls == []


def test_main_preserves_empty_recovery_and_resume_only_modes(monkeypatch, capsys):
    captured = {}

    class Dispatcher:
        def __init__(self, config, **_kwargs):
            captured["config"] = config

        def run_cycle(self):
            return {"ok": True}

    monkeypatch.setattr(fd, "FleetDispatcher", Dispatcher)
    monkeypatch.setattr(fd, "configure_logging", lambda *_args, **_kwargs: None)

    assert fd.main(["--resume-only", "--recovery-tickets", ""]) == 0
    assert captured["config"].recovery_tickets == ()
    assert captured["config"].resume_only is True
    capsys.readouterr()


def test_three_tickets_three_hosts_in_parallel(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02"), _d("USR-03"), _d("USR-04")])
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "grokbox"), ("USR-03", "desktop")]
    assert cyc["queue_remaining"] == ["USR-04"]
    assert (tmp_path / "state.json").exists()


def test_host_order_skips_unavailable_and_reports_idle(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("desktop",))
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-01", "desktop")]
    assert cyc["idle_reasons"]["grokbox"].startswith("host_indisponivel")


def _silence_spies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _spy(name: str):
        def _record(*_args, **_kwargs):
            calls.append(name)
            return False
        return _record

    monkeypatch.setattr("core.notifications.service.NotificationService.notify", _spy("notify"))
    monkeypatch.setattr("core.owner_actions.notify.notify_owner_action", _spy("owner"))
    monkeypatch.setattr("core.line.human.notify_human_request", _spy("human"))
    monkeypatch.setattr(
        "core.line.express_lane.notify_repeated_self_modification", _spy("express_notify"),
    )
    return calls


def test_free_notebook_launches_before_grokbox_and_desktop(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02"), _d("USR-03")])
    e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "grokbox"), ("USR-03", "desktop")]


def test_grokbox_without_health_is_silent_and_does_not_block_the_cycle(tmp_path: Path, monkeypatch):
    calls = _silence_spies(monkeypatch)
    alerts: list[object] = []
    probes: list[str] = []
    demands = [_d("USR-01"), _d("USR-02"), _d("USR-03")]
    e = Env(tmp_path, demands)

    def probe(host):
        probes.append(host.id)
        if host.id == "grokbox":
            return fd.HostProbe(False, "health_indisponivel", f"{host.id}:health_unavailable")
        return fd.HostProbe(True, "ok", f"{host.id}:ok")

    e.d.probe = probe
    e.d.express_alert = lambda alert: alerts.append(alert) or True
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "desktop")]
    assert cycle["queue_remaining"] == ["USR-03"]
    assert probes.count("grokbox") == 1
    assert calls == [] and alerts == []
    for demand in demands:
        assert demand["status"] == "planned"

    # Notebook e Desktop ocupados: o ciclo segue e sonda o Grokbox de novo.
    e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "desktop")]
    assert probes.count("grokbox") == 2
    assert calls == [] and alerts == []

    # Notebook cheio e Grokbox ainda sem health: o Desktop livre recebe o ticket.
    e.handles["USR-02"].rc = 75
    log = tmp_path / "USR-02.log"
    log.write_text("fim sem merge\n", encoding="utf-8")
    e.d.in_flight["desktop"].log_path = log
    finished = e.d.run_cycle()
    assert e.launched[-1] == ("USR-03", "desktop")
    assert finished["in_flight"]["notebook"] == "USR-01"
    assert probes.count("grokbox") == 3
    assert calls == [] and alerts == []


def test_default_probe_grokbox_without_health_does_not_notify(monkeypatch):
    calls = _silence_spies(monkeypatch)
    monkeypatch.setattr("core.harness.remote_dispatch.probe_health", lambda _url, timeout=None: None)
    probe = fd.default_probe(fd.HostSpec("grokbox", 2, "http://g"), stable_probes=3)
    assert probe == fd.HostProbe(False, "health_indisponivel", "grokbox:health_unavailable")
    assert calls == []


def _lost_grokbox(env: Env, ticket_id: str, rc: int, tail: str) -> None:
    job = env.d.in_flight["grokbox"]
    if tail:
        log = env.tmp / f"{ticket_id}-lost.log"
        log.write_text(tail, encoding="utf-8")
        job.log_path = log
    env.handles[ticket_id].rc = rc


def _reservation_tokens(path: Path) -> list[str]:
    import sqlite3

    with sqlite3.connect(path) as con:
        return [row[0] for row in con.execute("SELECT token FROM reservations")]


@pytest.mark.parametrize(("rc", "tail"), [(75, ""), (2, "[REMOTE_JOB_LOST] worker sumiu\n")])
def test_grokbox_infra_loss_relaunches_on_the_next_free_host(tmp_path: Path, monkeypatch, rc, tail):
    calls = _silence_spies(monkeypatch)
    alerts: list[object] = []
    demands = [_d("USR-01")]
    e = Env(tmp_path, demands, free=("grokbox",))
    e.d.express_alert = lambda alert: alerts.append(alert) or True
    e.d.run_cycle()
    assert e.launched == [("USR-01", "grokbox")]
    lost = e.d.in_flight["grokbox"].admission_token
    assert lost
    _lost_grokbox(e, "USR-01", rc, tail)
    e.free = {"notebook", "grokbox", "desktop"}
    cycle = e.d.run_cycle()
    assert demands[0]["status"] == "planned"
    assert "USR-01" not in cycle["cooldown"]
    assert e.launched == [("USR-01", "grokbox"), ("USR-01", "notebook")]
    assert lost not in _reservation_tokens(tmp_path / "admission.sqlite3")
    assert e.d.admission_store.ticket_holds() == []
    assert calls == [] and alerts == []


def test_grokbox_loss_with_notebook_full_uses_desktop(tmp_path: Path):
    demands = [_d("USR-01"), _d("USR-02")]
    e = Env(tmp_path, demands, free=("notebook", "grokbox"))
    e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "grokbox")]
    _lost_grokbox(e, "USR-02", 75, "")
    e.free = {"notebook", "grokbox", "desktop"}
    cycle = e.d.run_cycle()
    assert demands[1]["status"] == "planned"
    assert "USR-02" not in cycle["cooldown"]
    assert e.launched[-1] == ("USR-02", "desktop")
    assert cycle["in_flight"]["notebook"] == "USR-01"
    assert cycle["in_flight"]["desktop"] == "USR-02"


def test_grokbox_route_wait_keeps_the_global_cooldown(tmp_path: Path):
    demands = [_d("USR-01")]
    e = Env(tmp_path, demands, free=("grokbox",))
    e.d.run_cycle()
    _lost_grokbox(e, "USR-01", 75, "harness_infra_error: route_wait\n")
    e.free = {"notebook", "grokbox", "desktop"}
    cycle = e.d.run_cycle()
    assert cycle["cooldown"] == ["USR-01"]
    assert e.launched == [("USR-01", "grokbox")]


@pytest.mark.parametrize("host_id", ["notebook", "desktop"])
def test_is_opportunistic_host_loss_is_grokbox_specific(host_id: str) -> None:
    """USR-492 T2: a mesma queda (exit 75 ou [REMOTE_JOB_LOST]) so e tratamento
    silencioso no Grokbox; Notebook e Desktop continuam como infra comum."""
    assert fd.is_opportunistic_host_loss("grokbox", 75, "") is True
    assert fd.is_opportunistic_host_loss("grokbox", 2, "[REMOTE_JOB_LOST] worker sumiu\n") is True
    assert fd.is_opportunistic_host_loss(host_id, 75, "") is False
    assert fd.is_opportunistic_host_loss(host_id, 2, "[REMOTE_JOB_LOST] worker sumiu\n") is False


def test_desktop_infra_loss_keeps_the_existing_cooldown_treatment(tmp_path: Path) -> None:
    """USR-492 T2: a mesma perda que e silenciosa no Grokbox continua reenfileirando
    com cooldown global no Desktop -- a supressao de alerta/cooldown e especifica
    do Grokbox, nao um relaxamento geral de infra."""
    demands = [_d("USR-01"), _d("USR-02")]
    e = Env(tmp_path, demands, free=("notebook", "desktop"))
    e.d.run_cycle()
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "desktop")]
    job = e.d.in_flight["desktop"]
    log = tmp_path / "USR-02-desktop-lost.log"
    log.write_text("", encoding="utf-8")  # mesmo exit code (75) usado na queda silenciosa do Grokbox
    job.log_path = log
    e.handles["USR-02"].rc = 75
    e.free = {"notebook", "grokbox", "desktop"}
    cycle = e.d.run_cycle()
    assert cycle["cooldown"] == ["USR-02"]
    assert demands[1]["status"] == "planned"
    assert e.launched == [("USR-01", "notebook"), ("USR-02", "desktop")]


def test_self_modification_alert_stays_on_the_express_path(tmp_path: Path):
    alerts: list[object] = []
    e = Env(tmp_path, [])
    e.d.express_alert = lambda alert: alerts.append(alert) or True
    log = "[ERRO NA ENTREGA] causa: self_modification; erro: x\n"
    job = fd.InFlight("USR-01", "notebook", Handle(), candidate_digest="candidate", context_digest="ctx")
    e.d._record_failure(job, 1, "failure", log)
    assert alerts == []
    e.d._record_failure(job, 1, "failure", log)
    assert len(alerts) == 1
    assert alerts[0].cause == "self_modification"
    assert "USR-01" in alerts[0].command


def test_host_lock_no_overlap(tmp_path: Path):
    e = Env(tmp_path, [_d(f"USR-0{i}") for i in range(1, 8)])
    e.d.run_cycle()
    e.d.run_cycle()
    assert len(e.launched) == 3


def _recovery_env(tmp_path: Path, demands, tickets, *, active=False):
    e = Env(tmp_path, demands)
    e.d.config.recovery_tickets = tuple(tickets)
    if active:
        e.d.admission_store.set_recovery(active=True, cause="gate_failed", fingerprint="fp")
    return e


def test_recovery_list_only_prioritizes_when_global_recovery_inactive(tmp_path: Path):
    demands = [_d("USR-11"), _d("USR-12"), _d("USR-13"), _d("USR-14")]
    e = _recovery_env(tmp_path, demands, ("USR-12", "USR-11"))
    cyc = e.d.run_cycle()
    assert [t for t, _ in e.launched] == ["USR-12", "USR-11", "USR-13"]
    assert "recuperacao_prioritaria" not in cyc["skipped"].values()
    assert cyc["recovery_list"]["pending"] == ["USR-12", "USR-11"]


def test_recovery_list_stays_serialized_when_global_recovery_active(tmp_path: Path):
    demands = [_d("USR-11"), _d("USR-12"), _d("USR-13")]
    e = _recovery_env(tmp_path, demands, ("USR-12", "USR-11"), active=True)
    cyc = e.d.run_cycle()
    assert [t for t, _ in e.launched] == ["USR-12"]
    assert "USR-13" in cyc["queue_remaining"]
    e.d.run_cycle()
    assert len(e.launched) == 1


def test_recovery_list_without_admissible_ticket_does_not_block_queue(tmp_path: Path):
    demands = [_d("USR-11", "validating"), _d("USR-13")]
    e = _recovery_env(tmp_path, demands, ("USR-11",))
    cyc = e.d.run_cycle()
    assert [t for t, _ in e.launched] == ["USR-13"]
    assert cyc["recovery_list"]["cause"] == "recovery_list_no_admissible"
    assert cyc["recovery_list"]["pending"] == ["USR-11"]


def test_recovery_list_ticket_in_flight_is_not_no_admissible(tmp_path: Path):
    demands = [_d("USR-11"), _d("USR-13")]
    e = _recovery_env(tmp_path, demands, ("USR-11",))
    e.d.run_cycle()
    cyc = e.d.run_cycle()
    assert cyc["recovery_list"]["cause"] == ""


def test_recovery_list_completes_and_expires_persistently(tmp_path: Path):
    demands = [_d("USR-11", "validating"), _d("USR-13", "validating")]
    e = _recovery_env(tmp_path, demands, ("USR-11",))
    e.d.run_cycle()
    e.now += e.d.config.recovery_list_ttl_sec + 1
    assert e.d.run_cycle()["recovery_list"]["cause"] == "recovery_list_expired"
    demands[0]["status"] = "completed"
    restarted = _recovery_env(tmp_path / "again", demands, ("USR-11",))
    assert restarted.d.run_cycle()["recovery_list"]["cause"] == "recovery_list_completed"
    # estado persistido: novo despachante com o mesmo state_path continua expirado
    again = Env(tmp_path, [_d("USR-11")])
    again.d.config.recovery_tickets = ("USR-11",)
    assert again.d.run_cycle()["recovery_list"]["cause"] == "recovery_list_expired"
    assert [t for t, _ in again.launched] == ["USR-11"]


def test_line_block_preempts_live_recovery_list_on_first_free_host(tmp_path: Path):
    """Com a lista viva e recovery inativo, line-block sai antes dela no primeiro host livre."""
    demands = [
        _d("USR-10", "completed"),
        _d("USR-11"),
        _d("USR-12"),
        _d("USR-13"),
        _d("USR-30", tags=["line-block"], deps=["USR-10"]),
    ]
    e = _recovery_env(tmp_path, demands, ("USR-12", "USR-11"))
    e.free.discard("grokbox")
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-30", "notebook"), ("USR-12", "desktop")]
    assert cyc["queue_remaining"] == ["USR-11", "USR-13"]
    assert "recuperacao_prioritaria" not in cyc["skipped"].values()
    assert cyc["recovery_list"]["pending"] == ["USR-12", "USR-11"]


def test_line_block_takes_the_only_slot_when_recovery_is_active(tmp_path: Path):
    """Sem job em voo, line-block ocupa o slot unico; o recovery sai no ciclo seguinte."""
    demands = [_d("USR-12"), _d("USR-13"), _d("USR-30", tags=["line-block"])]
    e = _recovery_env(tmp_path, demands, ("USR-12", "USR-11"), active=True)
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-30", "notebook")]
    assert cyc["queue_remaining"] == ["USR-12", "USR-13"]
    assert "recovery_single_delivery_reserved" in cyc["idle_reasons"].values()

    e.handles["USR-30"].rc = 75
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-30", "notebook"), ("USR-12", "notebook")]
    assert cyc["in_flight"].get("notebook") == "USR-12"
    assert "USR-30" in cyc["cooldown"]


def test_line_block_does_not_open_a_second_delivery_while_one_is_in_flight(tmp_path: Path):
    demands = [_d("USR-12"), _d("USR-13")]
    e = _recovery_env(tmp_path, demands, ("USR-12",), active=True)
    e.d.run_cycle()
    assert e.launched == [("USR-12", "notebook")]

    demands.append(_d("USR-30", tags=["line-block"]))
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-12", "notebook")]
    assert cyc["in_flight"] == {"notebook": "USR-12"}
    assert cyc["skipped"]["USR-30"] == "recuperacao_prioritaria"
    assert "USR-30" not in cyc["queue_remaining"]


def test_line_block_does_not_open_second_delivery_if_recovery_latches_mid_flight(tmp_path: Path):
    """Entrega lancada antes do latch nao segura a reserva; line-block ainda fica de fora."""
    demands = [_d("USR-11")]
    e = Env(tmp_path, demands, free=("grokbox",))
    e.d.run_cycle()
    assert e.launched == [("USR-11", "grokbox")]
    assert not e.d.in_flight["grokbox"].recovery_token

    e.d.admission_store.set_recovery(active=True, cause="gate_failed", fingerprint="fp")
    e.free = {"grokbox", "notebook", "desktop"}
    demands.append(_d("USR-30", tags=["line-block"]))
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-11", "grokbox")]
    assert cyc["in_flight"] == {"grokbox": "USR-11"}
    assert cyc["skipped"]["USR-30"] == "recuperacao_prioritaria"


def test_line_block_stays_out_for_excluded_tag_open_dependency_and_cooldown(tmp_path: Path):
    demands = [
        _d("USR-20", tags=["line-block", "congelado-v0"]),
        _d("USR-21", tags=["line-block", "waiting_human"]),
        _d("USR-22", tags=["line-block", "planning-only"]),
        _d("USR-23", tags=["line-block", "not-authorized-for-execution"]),
        _d("USR-24", tags=["line-block"], deps=["USR-99"]),
        _d("USR-25", tags=["line-block"]),
        _d("USR-12"),
        _d("USR-13"),
    ]
    e = _recovery_env(tmp_path, demands, ("USR-12",))
    e.free = {"grokbox"}
    e.d.admission_store.record_attempt("darkfac", "USR-25", "infra", run_id="test-cooldown", main_sha=e.d._main_sha)
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-12", "grokbox")]
    assert cyc["queue_remaining"] == ["USR-13"]
    for tid in ("USR-20", "USR-21", "USR-22", "USR-23"):
        assert cyc["skipped"][tid].startswith("tag:")
    assert cyc["skipped"]["USR-24"] == "dependencia_aberta"
    assert cyc["skipped"]["USR-25"] == "backoff_infra"


def test_dispatcher_reads_only_the_line_block_tag(tmp_path: Path):
    from core.demands.models import TAG_LINE_BLOCK, UserTicket

    assert TAG_LINE_BLOCK == "line-block"
    ticket = UserTicket(id="USR-30", title="Destrava a linha", tags=[TAG_LINE_BLOCK])
    assert ticket.tags == ["user-demand", TAG_LINE_BLOCK]
    assert fd.demand_has_line_block(ticket.model_dump(mode="json"))
    assert not fd.demand_has_line_block({"id": "USR-31", "tags": "line-block", "line_block": True, "title": "line-block"})

    impostor = _d("USR-31")
    impostor["line_block"] = True
    impostor["title"] = "line-block"
    demands = [
        _d("USR-11"),
        _d("USR-32", tags=["line_block"]),
        _d("USR-33", tags=["line-block-urgent"]),
        impostor,
        _d("USR-30", tags=["line-block"]),
    ]
    e = _recovery_env(tmp_path, demands, ("USR-11",))
    e.d.run_cycle()
    assert e.launched == [("USR-30", "notebook"), ("USR-11", "grokbox"), ("USR-31", "desktop")]


@pytest.fixture(autouse=True)
def _no_preserved_worktree_lookup(monkeypatch):
    monkeypatch.setattr(fd, "resumable_delivery_worktree", lambda *_a: None)


def test_line_block_keeps_the_fleet_launch_argv(tmp_path: Path, monkeypatch):
    calls: list[list[str]] = []

    class Process:
        def poll(self):
            return None

    def popen(cmd, **_kwargs):
        calls.append(list(cmd))
        return Process()

    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(fd, "subprocess", type("Subprocess", (), {
        "Popen": staticmethod(popen),
        "STDOUT": -2,
    })())
    host = fd.HostSpec("grokbox", 1, "http://g")
    fd.default_launch("USR-11", host, {"id": "USR-11", "tags": []})
    fd.default_launch("USR-30", host, {"id": "USR-30", "tags": ["user-demand", "line-block"]})
    assert len(calls) == 2
    for cmd, tid in zip(calls, ("USR-11", "USR-30"), strict=True):
        assert Path(cmd[1]).name == "run_ticket.py"
        assert cmd[2:] == [tid, "--fleet", "--host-id", host.id]
        assert "line-block" not in cmd
    # candidato preservado com plano valido: retoma a entrega sem replanejar
    monkeypatch.setattr(fd, "resumable_delivery_worktree", lambda *_a: tmp_path / "wt")
    fd.default_launch("USR-12", host, {"id": "USR-12", "tags": []})
    assert calls[2][2:] == ["USR-12", "--fleet", "--host-id", host.id,
                            "--resume-delivery", "--worktree", str(tmp_path / "wt")]


def test_resumable_delivery_worktree_needs_commits_and_plan(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from core.git import ticket_workspace as tw

    def lookup(ahead: int, changed: tuple = ()):
        chosen = SimpleNamespace(commits_ahead=ahead, changed_paths=changed,
                                 workspace=SimpleNamespace(path=wt_path))
        monkeypatch.setattr(tw, "find_preserved_workspaces", lambda *_a, **_k: SimpleNamespace(chosen=chosen))

    monkeypatch.undo()  # remove o stub autouse
    wt_path = tmp_path / "wt"
    wt_path.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(fd, "state_root", lambda: state_dir)

    context = wt_path / ".darkfac" / "runs" / "USR-9"
    context.mkdir(parents=True)
    lookup(2)
    assert fd.resumable_delivery_worktree("USR-9") is None  # sem plano
    (context / "SPEC.md").write_text("x", encoding="utf-8")
    (context / "tickets.json").write_text("[]", encoding="utf-8")
    assert fd.resumable_delivery_worktree("USR-9") == wt_path
    lookup(0)
    assert fd.resumable_delivery_worktree("USR-9") is None  # nada a frente da base

    # Plano gravado fora da worktree (em state_root()/artifacts/ticket_runs/<id>) tambem conta (USR-447).
    (context / "SPEC.md").unlink()
    (context / "tickets.json").unlink()
    ext_context = state_dir / "artifacts" / "ticket_runs" / "USR-9" / ".darkfac" / "runs" / "USR-9"
    ext_context.mkdir(parents=True)
    (ext_context / "SPEC.md").write_text("x", encoding="utf-8")
    (ext_context / "tickets.json").write_text("[]", encoding="utf-8")
    lookup(2)
    assert fd.resumable_delivery_worktree("USR-9") == wt_path


def _arm_completed_wait(tmp_path: Path, monkeypatch, *, cause: str, status: str = "completed"):
    """Pending row for a completed ticket, with the dispatcher collaborators stubbed."""
    from types import SimpleNamespace

    from core.line import pending_delivery as pd

    monkeypatch.setattr(pd, "_children", {})
    monkeypatch.setattr("core.line.ticket_engine.artifacts_root", lambda: tmp_path / "artifacts")
    context = tmp_path / "artifacts" / "USR-99" / ".darkfac" / "runs" / "USR-99"
    context.mkdir(parents=True)
    (context / "progress.json").write_text('{"harness":"codex"}', encoding="utf-8")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    directory = tmp_path / "pending"
    row = pd.schedule(
        "USR-99", candidate, "review", resume_delivery=True, cause=cause, directory=directory,
    )
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    monkeypatch.setattr(
        "core.demands.queue_store.control_store",
        lambda: SimpleNamespace(
            mock_mode=False, list_tickets=lambda: [{"id": "USR-99", "status": status}],
        ),
    )
    monkeypatch.setattr(
        "core.git.ticket_workspace.list_worktrees",
        lambda _root: [{"worktree": str(candidate), "branch": "ticket/usr-99"}],
    )
    monkeypatch.setattr(pd, "_route", lambda _row: ("grok", "grok"))
    monkeypatch.setattr(pd.run_outcome, "main_sha", lambda _repo: "")
    calls: list[list[str]] = []
    handle = SimpleNamespace(pid=424242, poll=lambda: None)
    monkeypatch.setattr(pd.subprocess, "Popen", lambda cmd, **kw: calls.append(list(cmd)) or handle)
    return pd, directory, context, candidate, row, calls


def _coherent_release(context: Path, *, merge_sha: str, report_sha: str | None = None) -> None:
    import json

    if report_sha is not None:
        (context / "deploy_report.json").write_text(
            json.dumps({"sha": report_sha, "deployed": False}), encoding="utf-8",
        )
    (context / "merge.json").write_text(
        json.dumps({"merge_sha": merge_sha, "candidate_digest": "candidate"}), encoding="utf-8",
    )
    (context / "validation.json").write_text(
        json.dumps({"passed": True, "candidate_digest": "candidate"}), encoding="utf-8",
    )
    (context / "review_state.json").write_text(
        json.dumps({"rounds": [{"candidate_digest": "candidate", "verdict": "approve"}]}),
        encoding="utf-8",
    )
    (context / "review_execution.json").write_text(
        json.dumps({
            "candidate_digest": "candidate",
            "implementer_family": "openai",
            "reviewer_family": "anthropic",
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize("cause", ["ci_fix_requires_revalidation", "ci_fix_route_wait"])
def test_ci_recovery_without_merge_resumes_completed_ticket(tmp_path: Path, monkeypatch, cause: str):
    pd, directory, _context, candidate, row, calls = _arm_completed_wait(tmp_path, monkeypatch, cause=cause)
    assert row.cause == cause and row.status == "waiting"

    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", "USR-99", "--worktree", str(candidate.resolve())]
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    assert saved.status == "running"
    assert saved.cause == cause


def test_ci_recovery_resumes_when_merge_and_deploy_files_have_no_sha(tmp_path: Path, monkeypatch):
    pd, directory, context, candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="ci_fix_requires_revalidation",
    )
    (context / "deploy_report.json").write_text('{"deployed": false}', encoding="utf-8")
    (context / "merge.json").write_text('{"candidate_digest": "candidate"}', encoding="utf-8")

    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", "USR-99", "--worktree", str(candidate.resolve())]


def test_ci_recovery_with_merge_sha_keeps_the_deploy_evidence_trail(tmp_path: Path, monkeypatch):
    pd, directory, context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="ci_fix_requires_revalidation",
    )
    _coherent_release(context, merge_sha="b" * 40, report_sha="a" * 40)

    assert pd.tick(tmp_path, directory=directory, now=row.next_check + 1)[0]["cause"] == "deploy_evidence_mismatch"
    assert calls == []

    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    saved.status = "waiting"
    pd._save(directory / "USR-99.json", saved)
    _coherent_release(context, merge_sha="a" * 40, report_sha="a" * 40)
    assert pd.tick(tmp_path, directory=directory, now=row.next_check + 2)[0]["status"] == "merged_awaiting_deploy"
    assert calls == []

    # External delivery with no applicable smoke is terminal: never a DarkFac redeploy wait.
    import json
    (context / "deploy_report.json").write_text(
        json.dumps({"sha": "a" * 40, "deployed": False, "smoke_ok": None, "smoke_applicable": False}),
        encoding="utf-8",
    )
    saved.status = "waiting"
    pd._save(directory / "USR-99.json", saved)
    outcome = pd.tick(tmp_path, directory=directory, now=row.next_check + 3)[0]
    assert outcome["status"] == "terminal" and outcome["proof"] == "merged_no_smoke", outcome
    assert calls == []



def test_pending_delivery_resume_isolates_parent_interactive_env(tmp_path: Path, monkeypatch):
    from core.line import pending_delivery as pd
    monkeypatch.setattr(pd, "_children", {})
    from types import SimpleNamespace
    import json
    from core.usage.models import ProviderAccountUsage, QuotaWindow
    snapshot_root = tmp_path / "snapshots"
    monkeypatch.setattr(pd, "state_root", lambda: snapshot_root)
    provider_file = snapshot_root / "usage" / "providers" / "openai.json"
    provider_file.parent.mkdir(parents=True)
    usage = ProviderAccountUsage(provider_id="openai", provider_name="OpenAI", family="frontier",
        status="connected", adapter="fixture", message="synthetic snapshot", quota_supported=True,
        windows=[QuotaWindow(quota_id="codex:weekly", label="fixture", used_percent=58, remaining_percent=42)])
    provider_file.write_text(usage.model_dump_json(), encoding="utf-8")
    assert pd._cached_quota("openai") == 42
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    monkeypatch.setattr("core.line.ticket_engine.artifacts_root", lambda: tmp_path / "artifacts")
    context = tmp_path / "artifacts" / "USR-99" / ".darkfac" / "runs" / "USR-99"
    context.mkdir(parents=True)
    (context / "progress.json").write_text('{"harness":"codex"}', encoding="utf-8")
    directory = tmp_path / "pending"
    row = pd.schedule("USR-99", candidate, "review", resume_delivery=True, directory=directory)
    assert row.implementing_harness == "codex"
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: SimpleNamespace(
        mock_mode=False, list_tickets=lambda: [{"id": "USR-99", "status": "planned"}]))
    monkeypatch.setattr("core.git.ticket_workspace.list_worktrees", lambda _root: [{"worktree": str(candidate)}])
    monkeypatch.setattr(pd.run_outcome, "main_sha", lambda _repo: "")
    monkeypatch.setattr(pd, "_route", lambda _row: None)
    calls = []
    handle = SimpleNamespace(pid=999999, poll=lambda: None)
    monkeypatch.setattr(pd.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or handle)
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "antigravity")
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-chat")
    monkeypatch.setenv("DARKFAC_TICKET_ID", "USR-1")
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path / "interactive-checkout"))
    monkeypatch.setenv("GH_TOKEN", "ghp_keep")
    due = row.next_check + 1
    assert pd.tick(tmp_path, directory=directory, now=due)[0]["status"] == "waiting_route"
    assert not calls
    monkeypatch.setattr(pd, "_route", lambda _row: ("openrouter", "distill.openrouter"))
    assert pd.tick(tmp_path, directory=directory, now=due + 1)[0]["status"] == "waiting"
    assert not calls  # persisted backoff also applies after restart
    result = pd.tick(tmp_path, directory=directory, now=due + pd.RECHECK_SECONDS + 1)
    assert result[0]["status"] == "running"
    assert calls[0][0][2:] == ["--resume-delivery", "USR-99", "--worktree", str(candidate.resolve())]
    assert "--fleet" not in calls[0][0]  # standalone launcher owns its canonical line lock
    resumed_env = calls[0][1]["env"]
    assert resumed_env["DARKFAC_PROJECT_ROOT"] == str(tmp_path.resolve())
    assert resumed_env["DARKFAC_STATE_ROOT"] == str(snapshot_root.resolve())
    assert "DARKFAC_FLEET_MODE" not in resumed_env
    assert resumed_env["DARKFAC_OPERATING_HARNESS"] == ""
    assert "CODEX_THREAD_ID" not in resumed_env
    assert "DARKFAC_TICKET_ID" not in resumed_env
    assert resumed_env["GH_TOKEN"] == "ghp_keep"
    pd.tick(tmp_path, directory=directory, now=due + 2 * pd.RECHECK_SECONDS)
    assert len(calls) == 1
    # Lost parent process: live child remains the owner after a service restart.
    pd._children.clear()
    monkeypatch.setattr(pd, "_pid_running", lambda _row: True)
    assert pd.tick(tmp_path, directory=directory, now=due + 3 * pd.RECHECK_SECONDS)[0]["status"] == "running"
    assert len(calls) == 1
    monkeypatch.setattr(pd, "_pid_running", lambda _row: False)

def test_completed_ticket_with_another_cause_stays_blocked_without_deploy_proof(tmp_path: Path, monkeypatch):
    pd, directory, _context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="ci_red_twice",
    )

    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["cause"] == "deploy_evidence_missing"
    assert result[0]["status"] == "blocked"
    assert calls == []
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    assert saved.status == "blocked"
    assert saved.cause == "deploy_evidence_missing"


@pytest.mark.parametrize("cause", ["deploy_evidence_missing", "deploy_evidence_mismatch"])
@pytest.mark.parametrize("status,age_h,archived", [
    ("completed", 7, True), ("cancelled", 7, True), ("completed", 1, False), ("planned", 7, False),
])
def test_blocked_evidence_wait_of_closed_ticket_is_archived_after_six_hours(
    tmp_path: Path, monkeypatch, cause: str, status: str, age_h: int, archived: bool,
):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    pd, directory, _context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause=cause, status=status,
    )
    row.status = "blocked"
    pd._save(directory / "USR-99.json", row)
    updated = (datetime.now(timezone.utc) - timedelta(hours=age_h)).isoformat()
    monkeypatch.setattr(
        "core.demands.queue_store.control_store",
        lambda: SimpleNamespace(
            mock_mode=False, list_tickets=lambda: [{"id": "USR-99", "status": status, "updated_at": updated}],
        ),
    )

    result = pd.tick(tmp_path, directory=directory, now=datetime.now(timezone.utc).timestamp())

    assert calls == []
    assert (directory / "archive" / "USR-99.json").is_file() is archived
    assert (directory / "USR-99.json").exists() is not archived
    assert (result[0]["status"] == "archived") is archived


@pytest.mark.parametrize("status", ["cancelled", "canceled"])
def test_cancelled_wait_is_removed_before_block_and_backoff(tmp_path: Path, monkeypatch, status: str):
    pd, directory, _context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1", status=status,
    )
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    saved.status = "blocked"
    saved.next_check = row.next_check + 10_000
    pd._save(directory / "USR-99.json", saved)

    result = pd.tick(tmp_path, directory=directory, now=row.next_check)

    assert result[0]["status"] == "terminal"
    assert result[0]["proof"] == status
    assert calls == []
    assert not (directory / "USR-99.json").exists()
    import json

    ledger = json.loads((directory / "reconciliations" / "USR-99.json").read_text(encoding="utf-8"))
    assert ledger["events"][-1]["to_status"] == "terminal"
    assert ledger["events"][-1]["reason"] == "ticket cancellation is durable"
    assert ledger["events"][-1]["evidence"] == f"ticket_status:{status}"


def test_completed_without_deploy_proof_is_reconciled_during_backoff(tmp_path: Path, monkeypatch):
    pd, directory, _context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    saved.status = "blocked"
    saved.cause = "launcher_exit:1"
    saved.next_check = row.next_check + 10_000
    pd._save(directory / "USR-99.json", saved)

    result = pd.tick(tmp_path, directory=directory, now=row.next_check)

    assert result[0]["status"] == "blocked"
    assert result[0]["cause"] == "completed_without_merge_evidence_ambiguous"
    assert calls == []
    kept = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    assert kept.status == "blocked"
    assert kept.cause == "completed_without_merge_evidence_ambiguous"
    import json

    ledger_path = directory / "reconciliations" / "USR-99.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["events"][-1]["from_cause"] == "launcher_exit:1"
    assert ledger["events"][-1]["to_cause"] == "completed_without_merge_evidence_ambiguous"
    assert ledger["events"][-1]["evidence"] == "candidate_sha_missing_or_invalid"

    ledger_bytes = ledger_path.read_bytes()
    pd.tick(tmp_path, directory=directory, now=row.next_check + 1)
    assert ledger_path.read_bytes() == ledger_bytes


def test_completed_without_merge_reopens_same_candidate_for_review(tmp_path: Path, monkeypatch):
    import json
    from types import SimpleNamespace

    from core.demands.models import UserTicket
    from core.line import pending_delivery as pd

    _pd, directory, _context, candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    candidate_sha = "8" * 40
    ticket = UserTicket(id="USR-99", project_id="darkfac", title="Preserved candidate",
                        status="completed", delivery_evidence=candidate_sha)

    class Queue:
        mock_mode = False

        def __init__(self):
            self.ticket = ticket.model_dump(mode="json")

        def list_tickets(self):
            return [self.ticket]

        def upsert_ticket(self, project_id, ticket_id, status, payload, now):
            self.ticket = payload

    queue = Queue()
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: queue)

    def git_run(args, **_kwargs):
        if args[1:3] == ["rev-parse", "origin/main"]:
            return SimpleNamespace(returncode=0, stdout="4" * 40)
        if args[1:3] == ["merge-base", "--is-ancestor"]:
            return SimpleNamespace(returncode=1, stdout="")
        raise AssertionError(args)

    monkeypatch.setattr(pd.subprocess, "run", git_run)
    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["status"] == "running"
    assert queue.ticket["status"] == "planned"
    assert queue.ticket["delivery_evidence"] == f"completed_without_merge:{candidate_sha}"
    assert candidate.is_dir()
    assert calls and calls[0][2:4] == ["--resume-delivery", "USR-99"]
    assert "--worktree" in calls[0]
    pending = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    assert pending.stage == "review" and pending.resume_delivery
    ledger = json.loads((directory / "reconciliations" / "USR-99.json").read_text(encoding="utf-8"))
    assert len(ledger["events"]) == 1
    assert ledger["events"][0]["to_cause"] == "completed_without_merge"


def test_completed_without_merge_ambiguous_git_evidence_stays_blocked(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from core.demands.models import UserTicket
    from core.line import pending_delivery as pd

    _pd, directory, _context, candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    queue = SimpleNamespace(
        mock_mode=False,
        list_tickets=lambda: [UserTicket(id="USR-99", title="Preserved candidate", status="completed",
                                         delivery_evidence="8" * 40).model_dump(mode="json")],
        upsert_ticket=lambda *_args: pytest.fail("ambiguous evidence must not mutate the queue"),
    )
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: queue)
    monkeypatch.setattr(pd.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=128, stdout=""))

    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["status"] == "blocked"
    assert result[0]["cause"] == "completed_without_merge_evidence_ambiguous"
    assert candidate.is_dir() and not calls


def test_conflicting_legacy_ticket_status_fails_closed_with_a_durable_reason(tmp_path: Path, monkeypatch):
    import json

    from types import SimpleNamespace

    from core.line import pending_delivery as pd

    pd, directory, _context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    monkeypatch.setattr(
        "core.demands.queue_store.control_store",
        lambda: SimpleNamespace(
            mock_mode=False,
            list_tickets=lambda: [
                {"id": "USR-99", "status": "completed"},
                {"id": "USR-99", "status": "cancelled"},
            ],
        ),
    )

    result = pd.tick(tmp_path, directory=directory, now=row.next_check + 1)

    assert result[0]["status"] == "blocked"
    assert result[0]["cause"] == "ticket_status_conflict"
    assert calls == []
    assert (directory / "USR-99.json").is_file()
    event = json.loads((directory / "reconciliations" / "USR-99.json").read_text(encoding="utf-8"))["events"][-1]
    assert event["reason"] == "terminal evidence is missing or contradictory"
    assert event["evidence"] == "ticket_statuses:cancelled,completed"

    dispatcher = Env(tmp_path / "fleet", [_d("USR-99"), _d("USR-100")], free=("grokbox",))
    dispatcher.d._resumptions = lambda: result
    cycle = dispatcher.d.run_cycle()
    assert dispatcher.launched == [("USR-100", "grokbox")]
    assert cycle["skipped"]["USR-99"].startswith("pending_delivery:")
    assert "USR-100" not in cycle["skipped"]


def test_completed_wait_is_removed_only_with_coherent_deploy_proof(tmp_path: Path, monkeypatch):
    import json

    pd, directory, context, _candidate, row, calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    _coherent_release(context, merge_sha="b" * 40, report_sha="a" * 40)
    report = json.loads((context / "deploy_report.json").read_text(encoding="utf-8"))
    report["deployed"] = True
    (context / "deploy_report.json").write_text(json.dumps(report), encoding="utf-8")
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    saved.status = "blocked"
    saved.next_check = row.next_check + 10_000
    pd._save(directory / "USR-99.json", saved)

    mismatch = pd.tick(tmp_path, directory=directory, now=row.next_check)
    assert mismatch[0]["cause"] == "deploy_evidence_mismatch"
    assert (directory / "USR-99.json").is_file()
    assert calls == []

    _coherent_release(context, merge_sha="a" * 40, report_sha="a" * 40)
    report["sha"] = "a" * 40
    (context / "deploy_report.json").write_text(json.dumps(report), encoding="utf-8")
    removed = pd.tick(tmp_path, directory=directory, now=row.next_check)
    assert removed[0]["status"] == "terminal"
    assert removed[0]["proof"] == "deployed"
    assert not (directory / "USR-99.json").exists()


def test_missing_deploy_proof_alerts_once_until_the_evidence_changes(tmp_path: Path, monkeypatch):
    pd, directory, context, _candidate, row, _calls = _arm_completed_wait(
        tmp_path, monkeypatch, cause="launcher_exit:1",
    )
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "alerts"))
    sent: list[dict] = []
    monkeypatch.setattr(
        failure_alerts_mod, "_emit_failure_alert", lambda **kwargs: sent.append(kwargs) or True,
    )
    monkeypatch.setattr(failure_alerts_mod, "notify_deterministic_failure", _REAL_DETERMINISTIC_ALERT)

    pd.tick(tmp_path, directory=directory, now=row.next_check + 1)
    pd.tick(tmp_path, directory=directory, now=row.next_check + 2)
    assert len(sent) == 1
    assert sent[0]["cause"] == "completed_without_merge_evidence_ambiguous"
    assert sent[0]["evidence"] == "candidate_sha_missing_or_invalid"

    (context / "deploy_report.json").write_text('{"deployed": false}', encoding="utf-8")
    pd.tick(tmp_path, directory=directory, now=row.next_check + 3)
    pd.tick(tmp_path, directory=directory, now=row.next_check + 4)
    assert len(sent) == 2
    assert sent[1]["evidence"] == "sha_missing"
    assert sent[1]["cause"] == "deploy_evidence_missing"


def test_blocked_wait_does_not_make_an_independent_ticket_ineligible(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))
    e.d.last_cycle = {
        "blocked_cycles": 41,
        "blocked_causes": {"delivery_failure": 1},
        "blocked_ticket_reasons": {"USR-01": "pending_delivery:launcher_exit:1"},
    }
    e.d._resumptions = lambda: [{
        "ticket": "USR-01", "status": "blocked", "cause": "launcher_exit:1", "stage": "development",
    }]
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-02", "grokbox")]
    assert cycle["skipped"]["USR-01"].startswith("pending_delivery:")
    assert "USR-02" not in cycle["skipped"]
    assert cycle["activity"] == "running"
    assert cycle["blocked_cycles"] == 0
    assert cycle["blocked_causes"] == {}


def test_exhausted_technical_ticket_stays_isolated_after_restart(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))
    for index in range(3):
        e.d.admission_store.record_attempt(
            "darkfac", "USR-01", "gate_failed", run_id=f"failed-{index}",
            main_sha="a" * 40, detail="assertion failed", phase="development",
        )
    e.d._resumptions = lambda: [{
        "ticket": "USR-01", "status": "blocked", "cause": "gate_failed", "stage": "development",
    }]
    restarted = fd.FleetDispatcher(
        e.d.config, demands_loader=lambda: [_d("USR-01"), _d("USR-02")],
        probe=e.d.probe, launch=e._launch,
        clock=lambda: e.now, admission_store=AdmissionStore(
            tmp_path / "admission.sqlite3",
            clock=lambda: datetime.fromtimestamp(e.now, timezone.utc),
        ),
    )
    restarted._resumptions = e.d._resumptions

    cycle = restarted.run_cycle()

    assert e.launched == [("USR-02", "grokbox")]
    assert cycle["skipped"]["USR-01"] == "ticket_hold:gate_failed"
    assert cycle["activity"] == "running"


def test_legacy_blocked_dispatcher_state_restarts_its_counter_from_ticket_evidence(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.last_cycle = {"blocked_cycles": 99, "blocked_causes": {"delivery_failure": 1}}
    e.d._resumptions = lambda: [{
        "ticket": "USR-01", "status": "blocked", "cause": "launcher_exit:1", "stage": "planning",
    }]

    first = e.d.run_cycle()
    restarted = fd.FleetDispatcher(
        e.d.config,
        demands_loader=lambda: [_d("USR-01")],
        probe=lambda _host: fd.HostProbe(True, "ready"),
        launch=e._launch,
        clock=lambda: e.now,
        admission_store=e.d.admission_store,
    )
    restarted._resumptions = e.d._resumptions
    second = restarted.run_cycle()

    assert first["blocked_cycles"] == 1
    assert second["blocked_cycles"] == 2
    assert second["blocked_causes"] == {"delivery_failure": 1}
    assert second["blocked_ticket_reasons"] == {"USR-01": "pending_delivery:launcher_exit:1"}


def test_non_mapping_legacy_dispatcher_state_does_not_block_independent_tickets(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))
    e.d.config.state_path.write_text("[]", encoding="utf-8")
    restarted = fd.FleetDispatcher(
        e.d.config,
        demands_loader=lambda: [_d("USR-01"), _d("USR-02")],
        probe=e.d.probe,
        launch=e._launch,
        clock=lambda: e.now,
        admission_store=e.d.admission_store,
    )
    restarted._resumptions = lambda: [{
        "ticket": "USR-01", "status": "blocked", "cause": "launcher_exit:1", "stage": "planning",
    }]

    cycle = restarted.run_cycle()

    assert restarted.last_cycle["activity"] == "running"
    assert e.launched == [("USR-02", "grokbox")]
    assert cycle["blocked_cycles"] == 0


def test_launch_failure_does_not_skip_the_next_planned_ticket(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))

    def flaky(tid, host, **kwargs):
        if tid == "USR-01":
            raise RuntimeError("checkout recusado")
        return e._launch(tid, host, **kwargs)

    e.d.launch = flaky
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-02", "grokbox")]
    assert cycle["skipped"]["USR-01"].startswith("lancamento_falhou:")
    assert "USR-02" not in cycle["skipped"]


def test_live_dispatcher_without_progress_is_idle_or_blocked(tmp_path: Path):
    idle = Env(tmp_path / "idle", [], free=("grokbox",))
    idle_cycle = idle.d.run_cycle()
    assert idle_cycle["activity"] == "idle"
    assert idle_cycle["dispatched"] == []
    assert idle_cycle["in_flight"] == {}

    blocked = Env(tmp_path / "blocked", [], free=("grokbox",))
    blocked.d._resumptions = lambda: [{
        "ticket": "USR-01", "status": "blocked", "cause": "launcher_exit:1", "stage": "planning",
    }]
    blocked_cycle = blocked.d.run_cycle()
    assert blocked_cycle["activity"] == "blocked"
    assert blocked_cycle["dispatched"] == []
    assert "USR-01" not in blocked_cycle["in_flight"].values()

    running = Env(tmp_path / "running", [], free=("grokbox",))
    running.d._resumptions = lambda: [{
        "ticket": "USR-09", "status": "running", "pid": 42, "stage": "development",
        "started_at": running.now, "heartbeat_epoch": running.now,
    }]
    assert running.d.run_cycle()["activity"] == "running"


def test_stage_change_is_progress_and_the_same_stage_is_not(tmp_path: Path):
    e = Env(tmp_path, [], free=("grokbox",))
    stage = {"value": "planning"}
    e.d._resumptions = lambda: [{
        "ticket": "USR-09", "status": "waiting", "cause": "route_wait", "stage": stage["value"],
    }]
    assert e.d.run_cycle()["activity"] == "idle"
    stage["value"] = "development"
    changed = e.d.run_cycle()
    assert changed["activity"] == "running"
    assert changed["last_progress_epoch"] == e.now
    assert e.d.run_cycle()["activity"] == "running"
    e.now += e.d.config.progress_window_sec + 1
    expired = e.d.run_cycle()
    assert expired["activity"] == "idle"
    assert expired["last_progress_epoch"] == e.now - e.d.config.progress_window_sec - 1


def test_stale_live_child_is_idle_after_restart_and_recent_log_heartbeat_is_running(tmp_path: Path):
    e = Env(tmp_path, [], free=("grokbox",))
    e.d.config.progress_window_sec = 30
    e.d.in_flight["grokbox"] = fd.InFlight(
        "USR-09", "grokbox", Handle(), started_at=e.now - 60,
    )
    stale = e.d.run_cycle()
    assert stale["activity"] == "idle"
    assert stale["heartbeat_epoch"] == e.now
    assert stale["last_progress_epoch"] == e.now - 60

    restarted = fd.FleetDispatcher(
        e.d.config, demands_loader=lambda: [], probe=lambda _host: fd.HostProbe(True, "ready"),
        launch=e._launch, clock=lambda: e.now, admission_store=e.d.admission_store,
    )
    assert restarted.run_cycle()["activity"] == "idle"

    running = Env(tmp_path / "heartbeat", [], free=("grokbox",))
    running.d.config.progress_window_sec = 30
    running.d._resumptions = lambda: [{
        "ticket": "USR-09", "status": "running", "pid": 42, "stage": "development",
        "started_at": running.now - 600, "heartbeat_epoch": running.now,
    }]
    assert running.d.run_cycle()["activity"] == "running"
    running.now += 31
    running.d._resumptions = lambda: [{
        "ticket": "USR-09", "status": "running", "pid": 42, "stage": "development",
        "started_at": running.now - 631, "heartbeat_epoch": running.now - 31,
    }]
    assert running.d.run_cycle()["activity"] == "idle"


def test_deterministic_delivery_failure_alerts_once_per_fingerprint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "alerts"))
    sent: list[dict] = []
    monkeypatch.setattr(
        failure_alerts_mod, "_emit_failure_alert", lambda **kwargs: sent.append(kwargs) or True,
    )
    monkeypatch.setattr(failure_alerts_mod, "notify_deterministic_failure", _REAL_DETERMINISTIC_ALERT)
    e = Env(tmp_path, [], free=("grokbox",))
    same = "[ERRO NA ENTREGA] causa: gate_failed; erro: assertion failed"
    for index in range(2):
        job = fd.InFlight("USR-01", "grokbox", Handle(), run_id=f"run-{index}")
        e.d._record_failure(job, 1, "failure", same)
    assert len(sent) == 1
    assert sent[0]["cause"] == "gate_failed"
    assert "assertion failed" in sent[0]["evidence"]

    changed = "[ERRO NA ENTREGA] causa: gate_failed; erro: different assertion"
    e.d._record_failure(fd.InFlight("USR-01", "grokbox", Handle(), run_id="run-2"), 1, "failure", changed)
    assert len(sent) == 2
    e.d._record_failure(
        fd.InFlight("USR-01", "grokbox", Handle(), run_id="run-3"), 1, "failure",
        "[ERRO NA ENTREGA] causa: ci_red; erro: different assertion",
    )
    assert len(sent) == 3
    assert sent[2]["cause"] == "ci_red"
    before = len(sent)
    e.d._record_failure(
        fd.InFlight("USR-01", "grokbox", Handle(), run_id="run-infra"), 75, "infra",
        "[ERRO NA ENTREGA] causa: gate_timeout; erro: worker timed out",
    )
    assert len(sent) == before


def test_actionable_failure_and_notification_transition_survive_restart(tmp_path: Path, monkeypatch):
    import importlib
    import json

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "failure-state"))
    module = failure_alerts_mod
    sent: list[dict] = []
    monkeypatch.setattr(module, "_emit_failure_alert", lambda **kwargs: sent.append(kwargs) or True)
    monkeypatch.setattr(module, "notify_deterministic_failure", _REAL_DETERMINISTIC_ALERT)
    first = module.notify_deterministic_failure(
        "USR-90", "gate_failed", "assertion failed", "run.log", phase="development",
        attempts=1, next_step="Inspect assertion evidence and fix the candidate.", transition="retrying",
    )
    repeated = module.notify_deterministic_failure(
        "USR-90", "gate_failed", "assertion failed", "run.log", phase="development",
        attempts=2, next_step="Inspect assertion evidence and fix the candidate.", transition="retrying",
    )
    blocked = module.notify_deterministic_failure(
        "USR-90", "gate_failed", "assertion failed", "run.log", phase="development",
        attempts=3, next_step="Correct the evidence and release the ticket hold.", transition="blocked",
    )
    assert first and not repeated and blocked
    assert len(sent) == 2
    assert sent[0]["phase"] == "development" and sent[0]["attempts"] == 1
    assert sent[1]["transition"] == "blocked" and sent[1]["attempts"] == 3

    restarted = importlib.reload(module)
    monkeypatch.setattr(restarted, "_emit_failure_alert", lambda **kwargs: sent.append(kwargs) or True)
    assert not restarted.notify_deterministic_failure(
        "USR-90", "gate_failed", "assertion failed", "run.log", phase="development",
        attempts=3, next_step="Correct the evidence and release the ticket hold.", transition="blocked",
    )
    actionable = json.loads((tmp_path / "failure-state" / "actionable_failures.json").read_text(encoding="utf-8"))
    record = actionable["tickets"]["USR-90"]
    assert record["phase"] == "development"
    assert record["fingerprint"] == module.failure_fingerprint("USR-90", "gate_failed", "assertion failed")
    assert record["attempts"] == 3
    assert record["last_evidence"] == "assertion failed"
    assert record["next_step"] == "Correct the evidence and release the ticket hold."
    assert len(sent) == 2


def test_concurrent_identical_failure_notifications_are_deduplicated(tmp_path: Path, monkeypatch):
    import time

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "concurrent-failure-state"))
    sent: list[dict] = []

    def emit(**kwargs):
        time.sleep(0.01)
        sent.append(kwargs)
        return True

    monkeypatch.setattr(failure_alerts_mod, "_emit_failure_alert", emit)
    monkeypatch.setattr(failure_alerts_mod, "notify_deterministic_failure", _REAL_DETERMINISTIC_ALERT)

    def notify(_index: int) -> bool:
        return failure_alerts_mod.notify_deterministic_failure(
            "USR-91", "ci_red", "CI failed on candidate abc", "run.log",
            phase="ci", attempts=2, next_step="Repair CI and resume delivery.", transition="retrying",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(notify, range(8)))

    assert sum(results) == 1
    assert len(sent) == 1


def test_admission_store_failure_is_structured_and_fails_closed(tmp_path: Path):
    class UnavailableStore:
        def retry_reason(self, *_args, **_kwargs):
            raise OSError("database unavailable")

    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.admission_store = UnavailableStore()
    cycle = e.d.run_cycle()
    assert e.launched == []
    assert cycle["skipped"]["USR-01"] == "admission_store_unavailable"
    assert cycle["skipped"]["USR-01"] == "admission_store_unavailable"


def test_recovery_latches_persistently_and_serializes_one_evidenced_delivery(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")])
    failed = fd.InFlight("USR-99", "grokbox", Handle(), candidate_digest="base",
                         context_digest="ctx", resource_version="worker-v1")
    for _ in range(3):
        e.d._record_failure(failed, 1, "failure", "[ERRO NA ENTREGA] causa: gate_failed; erro: assertion")
    # falha repetida do mesmo ticket segura so o ticket; a frota segue livre
    assert e.d.admission_store.recovery_state()["active"] == 0
    assert [h["ticket_id"] for h in e.d.admission_store.ticket_holds()] == ["USR-99"]
    assert len(e.d.run_cycle()["dispatched"]) == 2
    e.d.admission_store.set_recovery(active=True, cause="context", fingerprint="fp")

    # A fresh dispatcher sees the durable latch and only dispatches one delivery globally.
    e.d = fd.FleetDispatcher(
        e.d.config, demands_loader=lambda: [_d("USR-01"), _d("USR-02")],
        probe=lambda h: fd.HostProbe(True, "ready", "worker-v1"), launch=e._launch,
        deploy=e._deploy, clock=lambda: e.now, admission_store=e.d.admission_store,
    )
    cycle = e.d.run_cycle()
    assert len(cycle["dispatched"]) == 1
    assert cycle["recovery"]["active"] is True
    assert "recovery_single_delivery_reserved" in cycle["idle_reasons"].values()
    job = next(iter(e.d.in_flight.values()))
    assert job.run_id and job.recovery_token

    e.handles[job.ticket_id].rc = 0
    e.d.run_cycle()  # exit zero without a delivery receipt is still not success
    assert e.d.admission_store.recovery_state()["active"] == 1
    assert e.d.admission_store.budget("darkfac", job.ticket_id)["candidate_failures"] == 1
    normal = Env(tmp_path / "normal", [_d("USR-01")], free=("grokbox",))
    normal.d.run_cycle()
    normal.handles["USR-01"].rc = 0
    normal.d.run_cycle()
    assert normal.d.admission_store.budget("darkfac", "USR-01")["candidate_failures"] == 1
    assert normal.d.failures[-1]["kind"] == "no_delivery_evidence"


def test_recovery_success_requires_merge_receipt(tmp_path: Path, monkeypatch):
    import io
    import json
    import subprocess
    from types import SimpleNamespace
    from core.line import ticket_engine
    from core.learning_pack.models import SessionLearningPack
    from core.learning_pack.storage import LearningPackStore

    demands = [_d("USR-01"), _d("USR-02")]
    e = Env(tmp_path, demands, free=("grokbox",))
    e.d.admission_store.set_recovery(active=True, cause="context", fingerprint="fp")
    e.d.run_cycle()
    directory = tmp_path / "artifacts" / "USR-01" / ".darkfac" / "runs" / "USR-01"
    directory.mkdir(parents=True)
    digest, sha = "c" * 64, "a" * 40
    artifacts = {
        "validation.json": {"passed": True, "candidate_digest": digest},
        "review_state.json": {"rounds": [{"verdict": "approve", "candidate_digest": digest}]},
        "review_execution.json": {"candidate_digest": digest, "implementer_family": "openai", "reviewer_family": "deepseek"},
        "merge.json": {"pr_url": "https://github.com/example/repo/pull/1", "merge_sha": sha, "candidate_digest": digest},
    }
    for name, value in artifacts.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(ticket_engine, "artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr("core.git.autonomy._run_git", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr("core.git.ci_checks.query_checks", lambda *a: SimpleNamespace(state="green"))
    class Health(io.StringIO):
        status = 200
    live_health = {"status": "ok", "git_sha": "b" * 40}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Health(json.dumps(live_health)))
    packs = LearningPackStore(tmp_path / "packs")
    packs.save_pack(SessionLearningPack("lp-usr-01", "USR-01", datetime.now(timezone.utc).isoformat(), "Release", "Validated release", metrics={"candidate_digest": digest}))
    monkeypatch.setattr("core.learning_pack.storage.LearningPackStore", lambda: packs)
    job = e.d.in_flight["grokbox"]
    log = tmp_path / "release.log"
    log.write_text("[DELIVERY_MERGED]", encoding="utf-8")
    job.log_path = log
    e.handles["USR-01"].rc = 0
    demands[0]["status"] = "completed"
    cycle = e.d.run_cycle()
    assert cycle["recovery"]["active"] and e.d.pending_releases
    assert e.launched == [("USR-01", "grokbox")]
    # Restart with the durable merge pending. No fabricated production receipt.
    e.d = fd.FleetDispatcher(e.d.config, demands_loader=lambda: demands, probe=lambda h: fd.HostProbe(True),
                            launch=e._launch, deploy=e._deploy, clock=lambda: e.now,
                            admission_store=e.d.admission_store)
    cycle = e.d.run_cycle()
    assert cycle["recovery"]["active"] and e.d.pending_releases
    assert not (directory / "deploy_report.json").exists()
    assert e.launched == [("USR-01", "grokbox")]
    live_health["git_sha"] = sha
    e.now += e.d.config.deploy_window_sec + 1
    cycle = e.d.run_cycle()
    assert cycle["recovery"]["active"] is False
    assert cycle["recovery"]["successes"] == 1
    assert json.loads((directory / "deploy_report.json").read_text(encoding="utf-8"))["deployed"] is True
    assert e.deploys == 1 and not e.d.pending_releases
    assert e.launched == [("USR-01", "grokbox"), ("USR-02", "notebook")]


def test_recovery_rejects_missing_production_receipt_and_persists_cause(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.admission_store.set_recovery(active=True, cause="context", fingerprint="fp")
    e.d.run_cycle()
    host_id = next(iter(e.d.in_flight))
    log = tmp_path / "USR-01.log"
    log.write_text("[DELIVERY_MERGED]", encoding="utf-8")
    e.d.in_flight[host_id].log_path = log
    e.handles["USR-01"].rc = 0

    cycle = e.d.run_cycle()
    assert cycle["recovery"]["active"] is True
    assert cycle["recovery"]["successes"] == 0
    assert cycle["recovery"]["receipt_cause"] == "awaiting_deploy_proof"


def test_launcher_type_error_is_not_retried_without_recovery_identity(tmp_path: Path):
    calls = []

    def broken(tid, host, *, run_id=None, recovery=False, candidate_digest="unknown"):
        calls.append((run_id, recovery, candidate_digest))
        raise TypeError("bug inside launcher")

    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.launch = broken
    e.d.run_cycle()
    assert len(calls) == 1
    assert calls[0][0] and calls[0][2] == "candidate-USR-01"
    assert e.d.failures[-1]["kind"] == "launch"


def test_recovery_receipt_rejection_cause_is_part_of_failure_fingerprint(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest="candidate-USR-01")
    e.d._record_failure(job, 0, "recovery_receipt", "receipt_ci_stale")
    first = e.d.admission_store.snapshot()[-1]["fingerprint"]
    e.d._record_failure(job, 0, "recovery_receipt", "receipt_review_stale")
    second = e.d.admission_store.snapshot()[-1]["fingerprint"]
    assert first != second


def test_deploy_deduplicated_per_batch(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")])
    e.d.run_cycle()
    for h in e.handles.values():
        h.rc = 0
    for ticket in e.handles:
        e.mark_delivered(ticket)
    e.d.run_cycle()
    assert e.deploys == 0
    e.now += 101
    e.d.run_cycle()
    e.d.run_cycle()
    assert e.deploys == 1
    e.d.pending_deploy, e.d.last_merge_at = True, e.now - 101
    e.d.deploy = lambda: 1
    assert not e.d._maybe_deploy()
    assert e.d.pending_deploy and e.deploys == 1
    assert e.d.failures[-1]["kind"] == "deploy"
    assert e.d.failures[-1]["exit_code"] == 1


def test_line_lock_refusal_is_busy_not_infra(tmp_path: Path):
    log = tmp_path / "run.log"
    log.write_text("[TRAVA DE LINHA] Já existe um run_ticket em andamento nesta máquina", encoding="utf-8")
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.d.in_flight["grokbox"].log_path = log
    e.handles["USR-01"].rc = 2
    cyc = e.d.run_cycle()
    assert cyc["idle_reasons"]["grokbox"].startswith("host_ocupado_trava")
    text = log.read_text(encoding="utf-8")
    assert fd.is_lock_refusal(text) and fd.is_infra_failure(2, text)
    e.now += 15  # O cooldown passou, mas o host e o contexto continuam iguais.
    cyc = e.d.run_cycle()
    assert len(e.launched) == 1
    assert cyc["skipped"]["USR-01"] == "backoff_infra"


def test_control_store_queue_tickets_reach_the_dispatcher() -> None:
    """USR-343 S4a moved new tickets to the control store; the ledger alone left the fleet idle."""
    from core.line.fleet_dispatcher import merge_control_queue

    ledger = [
        {"id": "USR-1", "status": "planned", "tags": []},
        {"id": "USR-2", "status": "completed", "tags": []},
    ]
    queue = [
        {"id": "USR-2", "status": "planned", "tags": []},  # closed in the ledger: stays closed
        {"id": "USR-3", "status": "planned", "tags": []},  # only in the control store
    ]
    merged = {r["id"]: r["status"] for r in merge_control_queue(ledger, lambda: queue)}
    assert merged == {"USR-1": "planned", "USR-2": "completed", "USR-3": "planned"}

    def unreachable() -> list:
        raise ConnectionError("store down")

    assert merge_control_queue(ledger, unreachable) == ledger


def test_disabled_hosts_env_keeps_host_in_policy_for_published_exclusion(monkeypatch):
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "Desktop, ")
    hosts = fd.default_hosts()
    assert [h.id for h in hosts] == ["notebook", "grokbox", "desktop"]
    assert [h.priority for h in hosts] == [1, 2, 3]
    from core.line.fleet_policy import effective_host_policy, load_fleet_config
    decisions = effective_host_policy(load_fleet_config(), disabled_from_environment=["desktop"])
    assert next(row for row in decisions if row["host_id"] == "desktop")["reason"] == "excluded"


def test_dispatch_preserves_effective_state_root_override(tmp_path: Path, monkeypatch):
    captured = {}

    class Process:
        def poll(self):
            return None

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "shared-state"))
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        fd,
        "subprocess",
        type(
            "Subprocess",
            (),
            {"Popen": staticmethod(lambda _cmd, **kwargs: captured.update(kwargs) or Process()), "STDOUT": -2},
        )(),
    )

    fd.default_launch("USR-413", fd.HostSpec("notebook", 1), {"id": "USR-413"})

    assert captured["env"]["DARKFAC_STATE_ROOT"] == str((tmp_path / "shared-state").resolve())


def test_dispatch_removes_checkout_project_root_but_keeps_derived_state_root(tmp_path: Path, monkeypatch):
    captured = {}

    class Process:
        def poll(self):
            return None

    checkout = tmp_path / "operational-checkout"
    checkout.mkdir()
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(checkout))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    monkeypatch.setattr(fd, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        fd, "subprocess",
        type("Subprocess", (), {
            "Popen": staticmethod(lambda _cmd, **kwargs: captured.update(kwargs) or Process()),
            "STDOUT": -2,
        })(),
    )

    fd.default_launch("USR-516", fd.HostSpec("notebook", 1), {"id": "USR-516"})

    assert "DARKFAC_PROJECT_ROOT" not in captured["env"]
    assert captured["env"]["DARKFAC_STATE_ROOT"] == str((checkout / ".factory").resolve())


def test_dispatch_keeps_remote_gate_pin_without_inheriting_project_root(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "dirty-checkout"
    checkout.mkdir()
    (checkout / "dirty.py").write_text("shared change", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(checkout))
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "fleet-state"))

    env = _capture_launch_env(monkeypatch, tmp_path, fd.HostSpec("grokbox", 1, "http://worker:8080"))

    assert "DARKFAC_PROJECT_ROOT" not in env
    assert env["DARKFAC_STATE_ROOT"] == str((tmp_path / "fleet-state").resolve())
    assert env["DARKFAC_FLEET_GATE_REMOTE"] == "1"
    assert env["DARKFAC_TEST_WORKERS"] == "http://worker:8080"


def _capture_launch_env(monkeypatch, tmp_path: Path, host: fd.HostSpec) -> dict[str, str]:
    captured: dict[str, object] = {}

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        fd,
        "subprocess",
        type(
            "Subprocess",
            (),
            {"Popen": staticmethod(lambda _cmd, **kwargs: captured.update(kwargs) or Process()), "STDOUT": -2},
        )(),
    )
    fd.default_launch("USR-510", host, {"id": "USR-510"})
    env = captured.get("env")
    assert isinstance(env, dict)
    return env


@pytest.mark.parametrize(
    ("previous", "pin", "expected"),
    [
        (
            "http://custom:1,http://custom:2",
            "http://pin:9",
            "http://custom:1,http://custom:2,http://pin:9",
        ),
        (
            "http://a:1, http://pin:9 ,http://b:2",
            "http://pin:9",
            "http://a:1,http://pin:9,http://b:2",
        ),
        (
            "http://a:1,http://a:1,http://b:2",
            "http://b:2",
            "http://a:1,http://b:2",
        ),
        (
            "http://100.77.130.5:8080,http://100.78.181.90:8080",
            "http://100.77.130.5:8080",
            "http://100.77.130.5:8080,http://100.78.181.90:8080",
        ),
    ],
)
def test_default_launch_pins_gate_worker_and_keeps_admission_list(
    tmp_path: Path, monkeypatch, previous: str, pin: str, expected: str,
):
    """The gate pin is one URL. Admission keeps the dispatcher list plus that URL."""
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", previous)
    monkeypatch.delenv("DARKFAC_ADMISSION_WORKERS", raising=False)
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop")
    env = _capture_launch_env(monkeypatch, tmp_path, fd.HostSpec("grokbox", 1, pin))
    assert env["DARKFAC_TEST_WORKERS"] == pin
    assert env["DARKFAC_REMOTE_HARNESS"] == "required"
    assert env["DARKFAC_FLEET_GATE_REMOTE"] == "1"
    assert env["DARKFAC_ADMISSION_WORKERS"] == expected


@pytest.mark.parametrize("previous", [None, "", "   ", ","])
def test_default_launch_admission_uses_fleet_default_when_worker_list_is_empty(
    tmp_path: Path, monkeypatch, previous: str | None,
):
    from core.harness.remote_dispatch import DEFAULT_WORKERS

    assert DEFAULT_WORKERS == "http://100.81.84.124:8080,http://100.77.130.5:8080,http://100.78.181.90:8080"
    if previous is None:
        monkeypatch.delenv("DARKFAC_TEST_WORKERS", raising=False)
    else:
        monkeypatch.setenv("DARKFAC_TEST_WORKERS", previous)
    monkeypatch.delenv("DARKFAC_ADMISSION_WORKERS", raising=False)
    pin = "http://pin:9"
    env = _capture_launch_env(monkeypatch, tmp_path, fd.HostSpec("desktop", 3, pin))
    assert env["DARKFAC_TEST_WORKERS"] == pin
    assert env["DARKFAC_REMOTE_HARNESS"] == "required"
    assert env["DARKFAC_FLEET_GATE_REMOTE"] == "1"
    assert env["DARKFAC_ADMISSION_WORKERS"] == f"{DEFAULT_WORKERS},{pin}"


def test_default_launch_notebook_does_not_replace_test_workers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://keep:1,http://keep:2")
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "required")
    monkeypatch.delenv("DARKFAC_ADMISSION_WORKERS", raising=False)
    monkeypatch.delenv("DARKFAC_FLEET_GATE_REMOTE", raising=False)
    env = _capture_launch_env(monkeypatch, tmp_path, fd.HostSpec("notebook", 2, None))
    assert env["DARKFAC_REMOTE_HARNESS"] == "off"
    assert env["DARKFAC_TEST_WORKERS"] == "http://keep:1,http://keep:2"
    assert "DARKFAC_ADMISSION_WORKERS" not in env
    assert "DARKFAC_FLEET_GATE_REMOTE" not in env

    monkeypatch.delenv("DARKFAC_TEST_WORKERS", raising=False)
    env = _capture_launch_env(monkeypatch, tmp_path, fd.HostSpec("notebook", 2, None))
    assert env["DARKFAC_REMOTE_HARNESS"] == "off"
    assert "DARKFAC_TEST_WORKERS" not in env
    assert "DARKFAC_ADMISSION_WORKERS" not in env


def test_fleet_child_gets_its_own_checkout_and_drops_the_parent_session(tmp_path: Path, monkeypatch):
    """USR-471 T2: a fleet executor does not inherit the chat that started the service."""
    from core.line.agent_cli import supports
    from core.line.line_lock import ENV_INHERITED
    from core.line.local_cancel import ENV_PARENT
    from core.line.local_progress import ENV_RUN_ID as LOCAL_RUN_ID
    from core.line.operating_harness import detect_operating_harness
    from core.line.routing import harness_cli_pin, pick
    from core.workflow.execution_registry import ENV_EXECUTION_ID

    captured: dict[str, object] = {}

    class Process:
        def poll(self):
            return None

    checkout = tmp_path / "operational-checkout"
    shared_state = tmp_path / "shared-state"
    interactive = tmp_path / "interactive-worktree"
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(shared_state))
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(interactive))
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "grok")
    monkeypatch.setenv("GROK_SESSION_ID", "parent-grok")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("ANTIGRAVITY_AGENT", "1")
    monkeypatch.setenv("DARKFAC_TICKET_ID", "USR-1")
    monkeypatch.setenv(ENV_INHERITED, "1")
    monkeypatch.setenv(ENV_PARENT, "parent-run")
    monkeypatch.setenv(LOCAL_RUN_ID, "local-parent")
    monkeypatch.setenv(ENV_EXECUTION_ID, "exec-parent")
    monkeypatch.setenv("DARKFAC_RUN_ID", "fleet-parent")
    monkeypatch.setenv("DARKFAC_RECOVERY_MODE", "1")
    monkeypatch.setenv("DARKFAC_HARNESS_WORKER_JOB", "1")
    monkeypatch.setenv("GIT_DIR", str(interactive / ".git"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-keep")
    monkeypatch.setenv("GH_TOKEN", "ghp_keep")
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop")
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        fd,
        "subprocess",
        type(
            "Subprocess",
            (),
            {"Popen": staticmethod(lambda cmd, **kwargs: captured.update(cmd=cmd, cwd=kwargs["cwd"], env=kwargs["env"]) or Process()), "STDOUT": -2},
        )(),
    )

    monkeypatch.setattr(fd, "PROJECT_ROOT", checkout)
    fd.default_launch("USR-471", fd.HostSpec("notebook", 1), {"id": "USR-471"}, candidate_digest="digest-471")

    env = captured["env"]
    assert captured["cwd"] == checkout
    assert Path(captured["cmd"][1]).parent == checkout
    assert "--fleet" in captured["cmd"]
    assert "DARKFAC_PROJECT_ROOT" not in env
    assert env["DARKFAC_STATE_ROOT"] == str(shared_state.resolve())
    assert env["DARKFAC_FLEET_MODE"] == "1"
    assert env["DARKFAC_HOST_ID"] == "notebook"
    assert env["DARKFAC_CANDIDATE_DIGEST"] == "digest-471"
    assert env["DARKFAC_REMOTE_HARNESS"] == "off"
    assert "DARKFAC_RECOVERY_MODE" not in env
    assert env["OPENAI_API_KEY"] == "sk-keep"
    assert env["GH_TOKEN"] == "ghp_keep"
    assert env["DARKFAC_FLEET_DISABLED_HOSTS"] == "desktop"
    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    leaked = {
        "GROK_SESSION_ID", "CLAUDECODE", "ANTIGRAVITY_AGENT",
        "DARKFAC_TICKET_ID", ENV_INHERITED, ENV_PARENT, LOCAL_RUN_ID, ENV_EXECUTION_ID,
        "DARKFAC_RUN_ID", "DARKFAC_HARNESS_WORKER_JOB", "GIT_DIR",
    }
    assert leaked.isdisjoint(env)
    assert detect_operating_harness(env) is None
    assert supports("antigravity", "write", execution_context="headless", operating_harness="antigravity") is False

    def quota(provider: str) -> float:
        return {"anthropic": 90.0, "openai": 40.0, "xai": 80.0, "google": 99.0}[provider]

    route = pick(
        "development",
        ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        mode="write",
        operating_harness=detect_operating_harness(env),
        execution_context="headless",
        quota_lookup=quota,
        slack_lookup=lambda _provider: None,
        cooldown_path=tmp_path / "no-cooldowns.json",
    )
    from core.models.registry import resolve
    assert route == ("claude", "sonnet")
    assert harness_cli_pin("claude", route[1], stage="development")[0] == resolve("sonnet")


def test_running_fleet_tickets_reads_live_run_ticket_processes(monkeypatch):
    import psutil

    class P:
        def __init__(self, cmd):
            self.info = {"cmdline": cmd}

    procs = [P(["python", "C:/x/run_ticket.py", "USR-1", "--fleet", "--host-id", "grokbox"]),
             P(["python", "run_ticket.py", "USR-2"]), P(["py", "run_ticket.py", "--resume-delivery", "USR-3"]), P(None)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: procs)
    assert fd.running_fleet_tickets() == {"USR-1", "USR-3"}
    assert fd.running_fleet_hosts() == {"grokbox"}


def test_failure_records_exit_code_and_reason(tmp_path: Path):
    log = tmp_path / "t.log"
    log.write_text("linha\nTraceback\nValueError: boom token=sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8")
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.d.in_flight["grokbox"].log_path = log
    e.handles["USR-01"].rc = 3
    cyc = e.d.run_cycle()
    row = cyc["recent_failures"][0]
    assert (row["ticket"], row["host"], row["exit_code"], row["kind"]) == ("USR-01", "grokbox", 3, "failure")
    assert "ValueError: boom" in row["reason"] and "sk-abcdefghijklmnopqrstuvwx" not in row["reason"]


def test_failure_without_log_still_has_reason(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.handles["USR-01"].rc = 75
    row = e.d.run_cycle()["recent_failures"][0]
    assert row["kind"] == "infra" and "exit_code=75" in row["reason"]


def test_dispatch_launches_from_project_root_with_state_root_and_no_fleet_dispatch_checkout(
    tmp_path: Path, monkeypatch
):
    """EC-02: no ephemeral `fleet-dispatch-*` clone; the shared state root travels in the child env."""
    root = tmp_path / "shared"
    (root / ".worktrees").mkdir(parents=True)
    launched = {}

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(fd, "PROJECT_ROOT", root)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(root))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(fd, "subprocess", type("Subprocess", (), {
        "Popen": staticmethod(lambda cmd, **kw: launched.update(cmd=cmd, **kw) or Process()), "STDOUT": -2,
    })())

    job = fd.default_launch("USR-413", fd.HostSpec("notebook", 1), {"id": "USR-413"})

    assert launched["cwd"] == root and Path(launched["cmd"][1]) == root / "run_ticket.py"
    assert launched["env"]["DARKFAC_STATE_ROOT"] == str((root / ".factory").resolve())
    assert "DARKFAC_PROJECT_ROOT" not in launched["env"]
    assert list((root / ".worktrees").glob("fleet-dispatch-*")) == []
    assert job.ticket_id == "USR-413"
    import json

    handed = json.loads(Path(launched["env"]["DARKFAC_DISPATCH_TICKET"]).read_text(encoding="utf-8"))
    assert handed == {"id": "USR-413"}  # the selected record travels with the child


def test_reap_keeps_deploy_batch_after_job_exits(tmp_path, monkeypatch):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.handles["USR-01"].rc = 0
    e.mark_delivered("USR-01")

    e.d.run_cycle()
    assert e.d.pending_deploy and e.d.deploys == 0
    e.now += e.d.config.deploy_window_sec + 1
    e.d.run_cycle()
    assert e.d.deploys == 1


def test_normalize_error_preserves_ticket_causes():
    from core.line.admission_store import normalize_error

    for cause in ("gate_failed", "gate_dirty_worktree", "fix_agent_no_changes"):
        assert normalize_error(cause) == cause
    assert fd.classify_log_cause('"cause": "infra"\n[ERRO NA ENTREGA] causa: gate_failed; erro: x') == "gate_failed"


@pytest.mark.parametrize("cause", ["gate_failed", "gate_timeout", "ci_red_twice"])
def test_official_delivery_cause_ignores_gate_tail(tmp_path, cause):
    e = Env(tmp_path, [])
    nested = "infra" if cause == "gate_failed" else "gate_failed"
    for _ in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest="patch", context_digest="ctx")
        e.d._record_failure(job, 1, "failure",
                            f'[ERRO NA ENTREGA] causa: {cause}; erro: x\n{{"cause": "{nested}"}}')
    store = e.d.admission_store
    assert not store.recovery_state()["active"]  # falha repetida nunca serializa a frota
    assert [h["ticket_id"] for h in store.ticket_holds()] == ["USR-01"]
    # EC-02: the same candidate timing out the gate repeatedly holds with its own cause instead of looping.
    expected = "gate_timeout_repeated" if cause == "gate_timeout" else cause
    if cause in ("gate_failed", "gate_timeout"):
        assert store.ticket_holds()[0]["cause"] == expected


def test_fleet_reservation_refusal_counts_as_lock_not_failure(tmp_path):
    text = "[RESERVA DE FROTA] Fleet capacity ceiling of 1 active cycles reached"
    assert fd.is_lock_refusal(text) and fd.is_infra_failure(2, text)
    active_pr = "[RECUSA] Ticket USR-494 ja possui PR ativo #830: https://github.com/x/y/pull/830"
    assert fd.is_lock_refusal(active_pr) and fd.is_infra_failure(2, active_pr)
    assert not fd.is_lock_refusal("[RECUSA] pull_request_lookup_unavailable")
    e = Env(tmp_path, [])
    for _ in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest="patch", context_digest="ctx")
        kind = "lock" if fd.is_lock_refusal(text) else "failure"
        e.d._record_failure(job, 2, kind, text)
    assert not e.d.admission_store.recovery_state()["active"]


def test_lock_refusals_do_not_activate_recovery(tmp_path):
    e = Env(tmp_path, [])
    for _ in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest="patch", context_digest="ctx")
        e.d._record_failure(job, 2, "lock", "canonical_ownership_conflict")
    assert not e.d.admission_store.recovery_state()["active"]
    assert len(e.d.failures) == 3


@pytest.mark.parametrize("blocked", ["ticket_hold:gate_failed", "backoff_infra"])
def test_recovery_tries_next_ticket_when_priority_is_blocked(tmp_path, monkeypatch, blocked):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02"), _d("USR-03")])
    e.d.config.recovery_tickets = ("USR-01", "USR-02")
    e.d.admission_store.set_recovery(active=True, cause="infra", fingerprint="fp")
    dispatch = e.d._dispatch_ticket
    def attempt(host, probe, tid, demand, queue, idle, skipped, dispatched, now, reservation_state):
        if tid == "USR-01":
            skipped[tid] = blocked
            return
        return dispatch(host, probe, tid, demand, queue, idle, skipped, dispatched, now, reservation_state)
    monkeypatch.setattr(e.d, "_dispatch_ticket", attempt)
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-02", "notebook")]
    assert cycle["skipped"]["USR-01"] == blocked


_SHA_A = "a" * 40
_SHA_B = "b" * 40


class _ShaSeq:
    def __init__(self, values: list[object]) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> object:
        index = min(self.calls, len(self._values) - 1)
        self.calls += 1
        return self._values[index]


class _LiveHandle:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def poll(self) -> None:
        self.calls.append("poll")
        return None

    def kill(self) -> None:
        self.calls.append("kill")

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, *_args: object, **_kwargs: object) -> int:
        self.calls.append("wait")
        return 0


def _no_dispatch(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("o reinicio nao despacha ticket")


_KEEP_LAUNCH = object()


def _sha_dispatcher(
    tmp_path: Path,
    reader: object,
    *,
    dry_run: bool = False,
    launch_argv: list[str] | None = None,
    exec_fn: object | None = None,
    launch: object = _KEEP_LAUNCH,
    digest_hook: object | None = None,
    sentinel_hook: object | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    execs: list[tuple[str, list[str]]] = []

    def record(executable: str, argv: list[str]) -> None:
        execs.append((executable, list(argv)))

    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(hosts=HOSTS[:1], state_path=tmp_path / "state.json", cycle_sec=0, deploy_window_sec=100),
        demands_loader=list,
        probe=lambda _host: fd.HostProbe(False, "ocupado"),
        launch=_no_dispatch if launch is _KEEP_LAUNCH else launch,
        deploy=lambda: 0,
        clock=lambda: 0.0,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        dry_run=dry_run,
        digest_hook=digest_hook,
        sentinel_hook=sentinel_hook,
        code_sha_reader=reader,
        exec_fn=exec_fn or record,
        launch_argv=["--loop"] if launch_argv is None else launch_argv,
    )
    return dispatcher, execs


def _stop_after(limit: int):
    state = {"n": 0}

    def stop() -> bool:
        state["n"] += 1
        return state["n"] > limit

    return stop


def _one_cycle(dispatcher):
    seen: list[dict] = []
    original = dispatcher.run_cycle

    def wrapped():
        seen.append(original())
        return seen[-1]

    dispatcher.run_cycle = wrapped
    dispatcher.run_forever(stop=_stop_after(1))
    assert seen, "o ciclo nao rodou"
    return seen[0]


def _plant_live_work(dispatcher, tmp_path: Path, monkeypatch):
    from core.line import pending_delivery as pending

    handle = _LiveHandle()
    child = _LiveHandle()
    dispatcher.in_flight["grokbox"] = fd.InFlight("USR-508", "grokbox", handle)
    pending._children[508508] = child
    return handle, child, pending


def _assert_live_work_untouched(handle, child) -> None:
    for actor in (handle, child):
        assert "kill" not in actor.calls
        assert "terminate" not in actor.calls
        assert "wait" not in actor.calls


def test_changed_checkout_sha_reexecs_before_cycle_and_leaves_live_work(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    dispatcher, execs = _sha_dispatcher(
        tmp_path, _ShaSeq([_SHA_A, _SHA_B]), launch_argv=["--loop", "--resume-only"],
    )
    handle, child, pending = _plant_live_work(dispatcher, tmp_path, monkeypatch)
    cycles: list[str] = []
    dispatcher.run_cycle = lambda: cycles.append("cycle")
    try:
        dispatcher.run_forever(stop=_stop_after(1))
    finally:
        pending._children.pop(508508, None)
    assert cycles == []
    assert execs == [(
        sys.executable,
        [sys.executable, "-m", "core.line.fleet_dispatcher", "--loop", "--resume-only"],
    )]
    assert fd.IMPORT_RESTART_SHA_ENV not in os.environ
    _assert_live_work_untouched(handle, child)
    assert dispatcher.in_flight["grokbox"].handle is handle


def test_exec_failure_is_logged_and_the_loop_continues(tmp_path: Path, monkeypatch, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="darkfac.line.fleet_dispatcher")

    def boom(_executable: str, _argv: list[str]) -> None:
        raise OSError("exec recusado")

    dispatcher, _execs = _sha_dispatcher(tmp_path, _ShaSeq([_SHA_A, _SHA_B]), exec_fn=boom)
    handle, child, pending = _plant_live_work(dispatcher, tmp_path, monkeypatch)
    try:
        cycle = _one_cycle(dispatcher)
    finally:
        pending._children.pop(508508, None)
    assert "exec recusado" in caplog.text
    assert "reinicio do despachante falhou" in caplog.text
    assert cycle["code_sha"] == _SHA_B
    assert cycle["code_sha_changed"] is True
    _assert_live_work_untouched(handle, child)


@pytest.mark.parametrize("startup,later", [(_SHA_A, _SHA_A), ("A" * 40, "a" * 40)])
def test_equal_checkout_sha_does_not_replace_and_cycle_exposes_it(tmp_path: Path, startup: str, later: str) -> None:
    dispatcher, execs = _sha_dispatcher(tmp_path, _ShaSeq([startup, later]))
    cycle = _one_cycle(dispatcher)
    assert execs == []
    assert cycle["code_sha"] == _SHA_A
    assert cycle["code_sha_changed"] is False


@pytest.mark.parametrize(
    "startup,later,exposed",
    [
        (None, None, None),
        ("abc", "def", None),
        (_SHA_A, None, None),
        (None, _SHA_B, _SHA_B),
        (_SHA_A, "abc", None),
        ("abc", _SHA_B, _SHA_B),
        ("", _SHA_B, _SHA_B),
    ],
)
def test_unreadable_checkout_sha_does_not_replace(
    tmp_path: Path, startup: object, later: object, exposed: str | None,
) -> None:
    dispatcher, execs = _sha_dispatcher(tmp_path, _ShaSeq([startup, later]))
    cycle = _one_cycle(dispatcher)
    assert execs == []
    assert cycle["code_sha"] == exposed
    assert cycle["code_sha_changed"] is False


def test_dry_run_and_once_expose_sha_change_without_replacing(tmp_path: Path) -> None:
    dry, dry_execs = _sha_dispatcher(tmp_path / "dry", _ShaSeq([_SHA_A, _SHA_B]), dry_run=True)
    dry_cycle = _one_cycle(dry)
    assert dry_execs == []
    assert dry_cycle["code_sha"] == _SHA_B
    assert dry_cycle["code_sha_changed"] is True

    once, once_execs = _sha_dispatcher(tmp_path / "once", _ShaSeq([_SHA_A, _SHA_B]))
    once_cycle = once.run_cycle()
    assert once_execs == []
    assert once_cycle["code_sha"] == _SHA_B
    assert once_cycle["code_sha_changed"] is True

    resume, resume_execs = _sha_dispatcher(tmp_path / "resume", _ShaSeq([_SHA_A, _SHA_B]))
    resume.config.resume_only = True
    resume_cycle = resume.run_cycle()
    assert resume_execs == []
    assert resume_cycle["mode"] == "resume_only"
    assert resume_cycle["code_sha"] == _SHA_B
    assert resume_cycle["code_sha_changed"] is True


def test_reader_error_counts_as_unreadable_sha(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.ERROR, logger="darkfac.line.fleet_dispatcher")

    class _BoomAfterStart:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> str:
            self.calls += 1
            if self.calls == 1:
                return _SHA_A
            raise OSError("git explodiu")

    dispatcher, execs = _sha_dispatcher(tmp_path, _BoomAfterStart())
    cycle = _one_cycle(dispatcher)
    assert execs == []
    assert cycle["code_sha"] is None
    assert cycle["code_sha_changed"] is False
    assert "git explodiu" in caplog.text


def test_sha_comparison_uses_project_root_head_without_env_or_origin_main(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake(root: Path, **kwargs: object) -> str:
        calls.append({"root": root, "use_env": kwargs.get("use_env"), "env": kwargs.get("env")})
        return _SHA_A

    commands: list[object] = []
    real_run = fd.subprocess.run

    def spy(cmd: object, *args: object, **kwargs: object):
        commands.append(cmd)
        return real_run(cmd, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fd, "current_git_sha", fake)
    monkeypatch.setattr(fd.subprocess, "run", spy)
    monkeypatch.setenv("DARKFAC_GIT_SHA", _SHA_B)
    dispatcher = fd.FleetDispatcher(
        fd.DispatcherConfig(hosts=HOSTS[:1], state_path=tmp_path / "state.json", cycle_sec=0),
        demands_loader=list,
        probe=lambda _host: fd.HostProbe(False, "ocupado"),
        launch=_no_dispatch,
        deploy=lambda: 0,
        clock=lambda: 0.0,
        admission_store=AdmissionStore(tmp_path / "admission.sqlite3"),
        digest_hook=None,
        sentinel_hook=None,
        exec_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("exec inesperado")),
        launch_argv=["--loop"],
    )
    cycle = _one_cycle(dispatcher)
    assert calls
    assert all(row["root"] == fd.PROJECT_ROOT and row["use_env"] is False and row["env"] is None for row in calls)
    assert cycle["code_sha"] == _SHA_A
    assert cycle["code_sha_changed"] is False
    assert all(cmd == ["git", "rev-parse", "origin/main"] for cmd in commands)


def _plant_wait(directory: Path, *, status: str = "running", pid: int | None = 4242):
    from core.line import pending_delivery as pending

    directory.mkdir(parents=True, exist_ok=True)
    row = pending.PendingDelivery(
        ticket_id="USR-447",
        worktree="C:/dev/DarkFac/wt-usr-447",
        stage="development",
        status=status,
        pid=pid,
        started_at=10.0,
        cause="route_wait",
        next_check=0,
    )
    path = directory / "USR-447.json"
    pending._save(path, row)
    return pending, path


def test_failed_delivery_child_is_saved_before_retry_and_retry_keeps_delivery_mode(
    tmp_path: Path, monkeypatch,
) -> None:
    from types import SimpleNamespace

    from core.line import pending_delivery as pending

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    worktree = repo_root / "ticket-worktree"
    worktree.mkdir()
    directory = tmp_path / "pending"
    directory.mkdir()
    path = directory / "USR-447.json"
    evidence_log = path.with_suffix(".log")
    evidence_log.write_text("CI failed on candidate\n", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    outcome_path = pending.run_outcome.outcome_path("USR-447")
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    outcome_path.write_text("{}", encoding="utf-8")
    previous_failure = pending.PendingChildFailure(
        run_id="older-delivery", attempt=3, exit_code=1, cause="ci_red", cause_class="candidate",
        stage="review", detail="older CI failure", log_path=str(evidence_log),
        outcome_path=str(outcome_path), recorded_at="2026-11-05T00:00:00+00:00",
    )
    row = pending.PendingDelivery(
        ticket_id="USR-447", worktree=str(worktree), stage="development", resume_delivery=False,
        run_id="child-delivery-1", status="running", pid=4242, started_at=10.0,
        child_failures=[previous_failure],
    )
    pending._save(path, row)
    outcome = SimpleNamespace(
        exit_code=1, cause="ci_red", cause_class=SimpleNamespace(value="candidate"),
        stage="gate", detail="CI failed on candidate",
    )
    monkeypatch.setattr(pending.run_outcome, "read_outcome", lambda *_args: outcome)
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda *_args: "a" * 40)
    observed: dict[str, object] = {}

    class _Budget:
        def record_attempt(self, *_args, **_kwargs):
            saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
            assert saved.child_failures[-1].run_id == "child-delivery-1"
            assert saved.child_failures[-1].attempt == 4
            assert saved.child_failures[-1].log_path == str(evidence_log.resolve())
            assert saved.child_failures[-1].outcome_path == str(outcome_path.resolve())
            assert saved.resume_delivery is True
            observed["persisted_before_accounting"] = True
            return {"next_attempt_at": 2000.0}

        def ticket_hold(self, **_kwargs):
            return False

        def budget(self, *_args, **_kwargs):
            return {"main_sha": "", "infra_streak": 0}

        def retry_reason(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(pending, "_budget_store", lambda _now: _Budget())
    routed: list[tuple[str, bool]] = []

    def route(row):
        routed.append((row.stage, row.resume_delivery))
        return ("grok", "model")

    monkeypatch.setattr(pending, "_route", route)
    monkeypatch.setattr(pending, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(pending, "_ticket_status", lambda _ticket: "completed")
    import core.line.ticket_engine as ticket_engine
    monkeypatch.setattr(ticket_engine, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda *_args: "")

    class _ControlStore:
        mock_mode = False

        def list_tickets(self):
            return [{"id": "USR-447", "status": "completed"}]

    import core.demands.queue_store as queue_store
    import core.git.ticket_workspace as ticket_workspace
    import core.line.line_lock as line_lock

    monkeypatch.setattr(queue_store, "control_store", lambda: _ControlStore())
    monkeypatch.setattr(ticket_workspace, "list_worktrees", lambda _root: [
        {"worktree": str(worktree), "branch": "ticket/usr-447"},
    ])
    monkeypatch.setattr(line_lock, "is_locked", lambda: False)
    launched: dict[str, object] = {}

    class _Child:
        pid = 9898

    def fake_popen(command, **kwargs):
        launched["command"] = command
        return _Child()

    monkeypatch.setattr(pending.subprocess, "Popen", fake_popen)
    result = pending._child_finished(path, row, 1, repo_root, 1000.0)
    assert result["status"] == "waiting"
    assert observed["persisted_before_accounting"] is True
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.child_failures[-1].cause == "ci_red"
    assert saved.child_failures[-1].stage == "gate"
    assert saved.child_failures[-1].detail == "CI failed on candidate"
    assert saved.child_failures[-1].attempt == 4
    assert saved.child_failures[-1].log_path == str(evidence_log.resolve())
    assert saved.child_failures[-1].outcome_path == str(outcome_path.resolve())
    assert saved.resume_delivery is True
    assert saved.stage == "review"

    # A fresh dispatcher process reads only the persisted row; the completed build stays intact.
    implementation = worktree / "implementation.py"
    implementation.write_text("preserved = True\n", encoding="utf-8")
    implementation_before = implementation.read_bytes()
    saved.next_check = 0
    pending._save(path, saved)
    pending._tick_row(path, repo_root, 1001.0)
    restored = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored.stage == "review"
    assert restored.resume_delivery is True
    assert restored.child_failures[-1].attempt == 4
    assert routed == [("review", True)]
    assert implementation.read_bytes() == implementation_before
    command = launched["command"]
    assert command[-4:] == ["--resume-delivery", "USR-447", "--worktree", str(worktree)]


def test_failed_delivery_child_is_not_retried_when_failure_checkpoint_cannot_be_published(
    tmp_path: Path, monkeypatch,
) -> None:
    from types import SimpleNamespace

    from core.line import pending_delivery as pending

    path = tmp_path / "pending" / "USR-447.json"
    row = pending.PendingDelivery(
        ticket_id="USR-447", worktree=str(tmp_path / "worktree"), stage="review",
        run_id="delivery-attempt-1", status="running", pid=42,
    )
    pending._save(path, row)
    previous = path.read_bytes()
    monkeypatch.setattr(
        pending.run_outcome, "read_outcome",
        lambda *_args: SimpleNamespace(
            exit_code=1, cause="ci_red", cause_class=SimpleNamespace(value="candidate"),
            stage="gate", detail="CI failed",
        ),
    )
    accounting_calls: list[object] = []
    monkeypatch.setattr(pending, "_budget_store", lambda _now: accounting_calls.append(True))

    def fail_replace(*_args):
        raise OSError("simulated interrupted checkpoint publication")

    monkeypatch.setattr(pending.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated interrupted checkpoint publication"):
        pending._child_finished(path, row, 1, tmp_path, 1000.0)

    assert path.read_bytes() == previous
    assert accounting_calls == []
    assert pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8")).child_failures == []


def test_pending_delivery_blocks_a_registered_worktree_from_another_ticket(
    tmp_path: Path, monkeypatch,
) -> None:
    from core.line import pending_delivery as pending

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    directory = tmp_path / "pending"
    directory.mkdir()
    worktree = tmp_path / "registered-worktree"
    worktree.mkdir()
    path = directory / "USR-449.json"
    pending._save(path, pending.PendingDelivery(
        ticket_id="USR-449", worktree=str(worktree), stage="review", resume_delivery=True,
    ))

    class _Budget:
        def budget(self, *_args, **_kwargs):
            return {"main_sha": "", "infra_streak": 0}

        def retry_reason(self, *_args, **_kwargs):
            return None

    class _ControlStore:
        mock_mode = False

        def list_tickets(self):
            return [{"id": "USR-449", "status": "planned"}]

    monkeypatch.setattr(pending, "_budget_store", lambda _now: _Budget())
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda _repo: "")
    monkeypatch.setattr(pending, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: _ControlStore())
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    monkeypatch.setattr("core.line.ticket_engine.artifacts_root", lambda: tmp_path / "artifacts")
    monkeypatch.setattr("core.git.ticket_workspace.list_worktrees", lambda _root: [
        {"worktree": str(worktree), "branch": "ticket/usr-448"},
    ])
    monkeypatch.setattr(pending, "_route", lambda _row: ("grok", "model"))

    result = pending._tick_row(path, tmp_path / "repo", 1000.0)

    assert result["status"] == "blocked"
    assert result["cause"] == "pending_worktree_mismatch"
    restored = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored.status == "blocked"
    assert restored.cause == "pending_worktree_mismatch"


def test_schedule_migration_keeps_structured_child_failures(tmp_path: Path, monkeypatch) -> None:
    from core.line import pending_delivery as pending

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    directory = tmp_path / "pending"
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    directory.mkdir()
    failure = pending.PendingChildFailure(
        run_id="delivery-1", attempt=2, exit_code=1, cause="launcher_exit:1", cause_class="infra",
        stage="review", detail="deploy child failed", log_path="/tmp/delivery.log",
        outcome_path="/tmp/outcome.json", recorded_at="2026-11-06T00:00:00+00:00",
    )
    path = directory / "USR-447.json"
    pending._save(path, pending.PendingDelivery(
        ticket_id="USR-447", worktree=str(worktree), stage="review", resume_delivery=True,
        child_failures=[failure],
    ))

    resumed = pending.schedule(
        "USR-447", worktree, "review", resume_delivery=True,
        cause="route_wait", directory=directory,
    )

    assert resumed.child_failures == [failure]
    assert pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8")).child_failures == [failure]


def test_legacy_pending_delivery_defaults_child_failures_without_rewriting_state(tmp_path: Path) -> None:
    from core.line.pending_delivery import PendingDelivery

    path = tmp_path / "USR-448.json"
    path.write_text(
        '{"ticket_id":"USR-448","worktree":"/wt/usr-448","stage":"review",'
        '"resume_delivery":true,"next_check":0}',
        encoding="utf-8",
    )
    before = path.read_bytes()

    restored = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))

    assert restored.ticket_id == "USR-448"
    assert restored.child_failures == []
    assert restored.status == "waiting"
    assert path.read_bytes() == before


def _raise_from_tick(monkeypatch, directory: Path, exc: BaseException, *, mutate: bool):
    pending, path = _plant_wait(directory)
    before = path.read_bytes()

    def boom(file_path: Path, _repo: object, _now: object) -> dict:
        if mutate:
            current = pending.PendingDelivery.model_validate_json(file_path.read_text(encoding="utf-8"))
            current.status = "blocked"
            current.pid = None
            current.cause = "mutated"
            pending._save(file_path, current)
        raise exc

    monkeypatch.setattr(pending, "_tick_row", boom)
    return pending, path, before


@pytest.mark.parametrize(
    ("exc", "missing"),
    [
        (ImportError("cannot import name 'fresh'", name="core.line.routing"), "core.line.routing"),
        (ModuleNotFoundError("No module named 'core.line.fresh'", name="core.line.fresh"), "core.line.fresh"),
        (ImportError("cannot import name 'fresh'"), None),
        (ModuleNotFoundError("No module named 'core.line.fresh'"), None),
    ],
)
def test_import_error_in_tick_is_stale_cause_and_keeps_wait_json(
    tmp_path: Path, monkeypatch, exc: ImportError, missing: str | None,
) -> None:
    pending, path, before = _raise_from_tick(monkeypatch, tmp_path / "pending", exc, mutate=True)
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=0)[0]
    assert result["status"] == "check_failed"
    assert result["cause"] == pending.STALE_DISPATCHER_IMPORT
    if missing is None:
        assert "missing" not in result
        assert not exc.name
    else:
        assert result["missing"] == exc.name == missing
    assert path.read_bytes() == before
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "running"
    assert saved.pid == 4242


def test_real_importerror_name_is_the_missing_field(tmp_path: Path, monkeypatch) -> None:
    try:
        from core.line.fleet_dispatcher import _usr508_symbol_absent  # type: ignore[attr-defined]  # noqa: F401
    except ImportError as exc:
        caught = exc
    else:
        raise AssertionError("o simbolo ausente deveria falhar")
    pending, path, before = _raise_from_tick(monkeypatch, tmp_path / "pending", caught, mutate=True)
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=0)[0]
    assert result["cause"] == "stale_dispatcher_import"
    assert result["status"] == "check_failed"
    assert caught.name
    assert result["missing"] == caught.name
    assert path.read_bytes() == before
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "running"
    assert saved.pid == 4242


@pytest.mark.parametrize("exc", [RuntimeError("store down"), OSError("disk"), ValueError("bad row")])
def test_other_tick_exceptions_keep_the_class_name(tmp_path: Path, monkeypatch, exc: Exception) -> None:
    pending, path, before = _raise_from_tick(monkeypatch, tmp_path / "pending", exc, mutate=True)
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=0)[0]
    assert result == {"ticket": "USR-447", "status": "check_failed", "cause": type(exc).__name__}
    assert path.read_bytes() != before
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "blocked"
    assert saved.pid is None
    assert saved.cause == "mutated"


def _track_cycle(dispatcher, events: list[str]):
    original = dispatcher.run_cycle
    seen: list[dict] = []

    def wrapped():
        events.append("cycle")
        cycle = original()
        seen.append(cycle)
        return cycle

    dispatcher.run_cycle = wrapped
    original_exec = dispatcher._exec_fn

    def tracking(executable: str, argv: list[str]):
        events.append("exec")
        return original_exec(executable, argv)

    dispatcher._exec_fn = tracking
    return seen


def test_stale_import_restarts_once_after_cycle_when_head_is_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    from core.line import pending_delivery as pending

    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    directory = tmp_path / "pending"
    _pending, path, before = _raise_from_tick(
        monkeypatch, directory,
        ImportError("cannot import name 'fresh'", name="core.line.routing"),
        mutate=True,
    )
    monkeypatch.setattr(pending, "queue_dir", lambda: directory)
    events: list[str] = []
    dispatcher, execs = _sha_dispatcher(
        tmp_path / "state",
        lambda: _SHA_A,
        launch=None,
        digest_hook=lambda: events.append("digest"),
        sentinel_hook=lambda: events.append("sentinel"),
    )
    seen = _track_cycle(dispatcher, events)
    monkeypatch.setattr(fd.time, "sleep", _main_thread_sleep(lambda _seconds: events.append("sleep")))
    handle, child, planted = _plant_live_work(dispatcher, tmp_path, monkeypatch)
    try:
        dispatcher.run_forever(stop=_stop_after(1))
        assert os.environ[fd.IMPORT_RESTART_SHA_ENV] == dispatcher._startup_sha == _SHA_A
    finally:
        os.environ.pop(fd.IMPORT_RESTART_SHA_ENV, None)
        planted._children.pop(508508, None)
    assert events == ["cycle", "exec"]
    assert execs == [(
        sys.executable,
        [sys.executable, "-m", "core.line.fleet_dispatcher", "--loop"],
    )]
    cycle = seen[0]
    assert cycle["code_sha"] == _SHA_A
    assert cycle["code_sha_changed"] is False
    assert cycle["resumptions"][0]["status"] == "check_failed"
    assert cycle["resumptions"][0]["cause"] == "stale_dispatcher_import"
    assert cycle["resumptions"][0]["missing"] == "core.line.routing"
    assert path.read_bytes() == before
    _assert_live_work_untouched(handle, child)


def test_stale_import_with_restart_sha_records_the_cause_and_sleeps(
    tmp_path: Path, monkeypatch,
) -> None:
    from core.line import pending_delivery as pending

    monkeypatch.setenv(fd.IMPORT_RESTART_SHA_ENV, _SHA_A)
    directory = tmp_path / "pending"
    _pending, path, before = _raise_from_tick(
        monkeypatch, directory,
        ModuleNotFoundError("No module named 'core.line.fresh'", name="core.line.fresh"),
        mutate=True,
    )
    monkeypatch.setattr(pending, "queue_dir", lambda: directory)
    events: list[str] = []
    dispatcher, execs = _sha_dispatcher(
        tmp_path / "state",
        lambda: _SHA_A,
        launch=None,
        digest_hook=lambda: events.append("digest"),
        sentinel_hook=lambda: events.append("sentinel"),
    )
    seen = _track_cycle(dispatcher, events)
    monkeypatch.setattr(fd.time, "sleep", _main_thread_sleep(lambda seconds: events.append(f"sleep:{seconds}")))
    dispatcher.run_forever(stop=_stop_after(1))
    assert execs == []
    assert events == ["cycle", "digest", "sentinel", "sleep:0"]
    cycle = seen[0]
    assert cycle["code_sha"] == _SHA_A
    assert cycle["code_sha_changed"] is False
    assert cycle["resumptions"][0]["cause"] == "stale_dispatcher_import"
    assert cycle["resumptions"][0]["missing"] == "core.line.fresh"
    assert cycle["resumptions"][0]["status"] == "check_failed"
    assert path.read_bytes() == before
    assert os.environ[fd.IMPORT_RESTART_SHA_ENV] == _SHA_A


def test_run_forever_keeps_dispatching_and_reloads_during_worktree_cleanup(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[str] = []
    dispatcher, execs = _sha_dispatcher(tmp_path / "state", lambda: _SHA_A)
    dispatcher.run_cycle = lambda: events.append("cycle") or {}
    from core.git import orphan_worktree_cleanup

    monkeypatch.setattr(orphan_worktree_cleanup, "schedule_due", lambda *_args: True)

    class Maintenance:
        def poll(self) -> None:
            events.append("still_running")

    def launch(command, **_kwargs):
        assert command[-1] == "--scheduled"
        events.append("cleanup_started")
        return Maintenance()

    monkeypatch.setattr(fd.subprocess, "Popen", launch)
    dispatcher.orphan_worktree_cleanup_hook = fd.scheduled_cleanup_hook(tmp_path)
    monkeypatch.setattr(fd.time, "sleep", lambda _seconds: events.append("sleep"))
    dispatcher._read_checkout_sha = lambda: _SHA_B if events.count("sleep") == 2 else _SHA_A

    dispatcher.run_forever(stop=_stop_after(3))

    assert events == ["cycle", "cleanup_started", "sleep", "cycle", "still_running", "sleep"]
    assert len(execs) == 1


def test_scheduled_cleanup_hook_does_not_relaunch_during_persisted_backoff(
    tmp_path: Path, monkeypatch,
) -> None:
    from core.git.orphan_worktree_cleanup import SCHEDULE_STATE

    monkeypatch.setattr(fd, "PROJECT_ROOT", tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    launches: list[list[str]] = []
    state_path = tmp_path / SCHEDULE_STATE

    class FailedMaintenance:
        def poll(self) -> int:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                '{"status":"failed","last_attempt_at":"2026-11-23T00:00:00+00:00",'
                '"failure_count":1,"next_attempt_at":"2099-01-01T00:00:00+00:00",'
                '"last_failure":"synthetic failure"}',
                encoding="utf-8",
            )
            return 1

    def launch(command, **_kwargs):
        launches.append(command)
        return FailedMaintenance()

    monkeypatch.setattr(fd.subprocess, "Popen", launch)
    hook = fd.scheduled_cleanup_hook(log_dir)
    hook()
    hook()
    hook()

    assert len(launches) == 1


def test_checkout_sha_change_with_launch_absent_does_not_set_import_restart_sha(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    events: list[str] = []
    dispatcher, execs = _sha_dispatcher(tmp_path, _ShaSeq([_SHA_A, _SHA_B]), launch=None)
    dispatcher.run_cycle = lambda: events.append("cycle")
    dispatcher.digest_hook = lambda: events.append("digest")
    dispatcher.sentinel_hook = lambda: events.append("sentinel")
    dispatcher.run_forever(stop=_stop_after(1))
    assert events == []
    assert execs == [(
        sys.executable,
        [sys.executable, "-m", "core.line.fleet_dispatcher", "--loop"],
    )]
    assert fd.IMPORT_RESTART_SHA_ENV not in os.environ


def test_stale_import_does_not_restart_in_dry_run_or_when_launch_is_present(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    monkeypatch.setattr(fd.time, "sleep", lambda _seconds: None)
    forged = {"resumptions": [{"ticket": "USR-447", "status": "check_failed", "cause": "stale_dispatcher_import"}]}

    dry, dry_execs = _sha_dispatcher(tmp_path / "dry", lambda: _SHA_A, dry_run=True, launch=None)
    dry.run_cycle = lambda: forged
    dry.run_forever(stop=_stop_after(1))
    assert dry_execs == []

    launched, launched_execs = _sha_dispatcher(tmp_path / "launch", lambda: _SHA_A)
    launched.run_cycle = lambda: forged
    launched.run_forever(stop=_stop_after(1))
    assert launched_execs == []
    assert fd.IMPORT_RESTART_SHA_ENV not in os.environ


def test_stale_import_exec_failure_is_logged_and_does_not_exec_twice(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    from core.line import pending_delivery as pending

    caplog.set_level(logging.ERROR, logger="darkfac.line.fleet_dispatcher")
    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    directory = tmp_path / "pending"
    _pending, path, before = _raise_from_tick(
        monkeypatch, directory, ImportError("cannot import name 'fresh'", name="core.line.routing"), mutate=True,
    )
    monkeypatch.setattr(pending, "queue_dir", lambda: directory)
    events: list[str] = []

    def boom(_executable: str, _argv: list[str]) -> None:
        raise OSError("exec recusado")

    dispatcher, execs = _sha_dispatcher(
        tmp_path / "state",
        lambda: _SHA_A,
        launch=None,
        exec_fn=boom,
        digest_hook=lambda: events.append("digest"),
        sentinel_hook=lambda: events.append("sentinel"),
    )
    seen = _track_cycle(dispatcher, events)
    monkeypatch.setattr(fd.time, "sleep", _main_thread_sleep(lambda seconds: events.append(f"sleep:{seconds}")))
    try:
        dispatcher.run_forever(stop=_stop_after(2))
        assert os.environ[fd.IMPORT_RESTART_SHA_ENV] == _SHA_A
    finally:
        os.environ.pop(fd.IMPORT_RESTART_SHA_ENV, None)
    assert execs == []
    assert events == [
        "cycle", "exec", "digest", "sentinel", "sleep:0",
        "cycle", "digest", "sentinel", "sleep:0",
    ]
    assert seen[0]["resumptions"][0]["cause"] == "stale_dispatcher_import"
    assert seen[1]["resumptions"][0]["cause"] == "stale_dispatcher_import"
    assert seen[0]["code_sha_changed"] is False
    assert path.read_bytes() == before
    assert "exec recusado" in caplog.text
    assert "reinicio do despachante falhou apos import obsoleto" in caplog.text


def test_stale_import_without_complete_sha_does_not_replace(tmp_path: Path, monkeypatch) -> None:
    from core.line import pending_delivery as pending

    monkeypatch.delenv(fd.IMPORT_RESTART_SHA_ENV, raising=False)
    directory = tmp_path / "pending"
    _raise_from_tick(
        monkeypatch, directory, ImportError("cannot import name 'fresh'", name="core.line.routing"), mutate=False,
    )
    monkeypatch.setattr(pending, "queue_dir", lambda: directory)
    events: list[str] = []
    dispatcher, execs = _sha_dispatcher(
        tmp_path / "state",
        _ShaSeq([_SHA_A, _SHA_A, None]),
        launch=None,
        digest_hook=lambda: events.append("digest"),
        sentinel_hook=lambda: events.append("sentinel"),
    )
    seen = _track_cycle(dispatcher, events)
    monkeypatch.setattr(fd.time, "sleep", _main_thread_sleep(lambda seconds: events.append(f"sleep:{seconds}")))
    dispatcher.run_forever(stop=_stop_after(1))
    assert execs == []
    assert events == ["cycle", "digest", "sentinel", "sleep:0"]
    assert seen[0]["resumptions"][0]["cause"] == "stale_dispatcher_import"
    assert seen[0]["code_sha"] is None
    assert fd.IMPORT_RESTART_SHA_ENV not in os.environ


def test_main_forwards_original_argv_and_once_skips_the_loop(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Dispatcher:
        def __init__(self, _config: object, **kwargs: object) -> None:
            captured["kwargs"] = kwargs

        def run_cycle(self) -> dict[str, bool]:
            captured["cycle"] = True
            return {"ok": True}

        def run_forever(self) -> None:
            captured["forever"] = True

    monkeypatch.setattr(fd, "FleetDispatcher", Dispatcher)
    monkeypatch.setattr(fd, "configure_logging", lambda *_a, **_k: None)

    assert fd.main(["--loop", "--cycle-sec", "7", "--resume-only"]) == 0
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["launch_argv"] == ["--loop", "--cycle-sec", "7", "--resume-only"]
    assert captured.get("forever") is True
    assert "cycle" not in captured

    captured.clear()
    assert fd.main(["--once", "--dry-run", "--loop"]) == 0
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["launch_argv"] == ["--once", "--dry-run", "--loop"]
    assert kwargs["dry_run"] is True
    assert captured.get("cycle") is True
    assert "forever" not in captured


@pytest.fixture(autouse=True)
def _isolate_budget_notifications(monkeypatch):
    monkeypatch.setattr("core.line.failure_alerts.notify_ticket_hold", lambda *args: None)
    monkeypatch.setattr("core.line.failure_alerts.notify_deterministic_failure", lambda *args, **kwargs: False)


def _bind_review_tick(monkeypatch, tmp_path: Path, worktree: Path, ticket_id: str):
    """Isolated dispatcher tick: durable queue, registered worktree, captured launcher."""
    import json
    from types import SimpleNamespace

    from core.line import pending_delivery as pending

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(pending, "_children", {})
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda _repo: "")
    monkeypatch.setattr("core.line.line_lock.is_locked", lambda: False)
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: SimpleNamespace(
        mock_mode=False, list_tickets=lambda: [{"id": ticket_id, "status": "planned"}],
    ))
    monkeypatch.setattr(
        "core.git.ticket_workspace.list_worktrees",
        lambda _root: [{"worktree": str(worktree), "branch": f"refs/heads/ticket/{ticket_id.lower()}"}],
    )
    calls: list[list[str]] = []
    handle = SimpleNamespace(pid=515151, poll=lambda: None)
    monkeypatch.setattr(pending.subprocess, "Popen", lambda cmd, **_kw: calls.append(list(cmd)) or handle)
    worktree.mkdir(parents=True, exist_ok=True)
    return pending, calls, json


def _save_review_wait(pending, directory: Path, worktree: Path, ticket_id: str, **fields: object):
    row = pending.PendingDelivery(
        ticket_id=ticket_id,
        worktree=str(worktree),
        stage=fields.get("stage", "review"),
        status=fields.get("status", "blocked"),
        implementing_harness=fields.get("harness", "grok"),
        resume_delivery=bool(fields.get("resume_delivery", True)),
        cause=str(fields["cause"]),
        next_check=float(fields.get("next_check", 0)),
        pid=fields.get("pid"),
        started_at=float(fields.get("started_at", 0)),
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket_id}.json"
    pending._save(path, row)
    return path


def _plant_current_gate(tmp_path: Path, ticket_id: str, digest: str, *, passed: bool = True) -> Path:
    context = (
        tmp_path / "state" / "artifacts" / "ticket_runs" / ticket_id / ".darkfac" / "runs" / ticket_id
    )
    context.mkdir(parents=True, exist_ok=True)
    (context / "progress.json").write_text(
        '{"harness": "grok", "tickets_done": ["' + ticket_id + '"]}\n', encoding="utf-8",
    )
    payload = {"passed": passed, "gate": "core/harness/runner.py --quick", "detail": "", "candidate_digest": digest}
    import json
    (context / "validation.json").write_text(json.dumps(payload), encoding="utf-8")
    return context


def test_resumable_review_wait_runs_when_a_compatible_route_appears(tmp_path: Path, monkeypatch) -> None:
    """A blocked REVIEW wait with a route cause is retried on the same worktree, without PLAN or BUILD."""
    worktree = tmp_path / "wt-resumable"
    ticket_id = "USR-512"
    pending, calls, _json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    directory = tmp_path / "pending"
    cause = "review_retry:no_route_available not_before=2026-11-13T01:00:00+00:00"
    path = _save_review_wait(pending, directory, worktree, ticket_id, cause=cause)
    monkeypatch.setattr(pending, "_route", lambda _row: None)
    held = pending.tick(tmp_path, directory=directory, now=10)[0]
    assert held["status"] == "blocked"
    assert held["cause"] == cause
    assert calls == []
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "blocked"
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    opened = pending.tick(tmp_path, directory=directory, now=saved.next_check + 1)[0]
    assert opened["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", ticket_id, "--worktree", str(worktree.resolve())]
    assert "--fleet" not in calls[0]
    assert "PLAN" not in calls[0] and "BUILD" not in calls[0]
    running = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert running.resume_delivery is True
    assert running.status == "running"


def test_review_agent_exhausted_reopens_without_a_new_plan(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "wt-exhausted"
    ticket_id = "USR-512A"
    pending, calls, _json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    _save_review_wait(
        pending, tmp_path / "pending", worktree, ticket_id, cause="review_agent_exhausted", resume_delivery=False,
    )
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=5)[0]
    assert result["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", ticket_id, "--worktree", str(worktree.resolve())]
    assert "PLAN" not in calls[0] and "BUILD" not in calls[0]


@pytest.mark.parametrize("cause", ["deploy_evidence_missing", "deploy_evidence_mismatch", "implementing_family_unknown"])
def test_evidence_and_unknown_family_blocks_stay_blocked(tmp_path: Path, monkeypatch, cause: str) -> None:
    worktree = tmp_path / "wt-permanent"
    ticket_id = "USR-512B"
    harness = None if cause == "implementing_family_unknown" else "grok"
    pending, calls, _json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    path = _save_review_wait(
        pending, tmp_path / "pending", worktree, ticket_id, cause=cause, harness=harness,
    )
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=5)[0]
    assert result["status"] == "blocked"
    assert calls == []
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "blocked"
    assert saved.cause == cause


@pytest.mark.parametrize(
    "case",
    ["no_validation", "gate_failed", "digest_moved", "verdict_present", "execution_present", "approval_present", "other_exit"],
)
def test_legacy_launcher_exit_stays_blocked_until_the_usr467_checks_pass(
    tmp_path: Path, monkeypatch, case: str,
) -> None:
    """launcher_exit:1 is not a general reopen. Other exits never use this path."""
    digest = "a" * 64
    worktree = tmp_path / "wt-legacy"
    ticket_id = "USR-467"
    pending, calls, json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    monkeypatch.setattr(pending, "_live_candidate_digest", lambda _worktree: digest)
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    cause = "launcher_exit:2" if case == "other_exit" else "launcher_exit:1"
    if case != "no_validation":
        context = _plant_current_gate(tmp_path, ticket_id, digest, passed=case != "gate_failed")
        if case == "digest_moved":
            monkeypatch.setattr(pending, "_live_candidate_digest", lambda _worktree: "b" * 64)
        if case == "verdict_present":
            (context / "review_verdict.json").write_text('{"verdict": "approve"}', encoding="utf-8")
        if case == "execution_present":
            (context / "review_execution.json").write_text(
                json.dumps({"candidate_digest": digest, "reviewer_family": "anthropic"}), encoding="utf-8",
            )
        if case == "approval_present":
            (context / "review_state.json").write_text(json.dumps({"rounds": [{
                "round": 1, "verdict": "approve", "candidate_digest": digest, "forced": False,
            }]}), encoding="utf-8")
    path = _save_review_wait(pending, tmp_path / "pending", worktree, ticket_id, cause=cause)
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=5)[0]
    assert result["status"] == "blocked"
    if "cause" in result:
        assert result["cause"] == cause
    assert calls == []
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.status == "blocked"
    assert saved.cause == cause


def test_legacy_launcher_exit_runs_resume_delivery_when_gate_is_current(tmp_path: Path, monkeypatch) -> None:
    digest = "c" * 64
    worktree = tmp_path / "wt-usr-467"
    ticket_id = "USR-467"
    pending, calls, json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    context = _plant_current_gate(tmp_path, ticket_id, digest)
    (context / "review_state.json").write_text('{"rounds": []}', encoding="utf-8")
    monkeypatch.setattr(pending, "_live_candidate_digest", lambda _worktree: digest)
    monkeypatch.setattr(pending, "_route", lambda _row: None)
    path = _save_review_wait(pending, tmp_path / "pending", worktree, ticket_id, cause="launcher_exit:1")
    held = pending.tick(tmp_path, directory=tmp_path / "pending", now=20)[0]
    assert held["status"] == "blocked"
    assert calls == []
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    opened = pending.tick(tmp_path, directory=tmp_path / "pending", now=saved.next_check + 1)[0]
    assert opened["status"] == "running"
    assert calls[0][2:] == ["--resume-delivery", ticket_id, "--worktree", str(worktree.resolve())]
    assert "--fleet" not in calls[0]
    assert "PLAN" not in calls[0] and "BUILD" not in calls[0]
    assert json.loads((context / "validation.json").read_text(encoding="utf-8"))["candidate_digest"] == digest
    assert json.loads((context / "progress.json").read_text(encoding="utf-8"))["harness"] == "grok"


def test_development_launcher_exit_is_not_reopened_by_the_review_rule(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "wt-dev"
    ticket_id = "USR-512C"
    pending, calls, _json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    _plant_current_gate(tmp_path, ticket_id, "d" * 64)
    monkeypatch.setattr(pending, "_live_candidate_digest", lambda _worktree: "d" * 64)
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    path = _save_review_wait(
        pending, tmp_path / "pending", worktree, ticket_id,
        cause="launcher_exit:1", stage="development", resume_delivery=False,
    )
    result = pending.tick(tmp_path, directory=tmp_path / "pending", now=5)[0]
    assert result["status"] == "blocked"
    assert calls == []
    saved = pending.PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    assert saved.cause == "launcher_exit:1"


def test_review_child_crash_without_a_known_marker_stays_blocked_and_never_reopens_blindly(
    tmp_path: Path, monkeypatch,
) -> None:
    """A classified crash cause outside the resumable markers holds; it is not the USR-467 legacy exit."""
    from types import SimpleNamespace

    from core.line.admission_store import AdmissionStore

    worktree = tmp_path / "wt-crash"
    ticket_id = "USR-467"
    pending, calls, _json = _bind_review_tick(monkeypatch, tmp_path, worktree, ticket_id)
    monkeypatch.setattr(pending.run_outcome, "main_sha", lambda _repo: "a" * 40)
    monkeypatch.setattr(pending, "_route", lambda _row: ("claude", "claude-sonnet"))
    directory = tmp_path / "pending"
    now = 100.0
    result = {}
    for index in range(3):
        pending._children[4242] = SimpleNamespace(poll=lambda: 1)
        row = pending.PendingDelivery(
            ticket_id=ticket_id, worktree=str(worktree), stage="review", status="running",
            implementing_harness="grok", pid=4242, started_at=now - 1, run_id=f"crash-{index}",
        )
        directory.mkdir(parents=True, exist_ok=True)
        pending._save(directory / f"{ticket_id}.json", row)
        result = pending.tick(tmp_path, directory=directory, now=now)[0]
        now = result["next_check"] + 1
    assert result["status"] == "blocked"
    saved = pending.PendingDelivery.model_validate_json(
        (directory / f"{ticket_id}.json").read_text(encoding="utf-8"),
    )
    assert saved.status == "blocked" and saved.cause != "launcher_exit:1"
    assert not calls
    budget = AdmissionStore(tmp_path / "state" / "admission.sqlite3").budget("darkfac", ticket_id)
    assert budget["candidate_failures"] == 3
    reopened = pending.tick(tmp_path, directory=directory, now=saved.next_check + 1)[0]
    assert reopened["status"] == "blocked"
    assert not calls


def test_deploy_exit_3_persists_redacted_deferral_idempotent_per_sha(tmp_path: Path, monkeypatch):
    import json

    from core.infra import node_sync

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "deferral-state"))
    sha = "b" * 40
    secret = "ghp_" + "A" * 30
    node_sync.record_pending_convergence(
        "notebook", sha, f"behind origin/main; push https://user:{secret}@github.com/x token={secret}"
    )
    e = Env(tmp_path, [])
    e.d.deploy = lambda: 3
    e.d.pending_deploy, e.d.last_merge_at = True, e.now - 101

    assert e.d._maybe_deploy() is False
    e.d._deferred_sha = None  # novo merge: o deploy completo volta a rodar e re-adia por exit 3
    e.now += 101
    assert e.d._maybe_deploy() is False

    path = tmp_path / "deferral-state" / "runs" / fd.DEPLOY_DEFERRAL_FILE
    raw = path.read_text(encoding="utf-8")
    records = json.loads(raw)
    assert list(records) == [sha]
    record = records[sha]
    assert record["target_sha"] == sha and record["pending_node"] == "notebook"
    assert record["attempts"] == 2 and record["exit_code"] == 3 and record["timestamp"]
    assert secret not in raw and "user:" not in raw
    assert not list(path.parent.glob("*.tmp"))
    assert [f["reason"] for f in e.d.failures] == [fd.DEPLOY_PENDING_CONVERGENCE_REASON] * 2

    e.d.deploy = lambda: 1
    e.d._deferred_sha = None
    e.now += 101
    assert e.d._maybe_deploy() is False
    assert e.d.failures[-1]["reason"] == "canonical_deploy_exit:1"
    assert json.loads(path.read_text(encoding="utf-8"))[sha]["attempts"] == 2


def _deferred_env(tmp_path: Path, monkeypatch, converge_rcs):
    from core.infra import node_sync

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "recovery-state"))
    sha = "c" * 40
    node_sync.record_pending_convergence("notebook", sha, "behind origin/main")
    e = Env(tmp_path, [])
    calls = {"deploy": 0, "converge": 0}
    rcs = iter(converge_rcs)

    def deploy():
        calls["deploy"] += 1
        return 3

    def converge():
        calls["converge"] += 1
        return next(rcs)

    e.d.deploy, e.d.converge = deploy, converge
    e.d.pending_deploy, e.d.last_merge_at = True, e.now - 101
    return e, calls, sha, node_sync


def test_deploy_exit_3_switches_to_bounded_convergence_recovery_with_backoff_and_cap(tmp_path: Path, monkeypatch):
    import json

    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1] * 20)
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": 0}

    e.now += 101
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": 1}

    e.now += 101  # dentro do backoff (300 s): nada roda
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": 1}

    path = tmp_path / "recovery-state" / "runs" / fd.DEPLOY_DEFERRAL_FILE
    waits = []
    for _ in range(fd.CONVERGENCE_RECOVERY_MAX_ATTEMPTS - 1):
        record = json.loads(path.read_text(encoding="utf-8"))[sha]
        waits.append(record["recovery_next_at"] - e.now)
        e.now = record["recovery_next_at"] + 1
        e.d.last_merge_at = e.now - 101
        assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": fd.CONVERGENCE_RECOVERY_MAX_ATTEMPTS}
    assert waits == sorted(waits) and waits[-1] > waits[0]

    for _ in range(5):  # teto esgotado: sem deploy nem recuperacao, pendencia preservada
        e.now += 10_000
        assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": fd.CONVERGENCE_RECOVERY_MAX_ATTEMPTS}
    assert e.d.pending_deploy is True
    assert node_sync.load_pending_convergence()["expected_sha"] == sha
    assert json.loads(path.read_text(encoding="utf-8"))[sha]["recovery_exhausted"] is True
    assert [f["reason"] for f in e.d.failures].count("convergence_recovery_exhausted") == 1


def test_convergence_recovery_success_completes_deploy_and_clears_pending(tmp_path: Path, monkeypatch):
    import json
    from types import SimpleNamespace

    from core.infra import node_sync

    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1, 0])
    monkeypatch.setattr(node_sync, "verify", lambda *, expected_sha: SimpleNamespace(
        expected_sha=expected_sha,
        nodes=[SimpleNamespace(name=name, state="converged", git_sha=expected_sha)
               for name in ("Notebook", "Desktop", "VPS")],
    ))
    assert e.d._maybe_deploy() is False
    e.now += 101
    assert e.d._maybe_deploy() is False
    record = json.loads((tmp_path / "recovery-state" / "runs" / fd.DEPLOY_DEFERRAL_FILE).read_text(encoding="utf-8"))[sha]
    e.now = record["recovery_next_at"] + 1
    e.d.last_merge_at = e.now - 101

    assert e.d._maybe_deploy() is True
    assert calls == {"deploy": 1, "converge": 2}
    assert e.d.pending_deploy is False and e.d.deploys == 1
    assert node_sync.load_pending_convergence() is None


def test_convergence_retry_zero_without_target_evidence_stays_pending(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace

    from core.infra import node_sync

    e, calls, _sha, node_sync = _deferred_env(tmp_path, monkeypatch, [0])
    assert e.d._maybe_deploy() is False
    monkeypatch.setattr(node_sync, "verify", lambda *, expected_sha: SimpleNamespace(
        expected_sha=expected_sha,
        nodes=[SimpleNamespace(name="VPS", state="converged", git_sha="d" * 40)],
    ))
    e.now += 301
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": 1}
    assert e.d.pending_deploy is True and e.d.deploys == 0
    record = e.d._load_deferrals()[_sha]
    assert record["recovery_attempts"] == 1
    assert record["recovery_last_exit_code"] == fd.DEPLOY_PENDING_CONVERGENCE_EXIT_CODE


def test_dispatcher_restores_persisted_deferral_and_keeps_recovery_backoff(tmp_path: Path, monkeypatch):
    import json

    from core.infra import node_sync

    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1, 1])
    assert e.d._maybe_deploy() is False
    e.now += 301
    e.d.last_merge_at = e.now - 101
    assert e.d._maybe_deploy() is False
    record_path = tmp_path / "recovery-state" / "runs" / fd.DEPLOY_DEFERRAL_FILE
    record = json.loads(record_path.read_text(encoding="utf-8"))[sha]
    assert record["recovery_attempts"] == 1

    restored = fd.FleetDispatcher(
        e.d.config, demands_loader=lambda: [], probe=lambda h: fd.HostProbe(True, "x"),
        launch=e.d.launch, deploy=e.d.deploy, converge=e.d.converge,
        clock=lambda: e.now,
        admission_store=AdmissionStore(tmp_path / "admission-restored.sqlite3"),
    )
    assert restored._deferred_sha == sha
    assert restored.pending_deploy is True
    assert restored._load_deferrals()[sha]["recovery_attempts"] == 1
    e.now = record["recovery_next_at"] - 1
    restored.last_merge_at = e.now - 101
    assert restored._maybe_deploy() is False
    assert calls == {"deploy": 1, "converge": 1}


def test_new_merge_reopens_full_deploy_after_convergence_deferral(tmp_path: Path, monkeypatch):
    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1] * 5)
    assert e.d._maybe_deploy() is False
    assert e.d._deferred_sha == sha
    e.d._deferred_sha = None  # o merge de um ticket zera o marcador em _reap
    e.d.last_merge_at = e.now - 101
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 2, "converge": 0}


def test_pending_record_for_another_sha_reopens_full_deploy(tmp_path: Path, monkeypatch):
    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1] * 5)
    assert e.d._maybe_deploy() is False
    node_sync.record_pending_convergence("desktop", "d" * 40, "busy")
    e.now += 101
    assert e.d._maybe_deploy() is False
    assert calls == {"deploy": 2, "converge": 0}


@pytest.mark.parametrize("rc", [1, 2, 75, 124])
def test_non_convergence_exit_codes_keep_full_deploy_each_cycle(tmp_path: Path, monkeypatch, rc):
    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [])
    e.d.deploy = lambda: calls.__setitem__("deploy", calls["deploy"] + 1) or rc
    for _ in range(3):
        assert e.d._maybe_deploy() is False
        e.now += 101
    assert calls == {"deploy": 3, "converge": 0}
    assert e.d.failures[-1]["reason"] == f"canonical_deploy_exit:{rc}"


def test_deferral_snapshot_distinguishes_waiting_node_from_failed_deploy(tmp_path: Path, monkeypatch):
    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1])
    assert e.d.deploy_deferral_snapshot() is None
    assert e.d._maybe_deploy() is False
    e.now += 101
    assert e.d._maybe_deploy() is False

    snap = e.d.deploy_deferral_snapshot()
    assert snap["state"] == "awaiting_node_convergence" and snap["target_sha"] == sha
    assert snap["pending_node"] == "notebook" and snap["recovery_attempts"] == 1
    assert snap["recovery_max_attempts"] == fd.CONVERGENCE_RECOVERY_MAX_ATTEMPTS


def test_pending_record_vanishing_without_proof_reopens_full_deploy(tmp_path: Path, monkeypatch):
    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [0])
    assert e.d._maybe_deploy() is False
    node_sync.clear_pending_convergence()
    e.now += 101
    assert e.d._maybe_deploy() is False  # sem prova, nunca conclui: o deploy completo (e o backup) volta
    assert calls == {"deploy": 2, "converge": 0}
    assert e.d.pending_deploy is True and e.d.deploys == 0


def test_later_full_deploy_exit_0_clears_deferral_and_completes(tmp_path: Path, monkeypatch):
    import json

    e, calls, sha, node_sync = _deferred_env(tmp_path, monkeypatch, [1])
    assert e.d._maybe_deploy() is False
    e.d._deferred_sha = None  # novo merge reabre o deploy completo
    e.d.deploy = lambda: 0
    e.d.last_merge_at = e.now - 101

    assert e.d._maybe_deploy() is True
    assert e.d.pending_deploy is False and e.d.deploys == 1
    assert node_sync.load_pending_convergence() is None
    path = tmp_path / "recovery-state" / "runs" / fd.DEPLOY_DEFERRAL_FILE
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    assert e.d.deploy_deferral_snapshot() is None


def test_default_converge_requires_node_retry_then_backup(monkeypatch):
    launched = []

    class Proc:
        def __init__(self, argv, **_kw):
            launched.append(tuple(argv[2:]))
            self.pid, self.rc = 1, rcs[tuple(argv[2:])]

        def wait(self, timeout=None):
            return self.rc

    monkeypatch.setattr(fd.subprocess, "Popen", Proc)
    sha = "e" * 40
    rcs = {
        ("core.infra.node_sync", "retry"): 0,
        ("core.infra.node_sync", "verify", "--expected-sha", sha): 0,
        ("core.infra.backup_cron", "--once"): 0,
    }
    assert fd.default_converge(expected_sha=sha) == 0 and launched == [
        ("core.infra.node_sync", "retry"),
        ("core.infra.node_sync", "verify", "--expected-sha", sha),
        ("core.infra.backup_cron", "--once"),
    ]

    launched.clear()
    rcs[("core.infra.node_sync", "retry")] = 1
    assert fd.default_converge(expected_sha=sha) == 1 and launched == [
        ("core.infra.node_sync", "retry"),
    ]  # sem convergencia, sem backup

    launched.clear()
    rcs.update({
        ("core.infra.node_sync", "retry"): 0,
        ("core.infra.node_sync", "verify", "--expected-sha", sha): 0,
        ("core.infra.backup_cron", "--once"): 1,
    })
    assert fd.default_converge(expected_sha=sha) == 1
