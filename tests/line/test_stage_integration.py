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
from types import SimpleNamespace
from typing import Any

import pytest

from core.line import stage_integration
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.line.stage_build import DevelopmentStage
from core.line.stage_integration import IntegrationStageHandler
from core.line.workspace import checkout
from core.projects.models import ProjectCommands, ProjectDescriptor
from core.workflow.control_contracts import Claim, JobKey, StageContext
from core.workflow.control_store import SQLiteControlStore
from core.workflow.successors import _parse_retry_cause_code, materialize_result
from tests.line.conftest import write_python_shim

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


def _context(run_id: str, iteration: int = 0) -> StageContext:
    jk = JobKey(run_id=run_id, ticket_id="T1", plan_version="1.0", stage="integration", iteration=iteration)
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

        if starts("pr", "list") and "merged" in argv:
            sys.stdout.write(json.dumps(spec.get("pr_list_merged", [])))
            sys.exit(0)

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
            if "headRefOid" in argv:
                head = spec.get("pr_head_sha") or _git(["rev-parse", "HEAD"], cwd).stdout.strip()
                sys.stdout.write(json.dumps({{"headRefOid": head}}))
                sys.exit(0)
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
    cmd_path = write_python_shim(tmp_path / "fake_gh", script_path)

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


@pytest.mark.parametrize("validate_commands", [[], ["  "]])
def test_conflict_resolution_cannot_pass_without_validate_command(
    tmp_path: Path, validate_commands: list[str]
) -> None:
    project = _project(str(tmp_path)).model_copy(
        update={"commands": ProjectCommands(validate=validate_commands)}
    )
    handler = IntegrationStageHandler(project)

    ok, log = handler._run_validate(SimpleNamespace(path=tmp_path))

    assert ok is False
    assert "Nenhum comando de validate" in log


