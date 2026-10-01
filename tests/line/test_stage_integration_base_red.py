"""USR-86: a CI that is already red on the base branch must not burn the agent's iterations.

The integration stage compares every red PR check with the last conclusive run of the base branch
(`core.git.ci_checks.base_branch_is_red`). When the same job is red there, no change the agent makes
can fix it: the stage waits (`retry("base_red not_before=<now + 60 min>")`, six times per run) and only
then parks the run on the owner (`waiting_human`, `HumanRequest(kind="infra")`), opening the defect
ticket of the red base for the `darkfac` project only. A green base keeps the old behaviour (retry
with the failing job's log).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from core.git import ci_checks
from core.line import bindings, stage_integration
from core.line import workspace as ws_mod
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.human import HumanRequest
from core.line.stage_integration import IntegrationStageHandler
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import CAUSE_CODE_MAX_LEN, Claim, JobKey, StageContext, StageResult
from core.workflow.control_store import SQLiteControlStore
from core.workflow.successors import MAX_SAME_STAGE_RETRIES, materialize_result
from tests.line.conftest import copy_bare_origin, write_python_shim

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
REPO_URL = "https://github.com/acme/repo.git"

PR_CHECK = {
    "bucket": "fail",
    "name": "pr-validation (ubuntu-latest)",
    "workflow": "DarkFac CI",
    "link": "https://github.com/acme/repo/actions/runs/555/job/1",
}
BASE_RUN = {
    "databaseId": 900,
    "conclusion": "failure",
    "headSha": "abcdef1234567",
    "url": "https://github.com/acme/repo/actions/runs/900",
    "workflowName": "DarkFac CI",
    "displayTitle": "main",
}
BASE_RED_JOBS = {"900": [{"name": "main-validation (ubuntu-latest)", "conclusion": "failure", "url": "u"}]}

_FAKE_GH_SCRIPT = '''\
import json
import os
import sys

spec = json.loads(os.environ[{env_var!r}])
argv = sys.argv[1:]
with open(spec["calls_path"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")


def out(payload):
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)


if argv[:2] == ["pr", "list"]:
    out([] if "merged" in argv else [{{"number": 7, "url": spec["pr_url"], "state": "OPEN"}}])
if argv[:2] == ["pr", "checks"]:
    out(spec["pr_checks"])
if argv[:2] == ["run", "list"]:
    if spec.get("run_list_fails"):
        sys.stderr.write("HTTP 502")
        sys.exit(1)
    out(spec["run_list"])
if argv[:2] == ["run", "view"]:
    if "--log-failed" in argv:
        sys.stdout.write(spec.get("log", ""))
        sys.exit(0)
    out({{"jobs": spec["run_jobs"].get(argv[2], [])}})
sys.stderr.write("fake gh: unknown " + " ".join(argv))
sys.exit(1)
'''


class _Recorder:
    """Callable that records its arguments (and optionally raises)."""

    def __init__(self, result: Any = None, *, fail: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.result = result
        self.fail = fail

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("boom")
        return self.result


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`(gh_executable, set_spec, calls)` for a fake `gh` that never touches the network."""
    env_var = "FAKE_GH_BASE_RED"
    script = tmp_path / "fake_gh_base_red.py"
    script.write_text(_FAKE_GH_SCRIPT.format(env_var=env_var), encoding="utf-8")
    shim = write_python_shim(tmp_path / "fake_gh_base_red", script)
    calls_path = tmp_path / "gh_calls.jsonl"

    def set_spec(**spec: Any) -> None:
        base = {
            "calls_path": str(calls_path),
            "pr_url": "https://github.com/acme/repo/pull/7",
            "pr_checks": [PR_CHECK],
            "run_list": [BASE_RUN],
            "run_jobs": BASE_RED_JOBS,
            "log": "FAIL tests/test_widget.py\nAssertionError: boom\n",
        }
        base.update(spec)
        monkeypatch.setenv(env_var, json.dumps(base))

    def calls() -> list[list[str]]:
        if not calls_path.is_file():
            return []
        return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines() if line]

    set_spec()
    return str(shim), set_spec, calls


def _project(origin: Path | str, project_id: str = "acme") -> ProjectDescriptor:
    return ProjectDescriptor(id=project_id, name="Acme", repo_url=str(origin), default_branch="main")


