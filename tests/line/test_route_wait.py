"""USR-87: no agent route is a matter of time, not of the owner.

Covers `routing.earliest_route_available_at`, `route_wait.RouteWaiter` and the four agent stages
(development, independent review, grill, planning) with an injected clock: before the run's
wall-clock budget they return `retry("no_route_available not_before=<iso>")`, after it
`waiting_human` with a `HumanRequest(kind="infra")`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.line import bindings, stage_grill, stage_planning
from core.line import workspace as ws_mod
from core.line.human import HumanRequest
from core.line.route_wait import (
    NO_ROUTE_CAUSE,
    WALL_CLOCK_MARGIN,
    RouteWaiter,
    parse_run_created_at,
    run_started_at_lookup,
)
from core.line.routing import (
    MIN_ROUTE_WAIT_SECONDS,
    RoutingConfig,
    StageRoute,
    earliest_route_available_at,
)
from core.line.stage_build import DevelopmentStage
from core.line.stage_review import ReviewStage
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import CAUSE_CODE_MAX_LEN, JobKey
from core.workflow.control_store import SQLiteControlStore
from core.workflow.successors import _parse_retry_cause_code, materialize_result
from tests.line.conftest import copy_bare_origin

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
ALL_CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


def _config(wall_clock_hours: float = 6.0) -> RoutingConfig:
    return RoutingConfig(
        stages={
            "development": StageRoute(cascade=[("claude", "sonnet"), ("codex", None)]),
            "review": StageRoute(cascade="other_family_than_development"),
            "grill": StageRoute(cascade=[("claude", "sonnet")]),
            "planning": StageRoute(cascade=[("claude", "opus"), ("codex", None)]),
        },
        run_caps={"wall_clock_hours": wall_clock_hours},
    )


def _cooldowns(tmp_path: Path, until_by_harness: dict[str, datetime]) -> Path:
    path = tmp_path / "cooldowns.json"
    path.write_text(
        json.dumps({h: {"until": until.isoformat(), "reason": "rate_limited"} for h, until in until_by_harness.items()}),
        encoding="utf-8",
    )
    return path


def _no_reset(_provider: str) -> None:
    return None


# --------------------------------------------------------------------------
# routing.earliest_route_available_at
# --------------------------------------------------------------------------


def test_earliest_is_the_soonest_known_cooldown_end(tmp_path: Path) -> None:
    path = _cooldowns(tmp_path, {"claude": NOW + timedelta(minutes=20), "codex": NOW + timedelta(minutes=45)})
    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=path, reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(minutes=20)


def test_earliest_without_any_cooldown_or_reset_is_one_hour(tmp_path: Path) -> None:
    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=tmp_path / "missing.json", reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(minutes=60)


def test_earliest_never_waits_longer_than_the_default_hour(tmp_path: Path) -> None:
    path = _cooldowns(tmp_path, {"claude": NOW + timedelta(hours=5), "codex": NOW + timedelta(hours=3)})
    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=path, reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(minutes=60)


def test_earliest_uses_a_known_quota_reset_of_a_candidate_that_is_not_cooling(tmp_path: Path) -> None:
    seen: list[str] = []

    def reset(provider: str) -> datetime | None:
        seen.append(provider)
        return NOW + timedelta(minutes=30) if provider == "openai" else None

    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=tmp_path / "none.json", reset_lookup=reset
    )
    assert when == NOW + timedelta(minutes=30)
    assert seen == ["anthropic", "openai"]  # harness -> provider, in cascade order


def test_earliest_ignores_cooldowns_and_resets_already_in_the_past(tmp_path: Path) -> None:
    path = _cooldowns(tmp_path, {"claude": NOW - timedelta(minutes=5)})
    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=path,
        reset_lookup=lambda provider: NOW - timedelta(hours=1),
    )
    assert when == NOW + timedelta(minutes=60)


def test_earliest_only_considers_candidates_this_host_can_run(tmp_path: Path) -> None:
    # claude would be back in 10 minutes, but this host has no claude: only codex counts.
    path = _cooldowns(tmp_path, {"claude": NOW + timedelta(minutes=10), "codex": NOW + timedelta(minutes=35)})
    when = earliest_route_available_at(
        "development", ["harness:codex"], _config(), now=NOW, cooldown_path=path, reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(minutes=35)


def test_earliest_skips_harnesses_that_cannot_write_for_a_write_stage(tmp_path: Path) -> None:
    config = RoutingConfig(stages={"development": StageRoute(cascade=[("antigravity", None), ("claude", "sonnet")])})
    path = _cooldowns(tmp_path, {"antigravity": NOW + timedelta(minutes=5), "claude": NOW + timedelta(minutes=40)})
    when = earliest_route_available_at(
        "development", ALL_CAPS, config, now=NOW, cooldown_path=path, reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(minutes=40)  # antigravity is read-only: its 5 minutes are irrelevant


def test_earliest_for_review_follows_the_implementing_family(tmp_path: Path) -> None:
    path = _cooldowns(tmp_path, {"claude": NOW + timedelta(minutes=5), "codex": NOW + timedelta(minutes=25)})
    when = earliest_route_available_at(
        "review", ["harness:claude", "harness:codex"], _config(), now=NOW, cooldown_path=path,
        reset_lookup=_no_reset, implementing_harness="claude",
    )
    # claude implemented it, so only codex may review: claude's early reset is not a route.
    assert when == NOW + timedelta(minutes=25)


def test_earliest_is_never_sooner_than_the_minimum_wait(tmp_path: Path) -> None:
    path = _cooldowns(tmp_path, {"claude": NOW + timedelta(seconds=5)})
    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=path, reset_lookup=_no_reset
    )
    assert when == NOW + timedelta(seconds=MIN_ROUTE_WAIT_SECONDS)


def test_earliest_for_an_unknown_stage_or_naive_now_is_the_default_hour(tmp_path: Path) -> None:
    naive_now = NOW.replace(tzinfo=None)
    when = earliest_route_available_at("nope", ALL_CAPS, _config(), now=naive_now, cooldown_path=tmp_path / "x.json")
    assert when == NOW + timedelta(minutes=60)
    assert when.tzinfo is not None


def test_earliest_survives_a_broken_reset_probe(tmp_path: Path) -> None:
    def boom(_provider: str) -> datetime | None:
        raise RuntimeError("usage snapshot unreadable")

    when = earliest_route_available_at(
        "development", ALL_CAPS, _config(), now=NOW, cooldown_path=tmp_path / "none.json", reset_lookup=boom
    )
    assert when == NOW + timedelta(minutes=60)


# --------------------------------------------------------------------------
# RouteWaiter
# --------------------------------------------------------------------------


def _project() -> ProjectDescriptor:
    return ProjectDescriptor(id="acme", name="Acme", repo_url="https://github.com/acme/repo.git", default_branch="main")


class _Humans:
    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[tuple[ProjectDescriptor, HumanRequest]] = []
        self.fail = fail

    def __call__(self, project: ProjectDescriptor, request: HumanRequest) -> None:
        self.requests.append((project, request))
        if self.fail:
            raise RuntimeError("git push rejected")


def _waiter(tmp_path: Path, humans: _Humans, *, started: datetime | None, until: dict[str, datetime] | None = None) -> RouteWaiter:
    return RouteWaiter(
        run_started_at=(lambda run_id: started),
        clock=lambda: NOW,
        request_human=humans,
        cooldown_path=_cooldowns(tmp_path, until or {}),
        reset_lookup=_no_reset,
    )


def test_before_the_wall_clock_budget_the_waiter_retries_at_the_known_cooldown_end(tmp_path: Path) -> None:
    humans = _Humans()
    waiter = _waiter(
        tmp_path, humans, started=NOW - timedelta(hours=1),
        until={"claude": NOW + timedelta(minutes=20), "codex": NOW + timedelta(minutes=45)},
    )
    result = waiter.no_route_result("development", _project(), "run-1", host_caps=ALL_CAPS, config=_config())

    assert result.outcome == "retry"
    assert result.cause_code == f"{NO_ROUTE_CAUSE} not_before={(NOW + timedelta(minutes=20)).isoformat()}"
    assert len(result.cause_code) <= CAUSE_CODE_MAX_LEN
    assert humans.requests == []


def test_without_a_known_cooldown_the_waiter_retries_in_one_hour(tmp_path: Path) -> None:
    waiter = _waiter(tmp_path, _Humans(), started=NOW - timedelta(minutes=5))
    result = waiter.no_route_result("grill", _project(), "run-1", host_caps=ALL_CAPS, config=_config())
    assert result.outcome == "retry"
    assert result.cause_code == f"{NO_ROUTE_CAUSE} not_before={(NOW + timedelta(minutes=60)).isoformat()}"


def test_the_scheduler_reads_the_not_before_of_the_retry(tmp_path: Path) -> None:
    waiter = _waiter(tmp_path, _Humans(), started=NOW - timedelta(minutes=5))
    result = waiter.no_route_result("grill", _project(), "run-1", host_caps=ALL_CAPS, config=_config())
    target_stage, not_before = _parse_retry_cause_code(result.cause_code)
    assert target_stage is None  # same-stage retry
    assert datetime.fromisoformat(not_before or "") == NOW + timedelta(minutes=60)


def test_past_the_wall_clock_budget_the_waiter_parks_the_run_with_an_infra_request(tmp_path: Path) -> None:
    humans = _Humans()
    waiter = _waiter(tmp_path, humans, started=NOW - timedelta(hours=7))
    result = waiter.no_route_result("development", _project(), "run-9", host_caps=ALL_CAPS, config=_config())

    assert result.outcome == "waiting_human"
    assert result.cause_code == NO_ROUTE_CAUSE
    assert len(humans.requests) == 1
    project, request = humans.requests[0]
    assert project.id == "acme"
    assert (request.kind, request.run_id, request.blocking_stage) == ("infra", "run-9", "development")
    assert "7.0h" in request.guide_md and "6h" in request.guide_md
    assert "python -m core.usage.cli accounts --refresh" in request.guide_md
    assert "escreve codigo" in request.guide_md  # development: OpenRouter is read-only


def test_the_review_request_blocks_the_independent_review_job_and_offers_openrouter(tmp_path: Path) -> None:
    humans = _Humans()
    waiter = _waiter(tmp_path, humans, started=NOW - timedelta(hours=8))
    result = waiter.no_route_result("review", _project(), "run-9", host_caps=ALL_CAPS, config=_config(), mode="read")

    assert result.outcome == "waiting_human"
    request = humans.requests[0][1]
    assert request.blocking_stage == "independent_review"  # the JOB stage, so resume_blocked_job finds it
    assert "openrouter.ai" in request.guide_md


def test_a_failing_human_request_never_changes_the_outcome(tmp_path: Path) -> None:
    waiter = _waiter(tmp_path, _Humans(fail=True), started=NOW - timedelta(hours=7))
    result = waiter.no_route_result("planning", _project(), "run-9", host_caps=ALL_CAPS, config=_config())
    assert (result.outcome, result.cause_code) == ("waiting_human", NO_ROUTE_CAUSE)


def test_an_unknown_run_start_keeps_waiting_instead_of_parking(tmp_path: Path) -> None:
    humans = _Humans()
    waiter = _waiter(tmp_path, humans, started=None)
    result = waiter.no_route_result("development", _project(), "run-1", host_caps=ALL_CAPS, config=_config())
    assert result.outcome == "retry"
    assert humans.requests == []


def test_the_budget_edge_is_consistent_with_the_scheduler(tmp_path: Path) -> None:
    """The last retry the stage returns must survive `materialize_result`'s own wall-clock check."""
    store = SQLiteControlStore(":memory:")
    run_id = "run-edge"
    started = NOW
    conn = store._connect()
    conn.execute(
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
        "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "proj", "dem", "1.0", "hf05_sqlite", "autonomous", "active", "d", "1.0", started.isoformat(), started.isoformat()),
    )
    conn.commit()
    conn.close()

    config = _config(wall_clock_hours=6.0)
    humans = _Humans()
    job = JobKey(run_id=run_id, ticket_id="proj", plan_version="1.0", stage="development", iteration=0)

    def waiter_at(elapsed: timedelta) -> RouteWaiter:
        return RouteWaiter(
            run_started_at=lambda _rid: started, clock=lambda: started + elapsed, request_human=humans,
            cooldown_path=tmp_path / "none.json", reset_lookup=_no_reset,
        )

    just_inside = timedelta(hours=6) - WALL_CLOCK_MARGIN - timedelta(seconds=1)
    result = waiter_at(just_inside).no_route_result("development", _project(), run_id, host_caps=ALL_CAPS, config=config)
    assert result.outcome == "retry"
    # The scheduler materializes a few seconds later: still not `loop_cap`.
    successors = materialize_result(job, result, store, now=started + just_inside + timedelta(seconds=30))
    assert [s.stage for s in successors] == ["development"]

    at_the_margin = timedelta(hours=6) - WALL_CLOCK_MARGIN
    parked = waiter_at(at_the_margin).no_route_result("development", _project(), run_id, host_caps=ALL_CAPS, config=config)
    assert parked.outcome == "waiting_human"