def test_red_ci_feedback_reaches_development_then_green_ci_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = ProjectDescriptor(
        id="acme", name="Acme", repo_url=str(origin), default_branch="main",
        commands=ProjectCommands(validate=["python check_status.py"]),
    )
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh
    ws = checkout(project, "run-ci")
    ws_mod.write_context(ws, "DEMAND.md", "# Fix the check\n")
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Fix check"}]))
    (ws.path / "check_status.py").write_text(
        "from pathlib import Path\nimport sys\nsys.exit(0 if Path('STATUS_OK').exists() else 1)\n",
        encoding="utf-8",
    )
    ws_mod.commit(ws, "feat: initial candidate", "run-ci:T1")
    ws_mod.push(ws)
    spec = {
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks": [{"bucket": "fail", "name": "build", "workflow": "CI",
                       "link": "https://github.com/acme/repo/actions/runs/555/job/1"}],
        "run_view_log": "FAIL check_status.py\nBearer example-secret\nAssertionError: CI_MARKER_103\n",
        "pr_url": "https://github.com/acme/repo/pull/7",
    }
    set_spec(spec)
    handler = IntegrationStageHandler(project, gh_executable=gh_path)
    red = handler.handle(_context("run-ci"))
    assert (red.outcome, red.cause_code) == ("retry", "retry:development")
    successors = materialize_result(
        _context("run-ci").claim.job_key, red, SQLiteControlStore(":memory:")
    )
    assert [job.stage for job in successors] == ["development"]
    feedback = (ws_mod.context_dir(ws) / "ci-1.log.md").read_text(encoding="utf-8")
    assert "CI failure 1: CI / build" in feedback
    assert "CI_MARKER_103" in feedback
    assert "example-secret" not in feedback
    assert "[REDACTED]" in feedback
    checks_before = sum(c[:2] == ["pr", "checks"] for c in calls())
    assert handler.handle(_context("run-ci")).cause_code == "retry:development"
    assert sum(c[:2] == ["pr", "checks"] for c in calls()) == checks_before

    prompts: list[str] = []

    def fix_agent(request: AgentRequest) -> AgentResult:
        prompts.append(request.prompt)
        (request.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")
        return AgentResult(ok=True, text="fixed", harness=request.harness, duration_s=0.01)

    development = DevelopmentStage(
        run_agent_func=fix_agent,
        pick_func=lambda *args, **kwargs: ("codex", None),
        routing_config=load_routing_config(),
    )
    assert development.run(project, "run-ci").outcome == "success"
    assert len(prompts) == 1 and "CI_MARKER_103" in prompts[0]
    assert "example-secret" not in prompts[0]
    assert ws_mod.find_commit_by_job(ws, "run-ci:fix-ci-1") is not None

    set_spec({**spec, "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}]})
    green = handler.handle(_context("run-ci", iteration=1))
    assert green.outcome == "success", green.cause_code
    assert not ws_mod.context_dir(ws).exists()


def test_ci_failure_cap_stops_another_red_check_without_repoll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh
    ws = checkout(project, "run-cap")
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Fix"}]))
    ws_mod.commit(ws, "feat: candidate", "run-cap:T1")
    ws_mod.push(ws)
    set_spec({
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks": [{"bucket": "fail", "name": "build", "workflow": "CI",
                       "link": "https://github.com/acme/repo/actions/runs/555/job/1"}],
        "run_view_log": "CI still red",
    })
    monkeypatch.setattr(stage_integration, "MAX_CI_ITERATIONS", 2)
    handler = IntegrationStageHandler(project, gh_executable=gh_path)
    assert handler.handle(_context("run-cap")).cause_code == "retry:development"
    second = handler.handle(_context("run-cap", iteration=1))
    assert (second.outcome, second.cause_code) == ("retry", "retry:development")
    assert (ws_mod.context_dir(ws) / "ci-1.log.md").is_file()
    assert (ws_mod.context_dir(ws) / "ci-2.log.md").is_file()
    # A third red result is terminal and cannot create another fix-up round.
    third = handler.handle(_context("run-cap", iteration=2))
    assert (third.outcome, third.cause_code) == ("failed", "ci_iteration_cap")
    assert not (ws_mod.context_dir(ws) / "ci-3.log.md").exists()
    checks_before = sum(c[:2] == ["pr", "checks"] for c in calls())
    assert handler.handle(_context("run-cap")).cause_code == "retry:development"
    assert sum(c[:2] == ["pr", "checks"] for c in calls()) == checks_before


def test_checks_query_error_waits_before_retrying_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, _calls = fake_gh
    ws = checkout(project, "run-query-error")
    ws_mod.write_context(ws, "DEMAND.md", "# Query checks\n")
    ws_mod.commit(ws, "feat: candidate", "run-query-error:T1")
    ws_mod.push(ws)
    set_spec({
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks_returncode": 1,
        "pr_checks_stderr": "HTTP 502",
    })
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    handler = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: now, ci_check_window_s=0
    )
    result = handler.handle(_context("run-query-error"))
    assert result.outcome == "retry"
    assert _parse_retry_cause_code(result.cause_code) == (
        None, (now + timedelta(seconds=handler.ci_check_window_s)).isoformat()
    )


def test_rebase_push_with_no_checks_does_not_merge_when_ci_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh
    ws = checkout(project, "run-no-checks")
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n")
    workflows = ws.path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    ws_mod.commit(ws, "feat: add CI and widget", "run-no-checks:T1")
    ws_mod.push(ws)
    set_spec({
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks": [],
    })
    monkeypatch.setenv("DARKFAC_CI_NO_CHECKS_GRACE_SECONDS", "30")
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    handler = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: now, ci_check_window_s=300
    )
    result = handler.handle(_context("run-no-checks"))
    assert result.outcome == "retry"
    assert _parse_retry_cause_code(result.cause_code) == (
        None, (now + timedelta(seconds=30)).isoformat()
    )
    assert sum(call[:2] == ["pr", "checks"] for call in calls()) == 1
    assert not any(call[:2] == ["pr", "merge"] for call in calls())

    later = now + timedelta(seconds=30)
    resumed = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: later, ci_check_window_s=300
    ).handle(_context("run-no-checks", iteration=1))
    assert resumed.outcome == "retry"
    assert _parse_retry_cause_code(resumed.cause_code) == (
        None, (later + timedelta(seconds=300)).isoformat()
    )
    assert not any(call[:2] == ["pr", "merge"] for call in calls())


