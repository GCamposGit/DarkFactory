"""Tests for the green-CI merge gate and the red-main probe (USR-85).

Everything runs against a scripted ``gh`` (an injected runner) and local bare
repositories: no network, no real GitHub. A fake clock drives the waiting so the
suite never sleeps.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pytest

from core.git import autonomy
from core.git import ci_checks
from core.git.autonomy import DeliveryReport, GitAutonomyManager
from core.git.ci_checks import (
    CiVerdict,
    base_branch_is_red,
    check_main,
    classify_checks,
    condense_log,
    ensure_green,
    is_ledger_only,
    parse_checks_output,
)

NO_CHECKS = "NO_CHECKS"
PR_JOB = "pr-validation (ubuntu-latest)"
MAIN_JOB = "main-validation (ubuntu-latest)"


@pytest.fixture(autouse=True)
def _hermetic_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ci_checks.CI_WAIT_ENV, raising=False)
    monkeypatch.delenv(ci_checks.NO_CHECKS_GRACE_ENV, raising=False)


def check(bucket: str, name: str = PR_JOB, workflow: str = "DarkFac CI", run: int = 111) -> dict[str, str]:
    return {
        "bucket": bucket,
        "name": name,
        "workflow": workflow,
        "link": f"https://github.com/x/y/actions/runs/{run}/job/7",
    }


class FakeClock:
    """Deterministic clock: ``sleep`` advances time, nothing really waits."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class GhScript:
    """Scripted ``gh``: ``pr checks`` plays back ``checks`` (last entry repeats)."""

    def __init__(
        self,
        checks: Sequence[Any] = (),
        *,
        base_runs: Optional[list[dict[str, Any]]] = None,
        base_jobs: Optional[dict[int, list[dict[str, Any]]]] = None,
        failed_log: str = "",
        run_list_rc: int = 0,
    ) -> None:
        self.checks = list(checks)
        self.base_runs = base_runs if base_runs is not None else []
        self.base_jobs = base_jobs or {}
        self.failed_log = failed_log
        self.run_list_rc = run_list_rc
        self.calls: list[list[str]] = []
        self.on_checks: Optional[Callable[[int], None]] = None
        self._checks_calls = 0

    def result(self, args: list[str], rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, rc, out, err)

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        a = list(args)
        self.calls.append(a)
        if a[:2] == ["pr", "checks"]:
            self._checks_calls += 1
            if self.on_checks is not None:
                self.on_checks(self._checks_calls)
            if not self.checks:
                return self.result(a, 1, "", "no checks reported on the 'x' branch")
            entry = self.checks[min(self._checks_calls - 1, len(self.checks) - 1)]
            if entry == NO_CHECKS:
                return self.result(a, 1, "", "no checks reported on the 'x' branch")
            if isinstance(entry, tuple):
                return self.result(a, entry[0], "", entry[1])
            return self.result(a, 0, json.dumps(entry))
        if a[:2] == ["run", "list"]:
            if self.run_list_rc != 0:
                return self.result(a, self.run_list_rc, "", "HTTP 502")
            return self.result(a, 0, json.dumps(self.base_runs))
        if a[:2] == ["run", "view"] and "--log-failed" in a:
            return self.result(a, 0, self.failed_log)
        if a[:2] == ["run", "view"] and "--json" in a:
            run_id = int(a[2])
            if run_id not in self.base_jobs:
                return self.result(a, 1, "", "run not found")
            return self.result(a, 0, json.dumps({"jobs": self.base_jobs[run_id]}))
        return self.result(a, 1, "", f"unsupported: {a}")

    def count(self, prefix: Sequence[str]) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == list(prefix))


def red_main_script(**overrides: Any) -> GhScript:
    """Base branch red on ``main-validation (ubuntu-latest)`` (run 900)."""
    params: dict[str, Any] = dict(
        base_runs=[
            {
                "databaseId": 900,
                "conclusion": "failure",
                "headSha": "5da723e04ace7e1ee7e30321ca728d6c34036757",
                "url": "https://github.com/x/y/actions/runs/900",
                "workflowName": "DarkFac CI",
                "displayTitle": "feat: something",
            }
        ],
        base_jobs={
            900: [
                {"name": MAIN_JOB, "conclusion": "failure", "url": "https://github.com/x/y/actions/runs/900/job/1"},
                {"name": "main-validation (windows-latest)", "conclusion": "success", "url": "u"},
                {"name": "trusted-pr-policy", "conclusion": "skipped", "url": "u"},
            ]
        },
    )
    params.update(overrides)
    return GhScript(**params)


def gate(runner: GhScript, clock: FakeClock, **kwargs: Any) -> CiVerdict:
    kwargs.setdefault("timeout_s", 60)
    kwargs.setdefault("poll_s", 10)
    return ensure_green(
        5, Path("."), runner, sleep_fn=clock.sleep, clock_fn=clock.clock, **kwargs
    )


