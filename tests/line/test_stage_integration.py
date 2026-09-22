"""Tests for the `gh`-CLI GitHub integration stage (HF-27-06).

Every test uses a local `git init --bare` repository under `tmp_path` as the
"origin" remote (same pattern as `tests/line/test_workspace.py`) and a fake
`gh` executable placed on `PATH` that never touches the network. The fake
`gh`'s `pr merge` implementation performs a *real* local git squash-merge
(via `commit-tree` + `push`) against the bare origin so the stage's
post-merge ancestry confirmation (FACTORY_RULES rule 11) is exercised
against genuine repository state rather than a stub.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.stage_integration import IntegrationStageHandler
from core.line.workspace import checkout
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import Claim, JobKey, StageContext

# --------------------------------------------------------------------------
# git helpers (mirrors tests/line/test_workspace.py; no network)
# --------------------------------------------------------------------------


def _win_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_win_kwargs(),
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path, default_branch: str = "main") -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)

    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", default_branch], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(["add", "."], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", default_branch], cwd=seed)
    return origin


def _project(repo_url: str, default_branch: str = "main") -> ProjectDescriptor:
    return ProjectDescriptor(
        id="acme",
        name="Acme Project",
        repo_url=repo_url,
        default_branch=default_branch,
    )


def _context(run_id: str) -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id="T1", plan_version="1.0", stage="integration", iteration=0)
    claim = Claim(
        job_key=jk,
        lease_id=f"lease_{run_id}",
        owner="worker-1",
        fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    return StageContext(
        claim=claim,
        plan_ref="plan://line",
        plan_digest="a" * 64,
        config_version="v1",
        environment_ref="acme",
        identity="worker-1",
        route_ref="route://line/integration",
        memory_version="mem_v1",
    )


# --------------------------------------------------------------------------
# Fake `gh` CLI: JSON-spec-driven, records every invocation, and performs a
# real local squash-merge (commit-tree + push) for `pr merge` so ancestry
# confirmation has genuine repository state to check.
# --------------------------------------------------------------------------

_FAKE_GH_SCRIPT = textwrap.dedent(
    '''\
    import json
    import os
    import subprocess
    import sys


    def _git(args, cwd):
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )


    def main():
        spec = json.loads(os.environ.get({env_var!r}, "{{}}"))
        argv = sys.argv[1:]
        cwd = os.getcwd()

        calls_path = spec.get("calls_path")
        if calls_path:
            existing = []
            if os.path.exists(calls_path):
                with open(calls_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            existing.append(argv)
            with open(calls_path, "w", encoding="utf-8") as fh:
                json.dump(existing, fh)

        def starts(*names):
            names = list(names)
            return argv[: len(names)] == names

        if starts("pr", "list"):
            sys.stdout.write(json.dumps(spec.get("pr_list", [])))
            sys.exit(int(spec.get("pr_list_returncode", 0)))

        if starts("pr", "create"):
            rc = int(spec.get("pr_create_returncode", 0))
            sys.stdout.write(spec.get("pr_create_stdout", ""))
            sys.stderr.write(spec.get("pr_create_stderr", ""))
            sys.exit(rc)

        if starts("pr", "checks"):
            rc = int(spec.get("pr_checks_returncode", 0))
            if rc != 0:
                sys.stderr.write(spec.get("pr_checks_stderr", ""))
                sys.exit(rc)
            sys.stdout.write(json.dumps(spec.get("pr_checks", [])))
            sys.exit(0)

        if starts("run", "view"):
            sys.stdout.write(spec.get("run_view_log", ""))
            sys.exit(int(spec.get("run_view_returncode", 0)))

        if starts("pr", "merge"):
            is_auto = "--auto" in argv
            key = "pr_merge_auto" if is_auto else "pr_merge"
            rc = int(spec.get(f"{{key}}_returncode", 0))
            if rc != 0:
                sys.stderr.write(spec.get(f"{{key}}_stderr", ""))
                sys.exit(rc)
            default_branch = spec.get("default_branch", "main")
            state_path = spec.get("state_path")
            fetch = _git(["fetch", "origin", default_branch], cwd)
            parent = _git(["rev-parse", f"origin/{{default_branch}}"], cwd).stdout.strip()
            tree = _git(["rev-parse", "HEAD^{{tree}}"], cwd).stdout.strip()
            new_commit = _git(
                [
                    "-c", "user.name=DarkFac",
                    "-c", "user.email=darkfac@users.noreply.github.com",
                    "commit-tree", tree, "-p", parent, "-m", "squash merge via fake gh",
                ],
                cwd,
            ).stdout.strip()
            push = _git(["push", "origin", f"{{new_commit}}:refs/heads/{{default_branch}}"], cwd)
            if push.returncode != 0:
                sys.stderr.write(push.stderr)
                sys.exit(1)
            if state_path:
                with open(state_path, "w", encoding="utf-8") as fh:
                    json.dump({{"merge_sha": new_commit}}, fh)
            sys.exit(0)

        if starts("pr", "view"):
            state_path = spec.get("state_path")
            merge_sha = ""
            if state_path and os.path.exists(state_path):
                with open(state_path, "r", encoding="utf-8") as fh:
                    merge_sha = json.load(fh).get("merge_sha", "")
            payload = {{
                "mergeCommit": {{"oid": merge_sha}} if merge_sha else None,
                "state": "MERGED" if merge_sha else "OPEN",
                "url": spec.get("pr_url", ""),
            }}
            sys.stdout.write(json.dumps(payload))
            sys.exit(0)

        sys.stderr.write("fake gh: unknown subcommand " + " ".join(argv))
        sys.exit(1)


    if __name__ == "__main__":
        main()
    '''
)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a fake `gh` executable and return `(executable_path, set_spec)`."""
    env_var = "FAKE_GH_RESPONSE"
    script_path = tmp_path / "fake_gh.py"
    script_path.write_text(_FAKE_GH_SCRIPT.format(env_var=env_var), encoding="utf-8")
    cmd_path = tmp_path / "fake_gh.cmd"
    cmd_path.write_text(f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n', encoding="utf-8")

    calls_path = tmp_path / "gh_calls.json"
    state_path = tmp_path / "gh_state.json"

    def _set_spec(spec: dict[str, Any]) -> None:
        full = dict(spec)
        full.setdefault("calls_path", str(calls_path))
        full.setdefault("state_path", str(state_path))
        monkeypatch.setenv(env_var, json.dumps(full))

    def _calls() -> list[list[str]]:
        if not calls_path.is_file():
            return []
        return json.loads(calls_path.read_text(encoding="utf-8"))

    return str(cmd_path), _set_spec, _calls


# --------------------------------------------------------------------------
# Full-flow tests
# --------------------------------------------------------------------------


def test_full_flow_creates_pr_and_merges_on_green_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh

    ws = checkout(project, "run-1")
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n\nBuild a widget.\n")
    ws_mod.write_context(ws, "SPEC.md", "# Add widget\n\nShort spec.\n")
    ws_mod.commit(ws, "feat: add widget", "run-1:T1")
    ws_mod.push(ws)

    set_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/7\n",
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/7",
        }
    )

    handler = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result = handler.handle(_context("run-1"))

    assert result.outcome == "success", result.cause_code
    assert result.output_refs[0] == "https://github.com/acme/repo/pull/7"
    merge_sha = result.output_refs[1]
    assert len(merge_sha) == 40

    call_names = [" ".join(c[:2]) for c in calls()]
    assert "pr create" in call_names
    assert "pr merge" in call_names

    # The context dir was folded into the PR body and removed from the branch.
    assert not ws_mod.context_dir(ws).exists()

    # FACTORY_RULES rule 11: origin/<default> really reached the merge SHA.
    verify = _git(["merge-base", "--is-ancestor", merge_sha, "origin/main"], cwd=ws.path)
    assert verify.returncode == 0


