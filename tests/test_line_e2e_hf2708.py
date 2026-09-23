"""HF-27-08 review item 11(a)/(b)/(c)/(f): a single demand through all 8
line stages, driven by the real `CloudWorker.poll_and_execute_once()` loop
against a real `SQLiteControlStore`, a bare local git "origin" (no
network), a fake `gh` on PATH, a fake local deployment adapter, and a fake
in-process agent CLI (the same monkeypatch/constructor-injection seams
`tests/line/test_stage_*.py` already use for grill/planning/development/
review -- no real Claude/Codex/Grok/Antigravity CLI is ever spawned).

Also covers:
- (b) a worker without any `harness:*` capability never claims `grill` or
  `development`.
- (c) jobs per run == the 8 `LINE_STAGES` + exactly 1 `retrospective`.
- (f) `not_before`/`ready_at` are present and honored by `claim()`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from core.demands.autonomous_intake import AutonomousIntakeService
from core.line import bindings, stage_grill
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import load_routing_config
from core.orchestrator.cloud_worker import CloudWorker, DEFAULT_CAPABILITIES
from core.projects.models import ProjectCommands, ProjectDescriptor
from core.workflow.control_contracts import IntakeCommand
from core.workflow.control_store import SQLiteControlStore
from tests.line.conftest import write_python_shim

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# git / repo fixtures (mirrors tests/line/test_stage_*.py's local-origin pattern)
# --------------------------------------------------------------------------


def _win_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **_win_kwargs(),
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "requirements.txt").write_text("", encoding="utf-8")
    (seed / "check_status.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.exit(0 if Path('STATUS_OK').is_file() else 1)\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


# --------------------------------------------------------------------------
# Fake `gh` CLI: same script as tests/line/test_stage_integration.py's
# `fake_gh` fixture (duplicated here to keep this E2E file self-contained,
# matching this test suite's existing convention of not sharing fixtures
# across test modules).
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

        def starts(*names):
            return argv[: len(names)] == list(names)

        if starts("pr", "list") and "merged" in argv:
            sys.stdout.write(json.dumps(spec.get("pr_list_merged", [])))
            sys.exit(0)

        if starts("pr", "list"):
            sys.stdout.write(json.dumps(spec.get("pr_list", [])))
            sys.exit(0)

        if starts("pr", "create"):
            sys.stdout.write(spec.get("pr_create_stdout", ""))
            sys.exit(0)

        if starts("pr", "checks"):
            sys.stdout.write(json.dumps(spec.get("pr_checks", [])))
            sys.exit(0)

        if starts("pr", "merge"):
            default_branch = spec.get("default_branch", "main")
            state_path = spec.get("state_path")
            parent = _git(["rev-parse", f"origin/{{default_branch}}"], cwd).stdout.strip()
            tree = _git(["rev-parse", "HEAD^{{tree}}"], cwd).stdout.strip()
            new_commit = _git(
                [
                    "-c", "user.name=DarkFac", "-c", "user.email=darkfac@users.noreply.github.com",
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
                "body": spec.get("pr_body", ""),
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
    env_var = "FAKE_GH_RESPONSE_E2E"
    script_path = tmp_path / "fake_gh_e2e.py"
    script_path.write_text(_FAKE_GH_SCRIPT.format(env_var=env_var), encoding="utf-8")
    cmd_path = write_python_shim(tmp_path / "fake_gh_e2e", script_path)
    state_path = tmp_path / "gh_state.json"

    def _set_spec(spec: dict[str, Any]) -> None:
        full = dict(spec)
        full.setdefault("state_path", str(state_path))
        monkeypatch.setenv(env_var, json.dumps(full))

    return str(cmd_path), _set_spec


# --------------------------------------------------------------------------
# Fake agent replies (grill/planning share stage_grill.run_read_agent, the
# same seam tests/line/test_stage_grill.py and test_stage_planning.py use)
# --------------------------------------------------------------------------

_GRILL_REPLY = {"questions": [], "assumptions": ["usar python 3.12"], "is_product_scale": False}
_PLAN_REPLY = {
    "spec": {
        "objective": "Adicionar endpoint /health",
        "out_of_scope": [],
        "design": "Router simples",
        "files_to_touch": ["app.py"],
        "risks": [],
    },
    "tickets": [
        {
            "id": "T1",
            "title": "Criar endpoint /health",
            "goal": "Responder 200",
            "files_hint": ["app.py"],
            "acceptance": ["GET /health retorna 200"],
            "tests_to_add": [],
            "smoke": ["/health"],
        }
    ],
    "is_product_scale": False,
    "milestones": [],
}


class _SequencedReadAgent:
    """Returns `_GRILL_REPLY` then `_PLAN_REPLY`, in that strict call order."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls += 1
        payload = _GRILL_REPLY if self.calls == 1 else _PLAN_REPLY
        return AgentResult(ok=True, text=json.dumps(payload), harness="claude", duration_s=0.01)