# ----------------------------------------------------------------------
# Parsing / classification
# ----------------------------------------------------------------------


def test_classify_checks_buckets() -> None:
    assert classify_checks([]).state == "no_checks"
    assert classify_checks("garbage").state == "no_checks"
    assert classify_checks([check("pass"), check("skipping"), check("cancel")]).state == "green"
    assert classify_checks([check("pass"), check("pending")]).state == "pending"
    snap = classify_checks([check("pass"), check("fail"), check("pending")])
    assert snap.state == "failed" and len(snap.failing) == 1 and len(snap.pending) == 0


def test_parse_checks_output_handles_gh_failures() -> None:
    assert parse_checks_output(1, "", "no checks reported on the 'b' branch").state == "no_checks"
    assert parse_checks_output(1, "", "no commit found for PR").state == "no_checks"
    err = parse_checks_output(1, "", "HTTP 502 bad gateway")
    assert err.state == "error" and "502" in err.error
    assert parse_checks_output(0, "not json", "").state == "error"
    assert parse_checks_output(0, json.dumps([check("pass")]), "").state == "green"


def test_condense_log_keeps_failure_not_cleanup_and_redacts_tokens() -> None:
    prefix = "pr-validation (ubuntu-latest)\tUNKNOWN STEP\t2026-09-30T20:22:33.2430347Z "
    raw = "\n".join(
        [
            f"{prefix}[ERROR] Candidate worktree is dirty",
            f"{prefix}token ghp_SECRET123abc leaked",
            f"{prefix}##[error]Process completed with exit code 1.",
            f"{prefix}Post job cleanup.",
            f"{prefix}[command]/usr/bin/git version",
        ]
    )
    out = condense_log(raw)
    assert "Candidate worktree is dirty" in out
    assert "Post job cleanup" not in out
    assert "UNKNOWN STEP" not in out
    assert "ghp_SECRET123abc" not in out and "[REDACTED_TOKEN]" in out
    assert len(condense_log("x" * 10_000)) == ci_checks.LOG_TAIL_CHARS


def test_condense_log_lists_early_error_annotations_when_the_tail_is_long() -> None:
    noise = "\n".join(f"step\tUNKNOWN STEP\t2026-09-30T20:22:33.0Z cleanup line {i}" for i in range(400))
    raw = (
        "job\tUNKNOWN STEP\t2026-09-30T20:22:30.0Z ##[error]fatal: couldn't find remote ref refs/pull/9/merge\n"
        + noise
        + "\njob\tUNKNOWN STEP\t2026-09-30T20:22:33.0Z ##[error]The process failed with exit code 128\n"
    )
    out = condense_log(raw)
    assert out.startswith("Errors: fatal: couldn't find remote ref refs/pull/9/merge | The process failed")
    assert len(out) <= ci_checks.LOG_TAIL_CHARS
    assert out.rstrip().endswith("exit code 128")


def test_is_ledger_only() -> None:
    assert is_ledger_only([".factory/demands/demands.json"])
    assert is_ledger_only([".factory/demands/demands.json", ".factory/roadmap/darkfac.json"])
    assert not is_ledger_only([])
    assert not is_ledger_only([".factory/demands/demands.json", "core/git/autonomy.py"])
    assert not is_ledger_only([".factory/state.json"])
    assert not is_ledger_only([".factory/demands/../../core/x.py"])


# ----------------------------------------------------------------------
# ensure_green
# ----------------------------------------------------------------------


def test_no_checks_is_accepted_only_after_the_grace_window() -> None:
    clock = FakeClock()
    verdict = gate(GhScript([]), clock, no_checks_grace_s=30)
    assert verdict.status == "no_checks" and verdict.passed
    assert sum(clock.sleeps) == pytest.approx(30)


def test_no_checks_grace_zero_returns_immediately() -> None:
    clock = FakeClock()
    verdict = gate(GhScript([]), clock, no_checks_grace_s=0)
    assert verdict.status == "no_checks" and clock.sleeps == []


def test_checks_appearing_inside_the_grace_window_are_honoured() -> None:
    clock = FakeClock()
    runner = GhScript([NO_CHECKS, [check("fail")]], failed_log="boom")
    verdict = gate(runner, clock, no_checks_grace_s=30)
    assert verdict.status == "failed"


def test_pending_then_green() -> None:
    clock = FakeClock()
    runner = GhScript([[check("pending")], [check("pending")], [check("pass")]])
    verdict = gate(runner, clock)
    assert verdict.status == "green" and verdict.passed
    assert clock.sleeps == [10, 10]


def test_pending_beyond_timeout_is_not_green() -> None:
    clock = FakeClock()
    verdict = gate(GhScript([[check("pending")]]), clock, timeout_s=35)
    assert verdict.status == "pending_timeout" and not verdict.passed
    assert verdict.pending_checks == (f"DarkFac CI / {PR_JOB}",)
    assert verdict.waited_s >= 35
    assert clock.sleeps == [10, 10, 10, 5]