def test_the_retry_carries_not_before_into_the_successor_job(tmp_path: Path) -> None:
    store = SQLiteControlStore(":memory:")
    waiter = _waiter(tmp_path, _Humans(), started=None, until={"claude": NOW + timedelta(minutes=20)})
    result = waiter.no_route_result("development", _project(), "run-nb", host_caps=ALL_CAPS, config=_config())
    job = JobKey(run_id="run-nb", ticket_id="proj", plan_version="1.0", stage="development", iteration=0)

    successors = materialize_result(job, result, store, now=NOW)

    assert [(s.stage, s.iteration) for s in successors] == [("development", 1)]
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT status, not_before FROM jobs WHERE run_id = ? AND stage = ? AND iteration = ?",
            ("run-nb", "development", 1),
        ).fetchone()
        parked = conn.execute(
            "SELECT status, cause_code FROM jobs WHERE run_id = ? AND stage = ? AND iteration = ?",
            ("run-nb", "development", 0),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert datetime.fromisoformat(row["not_before"]) == NOW + timedelta(minutes=20)
    assert parked["status"] == "retry"  # the run is not parked on a human
    assert parked["cause_code"] == result.cause_code


# --------------------------------------------------------------------------
# run start lookup (control store `runs.created_at`)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-30T12:00:00+00:00", NOW),
        ("2026-09-30T12:00:00", NOW),  # naive means UTC, like successors._run_wall_clock_exceeded
        (NOW, NOW),
        ("", None),
        (None, None),
        ("not a date", None),
    ],
)
def test_parse_run_created_at(raw: Any, expected: datetime | None) -> None:
    assert parse_run_created_at(raw) == expected


