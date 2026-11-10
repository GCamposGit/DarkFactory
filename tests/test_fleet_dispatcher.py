from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.line import fleet_dispatcher as fd
from core.line.admission_store import AdmissionStore

HOSTS = [fd.HostSpec("grokbox", 1, "http://g"), fd.HostSpec("notebook", 2, None), fd.HostSpec("desktop", 3, "http://d")]


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
        cfg = fd.DispatcherConfig(hosts=HOSTS, state_path=tmp / "state.json", deploy_window_sec=100, requeue_cooldown_sec=50)
        self.d = fd.FleetDispatcher(cfg, demands_loader=lambda: demands,
                                    probe=lambda h: fd.HostProbe(h.id in self.free, "x"),
                                    launch=self._launch, deploy=self._deploy, clock=lambda: self.now,
                                    admission_store=AdmissionStore(tmp / "admission.sqlite3"))

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


def test_selection_filters():
    dem = [_d("USR-01"), _d("USR-02", tags=["congelado-v0"]), _d("USR-03", tags=["waiting_human"]),
           _d("USR-04", deps=["USR-09"]), _d("USR-05", deps=["USR-06"]), _d("USR-06", "completed"), _d("USR-07")]
    q, sk = fd.select_tickets(dem, busy_ids={"USR-07"})
    assert q == ["USR-01", "USR-05"]
    assert sk["USR-02"].startswith("tag:") and sk["USR-04"] == "dependencia_aberta" and sk["USR-07"] == "em_voo"


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


def test_identical_failure_open_does_not_idle_host_with_other_admissible_ticket(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")], free=("grokbox",))
    e.d.admission_store.is_failure_open = lambda **kw: kw["ticket_id"] == "USR-01"
    cyc = e.d.run_cycle()
    assert e.launched == [("USR-02", "grokbox")]
    assert cyc["skipped"]["USR-01"] == "identical_failure_open"
    assert "grokbox" not in cyc["idle_reasons"]


def test_infra_failure_remains_open_after_cooldown_without_resource_change(tmp_path: Path, monkeypatch):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.run_cycle()
    e.handles["USR-01"].rc = 75
    cyc = e.d.run_cycle()
    assert cyc["cooldown"] == ["USR-01"] and len(e.launched) == 1
    e.now += 60
    cyc = e.d.run_cycle()
    assert len(e.launched) == 1
    assert cyc["idle_reasons"]["grokbox"] == "identical_failure_open"

    # A REVIEW wait survives a dispatcher restart and resumes the same worktree,
    # without rebuilding or spinning new processes while a route is absent.
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
    monkeypatch.setattr(pd, "_route", lambda _row: None)
    calls = []
    handle = SimpleNamespace(pid=999999, poll=lambda: None)
    monkeypatch.setattr(pd.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or handle)
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
    pd.tick(tmp_path, directory=directory, now=due + 2 * pd.RECHECK_SECONDS)
    assert len(calls) == 1
    # Lost parent process: live child remains the owner after a service restart.
    pd._children.clear()
    monkeypatch.setattr(pd, "_pid_running", lambda _row: True)
    assert pd.tick(tmp_path, directory=directory, now=due + 3 * pd.RECHECK_SECONDS)[0]["status"] == "running"
    assert len(calls) == 1
    monkeypatch.setattr(pd, "_pid_running", lambda _row: False)
    # A completed ticket is cleared instead of delivered again after reboot.
    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: SimpleNamespace(
        mock_mode=False, list_tickets=lambda: [{"id": "USR-99", "status": "completed"}]))
    release = context / "deploy_report.json"
    release.write_text(json.dumps({"sha": "a" * 40, "deployed": False}), encoding="utf-8")
    (context / "merge.json").write_text(json.dumps({"merge_sha": "b" * 40, "candidate_digest": "candidate"}), encoding="utf-8")
    (context / "validation.json").write_text(json.dumps({"passed": True, "candidate_digest": "candidate"}), encoding="utf-8")
    (context / "review_state.json").write_text(json.dumps({"rounds": [{"candidate_digest": "candidate", "verdict": "approve"}]}), encoding="utf-8")
    (context / "review_execution.json").write_text(json.dumps({"candidate_digest": "candidate", "implementer_family": "openai", "reviewer_family": "anthropic"}), encoding="utf-8")
    assert pd.tick(tmp_path, directory=directory, now=due + 4 * pd.RECHECK_SECONDS)[0]["cause"] == "deploy_evidence_mismatch"
    saved = pd.PendingDelivery.model_validate_json((directory / "USR-99.json").read_text(encoding="utf-8"))
    saved.status = "waiting"
    pd._save(directory / "USR-99.json", saved)
    (context / "merge.json").write_text(json.dumps({"merge_sha": "a" * 40, "candidate_digest": "candidate"}), encoding="utf-8")
    assert pd.tick(tmp_path, directory=directory, now=due + 4 * pd.RECHECK_SECONDS)[0]["status"] == "merged_awaiting_deploy"
    assert list(directory.glob("*.json"))  # completed ledger alone is not production evidence
    release.write_text(json.dumps({"sha": "a" * 40, "deployed": True}), encoding="utf-8")
    assert pd.tick(tmp_path, directory=directory, now=due + 5 * pd.RECHECK_SECONDS)[0]["status"] == "terminal"
    assert not list(directory.glob("*.json"))
    release.unlink()
    again = pd.schedule("USR-99", candidate, "review", resume_delivery=True, directory=directory)
    assert pd.tick(tmp_path, directory=directory, now=again.next_check + 1)[0]["status"] == "merged_awaiting_deploy"
    (context / "merge.json").unlink()
    assert pd.tick(tmp_path, directory=directory, now=again.next_check + 2)[0]["cause"] == "deploy_evidence_missing"

    # Empty external busy inventory cannot erase a resume started in this cycle.
    active = Env(tmp_path / "active-resume", [_d("USR-01")], free=("grokbox",))
    active.d.launch = None
    active.d.busy_ids_loader = lambda: set()
    monkeypatch.setattr(pd, "tick", lambda _root: [{"ticket": "USR-01", "status": "running"}])
    assert active.d.run_cycle()["dispatched"] == []