def test_zero_timeout_does_not_wait() -> None:
    clock = FakeClock()
    verdict = gate(GhScript([[check("pending")]]), clock, timeout_s=0)
    assert verdict.status == "pending_timeout" and clock.sleeps == []


def test_gh_errors_are_retried_and_never_turn_into_a_pass() -> None:
    clock = FakeClock()
    flaky = GhScript([(1, "HTTP 502"), (1, "HTTP 502"), [check("pass")]])
    assert gate(flaky, clock).status == "green"

    clock = FakeClock()
    down = GhScript([(1, "HTTP 502 bad gateway")])
    verdict = gate(down, clock, timeout_s=30)
    assert verdict.status == "pending_timeout" and not verdict.passed
    assert "502" in verdict.detail


def test_runner_exceptions_count_as_errors() -> None:
    clock = FakeClock()

    def boom(args: Sequence[str], cwd: Path) -> Any:
        raise OSError("gh vanished")

    verdict = gate(boom, clock, timeout_s=20)  # type: ignore[arg-type]
    assert verdict.status == "pending_timeout" and "gh vanished" in verdict.detail


def test_wait_seconds_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ci_checks.resolve_wait_seconds() == 1200
    monkeypatch.setenv(ci_checks.CI_WAIT_ENV, "45")
    assert ci_checks.resolve_wait_seconds() == 45
    assert ci_checks.resolve_wait_seconds(7) == 7
    monkeypatch.setenv(ci_checks.CI_WAIT_ENV, "not-a-number")
    assert ci_checks.resolve_wait_seconds() == 1200

    monkeypatch.setenv(ci_checks.CI_WAIT_ENV, "25")
    clock = FakeClock()
    verdict = ensure_green(
        5, Path("."), GhScript([[check("pending")]]), poll_s=10, sleep_fn=clock.sleep, clock_fn=clock.clock
    )
    assert verdict.status == "pending_timeout" and sum(clock.sleeps) == pytest.approx(25)


def test_a_stubbed_sleep_cannot_turn_the_wait_into_a_busy_loop() -> None:
    calls = {"n": 0}

    def sleep(_s: float) -> None:
        calls["n"] += 1

    verdict = ensure_green(
        5,
        Path("."),
        GhScript([[check("pending")]]),
        timeout_s=100,
        poll_s=20,
        sleep_fn=sleep,
        clock_fn=lambda: 0.0,
    )
    assert verdict.status == "pending_timeout" and calls["n"] == 5


def test_failed_reports_checks_log_tail_and_fails_fast() -> None:
    clock = FakeClock()
    log = (
        f"{PR_JOB}\tUNKNOWN STEP\t2026-09-30T20:22:33.0Z [ERROR] Candidate worktree is dirty\n"
        f"{PR_JOB}\tUNKNOWN STEP\t2026-09-30T20:22:33.1Z token ghp_TOPSECRET9 here\n"
        f"{PR_JOB}\tUNKNOWN STEP\t2026-09-30T20:22:33.2Z ##[error]Process completed with exit code 1.\n"
        f"{PR_JOB}\tUNKNOWN STEP\t2026-09-30T20:22:33.3Z Post job cleanup.\n"
    )
    runner = GhScript([[check("fail"), check("pending", "other")]], failed_log=log)
    verdict = gate(runner, clock)
    assert verdict.status == "failed" and not verdict.passed
    assert verdict.failed_checks == (f"DarkFac CI / {PR_JOB}",)
    assert "Candidate worktree is dirty" in verdict.log_tail
    assert "Post job cleanup" not in verdict.log_tail
    assert "ghp_TOPSECRET9" not in verdict.log_tail
    assert clock.sleeps == []  # failed fast, did not wait for the pending check
    assert ["run", "view", "111", "--log-failed"] in runner.calls
    assert verdict.base_red is False  # base has no runs in this script


def test_failed_with_the_same_check_red_on_main_is_base_red() -> None:
    runner = red_main_script(checks=[[check("fail"), check("pass", "pr-validation (windows-latest)")]])
    verdict = gate(runner, FakeClock())
    assert verdict.status == "failed" and verdict.base_red is True
    assert ["run", "list", "--branch", "main", "--status", "completed", "--limit", "10", "--workflow", "DarkFac CI",
            "--json", "databaseId,conclusion,headSha,url,workflowName,displayTitle"] in runner.calls
    assert ["run", "view", "900", "--json", "jobs"] in runner.calls


def test_failed_with_a_check_green_on_main_is_not_base_red() -> None:
    # `trusted-pr-policy` is skipped on main: the PR itself broke it.
    runner = red_main_script(checks=[[check("fail"), check("fail", "trusted-pr-policy", run=222)]])
    verdict = gate(runner, FakeClock())
    assert verdict.status == "failed" and verdict.base_red is False
    assert len(verdict.failed_checks) == 2


