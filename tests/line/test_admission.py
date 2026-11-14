"""Read-only admission before a ticket worktree or PLAN (USR-468 T1)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from core.line import admission, routing
from core.line.admission import (
    CODE_GATE_ABSENT,
    CODE_GATE_BUSY,
    CODE_GATE_GENERIC,
    CODE_GATE_STALE,
    CODE_GATE_ZERO,
    CODE_NO_BUILD_ROUTE,
    CODE_NO_PLANNING_ROUTE,
    CODE_NO_REVIEW_ROUTE,
    CODE_REVIEW_SAME_FAMILY,
    STAGE_BUILD,
    STAGE_GATE,
    STAGE_PLANNING,
    STAGE_REVIEW,
    AdmissionDecision,
    GateObservation,
    GateWorkerObservation,
    admit,
    classify_gate,
    classify_worker,
    collect_gate_observation,
    decide,
    worker_from_health,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
_FIELDS = {"outcome", "code", "stage", "resource", "observed_at", "evidence_ref", "not_before"}


def _worker(
    worker_id: str,
    workers: Optional[int],
    busy: Optional[bool],
    observed_at: Optional[datetime],
    evidence_ref: str,
) -> GateWorkerObservation:
    evidenced: list[str] = []
    if workers is not None:
        evidenced.append("workers")
    if busy is not None:
        evidenced.append("busy")
    if observed_at is not None:
        evidenced.append("observed_at")
    return GateWorkerObservation(
        worker_id=worker_id,
        evidence_ref=evidence_ref,
        workers=workers,
        busy=busy,
        observed_at=observed_at,
        evidenced_fields=tuple(evidenced),
    )


def _hit(stage: str, harness: Optional[str]) -> admission.RouteHit:
    ref = f"routing://{stage}/{harness or 'none'}"
    return admission.RouteHit(stage=stage, harness=harness, model=None, evidence_ref=ref)


def _gate(*workers: GateWorkerObservation) -> GateObservation:
    return GateObservation(workers=list(workers), observed_at=NOW)


def _usable() -> GateWorkerObservation:
    return _worker("ok", 2, False, NOW, "gate://ok")


def _routes(
    planning: str = "claude",
    build: str = "codex",
    review: str = "grok",
) -> dict[str, tuple[str, Optional[str]]]:
    return {
        "planning": (planning, "opus"),
        "development": (build, "sonnet"),
        "review": (review, None),
    }


def test_decision_fields_are_allow_or_wait_with_the_audit_set() -> None:
    allowed = admission.allow_decision(NOW, evidence_ref="admission://allow")
    assert set(AdmissionDecision.model_fields) == _FIELDS
    assert set(allowed.public_dict()) == _FIELDS
    assert allowed.outcome == "allow" and allowed.not_before is None
    assert allowed.public_dict()["not_before"] is None
    assert allowed.public_dict()["observed_at"].endswith("+00:00")
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="allow", code="admitted", stage="admission", resource="line",
            observed_at=NOW, evidence_ref="admission://allow", not_before=NOW,
        )
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="wait", code="no_build_route", stage="build", resource="route:build",
            observed_at=NOW, evidence_ref="routing://development/none",
        )
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="allow", code="admitted", stage="admission", resource="line",
            observed_at=NOW, evidence_ref="admission://allow", harness="claude",
        )


def test_same_family_review_waits_and_a_different_family_with_capacity_allows() -> None:
    calls: list[str] = []

    def not_before_for(kind: str) -> datetime:
        calls.append(kind)
        return NOW

    same = decide(
        planning=_hit("planning", "claude"),
        build=_hit("build", "Claude"),
        review=_hit("review", "claude"),
        gate=_gate(_usable()),
        now=NOW,
        not_before_for=not_before_for,
    )
    assert same.outcome == "wait" and same.code == CODE_REVIEW_SAME_FAMILY
    assert same.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
    assert calls == [STAGE_REVIEW]

    calls.clear()
    allowed = decide(
        planning=_hit("planning", "claude"),
        build=_hit("build", "codex"),
        review=_hit("review", "grok"),
        gate=_gate(_usable()),
        now=NOW,
        not_before_for=not_before_for,
    )
    assert allowed.outcome == "allow" and allowed.not_before is None and calls == []
    assert "harness" not in allowed.public_dict()


def test_gate_failures_keep_their_cause_and_one_usable_worker_allows() -> None:
    zero = _worker("z", 0, False, NOW, "gate://z")
    busy = _worker("b", 3, True, NOW, "gate://b")
    stale = _worker("s", 3, False, NOW - timedelta(seconds=90), "gate://s")
    fresh = _worker("f", 3, False, NOW - timedelta(seconds=89), "gate://f")
    generic = worker_from_health("g", {"status": "ok"}, evidence_ref="gate://g")
    future = _worker("u", 3, False, NOW + timedelta(seconds=5), "gate://u")

    assert classify_worker(stale, NOW) == CODE_GATE_STALE
    assert classify_worker(fresh, NOW) is None
    assert classify_worker(future, NOW) is None
    assert classify_gate(_gate(), NOW) == (CODE_GATE_ABSENT, "gate://collection")
    assert classify_gate(_gate(zero, generic), NOW) == (CODE_GATE_ZERO, "gate://z")
    assert classify_gate(_gate(generic, zero), NOW) == (CODE_GATE_ZERO, "gate://z")
    assert classify_gate(_gate(busy, generic), NOW) == (CODE_GATE_BUSY, "gate://b")
    assert classify_gate(_gate(stale, generic), NOW) == (CODE_GATE_STALE, "gate://s")
    assert classify_gate(_gate(generic), NOW) == (CODE_GATE_GENERIC, "gate://g")
    assert classify_gate(_gate(zero, _usable()), NOW) == (None, "gate://ok")
    assert classify_gate(_gate(_usable(), zero), NOW) == (None, "gate://ok")


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok"},
        {"status": "ok", "busy": False},
        {"status": "healthy"},
        {"workers": True, "busy": False, "observed_at": NOW.isoformat()},
        {"xdist_workers": 4, "busy": False, "observed_at": NOW.isoformat()},
        {"workers": 4, "busy": False, "started_at": NOW.isoformat(), "status": "ok"},
    ],
)
def test_generic_health_is_not_capacity(payload: dict[str, Any]) -> None:
    worker = worker_from_health("remote", payload, evidence_ref="gate://remote")
    assert classify_worker(worker, NOW) == CODE_GATE_GENERIC


def test_explicit_zero_busy_and_stale_survive_a_status_ok() -> None:
    zero = worker_from_health(
        "n",
        {"status": "ok", "workers": 0, "busy": False, "observed_at": NOW.isoformat()},
        evidence_ref="gate://n",
    )
    busy = worker_from_health(
        "n",
        {"status": "ok", "workers": 4, "busy": True, "checked_at": NOW.isoformat()},
        evidence_ref="gate://n",
    )
    stale = worker_from_health(
        "n",
        {
            "status": "ok", "workers": 4, "busy": False,
            "heartbeat_at": (NOW - timedelta(seconds=90)).isoformat().replace("+00:00", "Z"),
        },
        evidence_ref="gate://n",
    )
    assert classify_worker(zero, NOW) == CODE_GATE_ZERO
    assert classify_worker(busy, NOW) == CODE_GATE_BUSY
    assert classify_worker(stale, NOW) == CODE_GATE_STALE


def test_collect_does_not_promote_local_zero_with_generic_remote_health(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=0),
        local_busy=lambda: False,
        list_remote=lambda: ["http://user:secret@notebook.example:9/health?token=abc"],
        probe=lambda _url: {"status": "ok"},
    )
    code, ref = classify_gate(observation, NOW)
    assert code == CODE_GATE_ZERO and ref == "gate://local"
    remote = next(worker for worker in observation.workers if worker.worker_id != "local")
    assert remote.worker_id == "gate://notebook.example"
    assert remote.evidence_ref == "gate://notebook.example"
    assert "secret" not in remote.evidence_ref and "token" not in remote.evidence_ref
    assert classify_worker(remote, NOW) == CODE_GATE_GENERIC

    # Production /health has no capacity: combine the real hardware schema rather than inventing it.
    import io
    import json
    import urllib.request
    from core.line import admission
    from core.harness import remote_dispatch

    hardware = dict(cpu_percent=0, cpu_cores_logical=8, cpu_cores_physical=4,
                    ram_total_gb=16, ram_used_gb=8, ram_free_gb=8, ram_percent=50,
                    disk_primary_mount="C:/", disk_total_gb=100, disk_used_gb=50,
                    disk_free_gb=50, disk_percent=50, boot_time=NOW.isoformat(),
                    uptime_seconds=10, os_platform="Windows", collected_at=NOW.isoformat())
    monkeypatch.setattr(remote_dispatch, "probe_health", lambda url: {"status": "ok", "busy": False})
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(hardware).encode()))
    payload = admission._probe_remote("http://desktop:8080")
    observed = worker_from_health("desktop", payload, evidence_ref="gate://desktop")
    assert observed.workers == 3 and observed.observed_at == NOW
    assert classify_worker(observed, NOW) != CODE_GATE_GENERIC
    hardware["ram_free_gb"] = 1
    assert admission._probe_remote("http://desktop:8080")["workers"] == 0


def test_empty_collection_and_generic_remote_stay_distinct_from_an_allow() -> None:
    absent = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: (_ for _ in ()).throw(OSError("unreadable")),
        local_busy=lambda: False,
        list_remote=lambda: [],
        probe=lambda _url: {"status": "ok"},
    )
    generic = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=None),
        local_busy=lambda: False,
        list_remote=lambda: ["http://worker:9"],
        probe=lambda _url: {"status": "ok"},
    )
    assert classify_gate(absent, NOW)[0] == CODE_GATE_ABSENT
    assert classify_gate(generic, NOW)[0] == CODE_GATE_GENERIC


def test_local_busy_or_a_failed_busy_probe_is_not_idle_capacity() -> None:
    busy = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=3),
        local_busy=lambda: True,
        list_remote=lambda: [],
    )
    failed = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=3),
        local_busy=lambda: (_ for _ in ()).throw(RuntimeError("lock")),
        list_remote=lambda: [],
    )
    assert classify_gate(busy, NOW)[0] == CODE_GATE_BUSY
    assert classify_gate(failed, NOW)[0] == CODE_GATE_BUSY


def test_remote_stale_reading_stays_stale_when_status_is_ok() -> None:
    old = (NOW - timedelta(seconds=90)).isoformat()
    observation = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: (_ for _ in ()).throw(OSError("no ram")),
        list_remote=lambda: ["http://notebook:9/health"],
        probe=lambda _url: {"workers": 4, "busy": False, "heartbeat_at": old, "status": "ok"},
    )
    assert classify_gate(observation, NOW) == (CODE_GATE_STALE, "gate://notebook")


def _clear_admission_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("DARKFAC_HARNESS_WORKER_JOB", raising=False)
    monkeypatch.delenv("DARKFAC_FLEET_DISABLED_HOSTS", raising=False)
    monkeypatch.delenv(admission.ENV_ADMISSION_WORKERS, raising=False)
    monkeypatch.delenv("DARKFAC_TEST_WORKERS", raising=False)


def _fresh_remote(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workers": 2,
        "busy": False,
        "observed_at": NOW.isoformat(),
        "node_id": "grokbox",
        "hostname": "grokbox",
    }
    payload.update(extra)
    return payload


def test_remote_harness_off_still_reads_the_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.harness import remote_dispatch as dispatch

    _clear_admission_worker_env(monkeypatch)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")
    monkeypatch.setenv(admission.ENV_ADMISSION_WORKERS, "http://admit:9")
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://test:9")
    assert admission._remote_worker_urls() == ["http://admit:9"]

    monkeypatch.setenv(admission.ENV_ADMISSION_WORKERS, "")
    assert admission._remote_worker_urls() == []

    monkeypatch.delenv(admission.ENV_ADMISSION_WORKERS)
    assert admission._remote_worker_urls() == ["http://test:9"]

    monkeypatch.delenv("DARKFAC_TEST_WORKERS")
    assert admission._remote_worker_urls() == list(dispatch.worker_urls())


def test_ci_and_worker_job_keep_the_admission_list_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_admission_worker_env(monkeypatch)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "auto")
    monkeypatch.setenv(admission.ENV_ADMISSION_WORKERS, "http://admit:9")
    monkeypatch.setenv("CI", "true")
    assert admission._remote_worker_urls() == []

    monkeypatch.delenv("CI")
    monkeypatch.setenv("DARKFAC_HARNESS_WORKER_JOB", "1")
    assert admission._remote_worker_urls() == []


def test_disabled_url_is_not_probed_and_reported_node_leaves_the_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_admission_worker_env(monkeypatch)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop,notebook")
    monkeypatch.setenv(
        admission.ENV_ADMISSION_WORKERS,
        "http://100.78.181.90:8080,http://10.9.9.9:8080,http://grokbox:8080",
    )
    assert admission._remote_worker_urls() == ["http://10.9.9.9:8080", "http://grokbox:8080"]

    probed: list[str] = []

    def probe(url: str) -> dict[str, Any]:
        probed.append(url)
        if "10.9.9.9" in url:
            return _fresh_remote(workers=4, node_id="desktop", hostname="somewhere-else")
        return _fresh_remote(workers=3, node_id="grokbox", hostname="grokbox")

    by_node = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=0),
        local_busy=lambda: False,
        list_remote=lambda: ["http://100.78.181.90:8080", "http://10.9.9.9:8080", "http://grokbox:8080"],
        probe=probe,
    )
    assert probed == ["http://10.9.9.9:8080", "http://grokbox:8080"]
    refs = [worker.evidence_ref for worker in by_node.workers]
    assert refs == ["gate://local", "gate://grokbox"]

    def probe_hostname(url: str) -> dict[str, Any]:
        return _fresh_remote(workers=4, node_id="other", hostname="ai-notebook")

    by_hostname = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=0),
        local_busy=lambda: False,
        list_remote=lambda: ["http://10.8.8.8:8080"],
        probe=probe_hostname,
    )
    assert [worker.evidence_ref for worker in by_hostname.workers] == ["gate://local"]
    assert classify_gate(by_hostname, NOW) == (CODE_GATE_ZERO, "gate://local")


def test_local_zero_admits_on_one_free_remote_when_dispatch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_admission_worker_env(monkeypatch)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://grokbox:8080")
    probed: list[str] = []

    def probe(url: str) -> dict[str, Any]:
        probed.append(url)
        return _fresh_remote()

    observation = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=0),
        local_busy=lambda: False,
        list_remote=admission._remote_worker_urls,
        probe=probe,
    )
    assert probed == ["http://grokbox:8080"]
    decision = decide(
        planning=_hit("planning", "claude"),
        build=_hit("build", "codex"),
        review=_hit("review", "grok"),
        gate=observation,
        now=NOW,
        not_before_for=lambda _kind: NOW,
    )
    assert decision.outcome == "allow" and decision.not_before is None
    assert decision.evidence_ref.endswith(";gate://grokbox")
    assert "gate://local" not in decision.evidence_ref


def test_status_ok_remote_does_not_cover_local_zero_on_the_fleet_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_admission_worker_env(monkeypatch)
    monkeypatch.setenv("DARKFAC_REMOTE_HARNESS", "off")
    monkeypatch.setenv("DARKFAC_TEST_WORKERS", "http://grokbox:8080,http://desktop:8080")
    observation = collect_gate_observation(
        clock=lambda: NOW,
        measure=lambda: SimpleNamespace(workers=0),
        local_busy=lambda: False,
        list_remote=admission._remote_worker_urls,
        probe=lambda _url: {"status": "ok"},
    )
    assert classify_gate(observation, NOW) == (CODE_GATE_ZERO, "gate://local")
    decision = decide(
        planning=_hit("planning", "claude"),
        build=_hit("build", "codex"),
        review=_hit("review", "grok"),
        gate=observation,
        now=NOW,
        not_before_for=lambda _kind: NOW,
    )
    assert decision.outcome == "wait" and decision.code == CODE_GATE_ZERO
    assert decision.evidence_ref == "gate://local"


def test_pytest_does_not_probe_the_suite_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[str] = []
    import core.harness.suite_lock as suite_lock

    monkeypatch.setattr(suite_lock, "_try_lock_file", lambda *_a, **_k: probes.append("lock"))
    assert os.environ.get("PYTEST_CURRENT_TEST")
    assert admission._local_gate_busy() is False
    assert probes == []


def _admit(
    routes: dict[str, Optional[tuple[str, Optional[str]]]],
    *,
    gate: GateObservation,
    execution_context: str = "headless",
    pinned_harness: Optional[str] = None,
    earliest_at: Optional[datetime] = None,
    build_caps: Optional[list[str]] = None,
    read_caps: Optional[list[str]] = None,
) -> tuple[AdmissionDecision, list[dict[str, Any]], list[tuple[Any, ...]]]:
    seen: list[dict[str, Any]] = []
    earliest_calls: list[tuple[Any, ...]] = []

    def pick_route(stage: str, caps: list[str], reserve_quota: bool = False, **kwargs: Any) -> Any:
        seen.append({
            "stage": stage,
            "caps": list(caps),
            "reserve_quota": reserve_quota,
            "mode": kwargs.get("mode"),
            "implementing_harness": kwargs.get("implementing_harness"),
            "execution_context": kwargs.get("execution_context"),
        })
        return routes.get(stage)

    def earliest(*args: Any, **kwargs: Any) -> datetime:
        earliest_calls.append(args)
        earliest_calls.append(tuple(sorted(kwargs.items())))
        return earliest_at if earliest_at is not None else NOW

    decision = admit(
        build_caps=build_caps or ["harness:codex", "harness:grok"],
        read_caps=read_caps or ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        execution_context=execution_context,
        pinned_harness=pinned_harness,
        pick_route=pick_route,
        earliest=earliest,
        observe_gate=lambda: gate,
        clock=lambda: NOW,
    )
    return decision, seen, earliest_calls


def test_admit_requires_three_routes_and_does_not_reserve_quota_or_call_earliest_on_allow(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    agent_calls: list[str] = []
    import core.line.agent_cli as agent_cli

    monkeypatch.setattr(agent_cli, "run_agent", lambda *_a, **_k: agent_calls.append("agent"))
    monkeypatch.setattr(routing, "pick", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("real pick")))
    before = list(tmp_path.rglob("*"))
    decision, seen, earliest_calls = _admit(_routes(), gate=_gate(_usable()))
    assert decision.outcome == "allow" and earliest_calls == [] and agent_calls == []
    assert list(tmp_path.rglob("*")) == before
    assert [row["stage"] for row in seen] == ["planning", "development", "review"]
    assert all(row["reserve_quota"] is False for row in seen)
    assert [row["mode"] for row in seen] == ["read", "write", "read"]
    assert seen[2]["implementing_harness"] == "codex"
    assert decision.evidence_ref == (
        "routing://planning/claude;routing://development/codex;routing://review/grok;gate://ok"
    )


def test_missing_build_route_waits_on_development_even_with_a_write_pin() -> None:
    routes = {"planning": ("claude", "opus"), "development": None, "review": ("grok", None)}
    decision, seen, earliest_calls = _admit(routes, gate=_gate(_usable()), pinned_harness="claude")
    assert decision.code == CODE_NO_BUILD_ROUTE
    assert decision.stage == STAGE_BUILD and decision.resource == "route:build"
    assert [row["stage"] for row in seen] == ["planning", "development"]
    assert earliest_calls[0][0] == "development"
    assert decision.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)


def test_route_wait_keeps_a_later_reset_and_floors_an_earlier_one() -> None:
    routes = {"planning": None, "development": ("codex", None), "review": ("grok", None)}
    later, _seen, _calls = _admit(routes, gate=_gate(_usable()), earliest_at=NOW + timedelta(hours=2))
    assert later.code == CODE_NO_PLANNING_ROUTE
    assert later.stage == STAGE_PLANNING
    assert later.not_before == NOW + timedelta(hours=2)
    earlier, _seen, calls = _admit(routes, gate=_gate(_usable()), earliest_at=NOW - timedelta(hours=1))
    assert earlier.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
    assert calls[0][0] == "planning"


def test_contingency_hit_may_share_the_writer_family_but_a_plain_hit_may_not() -> None:
    kwargs = dict(planning=_hit("planning", "claude"), build=_hit("build", "claude"), gate=_gate(_usable()),
                  now=NOW, not_before_for=lambda kind: NOW)
    plain = decide(review=_hit("review", "claude"), **kwargs)
    assert plain.outcome == "wait" and plain.code == CODE_REVIEW_SAME_FAMILY
    cascade = decide(review=_hit("review", "claude").model_copy(update={"contingency_level": 4}), **kwargs)
    assert cascade.outcome == "allow"


def test_missing_review_route_waits_without_asking_the_gate_clock() -> None:
    routes = {"planning": ("claude", "opus"), "development": ("codex", "sonnet"), "review": None}
    decision, seen, earliest_calls = _admit(routes, gate=_gate(_usable()))
    assert decision.code == CODE_NO_REVIEW_ROUTE and decision.stage == STAGE_REVIEW
    assert [row["stage"] for row in seen] == ["planning", "development", "review", "review"]
    assert earliest_calls[0][0] == "review"
    assert decision.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)


def test_same_family_review_uses_contingency_and_a_gate_failure_does_not_call_earliest() -> None:
    # Only the writer's family answers: cascade level 3 (same family, another model) admits.
    same, seen, calls = _admit(_routes(build="claude", review="claude"), gate=_gate(_usable()))
    assert same.outcome == "allow" and "level=3" in same.evidence_ref
    assert seen[2]["implementing_harness"] == "claude"
    assert calls == []

    zero = _worker("local", 0, False, NOW, "gate://local")
    waiting, _seen, gate_calls = _admit(_routes(), gate=_gate(zero))
    assert waiting.code == CODE_GATE_ZERO and waiting.stage == STAGE_GATE
    assert waiting.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
    assert gate_calls == []


def test_write_pin_is_the_review_family_and_headless_antigravity_is_not() -> None:
    pinned, seen, _calls = _admit(
        _routes(), gate=_gate(_usable()), execution_context="interactive", pinned_harness="claude",
    )
    assert pinned.outcome == "allow"
    assert seen[2]["implementing_harness"] == "claude"
    assert "routing://development/claude" in pinned.evidence_ref
    assert "routing://development/codex" not in pinned.evidence_ref

    headless, seen, _calls = _admit(
        _routes(build="grok", review="codex"),
        gate=_gate(_usable()),
        execution_context="headless",
        pinned_harness="antigravity",
    )
    assert headless.outcome == "allow"
    assert seen[2]["implementing_harness"] == "grok"
    assert "routing://development/grok" in headless.evidence_ref
    assert "antigravity" not in headless.evidence_ref


def test_unknown_execution_context_is_rejected() -> None:
    with pytest.raises(ValueError):
        admit(
            build_caps=["harness:codex"],
            read_caps=["harness:claude"],
            execution_context="fleet",
            pick_route=lambda *_a, **_k: ("codex", None),
            observe_gate=lambda: _gate(_usable()),
            clock=lambda: NOW,
        )


def test_recovery_receipt_requires_fresh_candidate_bound_production_chain():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    candidate = "candidate-468"
    stamp = now.isoformat()

    def stage(**values):
        return {"candidate_digest": candidate, "observed_at": stamp,
                "evidence_ref": "evidence://stage", **values}

    receipt = {
        "schema_version": 1, "simulated": False, "candidate_digest": candidate,
        "run_id": "fleet-run-468", "implementer_family": "codex",
        "gate": stage(passed=True, marker="[HARNESS_PASS]"),
        "review": stage(approved=True, independent=True, reviewer_family="claude", implementer_family="codex"),
        "ci": stage(green=True),
        "merge": stage(sha="a" * 40, reachable=True),
        "deploy": stage(deployed=True, sha="a" * 40),
        "smoke": stage(http_status=204),
        "learning_pack": stage(indexed=True),
    }
    assert admission.verify_recovery_receipt(
        receipt, candidate_digest=candidate, run_id="fleet-run-468", now=now
    ) == (True, "receipt_valid")
    assert admission.verify_recovery_receipt(
        {**receipt, "candidate_digest": "other-candidate"}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now
    )[1] == "receipt_candidate_mismatch"
    assert admission.verify_recovery_receipt(
        {**receipt, "simulated": True}, candidate_digest=candidate, run_id="fleet-run-468", now=now
    )[1] == "receipt_simulation"
    assert admission.verify_recovery_receipt(
        {**receipt, "ci": stage(green=True, observed_at=(now - timedelta(days=8)).isoformat())},
        candidate_digest=candidate, run_id="fleet-run-468", now=now,
    )[1] == "receipt_ci_stale"
    assert admission.verify_recovery_receipt(
        {**receipt, "learning_pack": None}, candidate_digest=candidate, run_id="fleet-run-468", now=now
    )[1] == "receipt_learning_pack_missing"
    assert admission.verify_recovery_receipt(
        {**receipt, "deploy": stage(deployed=True, sha="b" * 40)}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now
    )[1] == "receipt_deploy_mismatch"
    assert admission.verify_recovery_receipt(
        {**receipt, "run_id": "other-run"}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now,
    )[1] == "receipt_run_mismatch"
    bad_review = {**receipt, "review": stage(approved=True, independent=True,
                                              reviewer_family="claude", implementer_family="grok")}
    assert admission.verify_recovery_receipt(
        bad_review, candidate_digest=candidate, run_id="fleet-run-468", now=now,
    )[1] == "receipt_review_not_independent"
    same_family = dict(approved=True, independent=True, reviewer_family="codex", implementer_family="codex")
    contingency = {"level": 3, "persona": "standard", "reason": "same_family_other_model"}
    for review, expected in (
        (stage(**same_family), "receipt_review_not_independent"),
        (stage(**same_family, contingency={"level": 1, "persona": "standard", "reason": "x"}),
         "receipt_review_not_independent"),
        (stage(**same_family, contingency=contingency), "receipt_valid"),
    ):
        assert admission.verify_recovery_receipt(
            {**receipt, "review": review}, candidate_digest=candidate, run_id="fleet-run-468", now=now,
        )[1] == expected

def test_openrouter_writer_and_reviewer_compare_model_family_not_harness_name() -> None:
    def hit(stage: str, model: str) -> admission.RouteHit:
        return admission.RouteHit(stage=stage, harness="openrouter", model=model, evidence_ref=f"routing://{stage}/openrouter")

    def run(build_model: str, review_model: str) -> AdmissionDecision:
        return decide(
            planning=hit("planning", "gpt-sol"), build=hit("build", build_model), review=hit("review", review_model),
            gate=_gate(_usable()), now=NOW, not_before_for=lambda kind: NOW,
        )

    assert run("gpt-sol", "sonnet").outcome == "allow"
    blocked = run("sonnet", "sonnet")
    assert blocked.outcome == "wait" and blocked.code == CODE_REVIEW_SAME_FAMILY
