"""Read-only admission preflight before a ticket worktree or PLAN (USR-468 T1, simplified by USR-504).

The launcher asks this module only whether a development route exists (subscription with known or
unknown quota, or OpenRouter) and, while the run has no plan yet, a planning route. Review and gate
decide when their turn comes: the review cascade and the gate already handle their own absence.
The answer is an allow or a wait. Nothing here starts an agent, reserves quota or writes a candidate
artifact. An allow is not a cached route: each later stage calls ``pick`` again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from core.line import routing
from core.line.review_cascade import valid_contingency


CODE_ADMITTED = "admitted"
CODE_NO_PLANNING_ROUTE = "no_planning_route"
CODE_NO_BUILD_ROUTE = "no_build_route"

STAGE_ADMISSION = "admission"
STAGE_PLANNING = "planning"
STAGE_BUILD = "build"

RESOURCE_LINE = "line"
RESOURCE_PLANNING = "route:planning"
RESOURCE_BUILD = "route:build"

# A wait never asks the line to look again later than this, whatever reset is known further away.
ADMISSION_MAX_WAIT = timedelta(minutes=30)

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
    cause: Optional["DataPolicyCause"] = None

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
        """JSON-ready view. ``not_before`` is null on allow; cause is structured when present."""
        return {
            "outcome": self.outcome,
            "code": self.code,
            "stage": self.stage,
            "resource": self.resource,
            "observed_at": self.observed_at.isoformat(),
            "evidence_ref": self.evidence_ref,
            "not_before": None if self.not_before is None else self.not_before.isoformat(),
            "cause": None if self.cause is None else self.cause.model_dump(mode="json"),
        }


class DataPolicyCause(BaseModel):
    """Sanitized explanation for a route removed by the effective project data policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    project_id: str
    source_file: str
    providers: list[str]
    restriction: dict[str, Any]


class RouteHit(BaseModel):
    """One stage route observed by a read-only ``pick``. ``harness`` is None when none was elected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    harness: Optional[str] = None
    model: Optional[str] = None
    evidence_ref: str
    cause: Optional[DataPolicyCause] = None


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
    cause: Optional[DataPolicyCause] = None,
) -> AdmissionDecision:
    return AdmissionDecision(
        outcome="wait",
        code=code,
        stage=stage,
        resource=resource,
        observed_at=_as_utc(observed_at),
        evidence_ref=evidence_ref,
        not_before=_as_utc(not_before),
        cause=cause,
    )


def decide(
    *,
    planning: Optional[RouteHit],
    build: RouteHit,
    now: datetime,
    not_before_for: Callable[[str], datetime],
) -> AdmissionDecision:
    """Pure admission decision. ``planning`` is None when the run already has a plan."""
    observed = _as_utc(now)

    def wait_for(hit: RouteHit, code: str, resource: str, kind: str) -> AdmissionDecision:
        nearest = min(_as_utc(not_before_for(kind)), observed + ADMISSION_MAX_WAIT)
        return _wait(
            code=code,
            stage=hit.stage,
            resource=resource,
            observed_at=observed,
            evidence_ref=hit.evidence_ref,
            not_before=_backoff(observed, nearest),
            cause=hit.cause,
        )

    if planning is not None and not planning.harness:
        return wait_for(planning, CODE_NO_PLANNING_ROUTE, RESOURCE_PLANNING, STAGE_PLANNING)
    if not build.harness:
        return wait_for(build, CODE_NO_BUILD_ROUTE, RESOURCE_BUILD, STAGE_BUILD)
    evidence = ";".join(hit.evidence_ref for hit in (planning, build) if hit is not None)
    return allow_decision(observed, evidence_ref=evidence)


def _select_route(
    pick_route: PickFn,
    routing_stage: str,
    caps: Iterable[str],
    *,
    decision_stage: str,
    policy_diagnostic: Optional[dict[str, Any]] = None,
    **kwargs: Any,
) -> RouteHit:
    chosen = pick_route(
        routing_stage, list(caps), reserve_quota=False,
        policy_diagnostic=policy_diagnostic, **kwargs,
    )
    if not chosen or not chosen[0]:
        cause = (
            DataPolicyCause.model_validate(policy_diagnostic)
            if policy_diagnostic and policy_diagnostic.get("code") == "data_policy"
            else None
        )
        return RouteHit(
            stage=decision_stage,
            harness=None,
            model=None,
            evidence_ref=f"routing://{routing_stage}/none",
            cause=cause,
        )
    harness = str(chosen[0])
    model = chosen[1] if len(chosen) > 1 else None
    return RouteHit(
        stage=decision_stage,
        harness=harness,
        model=None if model is None else str(model),
        evidence_ref=f"routing://{routing_stage}/{harness}",
    )


def admit(
    *,
    build_caps: Iterable[str],
    read_caps: Iterable[str],
    execution_context: str = "interactive",
    operating_harness: Optional[str] = None,
    project_id: Optional[str] = None,
    complexity: Optional[str] = None,
    pinned_harness: Optional[str] = None,
    plan_exists: bool = False,
    config: Optional[routing.RoutingConfig] = None,
    pick_route: PickFn = routing.pick,
    earliest: EarliestFn = routing.earliest_route_available_at,
    clock: Clock = lambda: datetime.now(timezone.utc),
) -> AdmissionDecision:
    """Read the development route and, without a plan yet, the planning route.

    ``plan_exists`` is true when the run or preserved worktree already has SPEC.md/tickets.json.
    ``pinned_harness`` is accepted for callers and ignored: a pin does not invent a route.
    ``reserve_quota`` is always false. The returned allow does not replace the ``pick`` each stage
    runs later.
    """
    if execution_context not in ("interactive", "headless"):
        raise ValueError(f"Unknown execution_context: {execution_context}")
    now = _as_utc(clock())
    operating = operating_harness if execution_context == "interactive" else None
    common: dict[str, Any] = {
        "project_id": project_id,
        "execution_context": execution_context,
        "config": config,
    }
    planning_diagnostic: dict[str, Any] = {}
    planning = None if plan_exists else _select_route(
        pick_route, "planning", read_caps, decision_stage=STAGE_PLANNING,
        policy_diagnostic=planning_diagnostic, mode="read", **common,
    )
    build = _select_route(
        pick_route,
        "development",
        build_caps,
        decision_stage=STAGE_BUILD,
        mode="write",
        operating_harness=operating,
        complexity=complexity,
        **common,
    )

    def not_before_for(kind: str) -> datetime:
        routing_stage = "development" if kind == STAGE_BUILD else kind
        caps = build_caps if routing_stage == "development" else read_caps
        return earliest(
            routing_stage,
            list(caps),
            config,
            now=now,
            mode="write" if routing_stage == "development" else "read",
            operating_harness=operating if routing_stage == "development" else None,
        )

    return decide(planning=planning, build=build, now=now, not_before_for=not_before_for)


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
