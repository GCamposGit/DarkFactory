"""
Policy binding service connecting workflow evidence and grants to effective policy (HF-26-02).

Constructs trusted PolicyContext from explicit grants and verified evidence,
and binds decisions to DeliveryPolicy without allowing global risk bypass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from core.workflow.effective_policy import (
    AcceptanceProof,
    Operation,
    PolicyContext,
    PolicyGrant,
)


def _to_decimal(val: Any) -> Decimal:
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val))
    except Exception as exc:
        raise ValueError(f"Cannot convert {val!r} to Decimal") from exc


def build_policy_context(
    project_id: str,
    operation: Operation | str,
    trusted_grants: Sequence[PolicyGrant] | Iterable[PolicyGrant],
    evidence: Any = None,
    *,
    policy_version: str = "v1",
    now: datetime | None = None,
    **kwargs: Any,
) -> PolicyContext:
    """
    Construct a validated PolicyContext from trusted grants and verified evidence.

    Does not accept arbitrary client-forged trusted IDs; all trusted IDs are
    strictly bound to the supplied trusted_grants.
    """
    if isinstance(operation, str):
        operation = Operation(operation)

    grants_tuple = tuple(trusted_grants)
    for g in grants_tuple:
        if not isinstance(g, PolicyGrant):
            raise TypeError(f"Expected PolicyGrant instance, got {type(g).__name__}")

    trusted_ids = tuple(g.grant_id for g in grants_tuple)

    # Defaults
    paused = False
    cancelled = False
    commercial_paid = False
    failed_gate_refs: tuple[str, ...] = ()
    remaining_budget_usd = Decimal("0")
    acceptance: AcceptanceProof | None = None
    trusted_receipt_refs: tuple[str, ...] = ()
    explicit_denials: tuple[Operation, ...] = ()
    resolved_now = now or datetime.now(timezone.utc)
    resolved_version = policy_version

    # Extract from evidence if mapping
    if isinstance(evidence, Mapping):
        paused = bool(evidence.get("paused", paused))
        cancelled = bool(evidence.get("cancelled", cancelled))
        commercial_paid = bool(evidence.get("commercial_paid", commercial_paid))
        if "policy_version" in evidence:
            resolved_version = str(evidence["policy_version"])
        if "now" in evidence and isinstance(evidence["now"], datetime):
            resolved_now = evidence["now"]
        if "failed_gate_refs" in evidence:
            failed_gate_refs = tuple(str(x) for x in evidence["failed_gate_refs"])
        if "remaining_budget_usd" in evidence:
            remaining_budget_usd = _to_decimal(evidence["remaining_budget_usd"])
        if "acceptance" in evidence:
            acc_val = evidence["acceptance"]
            if isinstance(acc_val, AcceptanceProof) or acc_val is None:
                acceptance = acc_val
            elif isinstance(acc_val, dict):
                acceptance = AcceptanceProof(**acc_val)
        if "trusted_receipt_refs" in evidence:
            trusted_receipt_refs = tuple(str(x) for x in evidence["trusted_receipt_refs"])
        if "explicit_denials" in evidence:
            explicit_denials = tuple(
                Operation(d) if isinstance(d, str) else d for d in evidence["explicit_denials"]
            )

    # Extract from evidence if object
    elif evidence is not None and not isinstance(evidence, (str, bytes)):
        paused = bool(getattr(evidence, "paused", paused))
        cancelled = bool(getattr(evidence, "cancelled", cancelled))
        commercial_paid = bool(getattr(evidence, "commercial_paid", commercial_paid))
        if hasattr(evidence, "policy_version"):
            resolved_version = str(getattr(evidence, "policy_version"))
        if hasattr(evidence, "now") and isinstance(getattr(evidence, "now"), datetime):
            resolved_now = getattr(evidence, "now")
        if hasattr(evidence, "failed_gate_refs"):
            failed_gate_refs = tuple(str(x) for x in getattr(evidence, "failed_gate_refs"))
        if hasattr(evidence, "remaining_budget_usd"):
            remaining_budget_usd = _to_decimal(getattr(evidence, "remaining_budget_usd"))
        if hasattr(evidence, "acceptance"):
            acc_val = getattr(evidence, "acceptance")
            if isinstance(acc_val, AcceptanceProof) or acc_val is None:
                acceptance = acc_val
            elif isinstance(acc_val, dict):
                acceptance = AcceptanceProof(**acc_val)
        if hasattr(evidence, "trusted_receipt_refs"):
            trusted_receipt_refs = tuple(str(x) for x in getattr(evidence, "trusted_receipt_refs"))
        if hasattr(evidence, "explicit_denials"):
            explicit_denials = tuple(
                Operation(d) if isinstance(d, str) else d for d in getattr(evidence, "explicit_denials")
            )

    # Overrides from explicit kwargs
    if "paused" in kwargs:
        paused = bool(kwargs["paused"])
    if "cancelled" in kwargs:
        cancelled = bool(kwargs["cancelled"])
    if "commercial_paid" in kwargs:
        commercial_paid = bool(kwargs["commercial_paid"])
    if "failed_gate_refs" in kwargs:
        failed_gate_refs = tuple(str(x) for x in kwargs["failed_gate_refs"])
    if "remaining_budget_usd" in kwargs:
        remaining_budget_usd = _to_decimal(kwargs["remaining_budget_usd"])
    if "acceptance" in kwargs:
        acceptance = kwargs["acceptance"]
    if "trusted_receipt_refs" in kwargs:
        trusted_receipt_refs = tuple(str(x) for x in kwargs["trusted_receipt_refs"])
    if "explicit_denials" in kwargs:
        explicit_denials = tuple(
            Operation(d) if isinstance(d, str) else d for d in kwargs["explicit_denials"]
        )

    # Handle untrusted grants if provided in kwargs
    all_grants = list(grants_tuple)
    if "untrusted_grants" in kwargs:
        for ug in kwargs["untrusted_grants"]:
            if not isinstance(ug, PolicyGrant):
                raise TypeError(f"Expected PolicyGrant in untrusted_grants, got {type(ug).__name__}")
            all_grants.append(ug)

    return PolicyContext(
        project_id=project_id,
        policy_version=resolved_version,
        now=resolved_now,
        paused=paused,
        cancelled=cancelled,
        commercial_paid=commercial_paid,
        grants=tuple(all_grants),
        trusted_grant_ids=trusted_ids,
        explicit_denials=explicit_denials,
        failed_gate_refs=failed_gate_refs,
        remaining_budget_usd=remaining_budget_usd,
        acceptance=acceptance,
        trusted_receipt_refs=trusted_receipt_refs,
    )
