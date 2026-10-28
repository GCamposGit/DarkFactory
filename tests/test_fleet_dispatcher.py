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


def test_disabled_hosts_env_removes_host(monkeypatch):
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "Desktop, ")
    assert [h.id for h in fd.default_hosts()] == ["grokbox", "notebook"]


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


def test_dispatch_uses_clean_operational_worktree_and_only_selected_ticket(tmp_path: Path, monkeypatch):
    """Shared runtime edits stay in place and cannot leak into the launched run_ticket checkout."""
    import json
    import subprocess

    from core.demands.models import UserTicket

    root = tmp_path / "shared"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)

    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (root / "run_ticket.py").write_text("# clean runner\n", encoding="utf-8")
    ledger = root / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    (root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    git("add", "run_ticket.py", ".factory/demands/demands.json", ".gitignore")
    git("commit", "-m", "baseline")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
    git("remote", "add", "origin", str(bare))
    git("push", "-u", "origin", "main")

    foreign = '[{"id":"USR-900","title":"another session"}]\n'
    ledger.write_text(foreign, encoding="utf-8")
    shared_runtime = root / ".factory" / "queue_snapshot.json"
    shared_runtime.parent.mkdir(parents=True, exist_ok=True)
    shared_runtime.write_text('{"foreign":true}\n', encoding="utf-8")
    ticket = UserTicket(id="USR-413", project_id="darkfac", title="isolated", problem_statement="test", status="planned")
    launched = {}

    class Process:
        def poll(self):
            return None

    def popen(cmd, **kwargs):
        launched.update(cmd=cmd, cwd=kwargs["cwd"])
        return Process()

    monkeypatch.setattr(fd, "PROJECT_ROOT", root)
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(fd, "subprocess", type("Subprocess", (), {
        "run": staticmethod(subprocess.run), "Popen": staticmethod(popen),
        "STDOUT": subprocess.STDOUT,
    })())
    job = fd.default_launch(ticket.id, fd.HostSpec("notebook", 1), ticket.model_dump(mode="json"))

    operational = job.operational_checkout
    assert operational is not None and operational != root
    assert launched["cwd"] == operational
    assert Path(launched["cmd"][1]).parent == operational
    assert json.loads((operational / ".factory/demands/demands.json").read_text(encoding="utf-8"))[0]["id"] == ticket.id
    assert not (operational / ".factory/queue_snapshot.json").exists()
    assert ledger.read_text(encoding="utf-8") == foreign
    assert shared_runtime.read_text(encoding="utf-8") == '{"foreign":true}\n'
    fd._remove_operational_checkout(operational)
    assert not operational.exists()


def test_operational_checkout_source_failure_is_visible_and_not_dispatched(tmp_path: Path, monkeypatch):
    cfg = fd.DispatcherConfig(hosts=[fd.HostSpec("grokbox", 1)], state_path=tmp_path / "state.json")
    dispatcher = fd.FleetDispatcher(
        cfg, demands_loader=lambda: [_d("USR-41")],
        probe=lambda _host: fd.HostProbe(True), clock=lambda: 1000.0,
    )

    def source_failure(ticket_id, host_id, demand):
        raise RuntimeError("git fetch origin main falhou (rc=128): source unavailable")

    monkeypatch.setattr(fd, "_create_operational_checkout", source_failure)
    cycle = dispatcher.run_cycle()

    assert cycle["dispatched"] == []
    assert cycle["idle_reasons"]["grokbox"].startswith("lancamento_falhou:git fetch origin main")
    assert cycle["cooldown"] == ["USR-41"]
    failure = cycle["recent_failures"][-1]
    assert failure["kind"] == "launch" and "source unavailable" in failure["reason"]