def test_run_started_at_lookup_reads_the_store_and_never_raises() -> None:
    class Store:
        def get_run_created_at(self, run_id: str) -> str | None:
            if run_id == "boom":
                raise RuntimeError("store down")
            return "2026-09-30T12:00:00+00:00" if run_id == "run-1" else None

    lookup = run_started_at_lookup(Store())
    assert lookup("run-1") == NOW
    assert lookup("other") is None
    assert lookup("boom") is None
    assert run_started_at_lookup(object())("run-1") is None  # a store without the method


def test_the_line_registry_gives_every_agent_stage_the_run_clock() -> None:
    store = SQLiteControlStore(":memory:")
    conn = store._connect()
    conn.execute(
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
        "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("run-clock", "proj", "dem", "1.0", "hf05_sqlite", "autonomous", "active", "d", "1.0", NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    conn.close()

    registry = bindings.build_line_registry(["git", "harness:any"], store=store, routing_config=_config())

    waiters = [
        registry[("grill", "v1")].route_waiter,
        registry[("planning", "v1")].route_waiter,
        registry[("development", "v1")]._stage.route_waiter,
        registry[("independent_review", "v1")]._stage.route_waiter,
    ]
    assert len({id(w) for w in waiters}) == 1  # one shared waiter
    assert waiters[0].run_started_at("run-clock") == NOW


# --------------------------------------------------------------------------
# The four agent stages
# --------------------------------------------------------------------------


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectDescriptor:
    origin = copy_bare_origin(tmp_path / "origin.git")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    return ProjectDescriptor(id="acme", name="Acme", repo_url=str(origin), default_branch="main")


def _never_routable(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_development_waits_for_the_cooldown_instead_of_asking_the_owner(
    tmp_path: Path, project: ProjectDescriptor
) -> None:
    ws = ws_mod.checkout(project, "run-dev")
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "t", "goal": "g"}]))
    ws_mod.commit(ws, "planning: tickets", job_key="run-dev:planning")
    ws_mod.push(ws)
    humans = _Humans()
    stage = DevelopmentStage(
        pick_func=_never_routable, host_caps=ALL_CAPS, routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(hours=2), until={"claude": NOW + timedelta(minutes=25)}),
    )

    result = stage.run(project, "run-dev")

    assert result.outcome == "retry"
    assert result.cause_code == f"{NO_ROUTE_CAUSE} not_before={(NOW + timedelta(minutes=25)).isoformat()}"
    assert humans.requests == []