def test_existing_open_pr_is_reused_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh

    ws = checkout(project, "run-2")
    ws_mod.write_context(ws, "DEMAND.md", "# Existing PR case\n")
    ws_mod.commit(ws, "feat: change", "run-2:T1")
    ws_mod.push(ws)

    set_spec(
        {
            "pr_list": [{"number": 42, "url": "https://github.com/acme/repo/pull/42", "state": "OPEN"}],
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/42",
        }
    )

    handler = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result = handler.handle(_context("run-2"))

    assert result.outcome == "success", result.cause_code
    assert result.output_refs[0] == "https://github.com/acme/repo/pull/42"

    call_names = [" ".join(c[:2]) for c in calls()]
    assert "pr create" not in call_names
    assert "pr merge" in call_names


def test_red_check_returns_retry_with_log_and_does_not_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh

    ws = checkout(project, "run-3")
    ws_mod.write_context(ws, "DEMAND.md", "# Broken build\n")
    ws_mod.commit(ws, "feat: broken", "run-3:T1")
    ws_mod.push(ws)

    set_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/9\n",
            "pr_checks": [{"bucket": "fail", "name": "build", "link": "https://x/runs/555"}],
            "run_view_log": "FAIL test_widget\nAssertionError: boom\n",
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/9",
        }
    )

    handler = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result = handler.handle(_context("run-3"))

    assert result.outcome == "retry"
    assert "build" in result.cause_code
    assert "AssertionError: boom" in result.cause_code

    call_names = [" ".join(c[:2]) for c in calls()]
    assert "pr merge" not in call_names


