"""USR-148: retention of the `darkfac-artifacts` volume (policy, dry-run, CloudWorker scheduling)."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.orchestrator.artifact_retention import (
    ArtifactRetentionConfig,
    retention_config_from_env,
    sweep_stale_artifacts,
)
from core.orchestrator.cloud_artifacts import CloudArtifactStore


def _iso(now: float, age_days: float) -> str:
    return (datetime.fromtimestamp(now, UTC) - timedelta(days=age_days)).isoformat()


def _mk_workflow(root: Path, workflow_id: str, age_days: float, now: float, payload: bytes = b"x" * 100) -> Path:
    store = CloudArtifactStore(root_dir=root)
    store.store_artifact(workflow_id, "report.txt", payload)
    store.store_artifact(workflow_id, "log.txt", payload)
    directory = root / workflow_id
    ts = now - age_days * 86400
    for child in directory.iterdir():
        os.utime(child, (ts, ts))
    os.utime(directory, (ts, ts))
    return directory


def _status(now: float, status: str, age_days: float, jobs: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "status": status,
        "completed_at": _iso(now, age_days) if status in {"completed", "failed", "cancelled", "succeeded"} else None,
        "updated_at": _iso(now, age_days),
        "jobs": [{"status": j} for j in (jobs or [])],
    }


def _scenario(tmp_path: Path, now: float) -> Dict[str, Optional[Dict[str, Any]]]:
    root = tmp_path / "artifacts"
    for wid, age in (
        ("run-active", 90),
        ("run-waiting-human", 90),
        ("run-waiting-job", 90),
        ("run-old-done", 45),
        ("run-old-failed", 60),
        ("run-recent-done", 5),
        ("run-protected", 90),
        ("run-touched", 90),
        ("run-orphan-old", 120),
        ("run-orphan-young", 40),
    ):
        _mk_workflow(root, wid, age, now)
    # A terminal run whose directory was written to recently (late evidence) must survive.
    recent_file = root / "run-touched" / "late.txt"
    recent_file.write_text("late")
    return {
        "run-active": _status(now, "active", 90, ["running"]),
        "run-waiting-human": _status(now, "waiting_human", 90, ["waiting_human"]),
        "run-waiting-job": _status(now, "completed", 90, ["waiting_dependency"]),
        "run-old-done": _status(now, "completed", 45, ["succeeded"]),
        "run-old-failed": _status(now, "failed", 60, ["failed"]),
        "run-recent-done": _status(now, "completed", 5, ["succeeded"]),
        "run-protected": _status(now, "completed", 90, []),
        "run-touched": _status(now, "completed", 90, []),
        "run-orphan-old": None,
        "run-orphan-young": None,
    }


def test_sweep_applies_policy_per_run_state(tmp_path: Path) -> None:
    now = time.time()
    statuses = _scenario(tmp_path, now)
    root = tmp_path / "artifacts"
    (root / "build_ledger.json").write_text("{}")  # root-level state files are never candidates
    (root / ".hidden").mkdir()

    report = sweep_stale_artifacts(
        root=root,
        run_status=lambda run_id: statuses[run_id],
        protected_run_ids={"run-protected"},
        config=ArtifactRetentionConfig(),
        now=now,
    )

    assert sorted(report.removed_ids) == ["run-old-done", "run-old-failed", "run-orphan-old"]
    assert report.dry_run is False and report.would_remove == []
    assert report.bytes_freed == 3 * 200
    assert report.scanned == 10 and report.kept == 7 and report.errors == []
    for gone in ("run-old-done", "run-old-failed", "run-orphan-old"):
        assert not (root / gone).exists(), gone
    for keep in (
        "run-active",
        "run-waiting-human",
        "run-waiting-job",
        "run-recent-done",
        "run-protected",
        "run-touched",
        "run-orphan-young",
    ):
        assert (root / keep / "report.txt").is_file(), keep
    assert (root / "build_ledger.json").is_file() and (root / ".hidden").is_dir()


def test_active_and_waiting_runs_survive_even_when_ancient(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    for wid in ("a", "w", "h"):
        _mk_workflow(root, wid, 3650, now)
    statuses = {
        "a": _status(now, "active", 3650, ["running"]),
        "w": _status(now, "waiting_human", 3650, ["waiting_human"]),
        "h": _status(now, "waiting_dependency", 3650, ["waiting_dependency", "pending"]),
    }
    report = sweep_stale_artifacts(
        root=root, run_status=lambda r: statuses[r], config=ArtifactRetentionConfig(), now=now
    )
    assert report.removed_ids == [] and report.kept == 3
    assert all((root / wid).is_dir() for wid in statuses)


def test_dry_run_reports_but_deletes_nothing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    now = time.time()
    statuses = _scenario(tmp_path, now)
    root = tmp_path / "artifacts"
    before = sorted(p.name for p in root.iterdir())

    with caplog.at_level(logging.INFO, logger="darkfac.artifact_retention"):
        report = sweep_stale_artifacts(
            root=root,
            run_status=lambda run_id: statuses[run_id],
            protected_run_ids={"run-protected"},
            config=ArtifactRetentionConfig(dry_run=True),
            now=now,
        )

    assert report.dry_run is True
    assert report.removed == [] and report.bytes_freed == 0
    assert sorted(report.removed_ids) == ["run-old-done", "run-old-failed", "run-orphan-old"]
    assert report.bytes_reclaimable == 3 * 200
    assert sorted(p.name for p in root.iterdir()) == before
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("artifact_retention_sweep ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert payload["dry_run"] is True and payload["bytes_reclaimable"] == 600
    assert {e["workflow_id"] for e in payload["would_remove"]} == {"run-old-done", "run-old-failed", "run-orphan-old"}


def test_real_sweep_logs_structured_summary(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    _mk_workflow(root, "old", 40, now, payload=b"y" * 50)
    with caplog.at_level(logging.INFO, logger="darkfac.artifact_retention"):
        sweep_stale_artifacts(
            root=root,
            run_status=lambda r: _status(now, "completed", 40, []),
            config=ArtifactRetentionConfig(),
            now=now,
        )
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("artifact_retention_sweep "))
    payload = json.loads(line.split(" ", 1)[1])
    assert payload["dry_run"] is False and payload["bytes_freed"] == 100
    assert payload["removed"][0]["workflow_id"] == "old"
    assert payload["removed"][0]["reason"] == "terminal_older_than_30d"


def test_status_lookup_failure_fails_closed(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    _mk_workflow(root, "store-down", 400, now)

    def broken(_: str) -> None:
        raise RuntimeError("db unreachable")

    report = sweep_stale_artifacts(root=root, run_status=broken, config=ArtifactRetentionConfig(), now=now)
    assert report.removed_ids == [] and (root / "store-down").is_dir()
    assert report.errors and "store-down" in report.errors[0]


def test_terminal_run_without_timestamps_is_kept(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    _mk_workflow(root, "no-ts", 400, now)
    report = sweep_stale_artifacts(
        root=root,
        run_status=lambda r: {"status": "completed", "jobs": []},
        config=ArtifactRetentionConfig(),
        now=now,
    )
    assert report.removed_ids == [] and (root / "no-ts").is_dir()


def test_disabled_and_missing_root_are_noops(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    _mk_workflow(root, "old", 400, now)
    never = ArtifactRetentionConfig(terminal_max_age_days=0)
    assert never.enabled is False
    report = sweep_stale_artifacts(
        root=root, run_status=lambda r: _status(now, "completed", 400, []), config=never, now=now
    )
    assert report.removed_ids == [] and (root / "old").is_dir()
    assert sweep_stale_artifacts(root=tmp_path / "nope", run_status=lambda r: None, now=now).scanned == 0


def test_symlinked_directory_is_never_followed(tmp_path: Path) -> None:
    now = time.time()
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep")
    ts = now - 400 * 86400
    os.utime(outside, (ts, ts))
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    report = sweep_stale_artifacts(
        root=root, run_status=lambda r: _status(now, "completed", 400, []), config=ArtifactRetentionConfig(), now=now
    )
    assert report.scanned == 0 and (outside / "precious.txt").is_file()


def test_config_from_env_defaults_and_overrides() -> None:
    default = retention_config_from_env({})
    assert default.terminal_max_age_days == 30.0 and default.orphan_max_age_days == 60.0
    assert default.dry_run is False and default.enabled is True

    custom = retention_config_from_env(
        {
            "DARKFAC_ARTIFACT_RETENTION_DAYS": "10",
            "DARKFAC_ARTIFACT_ORPHAN_RETENTION_DAYS": "5",
            "DARKFAC_ARTIFACT_RETENTION_DRY_RUN": "true",
        }
    )
    assert custom.terminal_max_age_days == 10.0
    assert custom.orphan_max_age_days == 10.0  # orphans are never swept sooner than terminal runs
    assert custom.dry_run is True

    assert retention_config_from_env({"DARKFAC_ARTIFACT_RETENTION_DAYS": "0"}).enabled is False
    bad = retention_config_from_env({"DARKFAC_ARTIFACT_RETENTION_DAYS": "abc"})
    assert bad.terminal_max_age_days == 30.0
    assert retention_config_from_env({"DARKFAC_ARTIFACT_RETENTION_DAYS": "-3"}).terminal_max_age_days == 30.0


def test_status_sets_stay_in_sync_with_workspace_sweep() -> None:
    from core.line import workspace
    from core.orchestrator import artifact_retention

    assert artifact_retention.TERMINAL_RUN_STATUSES == workspace.TERMINAL_RUN_STATUSES
    assert artifact_retention.LIVE_JOB_STATUSES == workspace._LIVE_JOB_STATUSES


# ---------------------------------------------------------------------------
# CloudWorker scheduling
# ---------------------------------------------------------------------------


class _Store:
    def get_run_status(self, run_id: str) -> None:
        return None

    def claim(self, **_: Any) -> None:
        return None

    def list_waiting_jobs(self, **_: Any) -> list:
        return []


def test_cloud_worker_sweeps_artifacts_every_interval_and_protects_active_runs(tmp_path: Path) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    calls: List[Dict[str, Any]] = []

    def sweeper(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return None

    worker = CloudWorker(
        worker_id="w",
        max_slots=2,
        store=_Store(),
        capabilities=[],
        artifact_store=CloudArtifactStore(root_dir=tmp_path / "artifacts"),
        artifact_sweep_interval_s=3600.0,
        artifact_sweeper=sweeper,
    )
    worker._active_tasks["run-live:build"] = 0.0
    t0 = datetime(2026, 10, 7, tzinfo=UTC)
    worker.poll_and_execute_once(now=t0)
    worker.poll_and_execute_once(now=t0 + timedelta(minutes=10))  # inside the interval: no new sweep
    worker.poll_and_execute_once(now=t0 + timedelta(hours=2))
    assert len(calls) == 2
    assert calls[0]["protected_run_ids"] == {"run-live"}
    assert calls[0]["root"] == (tmp_path / "artifacts").resolve()
    assert calls[0]["config"].terminal_max_age_days == 30.0


def test_cloud_worker_artifact_sweep_end_to_end_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator import artifact_retention
    from core.orchestrator.cloud_worker import CloudWorker

    now = time.time()
    root = tmp_path / "artifacts"
    _mk_workflow(root, "old-done", 45, now)
    monkeypatch.setenv("DARKFAC_ARTIFACT_RETENTION_DRY_RUN", "1")

    class Store(_Store):
        def get_run_status(self, run_id: str) -> Dict[str, Any]:
            return _status(now, "completed", 45, [])

    worker = CloudWorker(
        worker_id="w",
        max_slots=1,
        store=Store(),
        capabilities=[],
        artifact_store=CloudArtifactStore(root_dir=root),
        artifact_sweeper=artifact_retention.sweep_stale_artifacts,
    )
    assert worker.sweep_stale_artifacts(now=datetime.fromtimestamp(now, UTC)) == ["old-done"]
    assert (root / "old-done" / "report.txt").is_file()  # dry-run: still there


def test_cloud_worker_artifact_sweep_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    monkeypatch.setenv("DARKFAC_ARTIFACT_RETENTION_DAYS", "0")
    worker = CloudWorker(
        worker_id="w",
        max_slots=1,
        store=object(),
        capabilities=[],
        artifact_sweeper=lambda **k: pytest.fail("must not sweep"),
    )
    assert worker.sweep_stale_artifacts() == []


def test_cloud_worker_never_sweeps_real_artifacts_under_pytest_without_injection() -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    worker = CloudWorker(worker_id="w", max_slots=1, store=object(), capabilities=[])
    assert worker.sweep_stale_artifacts() == []
