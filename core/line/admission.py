"""Read-only admission preflight before a ticket worktree or PLAN (USR-468 T1).

The launcher asks this module whether planning, build, an independent review
and a usable gate worker are available. The answer is an allow or a wait.
Nothing here starts an agent, reserves quota or writes a candidate artifact.
An allow is not a cached route: each later stage calls ``pick`` again.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.line import routing
from core.line.review_cascade import valid_contingency

logger = logging.getLogger(__name__)

CODE_ADMITTED = "admitted"
CODE_NO_PLANNING_ROUTE = "no_planning_route"
CODE_NO_BUILD_ROUTE = "no_build_route"
CODE_NO_REVIEW_ROUTE = "no_review_route"
CODE_REVIEW_SAME_FAMILY = "review_same_family"
CODE_GATE_ABSENT = "gate_capacity_absent"
CODE_GATE_ZERO = "gate_capacity_zero"
CODE_GATE_BUSY = "gate_worker_busy"
CODE_GATE_STALE = "gate_worker_stale"
CODE_GATE_GENERIC = "gate_health_generic"

STAGE_ADMISSION = "admission"
STAGE_PLANNING = "planning"
STAGE_BUILD = "build"
STAGE_REVIEW = "review"
STAGE_GATE = "gate"

RESOURCE_LINE = "line"
RESOURCE_PLANNING = "route:planning"
RESOURCE_BUILD = "route:build"
RESOURCE_REVIEW = "route:review"
RESOURCE_GATE = "gate:worker"

# A gate reading older than this is not capacity. Equal to the line lock's
# heartbeat-stale window so a paused notebook is not treated as idle.
GATE_OBSERVATION_MAX_AGE = timedelta(seconds=90)
_GATE_CAUSE_PRIORITY = (
    CODE_GATE_ZERO,
    CODE_GATE_BUSY,
    CODE_GATE_STALE,
    CODE_GATE_GENERIC,
)
_STRUCTURAL_FIELDS = ("workers", "busy", "observed_at")

PickFn = Callable[..., Optional[tuple[str, Optional[str]]]]
EarliestFn = Callable[..., datetime]
Clock = Callable[[], datetime]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _as_utc(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


class AdmissionDecision(BaseModel):
    """Allow or wait. Both outcomes carry the same audit fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: str
    code: str
    stage: str
    resource: str
    observed_at: datetime
    evidence_ref: str
    not_before: Optional[datetime] = None

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, value: str) -> str:
        if value not in ("allow", "wait"):
            raise ValueError("outcome must be allow or wait")
        return value

    @field_validator("code", "stage", "resource", "evidence_ref")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("admission field must be a non-empty string")
        return value

    @field_validator("observed_at", "not_before")
    @classmethod
    def _aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return _as_utc(value)

    @model_validator(mode="after")
    def _wait_carries_not_before(self) -> "AdmissionDecision":
        if self.outcome == "wait" and self.not_before is None:
            raise ValueError("wait requires not_before")
        if self.outcome == "allow" and self.not_before is not None:
            raise ValueError("allow does not carry not_before")
        return self

    def public_dict(self) -> dict[str, Any]:
        """JSON-ready view. ``not_before`` is null on allow."""
        return {
            "outcome": self.outcome,
            "code": self.code,
            "stage": self.stage,
            "resource": self.resource,
            "observed_at": self.observed_at.isoformat(),
            "evidence_ref": self.evidence_ref,
            "not_before": None if self.not_before is None else self.not_before.isoformat(),
        }