def test_base_red_requires_the_base_run_to_be_red_and_listable() -> None:
    failing = [check("fail")]
    green_main = red_main_script(base_runs=[{"databaseId": 900, "conclusion": "success"}])
    assert base_branch_is_red(green_main, Path("."), "main", failing) is False
    assert base_branch_is_red(red_main_script(run_list_rc=1), Path("."), "main", failing) is False
    assert base_branch_is_red(red_main_script(base_jobs={}), Path("."), "main", failing) is False
    # The latest run was cancelled: fall back to the newest conclusive one.
    cancelled_first = red_main_script(
        base_runs=[
            {"databaseId": 901, "conclusion": "cancelled"},
            {"databaseId": 900, "conclusion": "failure"},
        ]
    )
    assert base_branch_is_red(cancelled_first, Path("."), "main", failing) is True
    assert base_branch_is_red(red_main_script(), Path("."), "main", []) is False


# ----------------------------------------------------------------------
# _deliver: real git + scripted gh
# ----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


def _ident(repo: Path) -> None:
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")


class FakeRepoGh(GhScript):
    """``GhScript`` plus the PR lifecycle, simulated against a local bare repository."""

    def __init__(self, bare: Path, checks: Sequence[Any], **kwargs: Any) -> None:
        super().__init__(checks, **kwargs)
        self.bare = bare
        self.prs: dict[int, dict[str, Any]] = {}

    def _bare(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--git-dir", str(self.bare), *args], capture_output=True, text=True, encoding="utf-8"
        )

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        a = list(args)
        if a[:2] == ["pr", "list"]:
            self.calls.append(a)
            head = a[a.index("--head") + 1]
            state = a[a.index("--state") + 1].upper()
            rows = [
                {"number": n, "url": f"https://github.com/x/y/pull/{n}", "headRefOid": p["oid"]}
                for n, p in self.prs.items()
                if p["head"] == head and p["state"] == state
            ]
            return self.result(a, 0, json.dumps(rows))
        if a[:2] == ["pr", "create"]:
            self.calls.append(a)
            number = len(self.prs) + 1
            self.prs[number] = {"head": a[a.index("--head") + 1], "state": "OPEN", "oid": None}
            return self.result(a, 0, f"https://github.com/x/y/pull/{number}\n")
        if a[:2] == ["pr", "edit"] or a[:2] == ["label", "create"]:
            self.calls.append(a)
            return self.result(a, 0)
        if a[:2] == ["pr", "view"]:
            self.calls.append(a)
            p = self.prs[int(a[2])]
            merge_commit = {"oid": p["oid"]} if p["state"] == "MERGED" else None
            return self.result(a, 0, json.dumps({"state": p["state"], "mergeCommit": merge_commit}))
        if a[:2] == ["pr", "merge"]:
            self.calls.append(a)
            p = self.prs[int(a[2])]
            sha = self._bare("rev-parse", f"refs/heads/{p['head']}").stdout.strip()
            main = self._bare("rev-parse", "refs/heads/main").stdout.strip()
            if self._bare("merge-base", "--is-ancestor", main, sha).returncode != 0:
                return self.result(a, 1, "", "Pull request is not mergeable")
            self._bare("update-ref", "refs/heads/main", sha)
            self._bare("update-ref", "-d", f"refs/heads/{p['head']}")
            p.update(state="MERGED", oid=sha)
            return self.result(a, 0)
        return super().__call__(args, cwd)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _ident(local)
    (local / "README.md").write_text("base\n", encoding="utf-8")
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "chore: initial")
    _git(local, "remote", "add", "origin", "https://github.com/x/y.git")
    _git(local, "config", f"url.{bare.as_posix()}.insteadOf", "https://github.com/x/y.git")
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return local, bare


def manager(local: Path, clock: FakeClock, timeout_s: float = 60) -> GitAutonomyManager:
    return GitAutonomyManager(
        local, ci_timeout_s=timeout_s, ci_poll_s=10, ci_sleep_fn=clock.sleep, ci_clock_fn=clock.clock
    )


def write_code(local: Path) -> None:
    (local / "feature.py").write_text("x = 1\n", encoding="utf-8")


def write_ledger(local: Path) -> None:
    ledger = local / ".factory" / "demands"
    ledger.mkdir(parents=True)
    (ledger / "demands.json").write_text(
        json.dumps([{"id": "USR-90", "title": "Gate"}], indent=2), encoding="utf-8"
    )


def deliver(local: Path, gh: FakeRepoGh, clock: FakeClock, **kwargs: Any) -> DeliveryReport:
    return manager(local, clock, **kwargs).deliver_branch("USR-90", "Gate", cwd=local, gh_runner=gh)


def test_delivery_without_checks_merges(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [])
    rep = deliver(local, gh, FakeClock())
    assert rep.ok and rep.action == "merged", rep.message
    assert "ci_gate=no_checks" in rep.message
    assert gh.count(["pr", "checks"]) >= 1  # the gate did look


