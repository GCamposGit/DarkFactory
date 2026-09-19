"""
Tests for HF-26-02 policy binding and DeliveryPolicy integration.

Verifies:
- build_policy_context constructs valid PolicyContext without client forgery
- DeliveryPolicy evaluates technical tickets in risk classes C and D autonomously when authorized
- Red/stale checks always block delivery even if policy is allowed
- No global bypass allowed for risk classes C and D
- Legacy behavior preserved when no policy context/decision is supplied
- Commercial paid tickets require client acceptance proof
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from core.integrations.github import GitHubCheck, PullRequestSnapshot
from core.orchestrator.delivery import (
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRisk,
    DeliveryStatus,
)
from core.workflow.effective_policy import (
    AcceptanceProof,
    Operation,
    PolicyGrant,
    resolve_effective_policy,
)
from core.workflow.policy_binding import build_policy_context


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def future_utc() -> datetime:
    return datetime(2026, 9, 18, 13, 0, 0, tzinfo=timezone.utc)


def make_delivery_request(
    task_id: str = "HF-26-02",
    candidate_sha: str = "b" * 40,
    risk_class: DeliveryRisk = DeliveryRisk.C,
) -> DeliveryRequest:
    return DeliveryRequest(
        task_id=task_id,
        repository="GCamposGit/DarkFactory",
        pull_request_number=26,
        base_sha="a" * 40,
        candidate_sha=candidate_sha,
        risk_class=risk_class,
        required_checks=("pr-validation",),
        idempotency_key="idemp-hf26-02",
    )


def make_snapshot(
    candidate_sha: str = "b" * 40,
    check_passed: bool = True,
    mergeable: bool = True,
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repository="GCamposGit/DarkFactory",
        number=26,
        base_sha="a" * 40,
        head_sha=candidate_sha,
        state="open",
        draft=False,
        mergeable=mergeable,
        checks=(
            GitHubCheck(
                name="pr-validation",
                head_sha=candidate_sha,
                status="completed",
                conclusion="success" if check_passed else "failure",
            ),
        ),
    )


# 1. build_policy_context tests
def test_build_policy_context_defaults(now_utc: datetime, future_utc: datetime) -> None:
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.MERGE,
        scope_ref="HF-26-02",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = build_policy_context(
        project_id="darkfac",
        operation=Operation.MERGE,
        trusted_grants=[g1],
        now=now_utc,
    )
    assert ctx.project_id == "darkfac"
    assert ctx.policy_version == "v1"
    assert ctx.now == now_utc
    assert ctx.trusted_grant_ids == ("g1",)
    assert ctx.grants == (g1,)
    assert not ctx.paused
    assert not ctx.cancelled
    assert not ctx.commercial_paid


def test_build_policy_context_extracts_from_evidence_dict(now_utc: datetime, future_utc: datetime) -> None:
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.MERGE,
        scope_ref="HF-26-02",
        policy_version="v1",
        expires_at=future_utc,
    )
    evidence = {
        "paused": True,
        "failed_gate_refs": ["gate_x"],
        "remaining_budget_usd": "50.00",
        "commercial_paid": True,
    }
    ctx = build_policy_context(
        project_id="darkfac",
        operation=Operation.MERGE,
        trusted_grants=[g1],
        evidence=evidence,
        now=now_utc,
    )
    assert ctx.paused is True
    assert ctx.failed_gate_refs == ("gate_x",)
    assert ctx.remaining_budget_usd == Decimal("50.00")
    assert ctx.commercial_paid is True


# 2. No global bypass for risk classes C and D
def test_delivery_policy_rejects_global_c_and_d_bypass() -> None:
    with pytest.raises(ValueError, match="Global bypass for risk classes C is forbidden"):
        DeliveryPolicy(autonomous_risk_classes={DeliveryRisk.A, DeliveryRisk.C})

    with pytest.raises(ValueError, match="Global bypass for risk classes D is forbidden"):
        DeliveryPolicy(autonomous_risk_classes={DeliveryRisk.D})


# 3. Legacy mode preserves manual review for classes C and D
def test_delivery_policy_preserves_legacy_manual_review() -> None:
    policy = DeliveryPolicy()
    req = make_delivery_request(risk_class=DeliveryRisk.C)
    snap = make_snapshot()
    decision = policy.evaluate(req, snap)
    assert decision.status == DeliveryStatus.MANUAL_REVIEW
    assert "requires manual review" in decision.reason
    assert not decision.eligible


# 4. Authorized technical ticket does not require human review
def test_authorized_technical_ticket_in_class_c_is_autonomous(now_utc: datetime, future_utc: datetime) -> None:
    policy = DeliveryPolicy()
    req = make_delivery_request(risk_class=DeliveryRisk.C)
    snap = make_snapshot()

    g_merge = PolicyGrant(
        grant_id="grant_merge_hf26",
        project_id="darkfac",
        operation=Operation.MERGE,
        scope_ref="HF-26-02",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = build_policy_context(
        project_id="darkfac",
        operation=Operation.MERGE,
        trusted_grants=[g_merge],
        now=now_utc,
    )

    decision = policy.evaluate(req, snap, policy_context=ctx)
    assert decision.status == DeliveryStatus.ELIGIBLE
    assert decision.eligible
    assert "authorized by policy grant grant_merge_hf26" in decision.reason
    assert decision.policy_decision is not None
    assert decision.policy_decision.status == "allowed"


# 5. Red checks in snapshot ALWAYS block, even if policy grant is allowed
def test_red_checks_always_block_delivery(now_utc: datetime, future_utc: datetime) -> None:
    policy = DeliveryPolicy()
    req = make_delivery_request(risk_class=DeliveryRisk.B)
    snap_failed = make_snapshot(check_passed=False)

    g_merge = PolicyGrant(
        grant_id="grant_merge",
        project_id="darkfac",
        operation=Operation.MERGE,
        scope_ref="HF-26-02",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = build_policy_context(
        project_id="darkfac",
        operation=Operation.MERGE,
        trusted_grants=[g_merge],
        now=now_utc,
    )

    decision = policy.evaluate(req, snap_failed, policy_context=ctx)
    assert decision.status == DeliveryStatus.BLOCKED
    assert not decision.eligible
    assert "failed_checks=pr-validation" in decision.reason


# 6. Commercial paid without acceptance blocks delivery
def test_commercial_paid_without_acceptance_blocks_delivery(now_utc: datetime, future_utc: datetime) -> None:
    policy = DeliveryPolicy()
    req = make_delivery_request(risk_class=DeliveryRisk.B)
    snap = make_snapshot()

    # If policy decision came from a blocked evaluation (e.g. client acceptance required on deploy/production)
    g_prod = PolicyGrant(
        grant_id="grant_prod",
        project_id="darkfac",
        operation=Operation.DEPLOY_PRODUCTION,
        scope_ref="HF-26-02",
        policy_version="v1",
        expires_at=future_utc,
    )
    # Context with commercial_paid and no acceptance
    ctx = build_policy_context(
        project_id="darkfac",
        operation=Operation.DEPLOY_PRODUCTION,
        trusted_grants=[g_prod],
        evidence={"commercial_paid": True},
        now=now_utc,
    )
    from core.workflow.effective_policy import PolicyRequest as EPRequest
    ep_req = EPRequest(
        project_id="darkfac",
        operation=Operation.DEPLOY_PRODUCTION,
        scope_ref="HF-26-02",
        candidate_digest=req.candidate_sha,
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    ep_decision = resolve_effective_policy(ep_req, ctx)
    assert ep_decision.status == "waiting_dependency"
    assert ep_decision.reason_code == "client_acceptance_required"

    decision = policy.evaluate(req, snap, policy_decision=ep_decision)
    assert decision.status == DeliveryStatus.BLOCKED
    assert not decision.eligible
    assert "client_acceptance_required" in decision.reason