def test_verified_resource_change_reopens_breaker(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    resource = {"version": "worker-v1"}
    e.d.probe = lambda h: fd.HostProbe(h.id in e.free, "ready", resource["version"])
    e.d.run_cycle()
    e.handles["USR-01"].rc = 75
    e.d.run_cycle()
    resource["version"] = "worker-v2"
    e.now += 60
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-01", "grokbox"), ("USR-01", "grokbox")]
    assert cycle["dispatched"] == [{"ticket": "USR-01", "host": "grokbox"}]


def test_new_quota_receipt_reopens_only_that_ticket_scope(tmp_path: Path):
    demand = _d("USR-01")
    demand["quota_receipt"] = "window-1"
    e = Env(tmp_path, [demand], free=("grokbox",))
    e.d.run_cycle()
    e.handles["USR-01"].rc = 75
    e.d.run_cycle()
    demand["quota_receipt"] = "window-2"
    e.now += 60
    cycle = e.d.run_cycle()
    assert len(e.launched) == 2
    assert cycle["dispatched"] == [{"ticket": "USR-01", "host": "grokbox"}]


def test_admission_store_failure_is_structured_and_fails_closed(tmp_path: Path):
    class UnavailableStore:
        def is_failure_open(self, **_kwargs):
            raise OSError("database unavailable")

    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    e.d.admission_store = UnavailableStore()
    cycle = e.d.run_cycle()
    assert e.launched == []
    assert cycle["idle_reasons"]["grokbox"] == "admission_store_unavailable"
    assert cycle["skipped"]["USR-01"] == "admission_store_unavailable"


def test_recovery_latches_persistently_and_serializes_one_evidenced_delivery(tmp_path: Path):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02")])
    failed = fd.InFlight("USR-99", "grokbox", Handle(), candidate_digest="base",
                         context_digest="ctx", resource_version="worker-v1")
    for _ in range(3):
        e.d._record_failure(failed, 1, "context", "context scanner failed")
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
    assert e.d.cooldown[job.ticket_id] > e.now
    normal = Env(tmp_path / "normal", [_d("USR-01")], free=("grokbox",))
    normal.d.run_cycle()
    normal.handles["USR-01"].rc = 0
    normal.d.run_cycle()
    assert normal.d.cooldown["USR-01"] > normal.now
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
    assert e.deploys == 2 and not e.d.pending_releases
    assert e.launched == [("USR-01", "grokbox")]


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
    e.d.config.lock_requeue_cooldown_sec = 10
    e.d.run_cycle()
    e.d.in_flight["grokbox"].log_path = log
    e.handles["USR-01"].rc = 2
    cyc = e.d.run_cycle()
    assert cyc["idle_reasons"]["grokbox"].startswith("host_ocupado_trava")
    text = log.read_text(encoding="utf-8")
    assert fd.is_lock_refusal(text) and not fd.is_infra_failure(2, text)
    e.now += 15  # O cooldown passou, mas o host e o contexto continuam iguais.
    cyc = e.d.run_cycle()
    assert len(e.launched) == 1
    assert cyc["idle_reasons"]["grokbox"] == "identical_failure_open"


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


def test_dispatch_preserves_effective_state_root_override(tmp_path: Path, monkeypatch):
    captured = {}

    class Process:
        def poll(self):
            return None

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "shared-state"))
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(fd, "_create_operational_checkout", lambda *_args: tmp_path / "checkout")
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
        launched.update(cmd=cmd, cwd=kwargs["cwd"], env=kwargs["env"])
        return Process()

    monkeypatch.setattr(fd, "PROJECT_ROOT", root)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(root))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    monkeypatch.setattr(fd, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(fd, "subprocess", type("Subprocess", (), {
        "run": staticmethod(subprocess.run), "Popen": staticmethod(popen),
        "STDOUT": subprocess.STDOUT,
    })())
    job = fd.default_launch(ticket.id, fd.HostSpec("notebook", 1), ticket.model_dump(mode="json"))

    operational = job.operational_checkout
    assert operational is not None and operational != root
    from core.line.store_selection import default_control_db_path, default_control_store

    dispatcher_db = default_control_db_path()
    assert launched["env"]["DARKFAC_STATE_ROOT"] == str((root / ".factory").resolve())
    # Resolve the launcher's store under the exact child environment and persist
    # a queue row. The operational checkout can then be removed independently.
    monkeypatch.setenv("DARKFAC_STATE_ROOT", launched["env"]["DARKFAC_STATE_ROOT"])
    launcher_db = default_control_db_path()
    assert launcher_db == dispatcher_db
    launcher_store = default_control_store()
    launcher_store.upsert_ticket(
        "darkfac", "USR-413", "planned", {"id": "USR-413"}, datetime.now(timezone.utc)
    )
    assert default_control_store().list_tickets("darkfac") == [{"id": "USR-413"}]
    assert launched["cwd"] == operational
    assert Path(launched["cmd"][1]).parent == operational
    operational_tickets = json.loads(
        (operational / ".factory/demands/demands.json").read_text(encoding="utf-8")
    )
    assert [row["id"] for row in operational_tickets] == [ticket.id]
    assert not (operational / ".factory/queue_snapshot.json").exists()
    assert ledger.read_text(encoding="utf-8") == foreign
    assert shared_runtime.read_text(encoding="utf-8") == '{"foreign":true}\n'
    fd._remove_operational_checkout(operational)
    assert not operational.exists()
    assert default_control_db_path() == dispatcher_db
    assert default_control_store().list_tickets("darkfac") == [{"id": "USR-413"}]

    # A `worktree add` that partially succeeds and then reports an error must
    # still be removed; otherwise later dispatches discover a stale checkout.
    real_git_checked = fd._git_checked
    partial_path = {}

    def fail_after_worktree_add(args, cwd):
        result = real_git_checked(args, cwd)
        if list(args[:2]) == ["worktree", "add"]:
            partial_path["path"] = Path(args[3])
            raise RuntimeError("git reported a late worktree creation failure")
        return result

    monkeypatch.setattr(fd, "_git_checked", fail_after_worktree_add)
    with pytest.raises(RuntimeError, match="late worktree creation failure"):
        fd._create_operational_checkout(ticket.id, "notebook", ticket.model_dump(mode="json"))
    assert not partial_path["path"].exists()
    assert str(partial_path["path"]) not in git("worktree", "list", "--porcelain").stdout


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


def test_operational_checkout_cleanup_uses_protected_cleanup_and_logs_recovery(tmp_path, monkeypatch, caplog):
    from core.git.ticket_workspace import CleanupResult

    checkout = tmp_path / "operational-checkout"
    checkout.mkdir()
    holder = "python (pid 17192)"
    calls = []

    def protected_cleanup(path, main_root):
        calls.append((path, main_root))
        return CleanupResult(
            path=path, removed=False, method="preserved",
            detail=f"{path} is in use; preserved because processes have cwd inside it: {holder}",
        )

    monkeypatch.setattr("core.git.ticket_workspace.cleanup", protected_cleanup)
    monkeypatch.setattr(fd, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("core.git.ticket_workspace.main_checkout_root", lambda _path: tmp_path)

    fd._remove_operational_checkout(checkout)

    assert calls == [(checkout, tmp_path)]
    assert "checkout operacional preservado" in caplog.text
    assert str(checkout) in caplog.text and holder in caplog.text
    assert "recuperacao possivel" in caplog.text


def test_reap_cleans_operational_checkout_after_job_exits_without_changing_deploy_batch(tmp_path, monkeypatch):
    e = Env(tmp_path, [_d("USR-01")], free=("grokbox",))
    checkout = tmp_path / "operational-checkout"
    calls = []
    monkeypatch.setattr(fd, "_remove_operational_checkout", lambda path: calls.append(path))
    e.d.run_cycle()
    job = e.d.in_flight["grokbox"]
    job.operational_checkout = checkout
    e.handles["USR-01"].rc = 0
    e.mark_delivered("USR-01")

    e.d.run_cycle()
    assert calls == [checkout]
    assert e.d.pending_deploy and e.d.deploys == 0
    e.now += e.d.config.deploy_window_sec + 1
    e.d.run_cycle()
    assert e.d.deploys == 1


def test_repeated_ticket_cause_holds_only_that_ticket_until_new_digest(tmp_path: Path, monkeypatch):
    import subprocess
    from core.demands.models import UserTicket

    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    git("init", "-b", "ticket/usr-01")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "fix.py").write_text("first\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "candidate a")
    monkeypatch.setattr(fd, "PROJECT_ROOT", repo)
    row = UserTicket(id="USR-01", title="Fix", problem_statement="Failure").model_dump(mode="json")
    row["context_digest"] = "ctx"
    row["status"] = "planned"
    row["created_at"] = "2026-01-01"
    rows = [row, _d("USR-02")]
    candidate = fd.ticket_candidate_digest("USR-01")
    e = Env(tmp_path, rows, free=("grokbox",))
    e.d.demands_loader = lambda: rows
    for i, host in enumerate(("grokbox", "notebook", "desktop")):  # fim do log e host diferentes
        job = fd.InFlight("USR-01", host, Handle(), candidate_digest=candidate,
                          context_digest="ctx", resource_version=f"{host}:v")
        e.d._record_failure(job, 1, "failure", f"[ERRO NA ENTREGA] causa: gate_failed; erro: x{i}\nfim {i}\n")
    store = e.d.admission_store
    assert not store.recovery_state()["active"]  # sem recovery global
    assert store.ticket_holds()[0]["occurrences"] == 3

    cyc = e.d.run_cycle()  # o ticket retido nao prende o host livre
    assert e.launched == [("USR-02", "grokbox")]
    assert cyc["skipped"]["USR-01"] == "ticket_hold:gate_failed"
    assert cyc["ticket_holds"][0]["ticket_id"] == "USR-01"

    (repo / "fix.py").write_text("fixed\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "candidate b")  # real worktree commit releases hold without ledger edit
    e.handles["USR-02"].rc = 0
    e.now += 10**6
    e.d.run_cycle()
    assert store.ticket_holds() == [] and ("USR-01", "grokbox") in e.launched


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
    if cause == "gate_failed":
        assert store.ticket_holds()[0]["cause"] == cause


@pytest.mark.parametrize(("rc", "held"), [(75, False), (1, True)])
def test_identical_failures_hold_ticket_except_structured_wait(tmp_path, rc, held):
    e = Env(tmp_path, [])
    for _ in range(3):
        job = fd.InFlight("USR-01", "grokbox", Handle(), candidate_digest="patch", context_digest="ctx")
        e.d._record_failure(job, rc, "failure", "route_wait: sem rota")
    assert not e.d.admission_store.recovery_state()["active"]
    assert bool(e.d.admission_store.ticket_holds()) is held


def test_fleet_reservation_refusal_counts_as_lock_not_failure(tmp_path):
    text = "[RESERVA DE FROTA] Fleet capacity ceiling of 1 active cycles reached"
    assert fd.is_lock_refusal(text) and not fd.is_infra_failure(2, text)
    active_pr = "[RECUSA] Ticket USR-494 ja possui PR ativo #830: https://github.com/x/y/pull/830"
    assert fd.is_lock_refusal(active_pr) and not fd.is_infra_failure(2, active_pr)
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


@pytest.mark.parametrize("blocked", ["ticket_hold:gate_failed", "identical_failure_open"])
def test_recovery_tries_next_ticket_when_priority_is_blocked(tmp_path, monkeypatch, blocked):
    e = Env(tmp_path, [_d("USR-01"), _d("USR-02"), _d("USR-03")])
    e.d.config.recovery_tickets = ("USR-01", "USR-02")
    e.d.admission_store.set_recovery(active=True, cause="infra", fingerprint="fp")
    dispatch = e.d._dispatch_ticket
    def attempt(host, probe, tid, demand, queue, idle, skipped, dispatched, now):
        if tid == "USR-01":
            skipped[tid] = blocked
            return
        return dispatch(host, probe, tid, demand, queue, idle, skipped, dispatched, now)
    monkeypatch.setattr(e.d, "_dispatch_ticket", attempt)
    cycle = e.d.run_cycle()
    assert e.launched == [("USR-02", "grokbox")]
    assert cycle["skipped"]["USR-01"] == blocked