def test_delivery_blocks_different_titles_for_same_id(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    ledger = local / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps([{"id": "USR-67", "title": "Original"}], indent=2), encoding="utf-8")
    _git(local, "add", ".factory/demands/demands.json")
    _git(local, "commit", "-m", "base ticket")
    _git(local, "push", "origin", "main")
    _git(local, "checkout", "-b", "ticket/usr-67")
    ledger.write_text(json.dumps([{"id": "USR-67", "title": "DarkHub"}], indent=2), encoding="utf-8")
    gh = FakeRepoGh(bare, [])

    rep = manager(local, FakeClock()).deliver_branch("USR-67", "DarkHub", cwd=local, gh_runner=gh, kind="queue")

    assert not rep.ok and rep.action == "conflict"
    assert "USR-67" in rep.message and "Original" in rep.message and "DarkHub" in rep.message
    assert gh.count(["pr", "merge"]) == 0


def test_delivery_waits_for_pending_then_merges(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("pending")], [check("pending")], [check("pass")]])
    clock = FakeClock()
    rep = deliver(local, gh, clock)
    assert rep.ok and rep.action == "merged", rep.message
    assert "ci_gate=green" in rep.message
    assert gh.count(["pr", "checks"]) == 3 and clock.sleeps == [10, 10]
    # merge happened strictly after the last checks call
    names = [" ".join(c[:2]) for c in gh.calls]
    assert names.index("pr merge") > max(i for i, n in enumerate(names) if n == "pr checks")


def test_delivery_pending_beyond_timeout_does_not_merge(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("pending")]])
    rep = deliver(local, gh, FakeClock(), timeout_s=30)
    assert not rep.ok and rep.action == "ci_pending", rep.message
    assert "ci_gate=pending_timeout" in rep.message and rep.pr_url
    assert gh.count(["pr", "merge"]) == 0
    assert not rep.cleaned
    assert gh.prs[1]["state"] == "OPEN"
    assert _git(local, "branch", "--list", "ticket/usr-90")
    assert _git(bare, "rev-parse", "--verify", "refs/heads/ticket/usr-90")
    assert _git(bare, "rev-parse", "refs/heads/main") == _git(local, "rev-parse", "origin/main")


def test_delivery_with_red_checks_does_not_merge(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("fail"), check("pass", "pr-validation (windows-latest)")]],
                    failed_log="AssertionError: boom\n##[error]Process completed with exit code 1.")
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "ci_failed", rep.message
    assert PR_JOB in rep.message and "AssertionError: boom" in rep.message
    assert gh.count(["pr", "merge"]) == 0 and not rep.cleaned
    assert gh.prs[1]["state"] == "OPEN"
    assert _git(bare, "rev-parse", "--verify", "refs/heads/ticket/usr-90")