def test_no_workflow_merges_only_after_grace_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    monkeypatch.setenv("DARKFAC_CI_NO_CHECKS_GRACE_SECONDS", "30")
    gh_path, set_spec, calls = fake_gh
    ws = checkout(project, "run-no-workflow")
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n")
    ws_mod.commit(ws, "feat: widget", "run-no-workflow:T1")
    ws_mod.push(ws)
    set_spec({
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks": [],
    })
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    current = [now]
    original_gate = stage_integration.ci_checks.ensure_green

    def one_snapshot(*args: Any, **kwargs: Any):
        assert kwargs["timeout_s"] == 0
        return original_gate(*args, **kwargs)

    monkeypatch.setattr(stage_integration.ci_checks, "ensure_green", one_snapshot)
    handler = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: current[0], ci_check_window_s=300
    )
    first = handler.handle(_context("run-no-workflow"))
    assert first.outcome == "retry"
    assert _parse_retry_cause_code(first.cause_code) == (
        None, (now + timedelta(seconds=30)).isoformat()
    )
    assert sum(call[:2] == ["pr", "checks"] for call in calls()) == 1
    assert not any(call[:2] == ["pr", "merge"] for call in calls())

    current[0] = now + timedelta(seconds=29)
    second = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: current[0], ci_check_window_s=300
    ).handle(_context("run-no-workflow", iteration=1))
    assert second.outcome == "retry"
    assert _parse_retry_cause_code(second.cause_code) == (
        None, (now + timedelta(seconds=30)).isoformat()
    )
    assert not any(call[:2] == ["pr", "merge"] for call in calls())

    current[0] = now + timedelta(seconds=30)
    third = IntegrationStageHandler(
        project, gh_executable=gh_path, clock=lambda: current[0], ci_check_window_s=300
    ).handle(_context("run-no-workflow", iteration=2))
    assert third.outcome == "success", third.cause_code
    assert sum(call[:2] == ["pr", "merge"] for call in calls()) == 1


def test_green_checks_on_different_pr_head_do_not_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh
    ws = checkout(project, "run-stale-head")
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n")
    ws_mod.commit(ws, "feat: widget", "run-stale-head:T1")
    ws_mod.push(ws)
    set_spec({
        "pr_list": [{"number": 7, "url": "https://github.com/acme/repo/pull/7", "state": "OPEN"}],
        "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
        "pr_head_sha": "b" * 40,
    })
    handler = IntegrationStageHandler(project, gh_executable=gh_path, ci_check_window_s=0)
    result = handler.handle(_context("run-stale-head"))
    assert result.outcome == "retry"
    assert not any(call[:2] == ["pr", "merge"] for call in calls())


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
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "Broken build"}]))
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
    assert result.cause_code == "retry:development"
    feedback = (ws_mod.context_dir(ws) / "ci-1.log.md").read_text(encoding="utf-8")
    assert "build" in feedback
    assert "AssertionError: boom" in feedback

    call_names = [" ".join(c[:2]) for c in calls()]
    assert "pr merge" not in call_names


# --------------------------------------------------------------------------
# Merge-conflict cap: the agent is invoked at most once per run.
# --------------------------------------------------------------------------


def _advance_main(tmp_path: Path, origin: Path, content: str, name: str) -> None:
    clone = tmp_path / name
    _git(["clone", str(origin), str(clone)], cwd=tmp_path)
    _git(["checkout", "-B", "main", "origin/main"], cwd=clone)
    _git(["config", "user.email", "seed@example.com"], cwd=clone)
    _git(["config", "user.name", "Seed"], cwd=clone)
    (clone / "shared.txt").write_text(content, encoding="utf-8")
    _git(["add", "shared.txt"], cwd=clone)
    _git(["commit", "-m", f"advance main: {content.strip()}"], cwd=clone)
    _git(["push", "origin", "main"], cwd=clone)