def _context(run_id: str, iteration: int = 0) -> StageContext:
    job = JobKey(run_id=run_id, ticket_id="T1", plan_version="1.0", stage="integration", iteration=iteration)
    claim = Claim(
        job_key=job, lease_id=f"lease_{run_id}", owner="worker-1", fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    return StageContext(
        claim=claim, plan_ref="plan://line", plan_digest="a" * 64, config_version="v1",
        environment_ref="acme", identity="worker-1", route_ref="route://line/integration", memory_version="mem_v1",
    )


def _branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str, project_id: str = "acme") -> ProjectDescriptor:
    """A pushed `df/<run_id>` branch with one commit, on a local bare origin."""
    origin = copy_bare_origin(tmp_path / "origin.git")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    project = _project(origin, project_id)
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "DEMAND.md", "# Add widget\n")
    ws_mod.commit(ws, "feat: add widget", f"{run_id}:T1")
    ws_mod.push(ws)
    return project


def _no_agent(_request: AgentRequest) -> AgentResult:
    raise AssertionError("a base_red wait must never invoke an agent")


def _handler(project: ProjectDescriptor, gh_path: str, **kwargs: Any) -> IntegrationStageHandler:
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("human_notifier", _Recorder())
    kwargs.setdefault("defect_ticket_func", _Recorder(None))
    return IntegrationStageHandler(
        project, gh_executable=gh_path, host_caps=["harness:claude"], agent_runner=_no_agent, **kwargs
    )


# --------------------------------------------------------------------------
# The wait
# --------------------------------------------------------------------------


def test_a_check_red_on_the_base_waits_an_hour_without_touching_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br1")
    humans, tickets = _Recorder(), _Recorder(None)
    handler = _handler(project, gh_path, human_notifier=humans, defect_ticket_func=tickets)

    result = handler.handle(_context("run-br1"))

    assert result.outcome == "retry"
    assert result.cause_code == f"base_red not_before={(NOW + timedelta(minutes=60)).isoformat()}"
    assert len(result.cause_code) <= CAUSE_CODE_MAX_LEN
    assert ci_checks.is_base_red_cause(result.cause_code)
    names = [" ".join(c[:2]) for c in calls()]
    assert "pr merge" not in names
    assert not any("--log-failed" in c for c in calls())  # no log is handed back to development
    assert humans.calls == [] and tickets.calls == []


def test_the_wait_count_comes_from_the_injected_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br2")
    seen: list[str] = []

    def spent(run_id: str) -> int:
        seen.append(run_id)
        return ci_checks.BASE_RED_MAX_RETRIES - 1

    still_waiting = _handler(project, gh_path, base_red_attempts=spent).handle(_context("run-br2"))

    assert still_waiting.outcome == "retry"  # the 6th wait is still allowed
    assert seen == ["run-br2"]


def test_the_job_iteration_is_the_conservative_fallback_without_a_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br3")

    early = _handler(project, gh_path).handle(_context("run-br3", iteration=2))
    late = _handler(project, gh_path).handle(_context("run-br3", iteration=ci_checks.BASE_RED_MAX_RETRIES))

    assert early.outcome == "retry"
    assert (late.outcome, late.cause_code) == ("waiting_human", "base_red_exhausted")


def test_a_broken_counter_falls_back_instead_of_waiting_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br4")

    result = _handler(project, gh_path, base_red_attempts=_Recorder(fail=True)).handle(
        _context("run-br4", iteration=ci_checks.BASE_RED_MAX_RETRIES)
    )

    assert result.outcome == "waiting_human"


# --------------------------------------------------------------------------
# Escalation after the waits
# --------------------------------------------------------------------------


def test_after_six_waits_the_run_waits_for_the_owner_with_an_infra_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br5")
    humans, tickets = _Recorder(), _Recorder({"ticket": {"id": "USR-999"}})
    handler = _handler(
        project, gh_path, human_notifier=humans, defect_ticket_func=tickets,
        base_red_attempts=lambda _run_id: ci_checks.BASE_RED_MAX_RETRIES,
    )

    result = handler.handle(_context("run-br5"))

    assert (result.outcome, result.cause_code) == ("waiting_human", "base_red_exhausted")
    assert len(humans.calls) == 1
    request = humans.calls[0][0]
    assert isinstance(request, HumanRequest)
    assert (request.kind, request.run_id, request.blocking_stage) == ("infra", "run-br5", "integration")
    assert request.probe_cmd is None  # a client's base is probed by the client's own CI, not by ours
    assert "pr-validation (ubuntu-latest)" in request.guide_md
    assert PR_CHECK["link"] in request.guide_md
    assert tickets.calls == []  # only the factory's own project files a ticket for its base


def test_the_factory_project_files_the_defect_ticket_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br6", project_id="darkfac")
    humans, tickets = _Recorder(), _Recorder({"ticket": {"id": "USR-999", "created": True}})
    handler = _handler(
        project, gh_path, human_notifier=humans, defect_ticket_func=tickets,
        base_red_attempts=lambda _run_id: ci_checks.BASE_RED_MAX_RETRIES,
    )

    result = handler.handle(_context("run-br6"))

    assert result.outcome == "waiting_human"
    assert len(tickets.calls) == 1
    assert tickets.calls[0][1] == "main"  # (runner, base branch)
    request = humans.calls[0][0]
    assert "USR-999" in request.guide_md
    assert request.probe_cmd == "python -m core.git.autonomy check-main --branch main"