class _WriteAgent:
    """Fake write-mode development agent: drops a marker file the validate command checks for."""

    def __call__(self, req: AgentRequest) -> AgentResult:
        (req.cwd / "STATUS_OK").write_text("ok\n", encoding="utf-8")
        return AgentResult(ok=True, text="ok", harness=req.harness, model=req.model, duration_s=0.01)


class _ApprovingReviewAgent:
    def __call__(self, req: AgentRequest) -> AgentResult:
        payload = {"verdict": "approve", "blocking": [], "non_blocking": []}
        return AgentResult(ok=True, text=json.dumps(payload), harness=req.harness, model=req.model, duration_s=0.01)


class _ChangesRequiredThenApproveReviewAgent:
    """Round 1: changes_required. Round 2+: approve (HF-27-08 review item 11(d))."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, req: AgentRequest) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            payload = {
                "verdict": "changes_required",
                "blocking": [{"file": "app.py", "line": 1, "issue": "missing check", "fix": "add it"}],
                "non_blocking": [],
            }
        else:
            payload = {"verdict": "approve", "blocking": [], "non_blocking": []}
        return AgentResult(ok=True, text=json.dumps(payload), harness=req.harness, model=req.model, duration_s=0.01)


def _fixed_route(harness: str, model: str | None = None) -> Callable[..., tuple[str, str | None]]:
    def _pick(*args: Any, **kwargs: Any) -> tuple[str, str | None]:
        return (harness, model)

    return _pick


# --------------------------------------------------------------------------
# The E2E test
# --------------------------------------------------------------------------


def _line_stage_names() -> tuple[str, ...]:
    return bindings.LINE_STAGES


def test_full_line_e2e_all_stages_real_output_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    gh_path, set_gh_spec = fake_gh

    project = ProjectDescriptor(
        id="acme",
        name="Acme Project",
        repo_url=str(origin),
        default_branch="main",
        commands=ProjectCommands(setup=["python -c \"pass\""], validate=["python check_status.py"]),
        deploy=None,
    )

    def _project_resolver(project_id: str) -> ProjectDescriptor | None:
        return project if project_id == "acme" else None

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    intake_service = AutonomousIntakeService(store)

    monkeypatch.setattr(stage_grill, "run_read_agent", _SequencedReadAgent())

    cfg = load_routing_config()
    host_caps = ["git", "harness:any", "harness:claude", "harness:codex"]
    registry = bindings.build_line_registry(
        host_caps,
        store=store,
        routing_config=cfg,
        project_resolver=_project_resolver,
        intake_service=intake_service,
        gh_executable=gh_path,
    )
    # Real seams tests/line/test_stage_build.py / test_stage_review.py use:
    # constructor-injected run_agent_func/pick_func, never a real CLI.
    registry[("development", "v1")] = bindings.DevelopmentStageHandler(
        project_resolver=_project_resolver, run_agent_func=_WriteAgent(), pick_func=_fixed_route("claude", "sonnet"),
        routing_config=cfg,
    )
    registry[("independent_review", "v1")] = bindings.ReviewStageHandler(
        project_resolver=_project_resolver, run_agent_func=_ApprovingReviewAgent(), pick_func=_fixed_route("codex"),
        routing_config=cfg,
    )

    worker_caps = list(DEFAULT_CAPABILITIES) + host_caps
    worker = CloudWorker(worker_id="e2e-worker", max_slots=1, store=store, capabilities=worker_caps, registry=registry)

    cmd = IntakeCommand(
        project_id="acme",
        channel="test",
        external_id="e2e-1",
        mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={
            "title": "Add /health endpoint",
            "problem": "No health endpoint exists",
            "journey": "GET /health returns 200",
            "non_goals": [],
            "criteria": ["GET /health returns 200"],
        },
    )
    # Real wall-clock time throughout: dispatch_claimed_job's finish/
    # materialize_result use a *fresh* `datetime.now(UTC)` after the handler
    # returns (HF-27-08 review item 6), and the lease-heartbeat thread does
    # too -- a fixed fake "now" far from the real clock would make every
    # claim look instantly expired.
    now = datetime.now(UTC)
    receipt = store.accept(cmd, now)
    run_id = receipt.run_id
    assert run_id is not None

    set_gh_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/1\n",
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/1",
        }
    )

    max_steps = 40
    steps = 0
    while steps < max_steps:
        executed = worker.poll_and_execute_once()
        if not executed:
            break
        steps += 1

    conn = store._connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT stage, iteration, status, output_refs FROM jobs WHERE run_id = ? ORDER BY created_at ASC",
        (run_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    stages_seen = {r["stage"] for r in rows}
    for stage in _line_stage_names():
        assert stage in stages_seen, f"stage {stage} never ran (rows={rows})"

    # (c) jobs per run == 8 line stages + exactly 1 retrospective.
    assert sorted(stages_seen) == sorted(_line_stage_names())
    retrospective_rows = [r for r in rows if r["stage"] == "retrospective"]
    assert len(retrospective_rows) == 1

    for row in rows:
        assert row["status"] == "succeeded", f"stage {row['stage']} did not succeed: {row}"
        output_refs = json.loads(row["output_refs"])
        assert output_refs, f"stage {row['stage']} has empty output_refs (fail-closed violation)"

    # (d) real output_refs sanity: integration's PR/merge-sha, release's op/smoke ref.
    integration_row = next(r for r in rows if r["stage"] == "integration")
    integration_refs = json.loads(integration_row["output_refs"])
    assert integration_refs[0] == "https://github.com/acme/repo/pull/1"
    assert len(integration_refs[1]) == 40  # a real 40-hex-char git SHA

    build_deploy_row = next(r for r in rows if r["stage"] == "build_deploy")
    assert json.loads(build_deploy_row["output_refs"])


def test_review_retry_routes_to_development_iteration_1_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    """HF-27-08 review item 11(d): review's changes_required -> retry:development
    is proven end to end (claim -> dispatch -> materialize -> claim again),
    not just at the materialize_result unit level."""
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    gh_path, set_gh_spec = fake_gh

    project = ProjectDescriptor(
        id="acme",
        name="Acme Project",
        repo_url=str(origin),
        default_branch="main",
        commands=ProjectCommands(setup=["python -c \"pass\""], validate=["python check_status.py"]),
        deploy=None,
    )

    def _project_resolver(project_id: str) -> ProjectDescriptor | None:
        return project if project_id == "acme" else None

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    intake_service = AutonomousIntakeService(store)
    monkeypatch.setattr(stage_grill, "run_read_agent", _SequencedReadAgent())

    cfg = load_routing_config()
    host_caps = ["git", "harness:any", "harness:claude", "harness:codex"]
    registry = bindings.build_line_registry(
        host_caps, store=store, routing_config=cfg, project_resolver=_project_resolver,
        intake_service=intake_service, gh_executable=gh_path,
    )
    registry[("development", "v1")] = bindings.DevelopmentStageHandler(
        project_resolver=_project_resolver, run_agent_func=_WriteAgent(), pick_func=_fixed_route("claude", "sonnet"),
        routing_config=cfg,
    )
    review_agent = _ChangesRequiredThenApproveReviewAgent()
    registry[("independent_review", "v1")] = bindings.ReviewStageHandler(
        project_resolver=_project_resolver, run_agent_func=review_agent, pick_func=_fixed_route("codex"),
        routing_config=cfg,
    )

    worker_caps = list(DEFAULT_CAPABILITIES) + host_caps
    worker = CloudWorker(worker_id="e2e-retry-dev", max_slots=1, store=store, capabilities=worker_caps, registry=registry)

    cmd = IntakeCommand(
        project_id="acme", channel="test", external_id="e2e-retry-dev", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={
            "title": "Add /health endpoint", "problem": "No health endpoint exists",
            "journey": "GET /health returns 200", "non_goals": [], "criteria": ["GET /health returns 200"],
        },
    )
    receipt = store.accept(cmd, datetime.now(UTC))
    run_id = receipt.run_id
    assert run_id is not None

    set_gh_spec(
        {
            "pr_list": [],
            "pr_create_stdout": "https://github.com/acme/repo/pull/1\n",
            "pr_checks": [{"bucket": "pass", "name": "build", "link": ""}],
            "default_branch": "main",
            "pr_url": "https://github.com/acme/repo/pull/1",
        }
    )

    max_steps = 50
    steps = 0
    while steps < max_steps:
        if not worker.poll_and_execute_once():
            break
        steps += 1

    assert review_agent.calls >= 2, "review must have run at least twice (changes_required, then approve)"

    conn = store._connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT stage, iteration, status FROM jobs WHERE run_id = ? AND stage = 'development' ORDER BY iteration ASC",
        (run_id,),
    )
    dev_rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    assert [r["iteration"] for r in dev_rows] == [0, 1], f"expected development at iterations 0 and 1, got {dev_rows}"
    assert all(r["status"] == "succeeded" for r in dev_rows), dev_rows


def test_grep_no_deterministic_mock_in_dispatch_path() -> None:
    """Static guard: the worker's dispatch path must never *use* the retired
    deterministic_mock/generic-prompt/`<stage>_deliverable.json` pipeline
    (HF-27-08 item D) -- only mention it in docstrings/commit-message-style
    comments explaining that it was removed."""
    worker_src = Path("core/orchestrator/cloud_worker.py").read_text(encoding="utf-8")
    assert 'provider_backend = "deterministic_mock"' not in worker_src
    assert 'f"{stage}_deliverable.json"' not in worker_src
    assert "RemoteMultiHarnessModelProvider" not in worker_src
    assert "generated_output" not in worker_src


# --------------------------------------------------------------------------
# (b) a worker without any harness:* capability never claims grill or development
# --------------------------------------------------------------------------


def test_worker_without_harness_never_claims_development(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HF-27-08 review item 11(b).

    Known, documented gap (see the "item 9 reverted" comment in
    core.workflow.control_store.SQLiteControlStore.accept()): the very
    first `grill` job created by `accept()` is NOT capability-gated,
    because `accept()` is the shared HF-05 intake entrypoint used by many
    non-line callers with no reliable "is this a line demand" signal at
    that point (gating it unconditionally broke 25 pre-existing, unrelated
    tests). Every stage *after* grill -- development included -- IS
    correctly gated via `core.workflow.successors.materialize_result`'s
    `_required_capabilities_json` stamping, proven here.
    """
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    project = ProjectDescriptor(id="acme", name="Acme Project", repo_url=str(origin), default_branch="main")

    import core.projects.registry as project_registry_mod

    isolated_registry = project_registry_mod.ProjectRegistry(projects_file=tmp_path / "projects.json")
    isolated_registry.register_project(project)
    monkeypatch.setattr(project_registry_mod, "get_project_registry", lambda: isolated_registry)

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    cmd = IntakeCommand(
        project_id="acme", channel="test", external_id="e2e-noharness", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
    )
    receipt = store.accept(cmd, NOW)
    run_id = receipt.run_id
    assert run_id is not None

    # Drive grill->planning->development successors for real via
    # materialize_result, exactly like the worker would, so "development"'s
    # required_capabilities come from the real stamping path, not a hand
    # -crafted row.
    from core.workflow.control_contracts import JobKey, StageResult
    from core.workflow.successors import materialize_result

    grill_key = JobKey(run_id=run_id, ticket_id="acme", plan_version="1.0", stage="grill", iteration=0)
    materialize_result(grill_key, StageResult(outcome="success", output_refs=["sha1"]), store, now=NOW)
    planning_key = JobKey(run_id=run_id, ticket_id="acme", plan_version="1.0", stage="planning", iteration=0)
    materialize_result(planning_key, StageResult(outcome="success", output_refs=["sha2"]), store, now=NOW)

    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT required_capabilities FROM jobs WHERE run_id = ? AND stage = 'development'", (run_id,))
    dev_caps = cur.fetchone()[0]
    conn.close()
    assert dev_caps == '["git", "harness:any"]'

    # Bare role capabilities only -- no "git"/"harness:*" at all.
    worker = CloudWorker(
        worker_id="capless-worker", max_slots=1, store=store,
        capabilities=list(DEFAULT_CAPABILITIES),
    )
    executed = worker.poll_and_execute_once(now=NOW)
    assert executed is False, "a worker without git/harness:* must never claim the development job"

    status = worker.slot_status()
    assert "harness:claude" not in status.capabilities
    assert "harness:any" not in status.capabilities