class RouteHit(BaseModel):
    """One stage route observed by a read-only ``pick``. ``harness`` is None when none was elected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    harness: Optional[str] = None
    model: Optional[str] = None
    evidence_ref: str
    # USR-488: cascade level 3 or 4 (same family, recorded). Only these may share the writer's family.
    contingency_level: Optional[int] = None


class GateWorkerObservation(BaseModel):
    """One gate worker. Missing structural fields are generic health, not capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_id: str
    evidence_ref: str
    workers: Optional[int] = None
    busy: Optional[bool] = None
    observed_at: Optional[datetime] = None
    evidenced_fields: tuple[str, ...] = ()

    @field_validator("workers")
    @classmethod
    def _workers_int(cls, value: Optional[int]) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("workers must be an int")
        return value

    @field_validator("observed_at")
    @classmethod
    def _worker_time(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return _as_utc(value)


class GateObservation(BaseModel):
    """The worker collection seen at ``observed_at``. An empty collection is not capacity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workers: list[GateWorkerObservation] = []
    observed_at: datetime
    evidence_ref: str = "gate://collection"

    @field_validator("observed_at")
    @classmethod
    def _collection_time(cls, value: datetime) -> datetime:
        return _as_utc(value)


def allow_decision(observed_at: datetime, evidence_ref: str = "admission://allow") -> AdmissionDecision:
    """An allow with the audit fields and no ``not_before``."""
    return AdmissionDecision(
        outcome="allow",
        code=CODE_ADMITTED,
        stage=STAGE_ADMISSION,
        resource=RESOURCE_LINE,
        observed_at=_as_utc(observed_at),
        evidence_ref=evidence_ref,
        not_before=None,
    )


def _backoff(now: datetime, when: datetime) -> datetime:
    floor = _as_utc(now) + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
    candidate = _as_utc(when)
    return candidate if candidate >= floor else floor


def _wait(
    *,
    code: str,
    stage: str,
    resource: str,
    observed_at: datetime,
    evidence_ref: str,
    not_before: datetime,
) -> AdmissionDecision:
    return AdmissionDecision(
        outcome="wait",
        code=code,
        stage=stage,
        resource=resource,
        observed_at=_as_utc(observed_at),
        evidence_ref=evidence_ref,
        not_before=_as_utc(not_before),
    )


def classify_worker(worker: GateWorkerObservation, now: datetime) -> Optional[str]:
    """None when the worker is usable capacity. Generic, stale, zero and busy are never usable.

    A payload that only says ``status=ok`` does not fill workers, busy or observed_at.
    Those missing fields stay generic health. An empty collection is handled by
    :func:`classify_gate`, not by inventing a worker here.
    """
    evidenced = set(worker.evidenced_fields)
    if any(field not in evidenced for field in _STRUCTURAL_FIELDS):
        return CODE_GATE_GENERIC
    if worker.observed_at is None or worker.workers is None or not isinstance(worker.busy, bool):
        return CODE_GATE_GENERIC
    age = _as_utc(now) - worker.observed_at
    if age >= GATE_OBSERVATION_MAX_AGE:
        return CODE_GATE_STALE
    if worker.workers <= 0:
        return CODE_GATE_ZERO
    if worker.busy:
        return CODE_GATE_BUSY
    return None


def classify_gate(gate: GateObservation, now: datetime) -> tuple[Optional[str], str]:
    """Return ``(code, evidence_ref)``. ``code`` is None when at least one worker is usable.

    Zero, busy and stale readings are not promoted by an empty collection or by a
    sibling whose health is only a generic status.
    """
    if not gate.workers:
        return CODE_GATE_ABSENT, gate.evidence_ref
    failures: list[tuple[str, str]] = []
    for worker in gate.workers:
        code = classify_worker(worker, now)
        if code is None:
            return None, worker.evidence_ref
        failures.append((code, worker.evidence_ref))
    for code in _GATE_CAUSE_PRIORITY:
        for found, ref in failures:
            if found == code:
                return code, ref
    return failures[0]


def decide(
    *,
    planning: RouteHit,
    build: RouteHit,
    review: RouteHit,
    gate: GateObservation,
    now: datetime,
    not_before_for: Callable[[str], datetime],
) -> AdmissionDecision:
    """Pure admission decision. Does not call a router, an agent or the filesystem."""
    observed = _as_utc(now)

    def wait_for(hit: RouteHit, code: str, resource: str, kind: str) -> AdmissionDecision:
        return _wait(
            code=code,
            stage=hit.stage,
            resource=resource,
            observed_at=observed,
            evidence_ref=hit.evidence_ref,
            not_before=_backoff(observed, not_before_for(kind)),
        )

    if not planning.harness:
        return wait_for(planning, CODE_NO_PLANNING_ROUTE, RESOURCE_PLANNING, STAGE_PLANNING)
    if not build.harness:
        return wait_for(build, CODE_NO_BUILD_ROUTE, RESOURCE_BUILD, STAGE_BUILD)
    if not review.harness:
        return wait_for(review, CODE_NO_REVIEW_ROUTE, RESOURCE_REVIEW, STAGE_REVIEW)
    if _hit_family(review) == _hit_family(build) and review.contingency_level is None:
        return wait_for(review, CODE_REVIEW_SAME_FAMILY, RESOURCE_REVIEW, STAGE_REVIEW)
    gate_code, gate_ref = classify_gate(gate, observed)
    if gate_code is not None:
        return _wait(
            code=gate_code,
            stage=STAGE_GATE,
            resource=RESOURCE_GATE,
            observed_at=observed,
            evidence_ref=gate_ref,
            not_before=_backoff(observed, not_before_for(STAGE_GATE)),
        )
    evidence = ";".join((planning.evidence_ref, build.evidence_ref, review.evidence_ref, gate_ref))
    return allow_decision(observed, evidence_ref=evidence)


def worker_from_health(worker_id: str, health: Any, *, evidence_ref: str) -> GateWorkerObservation:
    """Map a health payload. Only explicit ``workers``, ``busy`` and a timestamp count.

    ``status`` is ignored. A body of ``{"status": "ok"}`` is generic health.
    """
    if not isinstance(health, dict):
        return GateWorkerObservation(worker_id=worker_id, evidence_ref=evidence_ref, evidenced_fields=())
    evidenced: list[str] = []
    workers: Optional[int] = None
    if "workers" in health:
        evidenced.append("workers")
        raw_workers = health.get("workers")
        if isinstance(raw_workers, int) and not isinstance(raw_workers, bool):
            workers = raw_workers
    busy: Optional[bool] = None
    if "busy" in health:
        evidenced.append("busy")
        if isinstance(health.get("busy"), bool):
            busy = health.get("busy")
    observed: Optional[datetime] = None
    for key in ("observed_at", "checked_at", "heartbeat_at"):
        if key in health:
            evidenced.append("observed_at")
            observed = _parse_time(health.get(key))
            break
    return GateWorkerObservation(
        worker_id=worker_id,
        evidence_ref=evidence_ref,
        workers=workers,
        busy=busy,
        observed_at=observed,
        evidenced_fields=tuple(evidenced),
    )


def _local_gate_busy() -> bool:
    """True when this host's suite slots are all held. Never called under pytest.

    The probe takes a free slot only long enough to see that one exists, then
    releases it. It does not write a sidecar or a candidate file. A probe error
    is busy (fail closed), not free capacity.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from core.harness import suite_lock
    except Exception as exc:  # noqa: BLE001 - an unreadable lock is not free capacity
        logger.warning("gate slot probe unavailable: %s", type(exc).__name__)
        return True
    if suite_lock.is_disabled():
        return False
    if suite_lock.is_held_by_ancestor():
        return True
    try:
        return not suite_lock.probe_free_slot()
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("gate slot probe failed: %s", type(exc).__name__)
        return True


# USR-510: admission reads this list even when dispatch mode is off.
ENV_ADMISSION_WORKERS = "DARKFAC_ADMISSION_WORKERS"


def _filter_disabled_worker_urls(dispatch: Any, raw: str) -> list[str]:
    """Split a worker list and drop URLs whose host is in ``DARKFAC_FLEET_DISABLED_HOSTS``."""
    urls = [part.strip() for part in raw.split(",") if part.strip()]
    disabled = dispatch.disabled_hosts()
    if not disabled:
        return urls
    return [url for url in urls if not dispatch.is_worker_disabled(url, disabled)]


def _remote_worker_urls() -> list[str]:
    """Gate workers admission may read.

    ``DARKFAC_REMOTE_HARNESS=off`` does not hide them: that mode only stops
    dispatch. CI and a nested worker job still return an empty list.
    The source is ``DARKFAC_ADMISSION_WORKERS`` when the variable is set
    (including an empty value), otherwise ``DARKFAC_TEST_WORKERS`` when that
    variable is set, otherwise the default of ``worker_urls``. A URL in
    ``DARKFAC_FLEET_DISABLED_HOSTS`` is omitted and is not probed.
    """
    try:
        from core.harness import remote_dispatch as dispatch
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate worker list unavailable: %s", type(exc).__name__)
        return []
    if dispatch._unconditional_disable_reason():
        return []
    admission_raw = os.environ.get(ENV_ADMISSION_WORKERS)
    if admission_raw is not None:
        return _filter_disabled_worker_urls(dispatch, admission_raw)
    test_raw = os.environ.get(dispatch.ENV_TEST_WORKERS)
    if test_raw is not None:
        return _filter_disabled_worker_urls(dispatch, test_raw)
    return list(dispatch.worker_urls())


def _url_is_disabled(url: str) -> bool:
    """True when this URL must not be probed. An unreadable policy does not invent a ban."""
    try:
        from core.harness import remote_dispatch as dispatch
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate worker disable check unavailable: %s", type(exc).__name__)
        return False
    return bool(dispatch.is_worker_disabled(url))


def _reported_node_disabled(health: Any) -> bool:
    """True when health names a node or host banned by ``DARKFAC_FLEET_DISABLED_HOSTS``."""
    if not isinstance(health, dict):
        return False
    try:
        from core.harness import remote_dispatch as dispatch
    except Exception as exc:  # noqa: BLE001
        logger.warning("gate worker disable check unavailable: %s", type(exc).__name__)
        return False
    node_id = health.get("node_id")
    hostname = health.get("hostname")
    return bool(dispatch.is_worker_node_disabled(
        node_id if isinstance(node_id, str) else None,
        hostname if isinstance(hostname, str) else None,
    ))


def _probe_remote(url: str) -> Any:
    from core.harness import remote_dispatch as dispatch
    from core.infra.metrics import HardwareMetrics
    from tests import _worker_capacity as capacity
    import json
    import urllib.request

    health = dispatch.probe_health(url)
    if not isinstance(health, dict):
        return health
    request = urllib.request.Request(
        f"{url.rstrip('/')}/metrics/hardware", headers=dispatch._auth_headers()
    )
    with urllib.request.urlopen(request, timeout=dispatch.health_probe_timeout_sec()) as response:
        metrics = HardwareMetrics.model_validate(json.load(response))
    available = int(metrics.ram_free_gb * capacity.BYTES_PER_GIB)
    by_memory = max(0, available - capacity.DEFAULT_SYSTEM_RESERVE_BYTES) // capacity.DEFAULT_MIN_WORKER_MEMORY_BYTES
    workers = max(0, min(by_memory, metrics.cpu_cores_physical, capacity.DEFAULT_HARD_CAP_WORKERS))
    return {**health, "workers": workers, "observed_at": metrics.collected_at.isoformat()}


def _measure_local_workers() -> Any:
    """Local xdist capacity. Imported lazily so this module loads without the RAM probe."""
    from core.line.gate_ram import measure_workers

    return measure_workers()


def _worker_ref(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = parsed.hostname or "remote"
    return f"gate://{host}"


def collect_gate_observation(
    *,
    clock: Clock = lambda: datetime.now(timezone.utc),
    measure: Optional[Callable[[], Any]] = None,
    local_busy: Callable[[], bool] = _local_gate_busy,
    list_remote: Callable[[], Sequence[str]] = _remote_worker_urls,
    probe: Callable[[str], Any] = _probe_remote,
) -> GateObservation:
    """Read local worker capacity and remote health. Does not start a gate or write a candidate.

    A URL in ``DARKFAC_FLEET_DISABLED_HOSTS`` is not probed. Health whose
    ``node_id`` or ``hostname`` names a disabled host leaves the collection.
    """
    now = _as_utc(clock())
    workers: list[GateWorkerObservation] = []
    try:
        reading = measure() if measure is not None else _measure_local_workers()
        try:
            busy = bool(local_busy())
        except Exception as exc:  # noqa: BLE001 - unknown busy state is not idle
            logger.warning("local gate busy probe failed: %s", type(exc).__name__)
            busy = True
        count = getattr(reading, "workers", None)
        if isinstance(count, int) and not isinstance(count, bool):
            workers.append(
                GateWorkerObservation(
                    worker_id="local",
                    evidence_ref="gate://local",
                    workers=count,
                    busy=busy,
                    observed_at=now,
                    evidenced_fields=_STRUCTURAL_FIELDS,
                )
            )
    except Exception as exc:  # noqa: BLE001 - a failed reading is omitted, never invented
        logger.warning("local gate capacity unreadable: %s", type(exc).__name__)
    for url in list_remote():
        if _url_is_disabled(url):
            logger.info("gate worker %s is disabled; not probed", _worker_ref(url))
            continue
        try:
            health = probe(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("gate health probe failed for %s: %s", _worker_ref(url), type(exc).__name__)
            health = None
        if _reported_node_disabled(health):
            logger.info("gate worker %s reports a disabled node; omitted", _worker_ref(url))
            continue
        ref = _worker_ref(url)
        workers.append(worker_from_health(ref, health, evidence_ref=ref))
    return GateObservation(workers=workers, observed_at=now, evidence_ref="gate://collection")


def _select_route(
    pick_route: PickFn,
    routing_stage: str,
    caps: Iterable[str],
    *,
    decision_stage: str,
    **kwargs: Any,
) -> RouteHit:
    chosen = pick_route(routing_stage, list(caps), reserve_quota=False, **kwargs)
    if not chosen or not chosen[0]:
        return RouteHit(
            stage=decision_stage,
            harness=None,
            model=None,
            evidence_ref=f"routing://{routing_stage}/none",
        )
    harness = str(chosen[0])
    model = chosen[1] if len(chosen) > 1 else None
    return RouteHit(
        stage=decision_stage,
        harness=harness,
        model=None if model is None else str(model),
        evidence_ref=f"routing://{routing_stage}/{harness}",
    )


def _hit_family(hit: RouteHit) -> str:
    """Model family of a route; the harness name only when the family is unknown (OpenRouter carries many)."""
    from core.line.recovery_receipt import route_family

    try:
        return route_family((hit.harness or "").casefold(), hit.model)
    except ValueError:
        return (hit.harness or "").casefold()


def _select_review(
    pick_route: PickFn, caps: Iterable[str], writer: str, writer_model: Optional[str] = None, **common: Any,
) -> RouteHit:
    """Cascade of USR-488: independent route first, recorded contingency before any wait."""
    from core.line.review_cascade import select_review_route

    writer_family = _hit_family(RouteHit(stage=STAGE_BUILD, harness=writer, model=writer_model, evidence_ref=""))
    families = {writer_family} if writer_family != writer.casefold() else set()
    route = select_review_route(
        lambda stage, caps_, **kw: pick_route(stage, caps_, reserve_quota=False, mode="read", **common, **kw),
        caps, implementing_harness=writer, families=families,
    )
    if route is None:
        return RouteHit(stage=STAGE_REVIEW, harness=None, model=None, evidence_ref="routing://review/none")
    return RouteHit(
        stage=STAGE_REVIEW, harness=route.harness, model=route.model,
        evidence_ref=f"routing://review/{route.harness}" + (f"#level={route.level}" if route.level > 2 else ""),
        contingency_level=route.level if route.contingency else None,
    )


def _prospective_writer(picked: Optional[str], pinned: Optional[str], *, execution_context: str) -> Optional[str]:
    """The harness that would write, once a write route exists. A pin does not invent a route."""
    if not picked:
        return None
    pin = (pinned or "").lower().strip()
    if not pin or pin == picked:
        return picked
    if execution_context == "headless" and pin == "antigravity":
        return picked
    from core.line.agent_cli import supports

    if not supports(pin, "write", execution_context=execution_context):
        return picked
    return pin


def admit(
    *,
    build_caps: Iterable[str],
    read_caps: Iterable[str],
    execution_context: str = "interactive",
    operating_harness: Optional[str] = None,
    project_id: Optional[str] = None,
    complexity: Optional[str] = None,
    pinned_harness: Optional[str] = None,
    config: Optional[routing.RoutingConfig] = None,
    pick_route: PickFn = routing.pick,
    earliest: EarliestFn = routing.earliest_route_available_at,
    observe_gate: Optional[Callable[[], GateObservation]] = None,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> AdmissionDecision:
    """Read routes and gate capacity. Does not execute an agent or create a candidate file.

    ``reserve_quota`` is always false. The returned allow does not replace the
    ``pick`` each stage runs later.
    """
    if execution_context not in ("interactive", "headless"):
        raise ValueError(f"Unknown execution_context: {execution_context}")
    now = _as_utc(clock())
    cfg = config
    operating = operating_harness if execution_context == "interactive" else None
    common: dict[str, Any] = {
        "project_id": project_id,
        "execution_context": execution_context,
        "config": cfg,
    }
    planning = _select_route(
        pick_route, "planning", read_caps, decision_stage=STAGE_PLANNING, mode="read", **common,
    )
    build_pick = _select_route(
        pick_route,
        "development",
        build_caps,
        decision_stage=STAGE_BUILD,
        mode="write",
        operating_harness=operating,
        complexity=complexity,
        **common,
    )
    writer = _prospective_writer(build_pick.harness, pinned_harness, execution_context=execution_context)
    if writer and writer != build_pick.harness:
        build = RouteHit(
            stage=STAGE_BUILD,
            harness=writer,
            model=None,
            evidence_ref=f"routing://development/{writer}",
        )
    else:
        build = build_pick
    if writer:
        review = _select_review(pick_route, read_caps, writer, build.model, **common)
    else:
        review = RouteHit(stage=STAGE_REVIEW, harness=None, model=None, evidence_ref="routing://review/none")

    def not_before_for(kind: str) -> datetime:
        if kind == STAGE_GATE:
            return now + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
        routing_stage = "development" if kind == STAGE_BUILD else kind
        caps = build_caps if routing_stage == "development" else read_caps
        return earliest(
            routing_stage,
            list(caps),
            cfg,
            now=now,
            mode="write" if routing_stage == "development" else "read",
            implementing_harness=writer if routing_stage == "review" else None,
            operating_harness=operating if routing_stage == "development" else None,
        )

    gate = observe_gate() if observe_gate is not None else collect_gate_observation(clock=lambda: now)
    return decide(
        planning=planning,
        build=build,
        review=review,
        gate=gate,
        now=now,
        not_before_for=not_before_for,
    )


RECOVERY_RECEIPT_MAX_AGE = timedelta(days=7)
RECOVERY_RECEIPT_MARKER = "[RECOVERY_RECEIPT] "


def verify_recovery_receipt(
    receipt: Any,
    *,
    candidate_digest: str,
    run_id: Optional[str] = None,
    now: datetime,
    max_age: timedelta = RECOVERY_RECEIPT_MAX_AGE,
) -> tuple[bool, str]:
    """Fail closed unless every production stage is fresh and bound to one candidate.

    Each evidence object carries a timestamp, candidate digest and reference. The
    deploy SHA must equal the reachable merge SHA, and smoke must be an HTTP 2xx.
    """
    if not isinstance(receipt, dict) or not receipt:
        return False, "receipt_missing"
    if receipt.get("schema_version") != 1:
        return False, "receipt_schema_invalid"
    if receipt.get("simulated") is not False:
        return False, "receipt_simulation"
    expected = str(candidate_digest or "").strip()
    if not expected or expected.casefold() == "unknown":
        return False, "candidate_digest_unavailable"
    if receipt.get("candidate_digest") != expected:
        return False, "receipt_candidate_mismatch"
    expected_run_id = str(run_id or "").strip()
    if not expected_run_id or receipt.get("run_id") != expected_run_id:
        return False, "receipt_run_mismatch"

    current = _as_utc(now)
    required = ("gate", "review", "ci", "merge", "deploy", "smoke", "learning_pack")
    stages: dict[str, Any] = {}
    for stage in required:
        item = receipt.get(stage)
        if not isinstance(item, dict) or not item:
            return False, f"receipt_{stage}_missing"
        if item.get("candidate_digest") != expected:
            return False, f"receipt_{stage}_candidate_mismatch"
        reference = item.get("evidence_ref")
        if not isinstance(reference, str) or not reference.strip():
            return False, f"receipt_{stage}_evidence_missing"
        stamp = _parse_time(item.get("observed_at"))
        if stamp is None:
            return False, f"receipt_{stage}_timestamp_missing"
        age = current - stamp
        if age < timedelta(0) or age > max_age:
            return False, f"receipt_{stage}_stale"
        stages[stage] = item

    gate = stages["gate"]
    if gate.get("passed") is not True or gate.get("marker") != "[HARNESS_PASS]":
        return False, "receipt_gate_not_passed"
    review = stages["review"]
    if review.get("approved") is not True or review.get("independent") is not True:
        return False, "receipt_review_not_approved"
    implementer_family = str(receipt.get("implementer_family") or "").strip().casefold()
    same_family = str(review.get("reviewer_family") or "").strip().casefold() == implementer_family
    if (not review.get("reviewer_family") or not implementer_family
            or str(review.get("implementer_family") or "").strip().casefold() != implementer_family
            or (same_family and not valid_contingency(review.get("contingency")))):
        return False, "receipt_review_not_independent"
    if stages["ci"].get("green") is not True:
        return False, "receipt_ci_not_green"
    merge_sha = stages["merge"].get("sha")
    if not isinstance(merge_sha, str) or not re_full_sha(merge_sha):
        return False, "receipt_merge_sha_invalid"
    if stages["merge"].get("reachable") is not True:
        return False, "receipt_merge_unreachable"
    deploy = stages["deploy"]
    if deploy.get("deployed") is not True or deploy.get("sha") != merge_sha:
        return False, "receipt_deploy_mismatch"
    status = stages["smoke"].get("http_status")
    if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status < 300:
        return False, "receipt_smoke_failed"
    if stages["learning_pack"].get("indexed") is not True:
        return False, "receipt_learning_pack_not_indexed"
    return True, "receipt_valid"


def re_full_sha(value: str) -> bool:
    """Accept a full Git object ID (SHA-1 or SHA-256)."""
    return len(value) in (40, 64) and all(char in "0123456789abcdefABCDEF" for char in value)