def _merging_agent(call_count: dict[str, int]):
    """Stub agent that really merges origin/main and resolves shared.txt."""

    def _run(req: AgentRequest) -> AgentResult:
        call_count["n"] += 1
        subprocess.run(
            [
                "git", "-c", "user.name=Agent", "-c", "user.email=agent@example.com",
                "merge", "--no-commit", "origin/main",
            ],
            cwd=str(req.cwd), capture_output=True, text=True, **_win_kwargs(),
        )
        (req.cwd / "shared.txt").write_text("both-behaviours\n", encoding="utf-8")
        return AgentResult(ok=True, text="resolved", harness=req.harness, duration_s=0.1)

    return _run


def _conflicting_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str):
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    # Pin the route and skip cooldown bookkeeping: the result must not depend
    # on which agent CLIs or quota state the test host happens to have.
    monkeypatch.setattr(stage_integration, "pick", lambda *a, **k: ("claude", "sonnet"))
    monkeypatch.setattr(stage_integration, "record_result", lambda *a, **k: None)
    ws = checkout(project, run_id)
    _advance_main(tmp_path, origin, "changed-on-main\n", "_advance_1")
    (ws.path / "shared.txt").write_text("changed-on-branch\n", encoding="utf-8")
    ws_mod.commit(ws, "feat: conflicting change", f"{run_id}:T1")
    return origin, project, ws


def test_resolved_conflict_is_not_replayed_on_later_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, ws = _conflicting_run(tmp_path, monkeypatch, "run-4")
    call_count = {"n": 0}
    handler = IntegrationStageHandler(
        project, gh_executable="gh-not-used", host_caps=["harness:claude"],
        agent_runner=_merging_agent(call_count),
    )

    assert handler._rebase_onto_default(ws, "main") is None
    assert call_count["n"] == 1
    # The attempt marker was pushed before the agent ran (durable cap).
    remote_log = _git(["log", "--format=%B", "origin/df/run-4"], cwd=ws.path).stdout
    assert "run-4:integration:conflict_attempt" in remote_log

    # A later call (e.g. CI polling retry) must not rebase the merge away.
    assert handler._rebase_onto_default(ws, "main") is None
    assert call_count["n"] == 1


def test_merge_conflict_calls_agent_once_then_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, project, ws = _conflicting_run(tmp_path, monkeypatch, "run-5")
    call_count = {"n": 0}
    handler = IntegrationStageHandler(
        project, gh_executable="gh-not-used", host_caps=["harness:claude"],
        agent_runner=_merging_agent(call_count),
    )

    assert handler._rebase_onto_default(ws, "main") is None
    assert call_count["n"] == 1

    # main moves again on the same line: a second conflict is terminal.
    _advance_main(tmp_path, origin, "changed-on-main-again\n", "_advance_2")
    second = handler._rebase_onto_default(ws, "main")
    assert second is not None
    assert second.outcome == "failed"
    assert second.cause_code == "merge_conflict"
    assert call_count["n"] == 1  # the agent was NOT invoked a second time


def test_agent_that_does_not_merge_fails_merge_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, project, ws = _conflicting_run(tmp_path, monkeypatch, "run-6")

    def _no_merge(req: AgentRequest) -> AgentResult:
        (req.cwd / "shared.txt").write_text("edited-only\n", encoding="utf-8")
        return AgentResult(ok=True, text="resolved", harness=req.harness, duration_s=0.1)

    handler = IntegrationStageHandler(
        project, gh_executable="gh-not-used", host_caps=["harness:claude"], agent_runner=_no_merge,
    )
    result = handler._rebase_onto_default(ws, "main")
    assert result is not None and result.outcome == "failed"
    assert result.cause_code == "merge_conflict"


def test_already_merged_pr_short_circuits_to_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, calls = fake_gh

    ws = checkout(project, "run-7")
    main_sha = _git(["rev-parse", "origin/main"], cwd=ws.path).stdout.strip()
    set_spec(
        {
            "pr_list_merged": [
                {
                    "number": 11,
                    "url": "https://github.com/acme/repo/pull/11",
                    "state": "MERGED",
                    "mergeCommit": {"oid": main_sha},
                }
            ],
        }
    )

    handler = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result = handler.handle(_context("run-7"))

    assert result.outcome == "success", result.cause_code
    assert result.output_refs == ["https://github.com/acme/repo/pull/11", main_sha]
    call_names = [" ".join(c[:2]) for c in calls()]
    assert "pr create" not in call_names
    assert "pr merge" not in call_names


