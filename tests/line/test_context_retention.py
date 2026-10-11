"""USR-169: retention of the remote recovery refs `refs/heads/darkfac-context/<run_id>` (OA-005, option A).

Every scenario uses local bare repositories as the "remote": no network, no GitHub, no real clock.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from core.line import context_retention, recovery_context, workspace
from core.line.context_retention import (
    CONTEXT_REF_PREFIX,
    DEFAULT_CONTEXT_RETENTION_DAYS,
    ContextRetentionConfig,
    context_retention_config_from_env,
    sweep_recovery_contexts,
)
from core.projects.models import ProjectDescriptor

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC).timestamp()
_TERMINAL = {"completed", "failed", "cancelled", "succeeded"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing from the host (DARKFAC_* knobs, GitHub token) may influence a scenario."""
    for name in list(os.environ):
        if name.startswith("DARKFAC_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return result.stdout.strip()


def _project(base: Path, project_id: str = "sample") -> ProjectDescriptor:
    origin = base / f"{project_id}-origin.git"
    _git(["init", "--bare", str(origin)], base)
    seed = base / f"{project_id}-seed"
    _git(["clone", str(origin), str(seed)], base)
    _git(["checkout", "-B", "main"], seed)
    (seed / "README.md").write_text("product\n", encoding="utf-8")
    _git(["add", "."], seed)
    _git(["-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "seed"], seed)
    _git(["push", "origin", "main"], seed)
    return ProjectDescriptor(
        id=project_id, name=project_id.title(), repo_url=str(origin), default_branch="main",
    )


def _origin(project: ProjectDescriptor) -> Path:
    assert project.repo_url
    return Path(project.repo_url)


def _new_commit(origin: Path, parent: str) -> str:
    return _git(
        ["-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit-tree", f"{parent}^{{tree}}", "-p", parent, "-m", "context"],
        origin,
    )


def _pin(project: ProjectDescriptor, run_id: str, sha: Optional[str] = None) -> str:
    origin = _origin(project)
    target = sha or _git(["rev-parse", "refs/heads/main"], origin)
    _git(["update-ref", recovery_context.context_ref(run_id), target], origin)
    return target


def _refs(project: ProjectDescriptor, prefix: str = "refs/") -> dict[str, str]:
    out = _git(["for-each-ref", "--format=%(refname) %(objectname)", prefix], _origin(project))
    return {line.split(" ")[0]: line.split(" ")[1] for line in out.splitlines() if line}


def _context_refs(project: ProjectDescriptor) -> set[str]:
    return {name for name in _refs(project, CONTEXT_REF_PREFIX)}


def _ref(run_id: str) -> str:
    return recovery_context.context_ref(run_id)


def _iso(age_days: float) -> str:
    return (datetime.fromtimestamp(NOW, UTC) - timedelta(days=age_days)).isoformat()


def _status(
    state: str, age_days: float, jobs: tuple[str, ...] = (), project_id: Optional[str] = "sample",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": state,
        "completed_at": _iso(age_days) if state in _TERMINAL else None,
        "updated_at": _iso(age_days),
        "jobs": [{"status": job} for job in jobs],
    }
    if project_id is not None:
        payload["project_id"] = project_id
    return payload


class _Statuses:
    """A fake `store.get_run_status` that records every call and can act between two lookups."""

    def __init__(
        self,
        table: dict[str, Any],
        *,
        on_call: Optional[dict[tuple[str, int], Callable[[], None]]] = None,
        override: Optional[dict[tuple[str, int], Any]] = None,
    ) -> None:
        self.table = table
        self.calls: list[str] = []
        self._on_call = on_call or {}
        self._override = override or {}

    def __call__(self, run_id: str) -> Any:
        self.calls.append(run_id)
        number = self.calls.count(run_id)
        hook = self._on_call.get((run_id, number))
        if hook is not None:
            hook()
        if (run_id, number) in self._override:
            return self._override[(run_id, number)]
        value = self.table[run_id]
        if isinstance(value, Exception):
            raise value
        return value


def _sweep(
    projects: list[ProjectDescriptor],
    statuses: Any,
    *,
    config: Optional[ContextRetentionConfig] = None,
    protected: tuple[str, ...] = (),
    root: Optional[Path] = None,
) -> context_retention.ContextSweepReport:
    return sweep_recovery_contexts(
        projects=projects,
        run_status=statuses,
        protected_run_ids=protected,
        config=config or ContextRetentionConfig(),
        now=NOW,
        root=root,
    )


@pytest.fixture
def project(tmp_path: Path) -> ProjectDescriptor:
    return _project(tmp_path)


class _GitSpy:
    """Delegates to the real `_run_git` and records the subcommands that were run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.real = workspace._run_git
        self.subcommands: list[str] = []
        monkeypatch.setattr(workspace, "_run_git", self)

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.subcommands.append(args[0])
        return self.real(args, **kwargs)


# ---------------------------------------------------------------------------
# Policy: what is removed and what is kept
# ---------------------------------------------------------------------------


def test_old_terminal_runs_without_live_jobs_are_removed(project: ProjectDescriptor) -> None:
    statuses = {
        "run-done": _status("completed", 20, ("succeeded",)),
        "run-failed": _status("failed", 15, ("failed",)),
        "run-cancelled": _status("cancelled", 400),
        "run-succeeded": _status("succeeded", 30),
    }
    for run_id in statuses:
        _pin(project, run_id)
    main_before = _refs(project, "refs/heads/main")

    report = _sweep([project], _Statuses(statuses))

    assert sorted(report.removed_ids) == sorted(statuses)
    assert report.dry_run is False and report.would_remove == []
    assert (report.scanned, report.kept, report.lease_lost, report.errors) == (4, 0, 0, [])
    assert {entry.reason for entry in report.removed} == {"terminal_older_than_14d"}
    assert all(entry.project_id == "sample" and entry.sha for entry in report.removed)
    assert _context_refs(project) == set()
    assert _refs(project, "refs/heads/main") == main_before


def test_terminal_run_inside_the_retention_window_is_kept(project: ProjectDescriptor) -> None:
    statuses = {"run-recent": _status("completed", 13.9), "run-boundary": _status("failed", 14.0)}
    for run_id in statuses:
        _pin(project, run_id)

    report = _sweep([project], _Statuses(statuses))

    assert report.removed_ids == ["run-boundary"]  # exactly 14 days counts as old enough
    assert report.kept_reasons == {"too_recent": 1}
    assert _context_refs(project) == {_ref("run-recent")}


def test_non_terminal_runs_and_live_jobs_keep_the_ref_even_when_ancient(project: ProjectDescriptor) -> None:
    statuses = {
        "run-active": _status("active", 90, ("running",)),
        "run-waiting": _status("waiting_human", 90, ("waiting_human",)),
        "run-retry": _status("completed", 90, ("retry",)),
        "run-running": _status("failed", 90, ("running",)),
        "run-idle": _status("active", 90),
    }
    for run_id in statuses:
        _pin(project, run_id)

    report = _sweep([project], _Statuses(statuses))

    assert report.removed_ids == [] and report.errors == []
    assert report.kept == 5 and report.kept_reasons == {"live_job": 4, "not_terminal": 1}
    assert len(_context_refs(project)) == 5


def test_unknown_run_is_kept_and_counted(project: ProjectDescriptor) -> None:
    _pin(project, "run-orphan")

    report = _sweep([project], _Statuses({"run-orphan": None}))

    assert report.removed_ids == [] and report.errors == []
    assert report.kept_reasons == {"unknown_run": 1}
    assert _context_refs(project) == {_ref("run-orphan")}


def test_status_lookup_failure_keeps_the_ref_and_the_sweep_continues(project: ProjectDescriptor) -> None:
    statuses = {
        "run-aaa-store-down": RuntimeError("db unreachable"),
        "run-bbb-old": _status("completed", 40),
    }
    for run_id in statuses:
        _pin(project, run_id)

    report = _sweep([project], _Statuses(statuses))

    assert report.removed_ids == ["run-bbb-old"]
    assert _context_refs(project) == {_ref("run-aaa-store-down")}
    assert report.kept_reasons == {"status_lookup_failed": 1}
    assert len(report.errors) == 1 and "run-aaa-store-down" in report.errors[0]


def test_protected_runs_keep_the_ref_without_a_store_lookup(project: ProjectDescriptor) -> None:
    _pin(project, "run-mine")
    statuses = _Statuses({"run-mine": _status("completed", 90)})

    report = _sweep([project], statuses, protected=("run-mine",))

    assert report.removed_ids == [] and report.kept_reasons == {"protected": 1}
    assert statuses.calls == []
    assert _context_refs(project) == {_ref("run-mine")}


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (_status("completed", 90, project_id="someone-else"), "project_mismatch"),
        ({"status": "completed", "jobs": []}, "no_timestamp"),
        ({"status": "completed", "completed_at": "not a date", "updated_at": "", "jobs": []}, "no_timestamp"),
        (_status("completed", 1), "too_recent"),
        ("garbage", "status_lookup_failed"),
        (None, "unknown_run"),
    ],
)
def test_decide_keeps_when_in_doubt(status: Any, expected: str) -> None:
    decision = context_retention._decide(
        "run-x", status, project_id="sample", protected=frozenset(), moment=NOW, cfg=ContextRetentionConfig(),
    )
    assert (decision.remove, decision.reason) == (False, expected)


def test_decide_removal_reasons_and_timestamp_fallback() -> None:
    cfg = ContextRetentionConfig(terminal_max_age_days=7)
    old = _status("completed", 8)
    decision = context_retention._decide(
        "run-x", old, project_id="sample", protected=frozenset(), moment=NOW, cfg=cfg,
    )
    assert (decision.remove, decision.reason) == (True, "terminal_older_than_7d")

    only_updated = {"status": "failed", "updated_at": _iso(30), "jobs": []}  # no completed_at: updated_at counts
    assert context_retention._decide(
        "run-x", only_updated, project_id="sample", protected=frozenset(), moment=NOW, cfg=cfg,
    ).remove is True

    no_project_in_status = {"status": "completed", "completed_at": _iso(30), "jobs": []}
    assert context_retention._decide(
        "run-x", no_project_in_status, project_id="sample", protected=frozenset(), moment=NOW, cfg=cfg,
    ).remove is True

    assert context_retention._decide(
        "run-x", old, project_id="sample", protected=frozenset({"run-x"}), moment=NOW, cfg=cfg,
    ).reason == "protected"


# ---------------------------------------------------------------------------
# Safe, idempotent removal
# ---------------------------------------------------------------------------


def test_ref_repinned_between_read_and_remove_is_kept_with_its_new_sha(project: ProjectDescriptor) -> None:
    old_sha = _pin(project, "run-race")
    new_sha = _new_commit(_origin(project), old_sha)
    statuses = _Statuses(
        {"run-race": _status("completed", 30)},
        # The second lookup happens right before the delete: re-pin the ref there.
        on_call={("run-race", 2): lambda: _pin(project, "run-race", new_sha)},
    )

    report = _sweep([project], statuses)

    assert report.lease_lost == 1 and report.removed == [] and report.errors == []
    assert report.kept_reasons == {"lease_lost": 1}
    assert _refs(project, _ref("run-race")) == {_ref("run-race"): new_sha}


def test_run_reopened_between_decision_and_remove_keeps_the_ref(project: ProjectDescriptor) -> None:
    _pin(project, "run-reopened")
    statuses = _Statuses(
        {"run-reopened": _status("completed", 30)},
        override={("run-reopened", 2): _status("active", 0, ("running",))},
    )

    report = _sweep([project], statuses)

    assert report.removed == [] and report.errors == []
    assert report.kept_reasons == {"live_job": 1}
    assert statuses.calls == ["run-reopened", "run-reopened"]
    assert _context_refs(project) == {_ref("run-reopened")}


def test_second_sweep_is_a_clean_no_op(project: ProjectDescriptor) -> None:
    _pin(project, "run-old")
    statuses = _Statuses({"run-old": _status("completed", 30)})

    first = _sweep([project], statuses)
    second = _sweep([project], statuses)

    assert first.removed_ids == ["run-old"]
    assert (second.scanned, second.kept, second.removed, second.errors, second.lease_lost) == (0, 0, [], [], 0)
    assert _context_refs(project) == set()


def test_ref_already_removed_by_another_worker_is_not_an_error(project: ProjectDescriptor) -> None:
    _pin(project, "run-gone")
    statuses = _Statuses(
        {"run-gone": _status("completed", 30)},
        on_call={("run-gone", 2): lambda: _git(["update-ref", "-d", _ref("run-gone")], _origin(project))},
    )

    report = _sweep([project], statuses)

    assert report.errors == [] and report.removed == [] and report.lease_lost == 0
    assert report.kept_reasons == {"already_absent": 1}
    assert report.scanned == report.kept + len(report.removed)


def test_dry_run_reports_would_remove_and_never_pushes(
    project: ProjectDescriptor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = {"run-old": _status("completed", 30), "run-new": _status("completed", 1)}
    for run_id in statuses:
        _pin(project, run_id)
    before = _refs(project)
    spy = _GitSpy(monkeypatch)

    report = _sweep([project], _Statuses(statuses), config=ContextRetentionConfig(dry_run=True))

    assert report.dry_run is True and report.removed == []
    assert report.removed_ids == ["run-old"] and [e.run_id for e in report.would_remove] == ["run-old"]
    assert report.kept_reasons == {"too_recent": 1}
    assert "push" not in spy.subcommands
    assert _refs(project) == before


def test_one_project_failing_does_not_abort_the_others(tmp_path: Path) -> None:
    broken = ProjectDescriptor(id="broken", name="Broken", repo_url=str(tmp_path / "does-not-exist.git"))
    no_repo = ProjectDescriptor(id="norepo", name="No Repo")
    good = _project(tmp_path, "good")
    _pin(good, "run-old")

    report = _sweep([broken, no_repo, good], _Statuses({"run-old": _status("completed", 30, project_id="good")}))

    assert report.removed_ids == ["run-old"]
    assert len(report.errors) == 1 and report.errors[0].startswith("broken:")
    assert _context_refs(good) == set()


def test_refs_outside_the_namespace_and_invalid_names_are_never_touched(project: ProjectDescriptor) -> None:
    origin = _origin(project)
    main_sha = _git(["rev-parse", "refs/heads/main"], origin)
    _pin(project, "run-old")
    untouchable = [
        "refs/heads/darkfac-context/nested/deep",
        "refs/heads/darkfac-context/with@sign",
        "refs/heads/other/refs/heads/darkfac-context/run-tail",
        "refs/heads/df/run-old",
        "refs/heads/ticket/usr-1",
        "refs/tags/darkfac-context/run-old",
    ]
    for name in untouchable:
        _git(["update-ref", name, main_sha], origin)
    before = {name: main_sha for name in untouchable}
    statuses = _Statuses({"run-old": _status("completed", 30)})

    report = _sweep([project], statuses)

    assert report.removed_ids == ["run-old"] and report.errors == []
    assert set(statuses.calls) == {"run-old"}  # invalid names never reach the store
    assert report.kept_reasons.get("invalid_ref_name", 0) >= 2
    assert report.scanned == report.kept + len(report.removed)
    after = _refs(project)
    assert {name: after.get(name) for name in untouchable} == before
    assert after["refs/heads/main"] == main_sha


def test_run_id_of_accepts_only_well_formed_context_refs() -> None:
    assert context_retention._run_id_of(_ref("run-1.2_x")) == "run-1.2_x"
    for bad in (
        "refs/heads/main",
        "refs/heads/df/run-1",
        CONTEXT_REF_PREFIX,
        CONTEXT_REF_PREFIX + "a/b",
        CONTEXT_REF_PREFIX + "-leading",
        "refs/heads/x/" + CONTEXT_REF_PREFIX + "run-1",
        "refs/tags/darkfac-context/run-1",
    ):
        assert context_retention._run_id_of(bad) is None, bad
    assert CONTEXT_REF_PREFIX == recovery_context.context_ref("x")[: -len("x")]


def test_status_sets_are_the_workspace_sweep_sets() -> None:
    assert context_retention.TERMINAL_RUN_STATUSES is workspace.TERMINAL_RUN_STATUSES


def test_sweep_logs_one_structured_summary(project: ProjectDescriptor, caplog: pytest.LogCaptureFixture) -> None:
    _pin(project, "run-old")
    _pin(project, "run-new")
    statuses = _Statuses({"run-old": _status("completed", 30), "run-new": _status("completed", 2)})

    with caplog.at_level(logging.INFO, logger="darkfac.context_retention"):
        _sweep([project], statuses)

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("context_retention_sweep ")]
    assert len(lines) == 1
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert payload["dry_run"] is False and payload["terminal_max_age_days"] == 14.0
    assert payload["scanned"] == 2 and payload["kept"] == 1 and payload["kept_reasons"] == {"too_recent": 1}
    assert [e["run_id"] for e in payload["removed"]] == ["run-old"]
    assert payload["removed"][0]["ref"] == _ref("run-old") and len(payload["removed"][0]["sha"]) >= 40


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_from_env_defaults_and_overrides() -> None:
    default = context_retention_config_from_env({})
    assert default.terminal_max_age_days == DEFAULT_CONTEXT_RETENTION_DAYS == 14.0
    assert default.dry_run is False and default.enabled is True

    custom = context_retention_config_from_env(
        {"DARKFAC_CONTEXT_RETENTION_DAYS": "7.5", "DARKFAC_CONTEXT_RETENTION_DRY_RUN": "true"}
    )
    assert custom.terminal_max_age_days == 7.5 and custom.dry_run is True
    for truthy in ("1", "true", "YES", "on"):
        assert context_retention_config_from_env({"DARKFAC_CONTEXT_RETENTION_DRY_RUN": truthy}).dry_run is True
    for falsy in ("", "0", "false", "off", "nope"):
        assert context_retention_config_from_env({"DARKFAC_CONTEXT_RETENTION_DRY_RUN": falsy}).dry_run is False

    zero = context_retention_config_from_env({"DARKFAC_CONTEXT_RETENTION_DAYS": "0"})
    assert zero.terminal_max_age_days == 0.0 and zero.enabled is False
    for invalid in ("", "  ", "abc", "-3", "nan", "inf", "-inf"):
        cfg = context_retention_config_from_env({"DARKFAC_CONTEXT_RETENTION_DAYS": invalid})
        assert cfg.terminal_max_age_days == 14.0 and cfg.enabled is True, invalid
    assert not math.isnan(context_retention_config_from_env({"DARKFAC_CONTEXT_RETENTION_DAYS": "nan"}).terminal_max_age_days)


def test_config_is_read_from_the_process_environment_by_default(
    project: ProjectDescriptor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(project, "run-eight-days")
    statuses = _Statuses({"run-eight-days": _status("completed", 8)})

    assert sweep_recovery_contexts(projects=[project], run_status=statuses, now=NOW).removed_ids == []

    monkeypatch.setenv("DARKFAC_CONTEXT_RETENTION_DAYS", "7")
    monkeypatch.setenv("DARKFAC_CONTEXT_RETENTION_DRY_RUN", "1")
    dry = sweep_recovery_contexts(projects=[project], run_status=statuses, now=NOW)
    assert dry.dry_run is True and dry.removed_ids == ["run-eight-days"]
    assert _context_refs(project) == {_ref("run-eight-days")}

    monkeypatch.delenv("DARKFAC_CONTEXT_RETENTION_DRY_RUN")
    real = sweep_recovery_contexts(projects=[project], run_status=statuses, now=NOW)
    assert real.removed_ids == ["run-eight-days"] and _context_refs(project) == set()


def test_disabled_sweep_returns_an_empty_report_and_never_calls_git(
    project: ProjectDescriptor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(project, "run-old")

    def no_git(*_: Any, **__: Any) -> Any:
        raise AssertionError("a disabled sweep must not run git")

    monkeypatch.setattr(workspace, "_run_git", no_git)
    monkeypatch.setenv("DARKFAC_CONTEXT_RETENTION_DAYS", "0")
    statuses = _Statuses({"run-old": _status("completed", 400)})

    for report in (
        sweep_recovery_contexts(projects=[project], run_status=statuses, now=NOW),
        _sweep([project], statuses, config=ContextRetentionConfig(terminal_max_age_days=0)),
    ):
        assert (report.scanned, report.kept, report.removed, report.would_remove, report.errors) == (0, 0, [], [], [])
    assert statuses.calls == []
    assert _context_refs(project) == {_ref("run-old")}


# ---------------------------------------------------------------------------
# Preservation: the recovery path still works when it must, and fails cleanly when the ref is gone
# ---------------------------------------------------------------------------


def _planned_workspace(project: ProjectDescriptor, run_id: str) -> workspace.RunWorkspace:
    ws = workspace.checkout(project, run_id)
    workspace.write_context(ws, "SPEC.md", f"# Specification of {run_id}\n")
    workspace.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Repair smoke"}]))
    workspace.commit(ws, "test: planning context", f"{run_id}:planning")
    workspace.push(ws)
    return ws


def test_restore_context_survives_kept_refs_and_fails_cleanly_after_removal(
    tmp_path: Path, project: ProjectDescriptor, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-a"))
    for run_id in ("run-live", "run-expired"):
        ws = _planned_workspace(project, run_id)
        assert recovery_context.preserve_context(ws)
        _git(["push", "origin", "--delete", ws.branch], ws.path)  # the product merge deleted the run branch
    statuses = _Statuses({
        "run-live": _status("active", 60, ("retry",)),  # a retry is pending: the ref must survive
        "run-expired": _status("completed", 20, ("succeeded",)),
    })

    report = _sweep([project], statuses, root=tmp_path / "sweeper")

    assert report.removed_ids == ["run-expired"] and report.errors == []
    assert _context_refs(project) == {_ref("run-live")}

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "worker-b"))
    resumed = workspace.checkout(project, "run-live")
    assert recovery_context.restore_context(resumed) is True
    directory = workspace.context_dir(resumed)
    assert (directory / "SPEC.md").read_text(encoding="utf-8") == "# Specification of run-live\n"
    assert json.loads((directory / "tickets.json").read_text(encoding="utf-8"))[0]["id"] == "T1"

    expired = workspace.checkout(project, "run-expired")
    assert recovery_context.restore_context(expired) is False  # no exception, nothing written
    assert not workspace.context_dir(expired).exists()


# ---------------------------------------------------------------------------
# CloudWorker scheduling
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, status: Any = None) -> None:
        self._status = status

    def get_run_status(self, run_id: str) -> Any:
        return self._status

    def claim(self, **_: Any) -> None:
        return None

    def list_waiting_jobs(self, **_: Any) -> list:
        return []


def _registry_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *projects: ProjectDescriptor) -> None:
    from core.projects import registry as registry_module

    registry = registry_module.ProjectRegistry(projects_file=tmp_path / "registry-projects.json")
    for item in projects:
        registry.register_project(item)
    monkeypatch.setattr(registry_module, "_GLOBAL_REGISTRY", registry)


def test_cloud_worker_sweeps_contexts_every_interval_and_protects_active_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    _registry_with(
        monkeypatch, tmp_path,
        ProjectDescriptor(id="sample", name="Sample", repo_url="https://example.invalid/o/r.git"),
    )
    calls: list[dict[str, Any]] = []

    def sweeper(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return None

    worker = CloudWorker(
        worker_id="w", max_slots=2, store=_Store(), capabilities=[],
        context_sweep_interval_s=3600.0, context_sweeper=sweeper,
    )
    worker._active_tasks["run-live:build"] = 0.0
    t0 = datetime(2026, 10, 11, tzinfo=UTC)
    worker.poll_and_execute_once(now=t0)
    worker.poll_and_execute_once(now=t0 + timedelta(minutes=10))  # inside the interval: no new sweep
    worker.poll_and_execute_once(now=t0 + timedelta(hours=2))

    assert len(calls) == 2
    assert calls[0]["protected_run_ids"] == {"run-live"}
    assert calls[0]["config"].terminal_max_age_days == 14.0
    assert calls[0]["run_status"] == worker.store.get_run_status
    assert "sample" in {item.id for item in calls[0]["projects"]}
    assert calls[0]["now"] == t0.timestamp()


@pytest.mark.parametrize("dry_run", [True, False])
def test_cloud_worker_context_sweep_end_to_end(
    tmp_path: Path, project: ProjectDescriptor, monkeypatch: pytest.MonkeyPatch, dry_run: bool,
) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    _registry_with(monkeypatch, tmp_path, project)
    _pin(project, "run-old")
    if dry_run:
        monkeypatch.setenv("DARKFAC_CONTEXT_RETENTION_DRY_RUN", "1")

    worker = CloudWorker(
        worker_id="w", max_slots=1, store=_Store(_status("completed", 45)), capabilities=[],
        context_sweeper=context_retention.sweep_recovery_contexts,
    )

    assert worker.sweep_recovery_contexts(now=datetime.fromtimestamp(NOW, UTC)) == ["run-old"]
    assert _context_refs(project) == ({_ref("run-old")} if dry_run else set())


def test_cloud_worker_context_sweep_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    monkeypatch.setenv("DARKFAC_CONTEXT_RETENTION_DAYS", "0")
    worker = CloudWorker(
        worker_id="w", max_slots=1, store=object(), capabilities=[],
        context_sweeper=lambda **k: pytest.fail("must not sweep"),
    )
    assert worker.sweep_recovery_contexts() == []


def test_cloud_worker_never_touches_remotes_under_pytest_without_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    monkeypatch.setattr(
        context_retention, "sweep_recovery_contexts", lambda **k: pytest.fail("must not reach a remote")
    )
    worker = CloudWorker(worker_id="w", max_slots=1, store=object(), capabilities=[])
    assert worker.sweep_recovery_contexts() == []


def test_cloud_worker_poll_survives_a_failing_context_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from core.orchestrator.cloud_worker import CloudWorker

    _registry_with(monkeypatch, tmp_path)

    def exploding(**_: Any) -> Any:
        raise RuntimeError("github is down")

    worker = CloudWorker(
        worker_id="w", max_slots=1, store=_Store(), capabilities=[], context_sweeper=exploding,
    )
    with caplog.at_level(logging.WARNING, logger="darkfac.cloud_worker"):
        assert worker.poll_and_execute_once(now=datetime(2026, 10, 11, tzinfo=UTC)) is False
    assert any("Recovery context retention sweep failed" in r.getMessage() for r in caplog.records)