@pytest.mark.parametrize("opener", [_Recorder(fail=True), _Recorder(None), _Recorder({"ticket": {"id": None}})])
def test_a_failed_defect_ticket_never_blocks_the_escalation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh, opener: _Recorder
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br7", project_id="darkfac")
    humans = _Recorder()
    handler = _handler(
        project, gh_path, human_notifier=humans, defect_ticket_func=opener,
        base_red_attempts=lambda _run_id: ci_checks.BASE_RED_MAX_RETRIES,
    )

    result = handler.handle(_context("run-br7"))

    assert result.outcome == "waiting_human"
    assert "nao conseguiu abrir o ticket" in humans.calls[0][0].guide_md


def test_a_failed_owner_notification_never_changes_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, _set_spec, _calls = fake_gh
    project = _branch(tmp_path, monkeypatch, "run-br8")
    handler = _handler(
        project, gh_path, human_notifier=_Recorder(fail=True),
        base_red_attempts=lambda _run_id: ci_checks.BASE_RED_MAX_RETRIES,
    )
    assert handler.handle(_context("run-br8")).outcome == "waiting_human"


def test_the_infra_guide_is_step_by_step_and_promises_the_resume() -> None:
    project = ProjectDescriptor(id="acme", name="Acme", repo_url=REPO_URL, default_branch="main")
    handler = IntegrationStageHandler(project)

    guide = handler._base_red_guide(7, [PR_CHECK], "main", None)

    assert guide.startswith("kind=infra:")
    assert "https://github.com/acme/repo/actions?query=branch%3Amain" in guide
    assert "DarkFac CI / pr-validation (ubuntu-latest)" in guide
    assert "6h" in guide and "6 verificacoes de 60 min" in guide
    assert "Re-run all jobs" in guide
    assert "a fabrica retoma sozinha este run" in guide
    assert [line[:2] for line in guide.splitlines() if line[:1].isdigit()] == ["1.", "2.", "3.", "4.", "5."]
    assert "ticket" not in guide  # a client project is not told about the factory's own queue


# --------------------------------------------------------------------------
# What does NOT change
# --------------------------------------------------------------------------


def test_a_check_that_is_green_on_the_base_still_goes_back_to_development_with_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, set_spec, calls = fake_gh
    set_spec(run_list=[{**BASE_RUN, "conclusion": "success"}])
    project = _branch(tmp_path, monkeypatch, "run-br9")
    counter = _Recorder(fail=True)
    humans = _Recorder()
    handler = _handler(project, gh_path, base_red_attempts=counter, human_notifier=humans)

    result = handler.handle(_context("run-br9"))

    assert result.outcome == "retry"
    assert result.cause_code.startswith(f"ci_check_failed:{PR_CHECK['name']}")
    assert "AssertionError: boom" in result.cause_code
    assert counter.calls == [] and humans.calls == []  # the base_red machinery is never consulted
    assert any("--log-failed" in c for c in calls())


def test_a_check_red_on_the_base_for_another_job_is_still_the_prs_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, set_spec, _calls = fake_gh
    set_spec(run_jobs={"900": [{"name": "lint", "conclusion": "failure", "url": "u"}]})
    project = _branch(tmp_path, monkeypatch, "run-br10")

    result = _handler(project, gh_path).handle(_context("run-br10"))

    assert result.cause_code.startswith("ci_check_failed:")


def test_an_unreadable_base_is_not_proof_that_it_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_gh
) -> None:
    gh_path, set_spec, _calls = fake_gh
    set_spec(run_list_fails=True)
    project = _branch(tmp_path, monkeypatch, "run-br11")

    result = _handler(project, gh_path).handle(_context("run-br11"))

    assert result.cause_code.startswith("ci_check_failed:")


# --------------------------------------------------------------------------
# ci_checks helpers, store counter and the scheduler's caps
# --------------------------------------------------------------------------


def test_cause_code_helpers() -> None:
    cause = ci_checks.base_red_cause_code(NOW + timedelta(minutes=60, microseconds=7))
    assert cause == "base_red not_before=2026-09-30T13:00:00+00:00"
    assert len(cause) <= CAUSE_CODE_MAX_LEN
    assert ci_checks.base_red_cause_code(datetime(2026, 9, 30, 13, 0)) == cause  # naive means UTC
    assert ci_checks.is_base_red_cause(cause)
    for other in (None, "", "ci_pending:not_before=2026-09-30T13:00:00+00:00", "base_red_exhausted", "xbase_red"):
        assert not ci_checks.is_base_red_cause(other)