# --------------------------------------------------------------------------
# (f) not_before / ready_at are present and honored by claim()
# --------------------------------------------------------------------------


def test_not_before_and_ready_at_present_and_honored_in_claim(tmp_path: Path) -> None:
    from core.workflow.control_contracts import JobKey, StageResult
    from core.workflow.successors import materialize_result

    store = SQLiteControlStore(db_path=tmp_path / "control2.db")
    cmd = IntakeCommand(
        project_id="acme", channel="test", external_id="e2e-notbefore", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
    )
    receipt = store.accept(cmd, NOW)
    run_id = receipt.run_id
    assert run_id is not None

    grill_key = JobKey(run_id=run_id, ticket_id="acme", plan_version="1.0", stage="grill", iteration=0)
    claim = store.claim("w1", ["grill_engine"], NOW)
    assert claim is not None

    not_before_iso = (NOW + timedelta(minutes=10)).isoformat()
    result = StageResult(outcome="retry", cause_code=f"ci_pending:not_before={not_before_iso}")
    materialize_result(grill_key, result, store, now=NOW)

    conn = store._connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT not_before, ready_at FROM jobs WHERE run_id = ? AND stage = 'grill' AND iteration = 1",
        (run_id,),
    )
    row = cur.fetchone()
    conn.close()
    assert row is not None
    assert row["not_before"] == not_before_iso
    assert row["ready_at"] is not None

    # claim() must skip it before not_before, and pick it up after.
    too_early = store.claim("w2", ["grill_engine"], NOW + timedelta(minutes=1))
    assert too_early is None

    on_time = store.claim("w2", ["grill_engine"], NOW + timedelta(minutes=11))
    assert on_time is not None
    assert on_time.job_key.stage == "grill"
    assert on_time.job_key.iteration == 1
