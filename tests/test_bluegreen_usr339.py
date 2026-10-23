"""Blue-green render + switch logic (USR-339). No network."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from core.orchestrator import compose_render as cr
from core.orchestrator.bluegreen import (
    BlueGreenError,
    BlueGreenState,
    parse_state,
    plan_next,
    render_bluegreen_compose,
    run_bluegreen_deploy,
)

SRC = (Path(__file__).resolve().parent.parent / cr.HUB_COMPOSE_PATH).read_text(encoding="utf-8")
A, B, C = "a" * 40, "b" * 40, "c" * 40


def test_render_two_services_same_router_shared_volumes_and_env() -> None:
    out = render_bluegreen_compose(SRC, BlueGreenState("blue", A, B), ("blue",))
    assert "  darkhub-blue:\n" in out and "  darkhub-green:\n" in out
    assert "\n  darkhub:\n" not in out
    assert out.count("traefik.http.routers.darkhub.rule") == 2
    assert out.count("darkhub-hub-data:/app/hub/data") == 2
    assert out.count("name: darkhub-hub-data") == 1
    assert out.count("replicas: 1") == 1 and out.count("replicas: 0") == 1
    assert f"ghcr.io/gcamposgit/darkhub:{A}" in out and f"ghcr.io/gcamposgit/darkhub:{B}" in out
    assert "container_name:" not in out
    assert "${DARKFAC_GIT_SHA}" not in out
    assert parse_state(out) == BlueGreenState("blue", A, B)


def test_plan_alternates_colours_and_legacy_starts_blue() -> None:
    assert plan_next(None, A)[0] == "blue"
    color, state = plan_next(BlueGreenState("blue", A, A), B)
    assert color == "green" and state.green_sha == B and state.blue_sha == A
    color, state = plan_next(BlueGreenState("green", A, B), C)
    assert color == "blue" and state.blue_sha == C and state.green_sha == B


def _run(initial: Optional[str], serving: bool, applied: List[str], sha: str = C) -> BlueGreenState:
    return run_bluegreen_deploy(
        source=SRC,
        new_sha=sha,
        read_compose=lambda: initial,
        apply_compose=applied.append,
        sha_is_serving=lambda _s: serving,
        health_attempts=3,
    )


def test_switch_overlaps_then_cuts_over() -> None:
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    applied: List[str] = []
    final = _run(initial, True, applied)
    assert final.active == "green" and final.green_sha == C and final.blue_sha == A
    assert len(applied) == 2
    assert applied[0].count("replicas: 1") == 2  # overlap: both colours up
    assert applied[1].count("replicas: 1") == 1 and parse_state(applied[1]).active == "green"
    assert parse_state(applied[0]).active == "blue"  # state flips only at cutover


def test_unhealthy_new_colour_keeps_old_and_reports() -> None:
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    applied: List[str] = []
    with pytest.raises(BlueGreenError):
        _run(initial, False, applied)
    assert applied[-1] == initial  # rolled back to the previous compose
    assert all(parse_state(x).active == "blue" for x in applied)


def test_legacy_compose_first_migration_and_same_sha_noop() -> None:
    applied: List[str] = []
    final = _run(SRC, True, applied)
    assert final.active == "blue" and len(applied) == 2
    again: List[str] = []
    assert _run(applied[-1], True, again).active == "blue" and again == []


def test_ghcr_wait_runs_before_any_apply() -> None:
    order: List[str] = []
    run_bluegreen_deploy(
        source=SRC, new_sha=C, read_compose=lambda: None,
        apply_compose=lambda _c: order.append("apply"),
        sha_is_serving=lambda _s: True,
        wait_ghcr=lambda _s: order.append("ghcr"),
    )
    assert order[0] == "ghcr"


def test_legacy_migration_removes_orphan_after_new_colour_is_healthy() -> None:
    events: List[str] = []
    run_bluegreen_deploy(
        source=SRC, new_sha=C, read_compose=lambda: SRC,
        apply_compose=lambda _c: events.append("apply"),
        sha_is_serving=lambda _s: events.append("health") or True,
        remove_legacy=lambda: events.append("remove") or True,
    )
    assert events == ["apply", "health", "remove", "apply"]


def test_is_bluegreen_marker_or_env() -> None:
    from core.orchestrator.bluegreen import is_bluegreen

    marked = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    assert is_bluegreen(marked, {}) and not is_bluegreen(SRC, {})
    assert is_bluegreen(SRC, {"DARKHUB_DEPLOY_MODE": "bluegreen"})