def test_delivery_with_red_checks_also_red_on_main_reports_base_red(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_code(local)
    base = red_main_script(checks=[[check("fail")]])
    gh = FakeRepoGh(bare, [[check("fail")]], base_runs=base.base_runs, base_jobs=base.base_jobs)
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "base_red", rep.message
    assert "already red on main" in rep.message
    assert gh.count(["pr", "merge"]) == 0


def test_ledger_only_pr_skips_the_gate_even_with_red_main(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_ledger(local)
    gh = FakeRepoGh(bare, [[check("fail")]])
    rep = manager(local, FakeClock()).deliver_branch("USR-90", "Gate", cwd=local, gh_runner=gh, kind="queue")
    assert rep.ok and rep.action == "merged", rep.message
    assert "ci_gate=skipped_ledger_only" in rep.message
    assert gh.count(["pr", "checks"]) == 0 and gh.count(["run", "list"]) == 0


def test_ledger_only_implementation_is_rejected_by_ci_gate(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_ledger(local)
    _git(local, "add", ".factory/demands/demands.json")
    _git(local, "commit", "-m", "ledger only")
    gh = FakeRepoGh(bare, [[check("pass")]])

    blocked, note = manager(local, FakeClock())._ci_gate(
        1, "https://github.com/x/y/pull/1", "ticket/usr-90", "main", local, gh,
        kind="implementation",
    )

    assert blocked is not None and not blocked.ok and blocked.action == "no_changes"
    assert note == "ci_gate=rejected_ledger_only"
    assert not gh.calls


def test_pr_mixing_ledger_and_code_is_still_gated(repo: tuple[Path, Path]) -> None:
    local, bare = repo
    write_ledger(local)
    write_code(local)
    gh = FakeRepoGh(bare, [[check("fail")]])
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "ci_failed", rep.message
    assert gh.count(["pr", "merge"]) == 0


def test_merge_retry_after_resync_is_gated_again(repo: tuple[Path, Path], tmp_path: Path) -> None:
    """If main moves while CI runs, the re-synced head must pass the gate again."""
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("pass")]])

    def advance_main(call_number: int) -> None:
        if call_number != 1:
            return
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(other)], check=True, capture_output=True)
        _ident(other)
        (other / "other.txt").write_text("o\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "--quiet", "-m", "feat: other")
        _git(other, "push", "--quiet", "origin", "main")

    gh.on_checks = advance_main
    rep = deliver(local, gh, FakeClock())
    assert rep.ok and rep.action == "merged", rep.message
    assert gh.count(["pr", "checks"]) == 2
    assert gh.count(["pr", "merge"]) == 2  # first refused (not mergeable), second after re-gate
    names = [" ".join(c[:2]) for c in gh.calls]
    second_merge = len(names) - 1 - names[::-1].index("pr merge")
    last_checks = len(names) - 1 - names[::-1].index("pr checks")
    assert last_checks < second_merge


def test_retry_gate_failure_blocks_the_second_merge(repo: tuple[Path, Path], tmp_path: Path) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("pass")], [check("fail")]], failed_log="##[error]regressed after rebase")

    def advance_main(call_number: int) -> None:
        if call_number != 1:
            return
        other = tmp_path / "other"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(other)], check=True, capture_output=True)
        _ident(other)
        (other / "other.txt").write_text("o\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "--quiet", "-m", "feat: other")
        _git(other, "push", "--quiet", "origin", "main")

    gh.on_checks = advance_main
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "ci_failed", rep.message
    assert gh.count(["pr", "merge"]) == 1  # only the first (refused) attempt


# ----------------------------------------------------------------------
# Contract tests
# ----------------------------------------------------------------------


def test_contract_a_failed_gate_never_reaches_gh_pr_merge(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    local, bare = repo
    write_code(local)
    gh = FakeRepoGh(bare, [[check("pass")]])  # gh itself would happily say green
    monkeypatch.setattr(
        autonomy,
        "ensure_green",
        lambda *a, **k: CiVerdict(status="failed", failed_checks=("DarkFac CI / x",), base_red=False),
    )
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "ci_failed"
    assert all(c[:2] != ["pr", "merge"] for c in gh.calls)

    monkeypatch.setattr(
        autonomy, "ensure_green", lambda *a, **k: CiVerdict(status="pending_timeout", pending_checks=("x",))
    )
    rep = deliver(local, gh, FakeClock())
    assert not rep.ok and rep.action == "ci_pending"
    assert all(c[:2] != ["pr", "merge"] for c in gh.calls)


def test_contract_complete_ticket_does_not_bypass_the_gate(
    repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    local, bare = repo
    write_code(local)
    from core.demands.models import UserTicket
    from core.demands.store import DemandsStore
    from core.roadmap.models import DeliveryStatus

    (local / ".factory" / "demands").mkdir(parents=True)
    DemandsStore(local / ".factory" / "demands" / "demands.json").save_ticket(
        UserTicket(id="USR-91", project_id="darkfac", title="Ticket de teste", status=DeliveryStatus.PLANNED)
    )
    gh = FakeRepoGh(bare, [[check("fail")]])
    monkeypatch.setattr(GitAutonomyManager, "gh_ready", lambda self, cwd=None, gh_runner=None: True)
    clock = FakeClock()
    rep = GitAutonomyManager(
        local, ci_timeout_s=30, ci_poll_s=10, ci_sleep_fn=clock.sleep, ci_clock_fn=clock.clock
    ).complete_ticket("USR-91", cwd=local, gh_runner=gh)
    assert not rep.ok and rep.delivery is not None and rep.delivery.action == "ci_failed"
    assert gh.count(["pr", "merge"]) == 0


def _statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    lists: list[list[ast.stmt]] = []
    for child in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(child, field, None)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                lists.append(value)
    return lists


def test_contract_ast_the_only_pr_merge_call_is_in_deliver_after_the_gate() -> None:
    tree = ast.parse(Path(autonomy.__file__).read_text(encoding="utf-8"))

    def is_pr_merge(node: ast.AST) -> bool:
        if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
            return False
        first, second = node.elts[0], node.elts[1]
        return (
            isinstance(first, ast.Constant) and first.value == "pr"
            and isinstance(second, ast.Constant) and second.value == "merge"
        )

    merge_literals = [n for n in ast.walk(tree) if is_pr_merge(n)]
    assert len(merge_literals) == 1, "exactly one `gh pr merge` argument list may exist in autonomy.py"

    deliver_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_deliver"
    )
    assert merge_literals[0] in list(ast.walk(deliver_fn)), "`gh pr merge` must live inside _deliver"

    merge_calls = sorted(
        (
            n for n in ast.walk(deliver_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "runner"
            and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "merge_args"
        ),
        key=lambda n: n.lineno,
    )
    gate_assigns = sorted(
        (
            n for n in ast.walk(deliver_fn)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "_ci_gate"
        ),
        key=lambda n: n.lineno,
    )
    assert merge_calls, "_deliver no longer invokes the merge"
    assert len(gate_assigns) == len(merge_calls), "every merge invocation needs its own gate call"
    for gate_assign, merge_call in zip(gate_assigns, merge_calls):
        assert gate_assign.lineno < merge_call.lineno

    # ... and each gate result is honoured right away: `if blocked is not None: return blocked`.
    for gate_assign in gate_assigns:
        target = gate_assign.targets[0]
        assert isinstance(target, ast.Tuple) and isinstance(target.elts[0], ast.Name)
        blocked_name = target.elts[0].id
        for stmts in _statement_lists(deliver_fn):
            if gate_assign in stmts:
                following = stmts[stmts.index(gate_assign) + 1]
                assert isinstance(following, ast.If)
                assert ast.unparse(following.test) == f"{blocked_name} is not None"
                assert any(isinstance(s, ast.Return) for s in following.body)
                break
        else:  # pragma: no cover - defensive
            pytest.fail("gate call not found in any statement list")

    # The gate itself must call ensure_green (the name tests monkeypatch).
    gate_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_ci_gate")
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ensure_green"
        for n in ast.walk(gate_fn)
    )


def test_delivery_report_action_documents_the_ci_actions() -> None:
    description = DeliveryReport.model_fields["action"].description or ""
    for action in ("ci_pending", "ci_failed", "base_red"):
        assert action in description


# ----------------------------------------------------------------------
# check-main
# ----------------------------------------------------------------------


def test_check_main_green() -> None:
    runner = GhScript(base_runs=[{"databaseId": 1, "conclusion": "success", "headSha": "a" * 40, "url": "u"}])
    status = check_main(runner, Path("."))
    assert status.state == "green" and status.short_sha == "a" * 7
    code, payload = autonomy.run_check_main(runner=runner)
    assert code == 0 and payload["state"] == "green"
    assert ["run", "list", "--branch", "main", "--workflow", "DarkFac CI", "--status", "completed", "--limit", "10",
            "--json", "databaseId,conclusion,headSha,url,workflowName,displayTitle"] in runner.calls


def test_check_main_red_reports_sha_jobs_and_link() -> None:
    runner = red_main_script()
    code, payload = autonomy.run_check_main(runner=runner)
    assert code == 1
    assert payload["state"] == "red" and payload["short_sha"] == "5da723e"
    assert payload["run_url"].endswith("/runs/900")
    assert payload["red_jobs"] == [{"name": MAIN_JOB, "url": "https://github.com/x/y/actions/runs/900/job/1"}]
    text = autonomy.format_check_main(payload)
    assert "[MAIN_RED]" in text and "5da723e04ace7e1ee7e30321ca728d6c34036757" in text
    assert MAIN_JOB in text and "/runs/900/job/1" in text
    text.encode("ascii")  # no emojis / non-ASCII in CLI output


def test_check_main_skips_cancelled_runs_and_reports_unknown_on_gh_failure() -> None:
    runner = red_main_script(
        base_runs=[
            {"databaseId": 901, "conclusion": "cancelled", "headSha": "b" * 40},
            {"databaseId": 900, "conclusion": "success", "headSha": "a" * 40},
        ]
    )
    assert autonomy.run_check_main(runner=runner)[0] == 0
    code, payload = autonomy.run_check_main(runner=red_main_script(run_list_rc=1))
    assert code == 2 and payload["state"] == "unknown" and "502" in payload["detail"]
    code, payload = autonomy.run_check_main(runner=GhScript(base_runs=[]))
    assert code == 2 and payload["state"] == "unknown"


def test_check_main_open_ticket_is_idempotent_per_sha() -> None:
    created: list[tuple[str, str, list[str]]] = []
    ledger: dict[str, str] = {}

    def finder(sha: str) -> Optional[str]:
        return ledger.get(sha)

    def creator(title: str, problem: str, criteria: Sequence[str]) -> Optional[str]:
        created.append((title, problem, list(criteria)))
        ledger["5da723e"] = "USR-86"
        return "USR-86"

    runner = red_main_script()
    code, first = autonomy.run_check_main(runner=runner, open_ticket=True, ticket_finder=finder, ticket_creator=creator)
    assert code == 1 and first["ticket"] == {"id": "USR-86", "created": True}
    code, second = autonomy.run_check_main(runner=runner, open_ticket=True, ticket_finder=finder, ticket_creator=creator)
    assert code == 1 and second["ticket"] == {"id": "USR-86", "created": False}
    assert len(created) == 1
    title, problem, criteria = created[0]
    assert "5da723e" in title and len(title) <= 120 and MAIN_JOB in title
    assert "5da723e04ace7e1ee7e30321ca728d6c34036757" in problem and "/runs/900" in problem
    assert criteria


def test_check_main_does_not_open_a_ticket_when_main_is_green_or_flag_is_off() -> None:
    def creator(*_a: Any) -> Optional[str]:
        raise AssertionError("must not create a ticket")

    green = GhScript(base_runs=[{"databaseId": 1, "conclusion": "success", "headSha": "a" * 40}])
    assert autonomy.run_check_main(runner=green, open_ticket=True, ticket_creator=creator)[0] == 0
    code, payload = autonomy.run_check_main(runner=red_main_script(), ticket_creator=creator)
    assert code == 1 and "ticket" not in payload


def test_check_main_ticket_creation_failure_keeps_exit_code_red() -> None:
    code, payload = autonomy.run_check_main(
        runner=red_main_script(), open_ticket=True, ticket_finder=lambda s: None, ticket_creator=lambda t, p, c: None
    )
    assert code == 1 and payload["ticket"]["id"] is None and payload["ticket"]["created"] is False


def test_find_ticket_for_sha_matches_title_or_tags(tmp_path: Path) -> None:
    ledger = tmp_path / ".factory" / "demands"
    ledger.mkdir(parents=True)
    (ledger / "demands.json").write_text(
        json.dumps(
            [
                {"id": "USR-10", "title": "Outra coisa", "tags": ["user-demand"]},
                {"id": "USR-11", "title": "CI vermelho no main @5DA723E: job", "tags": []},
                {"id": "USR-12", "title": "Sem relacao", "tags": ["main-red:abc1234"]},
            ]
        ),
        encoding="utf-8",
    )
    assert autonomy.find_ticket_for_sha("5da723e", tmp_path) == "USR-11"
    assert autonomy.find_ticket_for_sha("abc1234", tmp_path) == "USR-12"
    assert autonomy.find_ticket_for_sha("0000000", tmp_path) is None
    assert autonomy.find_ticket_for_sha("", tmp_path) is None


def test_create_defect_ticket_uses_an_argument_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, "[+] Ticket USR-99 registrado na fila: 'x'\n", "")

    monkeypatch.setattr(autonomy.subprocess, "run", fake_run)
    ticket_id = autonomy.create_defect_ticket("Titulo", "Problema", ["a", "b"], tmp_path)
    assert ticket_id == "USR-99"
    cmd = seen["cmd"]
    assert isinstance(cmd, list) and seen["kwargs"].get("shell") is not True
    assert cmd[1].endswith("run_ticket.py")
    assert cmd[2:4] == ["--create", "--queue-only"]
    assert cmd[-3:] == ["--criteria", "a", "b"] and cmd[cmd.index("--title") + 1] == "Titulo"

    monkeypatch.setattr(
        autonomy.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "boom")
    )
    assert autonomy.create_defect_ticket("Titulo", "Problema", ["a"], tmp_path) is None


