"""Blue-green render + switch logic (USR-339). No network."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from core.orchestrator import compose_render as cr
from core.orchestrator.bluegreen import (
    BlueGreenError,
    BlueGreenState,
    has_serving_replica,
    parse_state,
    plan_next,
    render_bluegreen_compose,
    run_bluegreen_deploy,
    run_bluegreen_via_dokploy,
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


def _run_apply(initial: Optional[str], applied: List[str], fail_on: int, serving: bool = True) -> None:
    def apply(c: str) -> None:
        applied.append(c)
        if len(applied) == fail_on:
            raise BlueGreenError("compose up error")

    with pytest.raises(BlueGreenError) as ei:
        run_bluegreen_deploy(
            source=SRC, new_sha=C, read_compose=lambda: initial, apply_compose=apply,
            sha_is_serving=lambda _s: serving, health_attempts=2,
        )
    assert "compose up error" in str(ei.value) or not serving
    assert "previous compose restored" in str(ei.value)


def test_legacy_apply_failure_restores_legacy() -> None:
    applied: List[str] = []
    _run_apply(SRC, applied, fail_on=1)
    assert len(applied) == 2 and applied[-1] == SRC


def test_legacy_health_failure_restores_legacy() -> None:
    applied: List[str] = []
    _run_apply(SRC, applied, fail_on=99, serving=False)
    assert len(applied) == 2 and applied[-1] == SRC


def test_cutover_failure_restores_previous() -> None:
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    applied: List[str] = []
    _run_apply(initial, applied, fail_on=2)
    assert len(applied) == 3 and applied[-1] == initial


def test_restore_failure_is_reported() -> None:
    calls: List[str] = []

    def apply(c: str) -> None:
        calls.append(c)
        raise BlueGreenError("boom")

    with pytest.raises(BlueGreenError, match="RESTORE FAILED"):
        run_bluegreen_deploy(source=SRC, new_sha=C, read_compose=lambda: SRC, apply_compose=apply,
                             sha_is_serving=lambda _s: True)
    assert len(calls) == 2


def test_restore_legacy_cli_only_updates_without_marker(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from scripts import dokploy_bluegreen as bg
    from scripts import dokploy_redeploy as dr

    sent: list = []
    monkeypatch.setattr(dr, "resolve_credentials", lambda env: ("http://x", "k"))
    monkeypatch.setattr(dr, "make_urllib_transport", lambda u, k: lambda m, p, b=None: sent.append((m, p, b)))
    monkeypatch.setattr(bg, "_darkhub_service", lambda t: dr.Service("compose", "Darkhub", "cid"))
    monkeypatch.setattr(dr, "get_origin_main_compose", lambda sha: SRC)
    monkeypatch.setattr(dr, "fetch_darkhub_health", lambda *a, **k: {"git_sha": A})
    assert bg.main(["--restore-legacy"], env={}) == 0
    assert [p for _m, p, _b in sent] == ["/api/compose.update"]
    body = sent[0][2]
    assert body["composeId"] == "cid" and parse_state(body["composeFile"]) is None
    assert "restore-legacy ok" in capsys.readouterr().out


def test_refused_restore_keeps_serving_colour_and_never_writes_zero_replicas() -> None:
    """USR-388: refused restore writes nothing; the overlap compose (old colour up) stays in place."""
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    applied: List[str] = []
    with pytest.raises(BlueGreenError, match="NOT taken offline"):
        run_bluegreen_deploy(
            source=SRC, new_sha=C, read_compose=lambda: initial, apply_compose=applied.append,
            sha_is_serving=lambda _s: False, health_attempts=2, restore_allowed=lambda _prev: False,
        )
    assert len(applied) == 1 and all(has_serving_replica(c) for c in applied)
    assert "replicas: 0" not in applied[0]
    with pytest.raises(cr.ComposeRenderError):
        render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ())


def test_overlap_gates_on_new_colour_route_while_public_health_shows_old_sha() -> None:
    """USR-388 incident: public /health kept the old sha; the new colour's own route decides."""
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    applied: List[str] = []
    public = {"sha": A}
    probed: List[str] = []

    def apply(compose: str) -> None:
        applied.append(compose)
        if len(applied) == 2:
            public["sha"] = C  # after cutover only the new colour serves the public route

    final = run_bluegreen_deploy(
        source=SRC, new_sha=C, read_compose=lambda: initial, apply_compose=apply,
        sha_is_serving=lambda s: public["sha"] == s, health_attempts=2,
        color_is_serving=lambda color, s: probed.append(color) or s == C,
    )
    assert final.active == "green" and probed == ["green"] and len(applied) == 2
    assert "PathPrefix(`/__bg/green`)" in applied[0] and "routers.darkhub.service=darkhub" in applied[0]


def test_zero_replica_previous_is_never_restored_nor_revived() -> None:
    offline = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",)).replace(
        "replicas: 1", "replicas: 0")
    assert not has_serving_replica(offline)
    applied: List[str] = []
    with pytest.raises(BlueGreenError, match="never restored"):
        run_bluegreen_deploy(
            source=SRC, new_sha=C, read_compose=lambda: offline, apply_compose=applied.append,
            sha_is_serving=lambda _s: False, health_attempts=2, restore_allowed=lambda _prev: True,
        )
    assert offline not in applied and len(applied) == 1
    assert applied[0].count("replicas: 1") == 1  # the old insecure colour is not revived in the overlap


def test_dokploy_newest_deployment_is_picked_by_created_at_not_list_order() -> None:
    """USR-393: compose.one returns deployments out of order; the newest by createdAt must win."""
    initial = render_bluegreen_compose(SRC, BlueGreenState("blue", A, A), ("blue",))
    st = {"compose": initial, "n": 0, "t": 0.0, "sha": A, "updates": 0}
    deployments = [
        {"deploymentId": "d0", "status": "done", "createdAt": "2030-01-01T14:05:07.000Z"},
        {"deploymentId": "d1", "status": "done", "createdAt": "2030-01-01T02:15:39.000Z"},
        {"deploymentId": "d2", "status": "done", "createdAt": "bogus"},
    ]

    def request(method: str, path: str, body: Optional[dict]):
        if path.startswith("/api/compose.one"):
            return {"composeFile": st["compose"], "deployments": list(deployments)}
        if path == "/api/compose.update":
            st["compose"] = body["composeFile"]
            st["updates"] += 1
            if st["updates"] == 2:
                st["sha"] = C
        elif path == "/api/compose.deploy":
            st["n"] += 1
            # newest deployment lands at index 1, not 0 (the real API ordering bug)
            deployments.insert(1, {"deploymentId": f"new{st['n']}", "status": "done",
                                   "createdAt": f"2031-01-01T00:00:0{st['n']}.000Z"})
        return {}

    def sleep(seconds: float) -> None:
        st["t"] += seconds

    final = run_bluegreen_via_dokploy(
        request=request, compose_id="cid", source=SRC, new_sha=C,
        sha_is_serving=lambda s: st["sha"] == s, deploy_timeout=100.0, poll_interval=5.0,
        sleep_fn=sleep, clock_fn=lambda: st["t"],
        color_is_serving=lambda color, s: s == C,
    )
    assert final.active == "green" and st["n"] == 2 and st["t"] < 100.0