def test_classify_base_red_never_raises() -> None:
    def runner(_args: Any, _cwd: Path) -> Any:
        raise RuntimeError("gh timed out")

    assert ci_checks.classify_base_red(runner, Path("."), "main", [PR_CHECK]) is False


def test_the_store_counter_counts_only_this_runs_base_red_integration_waits() -> None:
    class Store:
        def get_run_status(self, run_id: str) -> dict[str, Any] | None:
            if run_id != "run-1":
                return None
            return {
                "jobs": [
                    {"stage": "integration", "cause_code": "base_red not_before=2026-09-30T13:00:00+00:00"},
                    {"stage": "integration", "cause_code": "base_red not_before=2026-09-30T14:00:00+00:00"},
                    {"stage": "integration", "cause_code": "ci_pending:not_before=2026-09-30T14:05:00+00:00"},
                    {"stage": "integration", "cause_code": "base_red_exhausted"},
                    {"stage": "development", "cause_code": "base_red not_before=2026-09-30T13:00:00+00:00"},
                    {"stage": "integration", "cause_code": None},
                ]
            }

    count = bindings.base_red_attempt_counter(Store())
    assert count("run-1") == 2
    assert count("unknown-run") == 0


def test_the_line_registry_hands_the_integration_stage_a_store_counter() -> None:
    registry = bindings.build_line_registry(["git", "harness:any"], store=SQLiteControlStore(":memory:"))
    counter = registry[("integration", "v1")]._handler_kwargs["base_red_attempts"]
    assert counter("run-without-jobs") == 0


def _store_with_run(run_id: str, created_at: datetime) -> SQLiteControlStore:
    store = SQLiteControlStore(":memory:")
    conn = store._connect()
    conn.execute(
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
        "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "proj", "dem", "1.0", "hf05_sqlite", "autonomous", "active", "d", "1.0",
         created_at.isoformat(), created_at.isoformat()),
    )
    conn.commit()
    conn.close()
    return store


def test_base_red_waits_are_not_cut_short_by_the_run_wall_clock() -> None:
    """Six hourly waits outlive the 6 h run budget; `ci_pending` polling still must not."""
    store = _store_with_run("run-wc", NOW)
    job = JobKey(run_id="run-wc", ticket_id="proj", plan_version="1.0", stage="integration", iteration=4)
    late = NOW + timedelta(hours=7)
    not_before = (late + timedelta(minutes=60)).isoformat()

    waiting = materialize_result(
        job, StageResult(outcome="retry", cause_code=f"base_red not_before={not_before}"), store, now=late
    )
    polling = materialize_result(
        job, StageResult(outcome="retry", cause_code=f"ci_pending:not_before={not_before}"), store, now=late
    )

    assert [(s.stage, s.iteration) for s in waiting] == [("integration", 5)]
    assert [s.stage for s in polling] == ["retrospective"]  # the wall-clock bound still applies to it


def test_base_red_keeps_the_absolute_same_stage_backstop() -> None:
    store = _store_with_run("run-bs", NOW)
    job = JobKey(
        run_id="run-bs", ticket_id="proj", plan_version="1.0", stage="integration", iteration=MAX_SAME_STAGE_RETRIES
    )
    result = StageResult(outcome="retry", cause_code="base_red not_before=2026-09-30T13:00:00+00:00")

    assert [s.stage for s in materialize_result(job, result, store, now=NOW)] == ["retrospective"]


def test_the_base_red_wait_persists_its_not_before_on_the_successor() -> None:
    store = _store_with_run("run-nb", NOW)
    job = JobKey(run_id="run-nb", ticket_id="proj", plan_version="1.0", stage="integration", iteration=0)
    cause = ci_checks.base_red_cause_code(NOW + timedelta(minutes=60))

    materialize_result(job, StageResult(outcome="retry", cause_code=cause), store, now=NOW)

    conn = store._connect()
    try:
        successor = conn.execute(
            "SELECT status, not_before FROM jobs WHERE run_id = 'run-nb' AND stage = 'integration' AND iteration = 1"
        ).fetchone()
        finished = conn.execute(
            "SELECT status, cause_code FROM jobs WHERE run_id = 'run-nb' AND stage = 'integration' AND iteration = 0"
        ).fetchone()
    finally:
        conn.close()
    assert successor["status"] == "pending"
    assert datetime.fromisoformat(successor["not_before"]) == NOW + timedelta(minutes=60)
    assert (finished["status"], finished["cause_code"]) == ("retry", cause)  # what the counter reads back


def test_stage_integration_exports_the_defect_ticket_opener() -> None:
    assert "open_base_red_ticket" in stage_integration.__all__