def test_development_parks_the_run_only_after_the_wall_clock_budget(
    tmp_path: Path, project: ProjectDescriptor
) -> None:
    ws = ws_mod.checkout(project, "run-dev2")
    ws_mod.write_context(ws, "tickets.json", json.dumps([{"id": "T1", "title": "t", "goal": "g"}]))
    ws_mod.commit(ws, "planning: tickets", job_key="run-dev2:planning")
    ws_mod.push(ws)
    humans = _Humans()
    stage = DevelopmentStage(
        pick_func=_never_routable, host_caps=ALL_CAPS, routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(hours=7)),
    )

    result = stage.run(project, "run-dev2")

    assert (result.outcome, result.cause_code) == ("waiting_human", NO_ROUTE_CAUSE)
    assert humans.requests[0][1].kind == "infra"


def test_review_waits_for_the_other_family(tmp_path: Path, project: ProjectDescriptor) -> None:
    ws = ws_mod.checkout(project, "run-rev")
    (ws.path / "feature.py").write_text("x = 1\n", encoding="utf-8")
    ws_mod.write_context(ws, "progress.json", json.dumps({"harness": "claude", "tickets_done": ["T1"]}))
    ws_mod.commit(ws, "feat: x", job_key="run-rev:T1")
    ws_mod.push(ws)
    humans = _Humans()
    stage = ReviewStage(
        pick_func=_never_routable, host_caps=ALL_CAPS, routing_config=_config(),
        route_waiter=_waiter(
            tmp_path, humans, started=NOW - timedelta(hours=1),
            until={"claude": NOW + timedelta(minutes=5), "codex": NOW + timedelta(minutes=50)},
        ),
    )

    result = stage.run(project, "run-rev")

    assert result.outcome == "retry"
    # claude implemented it: the cascade skips claude, so its 5-minute cooldown does not count.
    assert result.cause_code.startswith(f"{NO_ROUTE_CAUSE} not_before=")
    not_before = _parse_retry_cause_code(result.cause_code)[1]
    assert datetime.fromisoformat(not_before or "") == NOW + timedelta(minutes=50)
    assert humans.requests == []