# --------------------------------------------------------------------------
# Merge-conflict cap: the agent is invoked at most once per run.
# --------------------------------------------------------------------------


def test_merge_conflict_calls_agent_once_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-4")

    # Diverge origin/main and the run branch on the same line of shared.txt
    # so the rebase always conflicts, no matter how many times it is tried.
    mirror_seed = tmp_path / "_advance_main"
    _git(["clone", str(origin), str(mirror_seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main", "origin/main"], cwd=mirror_seed)
    _git(["config", "user.email", "seed@example.com"], cwd=mirror_seed)
    _git(["config", "user.name", "Seed"], cwd=mirror_seed)
    (mirror_seed / "shared.txt").write_text("changed-on-main\n", encoding="utf-8")
    _git(["add", "shared.txt"], cwd=mirror_seed)
    _git(["commit", "-m", "advance main"], cwd=mirror_seed)
    _git(["push", "origin", "main"], cwd=mirror_seed)

    (ws.path / "shared.txt").write_text("changed-on-branch\n", encoding="utf-8")
    ws_mod.commit(ws, "feat: conflicting change", "run-4:T1")

    call_count = {"n": 0}

    def _stub_agent_no_real_fix(req: AgentRequest) -> AgentResult:
        call_count["n"] += 1
        # "Resolves" the conflict-marker state without ever matching origin's
        # content, so a second rebase attempt (simulating a later retry)
        # conflicts again.
        (req.cwd / "shared.txt").write_text("still-not-matching-main\n", encoding="utf-8")
        return AgentResult(ok=True, text="resolved", harness=req.harness, duration_s=0.1)

    handler = IntegrationStageHandler(
        project,
        gh_executable="gh-not-used",
        host_caps=["harness:claude"],
        agent_runner=_stub_agent_no_real_fix,
    )

    first = handler._rebase_onto_default(ws, "main")
    assert first is None  # resolved (from the stage's point of view) on the first attempt
    assert call_count["n"] == 1

    # A fresh conflict against origin/main is still there (the stub never
    # truly reconciled it), so the branch conflicts with origin/main again.
    second = handler._rebase_onto_default(ws, "main")
    assert second is not None
    assert second.outcome == "failed"
    assert second.cause_code == "merge_conflict"
    assert call_count["n"] == 1  # the agent was NOT invoked a second time
