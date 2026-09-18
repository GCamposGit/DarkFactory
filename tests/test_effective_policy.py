"""
Deterministic tests for HF-26-01 pure effective policy resolver.

Covers:
- Strict Pydantic v2 validation (frozen, extra=forbid, non-empty strings, UTC tz, finite non-negative Decimal)
- 8 deterministic precedence rules
- Denial over grant, pause over budget, etc.
- Commercial staging vs production acceptance
- Lexicographical grant selection
- Zero I/O and zero mutation guarantees
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
import pytest
from pydantic import ValidationError

from core.workflow.effective_policy import (
    AcceptanceProof,
    Operation,
    PolicyContext,
    PolicyDecision,
    PolicyGrant,
    PolicyRequest,
    resolve_effective_policy,
)


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def future_utc() -> datetime:
    return datetime(2026, 9, 18, 13, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def past_utc() -> datetime:
    return datetime(2026, 9, 18, 11, 0, 0, tzinfo=timezone.utc)


def test_models_reject_extra_fields(now_utc: datetime) -> None:
    with pytest.raises(ValidationError):
        PolicyRequest(
            project_id="darkfac",
            operation=Operation.PLAN,
            scope_ref="ticket/HF-26-01",
            candidate_digest="sha256-abc",
            policy_version="v1",
            estimated_cost_usd=Decimal("0"),
            extra_field="illegal",  # type: ignore[call-arg]
        )


def test_models_reject_naive_datetime() -> None:
    naive_dt = datetime(2026, 9, 18, 12, 0, 0)
    with pytest.raises(ValidationError):
        PolicyGrant(
            grant_id="g1",
            project_id="darkfac",
            operation=Operation.PLAN,
            scope_ref="ticket/HF-26-01",
            policy_version="v1",
            expires_at=naive_dt,
        )


def test_models_reject_negative_or_nan_decimal(now_utc: datetime, future_utc: datetime) -> None:
    with pytest.raises(ValidationError):
        PolicyRequest(
            project_id="darkfac",
            operation=Operation.PLAN,
            scope_ref="ticket/HF-26-01",
            candidate_digest="sha256-abc",
            policy_version="v1",
            estimated_cost_usd=Decimal("-1.00"),
        )
    with pytest.raises(ValidationError):
        PolicyRequest(
            project_id="darkfac",
            operation=Operation.PLAN,
            scope_ref="ticket/HF-26-01",
            candidate_digest="sha256-abc",
            policy_version="v1",
            estimated_cost_usd=Decimal("NaN"),
        )


def test_models_reject_empty_strings(now_utc: datetime, future_utc: datetime) -> None:
    with pytest.raises(ValidationError):
        PolicyRequest(
            project_id="   ",
            operation=Operation.PLAN,
            scope_ref="ticket/HF-26-01",
            candidate_digest="sha256-abc",
            policy_version="v1",
            estimated_cost_usd=Decimal("0"),
        )


def test_context_rejects_duplicate_grants_or_unregistered_trusted(now_utc: datetime, future_utc: datetime) -> None:
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    g1_dup = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.IMPLEMENT,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    with pytest.raises(ValidationError, match="duplicate grant_id"):
        PolicyContext(
            project_id="darkfac",
            policy_version="v1",
            now=now_utc,
            paused=False,
            cancelled=False,
            commercial_paid=False,
            grants=(g1, g1_dup),
            trusted_grant_ids=("g1",),
            remaining_budget_usd=Decimal("10.00"),
        )

    with pytest.raises(ValidationError, match="does not exist in grants"):
        PolicyContext(
            project_id="darkfac",
            policy_version="v1",
            now=now_utc,
            paused=False,
            cancelled=False,
            commercial_paid=False,
            grants=(g1,),
            trusted_grant_ids=("g1", "g2_unregistered"),
            remaining_budget_usd=Decimal("10.00"),
        )


# Rule 1: Project or policy_version mismatch
def test_rule_1_context_mismatch(now_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    ctx = PolicyContext(
        project_id="jarvis",  # Mismatch
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        remaining_budget_usd=Decimal("10.00"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "blocked_policy"
    assert decision.reason_code == "context_mismatch"
    assert decision.matched_grant_id is None
    assert decision.blocking_refs == ()


# Rule 2: Cancelled or paused
def test_rule_2_cancelled_and_paused_precedence(now_utc: datetime, future_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    # Cancelled wins over paused and grants
    ctx_cancel = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=True,
        cancelled=True,
        commercial_paid=False,
        grants=(g1,),
        trusted_grant_ids=("g1",),
        remaining_budget_usd=Decimal("10.00"),
    )
    d_cancel = resolve_effective_policy(req, ctx_cancel)
    assert d_cancel.status == "cancelled"
    assert d_cancel.reason_code == "explicit_cancel"

    # Paused wins over grant
    ctx_pause = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=True,
        cancelled=False,
        commercial_paid=False,
        grants=(g1,),
        trusted_grant_ids=("g1",),
        remaining_budget_usd=Decimal("10.00"),
    )
    d_pause = resolve_effective_policy(req, ctx_pause)
    assert d_pause.status == "paused"
    assert d_pause.reason_code == "explicit_pause"


# Rule 3: Explicit denial wins over grant
def test_rule_3_explicit_deny_over_grant(now_utc: datetime, future_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.IMPLEMENT,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.IMPLEMENT,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        grants=(g1,),
        trusted_grant_ids=("g1",),
        explicit_denials=(Operation.IMPLEMENT,),
        remaining_budget_usd=Decimal("10.00"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "blocked_policy"
    assert decision.reason_code == "explicit_deny"
    assert decision.matched_grant_id is None


# Rule 4: Failed gates preserve sorted refs
def test_rule_4_failed_gates(now_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        failed_gate_refs=("gate_b", "gate_a"),
        remaining_budget_usd=Decimal("10.00"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "waiting_dependency"
    assert decision.reason_code == "gate_failed"
    assert decision.blocking_refs == ("gate_a", "gate_b")


# Rule 5: Budget ceiling exceeded
def test_rule_5_ceiling_exceeded(now_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.SPEND,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("15.00"),
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        remaining_budget_usd=Decimal("10.00"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "waiting_budget"
    assert decision.reason_code == "ceiling_exceeded"


# Rule 6: Commercial paid staging allowed; production requires valid acceptance
def test_rule_6_commercial_paid_staging_vs_production(now_utc: datetime, future_utc: datetime) -> None:
    g_stage = PolicyGrant(
        grant_id="g_stage",
        project_id="darkfac",
        operation=Operation.DEPLOY_STAGING,
        scope_ref="deploy/staging",
        policy_version="v1",
        expires_at=future_utc,
    )
    g_prod = PolicyGrant(
        grant_id="g_prod",
        project_id="darkfac",
        operation=Operation.DEPLOY_PRODUCTION,
        scope_ref="deploy/production",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=True,
        grants=(g_stage, g_prod),
        trusted_grant_ids=("g_stage", "g_prod"),
        remaining_budget_usd=Decimal("100.00"),
        acceptance=None,
    )

    # Staging allowed without client acceptance
    req_stage = PolicyRequest(
        project_id="darkfac",
        operation=Operation.DEPLOY_STAGING,
        scope_ref="deploy/staging",
        candidate_digest="digest-123",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    d_stage = resolve_effective_policy(req_stage, ctx)
    assert d_stage.status == "allowed"
    assert d_stage.matched_grant_id == "g_stage"

    # Production blocked without client acceptance
    req_prod = PolicyRequest(
        project_id="darkfac",
        operation=Operation.DEPLOY_PRODUCTION,
        scope_ref="deploy/production",
        candidate_digest="digest-123",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    d_prod = resolve_effective_policy(req_prod, ctx)
    assert d_prod.status == "waiting_dependency"
    assert d_prod.reason_code == "client_acceptance_required"

    # Production allowed when valid acceptance matches digest and trusted receipt
    valid_acc = AcceptanceProof(
        project_id="darkfac",
        candidate_digest="digest-123",
        receipt_ref="receipt-001",
        expires_at=future_utc,
    )
    ctx_with_acc = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=True,
        grants=(g_stage, g_prod),
        trusted_grant_ids=("g_stage", "g_prod"),
        remaining_budget_usd=Decimal("100.00"),
        acceptance=valid_acc,
        trusted_receipt_refs=("receipt-001",),
    )
    d_prod_allowed = resolve_effective_policy(req_prod, ctx_with_acc)
    assert d_prod_allowed.status == "allowed"
    assert d_prod_allowed.matched_grant_id == "g_prod"

    # Production blocked if digest in acceptance doesn't match candidate_digest
    mismatched_acc = AcceptanceProof(
        project_id="darkfac",
        candidate_digest="digest-OLD",
        receipt_ref="receipt-001",
        expires_at=future_utc,
    )
    ctx_mismatched = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=True,
        grants=(g_stage, g_prod),
        trusted_grant_ids=("g_stage", "g_prod"),
        remaining_budget_usd=Decimal("100.00"),
        acceptance=mismatched_acc,
        trusted_receipt_refs=("receipt-001",),
    )
    d_prod_blocked = resolve_effective_policy(req_prod, ctx_mismatched)
    assert d_prod_blocked.status == "waiting_dependency"
    assert d_prod_blocked.reason_code == "client_acceptance_required"


# Rule 7 & 8: Grant selection and lowest lexicographical tie-break
def test_rule_7_and_8_lexicographical_grant_selection(now_utc: datetime, future_utc: datetime) -> None:
    g_z = PolicyGrant(
        grant_id="grant_z",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    g_a = PolicyGrant(
        grant_id="grant_a",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        grants=(g_z, g_a),
        trusted_grant_ids=("grant_z", "grant_a"),
        remaining_budget_usd=Decimal("10.00"),
    )
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "allowed"
    assert decision.reason_code == "authorized"
    assert decision.matched_grant_id == "grant_a"  # grant_a < grant_z


def test_expired_grant_rejected(now_utc: datetime, past_utc: datetime) -> None:
    g_exp = PolicyGrant(
        grant_id="grant_exp",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=past_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        grants=(g_exp,),
        trusted_grant_ids=("grant_exp",),
        remaining_budget_usd=Decimal("10.00"),
    )
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "blocked_policy"
    assert decision.reason_code == "authorization_missing"


def test_scope_mismatch_rejected(now_utc: datetime, future_utc: datetime) -> None:
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        grants=(g1,),
        trusted_grant_ids=("g1",),
        remaining_budget_usd=Decimal("10.00"),
    )
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-02",  # Different scope!
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    decision = resolve_effective_policy(req, ctx)
    assert decision.status == "blocked_policy"
    assert decision.reason_code == "authorization_missing"


# Zero IO and purity verification
def test_pure_function_has_no_io_or_side_effects(now_utc: datetime, future_utc: datetime) -> None:
    req = PolicyRequest(
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        candidate_digest="sha256-abc",
        policy_version="v1",
        estimated_cost_usd=Decimal("0"),
    )
    g1 = PolicyGrant(
        grant_id="g1",
        project_id="darkfac",
        operation=Operation.PLAN,
        scope_ref="ticket/HF-26-01",
        policy_version="v1",
        expires_at=future_utc,
    )
    ctx = PolicyContext(
        project_id="darkfac",
        policy_version="v1",
        now=now_utc,
        paused=False,
        cancelled=False,
        commercial_paid=False,
        grants=(g1,),
        trusted_grant_ids=("g1",),
        remaining_budget_usd=Decimal("10.00"),
    )

    with patch("builtins.open", side_effect=RuntimeError("I/O attempted")), \
         patch("subprocess.run", side_effect=RuntimeError("Subprocess attempted")), \
         patch("socket.socket", side_effect=RuntimeError("Network attempted")):
        decision = resolve_effective_policy(req, ctx)
        assert decision.status == "allowed"
        assert decision.matched_grant_id == "g1"
