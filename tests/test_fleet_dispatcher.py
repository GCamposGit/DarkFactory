from __future__ import annotations

from pathlib import Path

from core.line import fleet_dispatcher as fd

HOSTS = [fd.HostSpec("grokbox", 1, "http://g"), fd.HostSpec("notebook", 2, None), fd.HostSpec("desktop", 3, "http://d")]


def _d(i, status="planned", tags=(), deps=()):
    return {"id": i, "status": status, "tags": list(tags), "dependencies": list(deps), "created_at": f"2026-01-{int(i[-2:]):02d}"}


class Handle:
    def __init__(self):
        self.rc = None

    def poll(self):
        return self.rc


class Env:
    def __init__(self, tmp, demands, free=("grokbox", "notebook", "desktop")):
        self.now = 1000.0
        self.launched: list[tuple[str, str]] = []
        self.handles: dict[str, Handle] = {}
        self.deploys = 0
        self.free = set(free)
        cfg = fd.DispatcherConfig(hosts=HOSTS, state_path=tmp / "state.json", deploy_window_sec=100, requeue_cooldown_sec=50)
        self.d = fd.FleetDispatcher(cfg, demands_loader=lambda: demands,
                                    probe=lambda h: fd.HostProbe(h.id in self.free, "x"),
                                    launch=self._launch, deploy=self._deploy, clock=lambda: self.now)

    def _launch(self, tid, host):
        self.launched.append((tid, host.id))
        h = self.handles[tid] = Handle()
        return fd.InFlight(tid, host.id, h)

    def _deploy(self):
        self.deploys += 1
        return 0


def test_selection_filters():
    dem = [_d("USR-01"), _d("USR-02", tags=["congelado-v0"]), _d("USR-03", tags=["waiting_human"]),
           _d("USR-04", deps=["USR-09"]), _d("USR-05", deps=["USR-06"]), _d("USR-06", "completed"), _d("USR-07")]
    q, sk = fd.select_tickets(dem, busy_ids={"USR-07"})
    assert q == ["USR-01", "USR-05"]
    assert sk["USR-02"].startswith("tag:") and sk["USR-04"] == "dependencia_aberta" and sk["USR-07"] == "em_voo"


def test_three_tickets_three_hosts_in_parallel(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02"), _d("USR-03"), _d("USR-04")])
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-01", "grokbox"), ("USR-02", "notebook"), ("USR-03", "desktop")]
    assert cyc["queue_remaining"] == ["USR-04"]
    assert (tmp_path / "state.json").exists()


def test_host_order_skips_unavailable_and_reports_idle(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("desktop",))
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-01", "desktop")]
    assert cyc["idle_reasons"]["grokbox"].startswith("host_indisponivel")


def test_host_lock_no_overlap(tmp_path: Path):
    e = Env(tmp_path, [_d(f"USR-0{i}") for i in range(1, 8)])
    e.d.run_cycle()
    e.d.run_cycle()
    assert len(e.launched) == 3


def test_infra_failure_requeues_with_cooldown(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.handles["USR-01"].rc = 75
    cyc = e.d.run_cycle()
    assert cyc["cooldown"] == ["USR-01"] and len(e.launched) == 1
    e.now += 60
    e.d.run_cycle()
    assert e.launched[-1] == ("USR-01", "grokbox")


def test_deploy_deduplicated_per_batch(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")])
    e.d.run_cycle()
    for h in e.handles.values():
        h.rc = 0
    e.d.run_cycle()
    assert e.deploys == 0
    e.now += 101
    e.d.run_cycle()
    e.d.run_cycle()
    assert e.deploys == 1


def test_line_lock_refusal_is_busy_not_infra(tmp_path: Path):
    log = tmp_path / "run.log"
    log.write_text("[TRAVA DE LINHA] Já existe um run_ticket em andamento nesta máquina", encoding="utf-8")
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.config.lock_requeue_cooldown_sec = 10
    e.d.run_cycle()
    e.d.in_flight["grokbox"].log_path = log
    e.handles["USR-01"].rc = 2
    cyc = e.d.run_cycle()
    assert cyc["idle_reasons"]["grokbox"].startswith("host_ocupado_trava")
    text = log.read_text(encoding="utf-8")
    assert fd.is_lock_refusal(text) and not fd.is_infra_failure(2, text)
    e.now += 15  # cooldown curto (10 s), bem abaixo do de infra (50 s)
    e.d.run_cycle()
    assert e.launched[-1] == ("USR-01", "grokbox")


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