def test_pr_body_is_recovered_after_context_already_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    gh_path, set_spec, _ = fake_gh

    ws = checkout(project, "run-8")
    ws_mod.write_context(ws, "SPEC.md", "# Widget spec\n\nDetails.\n")
    ws_mod.commit(ws, "feat: widget", "run-8:T1")
    ws_mod.push(ws)

    handler = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    first_title, first_body, err = handler._strip_context(ws, _context("run-8"))
    assert err is None
    second_title, second_body, err = handler._strip_context(ws, _context("run-8"))
    assert err is None
    assert second_title == first_title == "Widget spec"
    assert "Details." in second_body


def test_deleted_run_branch_is_recreated_and_reintegrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    """HF-27-08 review item 1: verify (not fake) the release-retry path.

    `df/<run_id>` is deleted on merge (`gh pr merge --delete-branch`, which
    the fake gh here does not itself simulate, so it is done explicitly).
    `workspace.checkout()` for the same run_id afterwards must recreate the
    branch from the *new* default-branch tip (which already contains the
    original merge), and a subsequent `IntegrationStageHandler` run against
    it must open a brand-new PR (the old one is long gone).

    This does NOT prove development can meaningfully resume: the recreated
    branch has no `.darkfac/runs/<run_id>/` any more (stripped before the
    original merge), so `DevelopmentStage.run()` against it would find no
    `tickets.json` and return `failed(no_tickets)`. That gap is documented
    in this ticket's final report, not solved here.
    """
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "host1"))
    gh_path, set_spec, calls = fake_gh

    ws = checkout(project, "run-smoke")
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n")
    ws_mod.commit(ws, "feat: add widget", "run-smoke:T1")
    ws_mod.push(ws)

    set_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/11\n",
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/11",
        }
    )
    handler1 = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result1 = handler1.handle(_context("run-smoke"))
    assert result1.outcome == "success", result1.cause_code
    merge_sha = result1.output_refs[1]

    # Simulate `gh pr merge --delete-branch` actually deleting the remote ref
    # (the fake gh's `pr merge` only performs the squash+push, not the
    # branch deletion) -- this is the state release's retry:development
    # cause_code is routed into.
    _git(["push", "origin", "--delete", "df/run-smoke"], cwd=ws.path)

    # A second host (fresh DARKFAC_WORKSPACES root, as a real worker on a
    # different machine would be) checks out the same run_id.
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "host2"))
    ws2 = checkout(project, "run-smoke")
    assert ws2.branch == "df/run-smoke"
    ancestry = _git(["merge-base", "--is-ancestor", merge_sha, "HEAD"], cwd=ws2.path)
    assert ancestry.returncode == 0, "recreated branch must descend from the already-merged default tip"
    assert not ws_mod.context_dir(ws2).exists(), (
        "documented gap: SPEC.md/tickets.json do not survive the strip+merge, "
        "so a resumed development pass has no ticket to resume"
    )

    # Whatever development *can* produce (here: a trivial commit standing in
    # for real resumed work) still lets integration open a fresh PR.
    ws_mod.write_context(ws2, "DEMAND.md", "# resumed after prod smoke failure\n")
    ws_mod.commit(ws2, "chore: resume after prod smoke failure", "run-smoke:resume")
    ws_mod.push(ws2)

    set_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/12\n",
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/12",
        }
    )
    handler2 = IntegrationStageHandler(project, gh_executable=gh_path, host_caps=["harness:claude"])
    result2 = handler2.handle(_context("run-smoke"))

    assert result2.outcome == "success", result2.cause_code
    assert result2.output_refs[0] == "https://github.com/acme/repo/pull/12"
    call_names = [" ".join(c[:2]) for c in calls()]
    assert call_names.count("pr create") == 2, "both the original and the re-integration must create a PR"
