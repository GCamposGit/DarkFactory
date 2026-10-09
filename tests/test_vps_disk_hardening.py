"""VPS disk exhaustion hardening (2026-10-06 incident): policy cleanup, 80/90% alerts, retention, wiring."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from core.infra import disk_guard
from core.infra import vps_cleanup as vc

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_DIR = REPO_ROOT / "deploy" / "dokploy"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDokploy:
    """Records calls; `project.all` reports the services statuses given."""

    def __init__(
        self,
        statuses: Optional[Dict[str, str]] = None,
        fail_projects: bool = False,
        fail_paths: Optional[Dict[str, BaseException]] = None,
    ) -> None:
        self.calls: List[tuple[str, str]] = []
        self.statuses = statuses or {}
        self.fail_projects = fail_projects
        self.fail_paths = dict(fail_paths or {})

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> Any:
        self.calls.append((method, path))
        if path in self.fail_paths:
            raise self.fail_paths[path]
        if path == "/api/project.all":
            if self.fail_projects:
                raise RuntimeError("boom")
            return [
                {
                    "name": "darkfac-core",
                    "environments": [
                        {
                            "name": "production",
                            "compose": [
                                {"composeId": f"c_{n}", "name": n, "composeStatus": st}
                                for n, st in self.statuses.items()
                            ],
                            "applications": [],
                        }
                    ],
                }
            ]
        if path == "/api/settings.getDockerDiskUsage":
            return [
                {"type": "Images", "size": "31GB", "totalCount": 233, "active": 11, "reclaimable": "28GB", "sizeBytes": 31e9},
                {"type": "Build Cache", "size": "22GB", "totalCount": 90, "active": 0, "reclaimable": "22GB", "sizeBytes": 22e9},
            ]
        return {}

    def prunes(self) -> List[str]:
        return [p for _m, p in self.calls if p.startswith("/api/settings.clean")]


def probe_of(free_gb: float, total_gb: float = 40.0):
    used = total_gb - free_gb

    def probe() -> Dict[str, Any]:
        return {
            "total_gb": total_gb,
            "used_gb": used,
            "free_gb": free_gb,
            "disk_percent": round(used / total_gb * 100, 1),
        }

    return probe


# ---------------------------------------------------------------------------
# find_running_deployments
# ---------------------------------------------------------------------------


def test_find_running_deployments_detects_running_compose_and_app() -> None:
    def transport(method: str, path: str, body: Any) -> Any:
        return [
            {
                "name": "p",
                "environments": [
                    {
                        "name": "production",
                        "compose": [{"name": "darkfac-cloud", "composeStatus": "running"}, {"name": "hub", "composeStatus": "done"}],
                        "applications": [{"name": "canary", "applicationStatus": "running"}],
                    }
                ],
            },
            {"name": "legacy", "compose": [{"name": "old", "composeStatus": "idle"}], "applications": []},
        ]

    assert vc.find_running_deployments(transport) == ["darkfac-cloud", "canary"]


def test_find_running_deployments_raises_on_bad_shape() -> None:
    with pytest.raises(RuntimeError):
        vc.find_running_deployments(lambda m, p, b: "nope")


# ---------------------------------------------------------------------------
# clean_vps_if_needed
# ---------------------------------------------------------------------------


def test_disk_ok_and_not_forced_does_nothing() -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    report = vc.clean_vps_if_needed(transport=api, disk_probe=probe_of(30.0), min_free_gb=8.0)
    assert report["action"] == "skipped" and report["reason"] == "disk_ok"
    assert api.calls == []


def test_low_disk_prunes_images_and_builder_cache() -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    report = vc.clean_vps_if_needed(transport=api, disk_probe=probe_of(5.0), min_free_gb=8.0, stage="pre-deploy")
    assert report["action"] == "cleaned" and report["reason"] == "low_disk"
    assert api.prunes() == ["/api/settings.cleanDockerBuilder", "/api/settings.cleanUnusedImages"]


def test_forced_images_only_keeps_build_cache_when_disk_is_fine() -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    report = vc.clean_vps_if_needed(
        transport=api, disk_probe=probe_of(30.0), min_free_gb=8.0, force_images=True, stage="post-deploy"
    )
    assert report["action"] == "cleaned" and report["reason"] == "forced"
    assert api.prunes() == ["/api/settings.cleanUnusedImages"]


def test_never_prunes_while_a_deploy_is_running() -> None:
    api = FakeDokploy({"darkfac-cloud": "running"})
    report = vc.clean_vps_if_needed(transport=api, disk_probe=probe_of(5.0), min_free_gb=8.0)
    assert report["action"] == "skipped"
    assert report["reason"] == "deploy_in_progress:darkfac-cloud"
    assert api.prunes() == []


def test_emergency_prunes_even_with_a_deploy_running() -> None:
    api = FakeDokploy({"darkfac-cloud": "running"})
    report = vc.clean_vps_if_needed(transport=api, disk_probe=probe_of(1.0), min_free_gb=8.0)
    assert report["action"] == "cleaned"
    assert len(api.prunes()) == 2


def test_running_check_failure_fails_closed() -> None:
    api = FakeDokploy(fail_projects=True)
    report = vc.clean_vps_if_needed(transport=api, disk_probe=probe_of(5.0), min_free_gb=8.0)
    assert report["action"] == "skipped"
    assert "unknown" in report["reason"]
    assert api.prunes() == []


def test_forced_prune_proceeds_when_disk_probe_unavailable() -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})

    def broken_probe() -> Dict[str, Any]:
        raise OSError("hub down")

    report = vc.clean_vps_if_needed(transport=api, disk_probe=broken_probe, force_images=True)
    assert report["action"] == "cleaned"
    assert api.prunes() == ["/api/settings.cleanUnusedImages"]
    # ... but without force an unreadable disk means no action
    api2 = FakeDokploy()
    report2 = vc.clean_vps_if_needed(transport=api2, disk_probe=broken_probe)
    assert report2["reason"] == "disk_probe_unavailable" and api2.calls == []


def test_missing_credentials_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under pytest real credentials are never read; the reason must be explicit.
    report = vc.clean_vps_if_needed(disk_probe=probe_of(5.0), min_free_gb=8.0, env={})
    assert report["action"] == "skipped" and report["reason"] == "no_credentials"


def test_post_deploy_read_timeout_is_incomplete_and_is_not_retried() -> None:
    """USR-180: the closeout timeout stays visible and does not start a second prune."""
    api = FakeDokploy(
        {"darkfac-cloud": "done"},
        fail_paths={"/api/settings.cleanUnusedImages": TimeoutError("The read operation timed out")},
    )
    report = vc.clean_vps_if_needed(
        transport=api,
        disk_probe=probe_of(30.0),
        min_free_gb=8.0,
        force_images=True,
        stage="post-deploy",
    )
    assert report["action"] == "incomplete"
    assert report["reason"] == "forced"
    assert report["hygiene_status"] == "transient"
    assert api.prunes() == ["/api/settings.cleanUnusedImages"]
    assert report["errors"] == ["settings.cleanUnusedImages failed: The read operation timed out"]
    line = vc.format_hygiene_record("post-deploy", report)
    assert "hygiene_status=transient" in line
    assert "cleanUnusedImages=false" in line
    assert "The read operation timed out" in line
    assert all("volume" not in path.lower() and "container" not in path.lower() for _method, path in api.calls)


def test_min_free_gb_from_env() -> None:
    assert vc.min_free_gb_from_env({}) == vc.DEFAULT_MIN_FREE_GB == 8.0
    assert vc.min_free_gb_from_env({"DARKFAC_VPS_MIN_FREE_GB": "12.5"}) == 12.5
    assert vc.min_free_gb_from_env({"DARKFAC_VPS_MIN_FREE_GB": "abc"}) == 8.0
    assert vc.min_free_gb_from_env({"DARKFAC_VPS_MIN_FREE_GB": "-1"}) == 8.0


# ---------------------------------------------------------------------------
# Two-level alert with cooldown
# ---------------------------------------------------------------------------


def usage_at(percent: float) -> Dict[str, Any]:
    return {"disk_percent": percent, "used_gb": round(40 * percent / 100, 1), "total_gb": 40.0,
            "free_gb": round(40 * (100 - percent) / 100, 1)}


def test_alert_levels() -> None:
    assert vc.disk_alert_level(79.9) is None
    assert vc.disk_alert_level(80.0) == "warning"
    assert vc.disk_alert_level(89.9) == "warning"
    assert vc.disk_alert_level(90.0) == "critical"


def test_alert_once_per_level_per_6h_and_escalation(tmp_path: Path) -> None:
    sent: List[tuple[str, str]] = []

    def notifier(level: str, title: str, message: str) -> bool:
        sent.append((level, message))
        return True

    state = tmp_path / "state.json"
    t0 = 1_000_000.0
    assert vc.evaluate_and_alert_disk(usage_at(70), state_path=state, now=t0, notifier=notifier) is None
    assert vc.evaluate_and_alert_disk(usage_at(82), state_path=state, now=t0, notifier=notifier) == "warning"
    # same level inside 6h: suppressed
    assert vc.evaluate_and_alert_disk(usage_at(83), state_path=state, now=t0 + 5 * 3600, notifier=notifier) is None
    # escalation to critical alerts immediately (separate level)
    assert vc.evaluate_and_alert_disk(usage_at(91), state_path=state, now=t0 + 5 * 3600, notifier=notifier) == "critical"
    assert vc.evaluate_and_alert_disk(usage_at(95), state_path=state, now=t0 + 6 * 3600, notifier=notifier) is None
    # warning level cooled down after 6h
    assert vc.evaluate_and_alert_disk(usage_at(85), state_path=state, now=t0 + 6 * 3600 + 1, notifier=notifier) == "warning"
    # critical cools down 6h after ITS last alert
    assert vc.evaluate_and_alert_disk(usage_at(92), state_path=state, now=t0 + 11 * 3600 + 1, notifier=notifier) == "critical"
    assert [lvl for lvl, _ in sent] == ["warning", "critical", "warning", "critical"]


def test_alert_state_survives_restart(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    calls: List[str] = []
    notifier = lambda lvl, t, m: calls.append(lvl) or True  # noqa: E731
    vc.evaluate_and_alert_disk(usage_at(85), state_path=state, now=500.0, notifier=notifier)
    # a "new process" reading the same state file stays quiet
    assert vc.evaluate_and_alert_disk(usage_at(85), state_path=state, now=600.0, notifier=notifier) is None
    assert calls == ["warning"]


def test_failed_delivery_is_retried_next_cycle(tmp_path: Path) -> None:
    state = tmp_path / "s.json"
    outcomes = iter([False, True])
    notifier = lambda lvl, t, m: next(outcomes)  # noqa: E731
    assert vc.evaluate_and_alert_disk(usage_at(91), state_path=state, now=1.0, notifier=notifier) is None
    assert vc.evaluate_and_alert_disk(usage_at(91), state_path=state, now=2.0, notifier=notifier) == "critical"


def test_alert_message_has_consumers_numbers_and_runbook(tmp_path: Path) -> None:
    captured: List[str] = []
    vc.evaluate_and_alert_disk(
        usage_at(91),
        state_path=tmp_path / "s.json",
        now=1.0,
        consumers=["/workspaces: 3.10 GB", "Images: 31GB (233 itens)"],
        notes=["Limpeza adiada: deploy em andamento (darkfac-cloud)."],
        notifier=lambda lvl, t, m: captured.append(m) or True,
    )
    msg = captured[0]
    assert "CRITICO" in msg and "91.0%" in msg
    assert "/workspaces: 3.10 GB" in msg and "Images: 31GB" in msg
    assert "deploy em andamento" in msg
    assert "docs/runbooks/vps_disk.md" in msg


# ---------------------------------------------------------------------------
# prune_old_files + top consumers
# ---------------------------------------------------------------------------


def test_prune_old_files_removes_only_old_files(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    (root / "sub").mkdir(parents=True)
    old, new = root / "sub" / "old.log", root / "new.log"
    old.write_text("x" * 100)
    new.write_text("y")
    now = time.time()
    os.utime(old, (now - 20 * 86400, now - 20 * 86400))
    res = vc.prune_old_files(root, max_age_days=14, now=now)
    assert res["removed_files"] == 1 and res["freed_bytes"] == 100
    assert not old.exists() and new.exists()
    assert not (root / "sub").exists()  # emptied dir dropped
    assert root.exists()
    assert vc.prune_old_files(root, max_age_days=0)["removed_files"] == 0
    assert vc.prune_old_files(tmp_path / "missing", max_age_days=1)["removed_files"] == 0


def test_collect_top_consumers_orders_by_size(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "f").write_bytes(b"x" * 10)
    (b / "f").write_bytes(b"x" * 1000)
    top = vc.collect_top_consumers([str(a), str(b), str(tmp_path / "nope")])
    assert [p for p, _ in top] == [str(b), str(a)]


# ---------------------------------------------------------------------------
# disk_guard
# ---------------------------------------------------------------------------


def test_guard_cycle_prunes_low_disk_and_alerts_with_consumers(tmp_path: Path) -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    alerts: List[str] = []
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f").write_bytes(b"x" * 2048)
    summary = disk_guard.run_cycle(
        env={"DARKFAC_DISK_REPORT_PATHS": str(ws), "DARKFAC_TESTLOG_DIR": str(tmp_path / "tl")},
        transport=api,
        disk_probe=probe_of(3.0),  # 92.5% used, stays 92.5% after the fake prune
        notifier=lambda lvl, t, m: alerts.append(m) or True,
        state_path=tmp_path / "state.json",
        now=10.0,
    )
    assert summary["cleanup"]["action"] == "cleaned"
    assert len(api.prunes()) == 2
    assert summary["alert"] == "critical"
    assert str(ws) in alerts[0] and "Images: 31GB" in alerts[0]


def test_guard_cycle_without_credentials_alerts_and_says_cleanup_is_off(tmp_path: Path) -> None:
    alerts: List[str] = []
    summary = disk_guard.run_cycle(
        env={},
        disk_probe=probe_of(6.0),
        notifier=lambda lvl, t, m: alerts.append(m) or True,
        state_path=tmp_path / "s.json",
        now=1.0,
    )
    assert summary["cleanup"] == {"action": "skipped", "reason": "no_credentials"}
    assert summary["alert"] == "warning"
    assert "DOKPLOY_API_URL" in alerts[0] and "DESATIVADA" in alerts[0]


def test_guard_cycle_healthy_disk_is_silent(tmp_path: Path) -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    summary = disk_guard.run_cycle(
        env={}, transport=api, disk_probe=probe_of(30.0), notifier=lambda *a: pytest.fail("no alert"),
        state_path=tmp_path / "s.json",
    )
    assert summary["alert"] is None and api.prunes() == []


def test_guard_cycle_prune_due_runs_images_prune_and_test_log_retention(tmp_path: Path) -> None:
    api = FakeDokploy({"darkfac-cloud": "done"})
    tl = tmp_path / "tl"
    tl.mkdir()
    old = tl / "old.log"
    old.write_text("x")
    now = time.time()
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    summary = disk_guard.run_cycle(
        env={"DARKFAC_TESTLOG_DIR": str(tl)}, transport=api, disk_probe=probe_of(30.0),
        notifier=lambda *a: True, state_path=tmp_path / "s.json", prune_due=True, now=now,
    )
    assert api.prunes() == ["/api/settings.cleanUnusedImages"]
    assert summary["test_logs"]["removed_files"] == 1 and not old.exists()


def test_guard_cycle_never_raises() -> None:
    def broken() -> Dict[str, Any]:
        raise RuntimeError("probe exploded")

    summary = disk_guard.run_cycle(env={}, disk_probe=broken)
    assert "probe exploded" in summary["error"]


def test_daemon_runs_6h_prune_on_schedule_and_retries_skipped_prunes() -> None:
    clock = {"t": 0.0}
    prune_flags: List[bool] = []
    results = iter(
        [
            {"cleanup": {"action": "skipped", "reason": "deploy_in_progress:x"}},  # retried next cycle
            {"cleanup": {"action": "cleaned"}},
            {"cleanup": {"action": "skipped", "reason": "disk_ok"}},
            {"cleanup": {"action": "skipped", "reason": "disk_ok"}},
            {"cleanup": {"action": "cleaned"}},
        ]
    )

    def cycle(prune_due: bool = False, **_: Any) -> Dict[str, Any]:
        prune_flags.append(prune_due)
        return next(results)

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    disk_guard.run_daemon(
        check_interval_minutes=30, prune_interval_hours=1, sleep_fn=sleep, clock_fn=lambda: clock["t"],
        max_cycles=5, cycle_fn=cycle,
    )
    # t=0 prune skipped (deploy running) -> retried at t=30m (cleaned) -> not due at 60m -> due again at 90m
    assert prune_flags == [True, True, False, True, False]


def test_daemon_retries_incomplete_transient_prune_next_cycle() -> None:
    clock = {"t": 0.0}
    prune_flags: List[bool] = []
    results = iter(
        [
            {"cleanup": {"action": "incomplete", "reason": "forced", "hygiene_status": "transient"}},
            {"cleanup": {"action": "cleaned", "hygiene_status": "ok"}},
            {"cleanup": {"action": "skipped", "reason": "disk_ok", "hygiene_status": "skipped"}},
        ]
    )

    def cycle(prune_due: bool = False, **_: Any) -> Dict[str, Any]:
        prune_flags.append(prune_due)
        return next(results)

    def sleep(seconds: float) -> None:
        clock["t"] += seconds

    disk_guard.run_daemon(
        check_interval_minutes=30,
        prune_interval_hours=1,
        sleep_fn=sleep,
        clock_fn=lambda: clock["t"],
        max_cycles=3,
        cycle_fn=cycle,
    )
    assert prune_flags == [True, True, False]


def test_start_background_guard_disabled_with_zero() -> None:
    assert disk_guard.start_background_guard(check_interval_minutes=0) is None


# ---------------------------------------------------------------------------
# Compose wiring
# ---------------------------------------------------------------------------


def _compose_services() -> List[tuple[str, str, Dict[str, Any]]]:
    out = []
    for path in sorted(COMPOSE_DIR.glob("docker-compose*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, svc in data["services"].items():
            out.append((path.name, name, svc))
    return out


def test_there_are_compose_files_to_check() -> None:
    names = {f for f, _n, _s in _compose_services()}
    assert {"docker-compose.cloud.yml", "docker-compose.hub.yml", "docker-compose.n8n.yml"} <= names


@pytest.mark.parametrize("fname,name,svc", _compose_services(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_service_has_bounded_json_file_logs(fname: str, name: str, svc: Dict[str, Any]) -> None:
    logging_cfg = svc.get("logging")
    assert logging_cfg, f"{fname}:{name} has no logging config (unbounded json-file logs)"
    assert logging_cfg["driver"] == "json-file"
    assert logging_cfg["options"]["max-size"] == "10m"
    assert str(logging_cfg["options"]["max-file"]) == "3"


def test_backup_cron_runs_disk_guard_with_dokploy_credentials_and_workspace_view() -> None:
    cloud = yaml.safe_load((COMPOSE_DIR / "docker-compose.cloud.yml").read_text(encoding="utf-8"))
    svc = cloud["services"]["darkfac-backup-cron"]
    env = "\n".join(svc["environment"])
    assert "DOKPLOY_API_URL=" in env and "DOKPLOY_API_KEY=" in env
    assert "DARKFAC_VPS_MIN_FREE_GB" in env
    assert "TELEGRAM_OWNER_BOT_TOKEN" in env
    cmd = svc["command"]
    assert "--disk-guard-interval-minutes" in cmd and "30" in cmd[cmd.index("--disk-guard-interval-minutes") + 1]
    assert "--disk-prune-interval-hours" in cmd and cmd[cmd.index("--disk-prune-interval-hours") + 1] == "6"
    assert "darkfac-workspaces:/workspaces:ro" in svc["volumes"]


def test_backup_cron_cli_exposes_disk_guard_flags() -> None:
    import inspect

    from core.infra import backup_cron

    src = inspect.getsource(backup_cron.main)
    assert "--disk-guard-interval-minutes" in src and "--disk-prune-interval-hours" in src


# ---------------------------------------------------------------------------
# Workspace retention
# ---------------------------------------------------------------------------


def _mk_run(root: Path, project: str, run_id: str, age_days: float, now: float) -> Path:
    d = root / project / "runs" / run_id
    d.mkdir(parents=True)
    (d / "f.txt").write_text("x")
    ts = now - age_days * 86400
    os.utime(d / "f.txt", (ts, ts))
    os.utime(d, (ts, ts))
    return d


def _iso(now: float, age_days: float) -> str:
    return (datetime.fromtimestamp(now, UTC) - timedelta(days=age_days)).isoformat()


def test_workspace_sweep_removes_only_old_terminal_runs(tmp_path: Path) -> None:
    from core.line.workspace import sweep_stale_workspaces

    now = time.time()
    old_done = _mk_run(tmp_path, "darkfac", "r-old-done", 5, now)
    old_failed = _mk_run(tmp_path, "darkfac", "r-old-failed", 9, now)
    recent_done = _mk_run(tmp_path, "darkfac", "r-recent-done", 1, now)
    old_active = _mk_run(tmp_path, "darkfac", "r-old-active", 8, now)
    old_waiting = _mk_run(tmp_path, "darkfac", "r-old-waiting", 8, now)
    protected = _mk_run(tmp_path, "darkfac", "r-protected", 30, now)
    old_orphan = _mk_run(tmp_path, "other", "r-orphan-old", 20, now)
    young_orphan = _mk_run(tmp_path, "other", "r-orphan-young", 5, now)
    unknown_store = _mk_run(tmp_path, "other", "r-store-down", 30, now)
    (tmp_path / "darkfac" / ".mirror").mkdir()
    statuses: Dict[str, Optional[dict]] = {
        "r-old-done": {"status": "completed", "completed_at": _iso(now, 5), "jobs": [{"status": "succeeded"}]},
        "r-old-failed": {"status": "failed", "completed_at": _iso(now, 9), "jobs": [{"status": "failed"}]},
        "r-recent-done": {"status": "completed", "completed_at": _iso(now, 1), "jobs": []},
        "r-old-active": {"status": "active", "updated_at": _iso(now, 8), "jobs": [{"status": "running"}]},
        "r-old-waiting": {"status": "completed", "completed_at": _iso(now, 8), "jobs": [{"status": "waiting_human"}]},
        "r-protected": {"status": "completed", "completed_at": _iso(now, 30), "jobs": []},
        "r-orphan-old": None,
        "r-orphan-young": None,
    }

    def lookup(run_id: str) -> Optional[dict]:
        if run_id == "r-store-down":
            raise RuntimeError("db unreachable")
        return statuses[run_id]

    removed = sweep_stale_workspaces(
        run_status=lookup, protected_run_ids={"r-protected"}, now=now, root=tmp_path
    )
    assert sorted(removed) == ["r-old-done", "r-old-failed", "r-orphan-old"]
    assert not old_done.exists() and not old_failed.exists() and not old_orphan.exists()
    for keep in (recent_done, old_active, old_waiting, protected, young_orphan, unknown_store):
        assert keep.exists(), keep
    assert (tmp_path / "darkfac" / ".mirror").exists()


def test_workspace_sweep_missing_root_is_noop(tmp_path: Path) -> None:
    from core.line.workspace import sweep_stale_workspaces

    assert sweep_stale_workspaces(run_status=lambda r: None, root=tmp_path / "nope") == []


def test_cloud_worker_sweeps_workspaces_periodically_and_protects_active_runs() -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    calls: List[Dict[str, Any]] = []

    def sweeper(**kwargs: Any) -> List[str]:
        calls.append(kwargs)
        return []

    class Store:
        def get_run_status(self, run_id: str) -> None:
            return None

        def claim(self, **_: Any) -> None:
            return None

        def list_waiting_jobs(self, **_: Any) -> list:
            return []

    worker = CloudWorker(
        worker_id="w", max_slots=2, store=Store(), capabilities=[], workspace_sweep_interval_s=3600.0,
        workspace_sweeper=sweeper,
    )
    worker._active_tasks["run-live:build"] = 0.0
    t0 = datetime(2026, 10, 6, tzinfo=UTC)
    worker.poll_and_execute_once(now=t0)
    worker.poll_and_execute_once(now=t0 + timedelta(minutes=10))  # inside interval: no new sweep
    worker.poll_and_execute_once(now=t0 + timedelta(hours=2))
    assert len(calls) == 2
    assert calls[0]["protected_run_ids"] == {"run-live"}
    assert calls[0]["terminal_max_age_days"] == 3.0


def test_cloud_worker_sweep_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    monkeypatch.setenv("DARKFAC_WORKSPACE_RETENTION_DAYS", "0")
    worker = CloudWorker(
        worker_id="w", max_slots=1, store=object(), capabilities=[],
        workspace_sweeper=lambda **k: pytest.fail("must not sweep"),
    )
    assert worker.sweep_stale_workspaces() == []


# ---------------------------------------------------------------------------
# Line release stage: disk hygiene around Dokploy deploys
# ---------------------------------------------------------------------------


def _release_handler(cleaner, tmp_path: Path):
    from core.line.stage_release import ReleaseStageHandler, ReleaseStateStore
    from core.projects.models import DeployConfig, DeployTargetType, ProjectDescriptor
    from tests.line.test_stage_release import FakeAdapter

    project = ProjectDescriptor(id="acme", name="Acme", deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={}))
    return ReleaseStageHandler(
        project,
        dokploy_adapter=FakeAdapter(),
        state_store=ReleaseStateStore(state_dir=tmp_path / "rs"),
        disk_cleaner=cleaner,
        sleep=lambda s: None,
    )


def test_release_deploy_runs_pre_and_post_disk_hygiene(tmp_path: Path) -> None:
    stages: List[str] = []
    handler = _release_handler(lambda stage: stages.append(stage) or {"action": "skipped"}, tmp_path)
    operation, _cfg, failure = handler._deploy("a" * 40, None)
    assert failure is None and operation is not None
    assert stages == ["pre-deploy", "post-deploy"]


def test_release_records_transient_hygiene_without_failing_the_deploy(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    def cleaner(stage: str) -> Dict[str, Any]:
        if stage == "post-deploy":
            return {
                "action": "incomplete",
                "reason": "forced",
                "hygiene_status": "transient",
                "errors": ["settings.cleanUnusedImages failed: The read operation timed out"],
                "result": {
                    "cleanUnusedImages": False,
                    "cleanDockerBuilder": False,
                    "hygiene_status": "transient",
                },
            }
        return {"action": "skipped", "reason": "disk_ok", "hygiene_status": "skipped"}

    handler = _release_handler(cleaner, tmp_path)
    with caplog.at_level(logging.INFO, logger="core.line.stage_release"):
        operation, _cfg, failure = handler._deploy("c" * 40, None)
    assert failure is None and operation is not None
    assert "hygiene_status=transient" in caplog.text
    assert "settings.cleanUnusedImages failed: The read operation timed out" in caplog.text


def test_release_disk_hygiene_failure_never_breaks_the_deploy(tmp_path: Path) -> None:
    def exploding(stage: str) -> None:
        raise RuntimeError("dokploy down")

    handler = _release_handler(exploding, tmp_path)
    operation, _cfg, failure = handler._deploy("b" * 40, None)
    assert failure is None and operation is not None


# ---------------------------------------------------------------------------
# dokploy_redeploy wiring
# ---------------------------------------------------------------------------


def test_redeploy_health_probe_asks_for_host_disk() -> None:
    from scripts import dokploy_redeploy as rd

    assert rd.DARKHUB_HEALTH_DISK_URL.endswith("/health?disk=true")
    usage = rd.fetch_vps_disk_usage(
        lambda: {"disk_percent": 85.0, "disk_free_gb": 6.0, "disk_total_gb": 40.0}
    )
    assert usage["free_gb"] == 6.0 and usage["total_gb"] == 40.0 and usage["disk_percent"] == 85.0

    def down() -> Dict[str, Any]:
        raise OSError("404")

    assert rd.fetch_vps_disk_usage(down)["total_gb"] == 0.0


def test_redeploy_default_hygiene_policy_uses_health_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import dokploy_redeploy as rd

    api = FakeDokploy({"darkfac-cloud": "done"})
    low = {"disk_percent": 90.0, "disk_free_gb": 4.0, "disk_total_gb": 40.0}
    res = rd.default_disk_hygiene_runner(api, "pre-deploy", health=lambda: low)
    assert res["action"] == "cleaned" and len(api.prunes()) == 2

    api2 = FakeDokploy({"darkfac-cloud": "done"})
    ok = {"disk_percent": 30.0, "disk_free_gb": 28.0, "disk_total_gb": 40.0}
    assert rd.default_disk_hygiene_runner(api2, "pre-deploy", health=lambda: ok)["action"] == "skipped"
    assert api2.prunes() == []
    assert rd.default_disk_hygiene_runner(api2, "post-deploy", health=lambda: ok)["action"] == "cleaned"
    assert api2.prunes() == ["/api/settings.cleanUnusedImages"]