def test_grill_waits_for_a_route_instead_of_asking_the_owner(tmp_path: Path, project: ProjectDescriptor) -> None:
    humans = _Humans()
    result = stage_grill.run_grill(
        project, "run-grill", "Add a widget", host_caps=["git"], routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(minutes=30)),
    )
    assert result.outcome == "retry"
    assert result.cause_code == f"{NO_ROUTE_CAUSE} not_before={(NOW + timedelta(minutes=60)).isoformat()}"
    assert humans.requests == []


def test_grill_parks_the_run_only_after_the_wall_clock_budget(tmp_path: Path, project: ProjectDescriptor) -> None:
    humans = _Humans()
    result = stage_grill.run_grill(
        project, "run-grill2", "Add a widget", host_caps=["git"], routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(hours=9)),
    )
    assert (result.outcome, result.cause_code) == ("waiting_human", NO_ROUTE_CAUSE)
    assert [r.blocking_stage for _p, r in humans.requests] == ["grill"]


def test_grill_keeps_the_legacy_auth_wait_without_a_waiter_callback() -> None:
    from core.line.agent_cli import AgentResult

    no_route = AgentResult(ok=False, text="", harness="none", duration_s=0, error_kind="no_authenticated_harness")
    result = stage_grill.retry_for_agent_failure(no_route, "grill_agent_failed", now=NOW)
    assert result.cause_code == f"no_authenticated_harness not_before={(NOW + timedelta(minutes=10)).isoformat()}"


def _plan_ready_run(project: ProjectDescriptor, run_id: str) -> None:
    ws = ws_mod.checkout(project, run_id)
    ws_mod.write_context(ws, "DEMAND.md", "# demanda\n")
    ws_mod.write_context(ws, "GRILL.md", "# GRILL\n")
    ws_mod.commit(ws, "grill: done", job_key=f"{run_id}:grill")
    ws_mod.push(ws)


def test_planning_waits_for_a_route_then_parks_after_the_budget(tmp_path: Path, project: ProjectDescriptor) -> None:
    _plan_ready_run(project, "run-plan")
    humans = _Humans()

    waiting = stage_planning.run_planning(
        project, "run-plan", host_caps=["git"], routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(hours=1)),
    )
    assert waiting.outcome == "retry"
    assert waiting.cause_code == f"{NO_ROUTE_CAUSE} not_before={(NOW + timedelta(minutes=60)).isoformat()}"
    assert humans.requests == []

    parked = stage_planning.run_planning(
        project, "run-plan", host_caps=["git"], routing_config=_config(),
        route_waiter=_waiter(tmp_path, humans, started=NOW - timedelta(hours=10)),
    )
    assert (parked.outcome, parked.cause_code) == ("waiting_human", NO_ROUTE_CAUSE)
    assert [r.blocking_stage for _p, r in humans.requests] == ["planning"]