def test_check_main_notify_is_non_fatal() -> None:
    sent: list[tuple[str, str]] = []

    def notifier(title: str, message: str) -> bool:
        sent.append((title, message))
        return True

    code, payload = autonomy.run_check_main(runner=red_main_script(), notify=True, notifier=notifier)
    assert code == 1 and payload["notified"] is True
    assert "5da723e" in sent[0][0] and MAIN_JOB in sent[0][1] and "/runs/900" in sent[0][1]

    def exploding(title: str, message: str) -> bool:
        raise RuntimeError("telegram down")

    code, payload = autonomy.run_check_main(runner=red_main_script(), notify=True, notifier=exploding)
    assert code == 1 and payload["notified"] is False

    green = GhScript(base_runs=[{"databaseId": 1, "conclusion": "success", "headSha": "a" * 40}])
    code, payload = autonomy.run_check_main(runner=green, notify=True, notifier=notifier)
    assert code == 0 and "notified" not in payload and len(sent) == 1


def test_default_notifier_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import core.notifications as notifications

    class Broken:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(notifications, "NotificationService", Broken)
    assert autonomy.notify_main_red("t", "m") is False

    class Quiet:
        def notify(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(notifications, "NotificationService", Quiet)
    assert autonomy.notify_main_red("t", "m") is False

    captured: dict[str, Any] = {}

    class Working:
        def notify(self, **kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

    monkeypatch.setattr(notifications, "NotificationService", Working)
    assert autonomy.notify_main_red("t", "m") is True
    assert captured["title"] == "t" and captured["message"] == "m"


def test_check_main_cli_exit_codes_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(autonomy, "_run_gh", red_main_script())
    assert autonomy.main(["check-main", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "red" and payload["short_sha"] == "5da723e"

    green = GhScript(base_runs=[{"databaseId": 1, "conclusion": "success", "headSha": "a" * 40}])
    monkeypatch.setattr(autonomy, "_run_gh", green)
    assert autonomy.main(["check-main"]) == 0
    assert "[MAIN_GREEN]" in capsys.readouterr().out


def test_check_main_cli_module_entrypoint_exposes_the_flags() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "core.git.autonomy", "check-main", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(Path(autonomy.__file__).resolve().parents[2]),
    )
    assert proc.returncode == 0, proc.stderr
    for flag in ("--json", "--open-ticket", "--notify"):
        assert flag in proc.stdout
