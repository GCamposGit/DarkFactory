"""Independent desired-behavior tests; red until HF-04 is remediated.

Run explicitly, outside the normal suite. All credentials/data are synthetic.
Fixtures cross the public model_validate boundary; no model_copy bypass.
These counterexamples share a deliberately untrusted seed. Remediation must
first establish a trusted positive control for each single-dimension negative;
a new general authority rejection can otherwise mask a remaining binding bug.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.workflow.contracts import WorkflowHandoff, WorkflowState
from core.workflow.readiness import ReadinessGate


def seed() -> dict:
    return {
        "ticket_id": "review-ticket", "parent_id": "review", "objective": "Synthetic gate review",
        "origin": "code-review", "plan_version": "1", "planner_id": "unverified-caller",
        "planner_tier": "high", "approval_reference": "not-registered", "baseline_sha": "a" * 40,
        "grill": {"demand_id": "review-ticket", "intent_summary": "Synthetic example", "ready_for_spec": True,
                  "readiness_justification": "Known scope", "example_criteria": ["No unjustified delivery"]},
        "environment": {"environment_ref": "target", "ticket_id": "review-ticket", "kind": "target_environment",
                        "system": "Linux", "architecture": "x86_64", "network_policy": "restricted",
                        "worker_identity": {"subject": "worker-target", "role": "worker"}},
        "state": "independent_review", "successor_event": "complete", "retry_policy": "bounded",
        "resume_strategy": "reconcile", "rollback_plan": "synthetic workspace only",
        "required_evidence": [{"evidence_id": "probe", "description": "Target proof", "required_for": "operationally_verified"}],
        "environment_evidence": [{"evidence_id": "probe", "requirement": "Target proof", "environment_ref": "target",
                                  "identity": {"subject": "worker-target", "role": "worker"}, "origin": "cloud",
                                  "build_digest": "sha256:" + "a" * 64, "config_version": "1", "test_name": "target_probe",
                                  "expected": "passed", "observed": "passed", "result": "passed", "freshness": "current",
                                  "observed_at": "2026-09-09T00:00:00Z", "evidence_ref": "artifact://synthetic/not-registered"}],
    }


def test_delivery_rejects_empty_evidence_policy() -> None:
    payload = seed()
    payload["required_evidence"] = []
    payload["environment_evidence"] = []
    handoff = WorkflowHandoff.model_validate(payload)
    assert not ReadinessGate().evaluate(handoff, target_state=WorkflowState.DELIVERED).eligible


def test_delivery_requires_trusted_planner_and_review_receipt() -> None:
    payload = seed()
    payload["planner_tier"] = "economy"
    handoff = WorkflowHandoff.model_validate(payload)
    assert not ReadinessGate().evaluate(handoff, target_state=WorkflowState.DELIVERED).eligible


@pytest.mark.parametrize("field,value", [
    ("observed_at", "2000-01-01T00:00:00Z"),
    ("identity", {"subject": "different-worker", "role": "not-the-target"}),
    ("build_digest", "sha256:" + "b" * 64),
])
def test_delivery_rejects_stale_or_unbound_evidence(field: str, value: object) -> None:
    payload = seed()
    payload["environment_evidence"][0][field] = value
    handoff = WorkflowHandoff.model_validate(payload)
    assert not ReadinessGate().evaluate(handoff, target_state=WorkflowState.DELIVERED).eligible


def test_cancelled_job_cannot_be_eligible_to_implement() -> None:
    payload = seed()
    payload["state"] = "cancelled"
    handoff = WorkflowHandoff.model_validate(payload)
    assert not ReadinessGate().evaluate(handoff, target_state=WorkflowState.IMPLEMENTING_ECONOMY).eligible


def test_future_operational_evidence_does_not_block_current_plan() -> None:
    payload = seed()
    payload["state"] = "ready_for_handoff"
    payload["environment_evidence"] = []
    handoff = WorkflowHandoff.model_validate(payload)
    report = ReadinessGate().evaluate(handoff, target_state=WorkflowState.READY_FOR_HANDOFF)
    assert "probe" not in report.missing_evidence, "Operational proof belongs to a later stage"


def test_unanswered_grill_decision_cannot_be_ready() -> None:
    payload = seed()
    payload["grill"]["decisions"] = [{"decision_id": "unanswered", "question": "Material owner intent?",
        "alternatives": [{"alternative_id": "a", "label": "A", "consequence": "Cost A"},
                         {"alternative_id": "b", "label": "B", "consequence": "Cost B"}],
        "selected_alternative_id": None, "response": None, "decision_source": "pending-owner"}]
    try:
        handoff = WorkflowHandoff.model_validate(payload)
    except ValidationError:
        return
    assert not ReadinessGate().evaluate(handoff).eligible


def test_resolved_manual_dependency_requires_its_own_probe() -> None:
    payload = seed()
    payload["manual_dependencies"] = [{"dependency_id": "credentials", "ticket_ids": ["review-ticket"],
        "status": "resolved", "reason": "Owner-only login", "alternatives_attempted": [{
            "alternative_id": "alternative", "description": "Different identity", "tested": True,
            "equivalent": False, "reason_unusable": "Wrong identity"}],
        "configuration_location": "approved secret manager", "steps": [{"number": 1, "instruction": "Sign in",
            "expected_result": "Probe must pass"}], "final_probe": "credential_probe",
        "resume_criteria": "credential_probe passed on target", "help_route": "local-runbook",
        "created_at": "2026-09-09T00:00:00Z", "resolved_at": "2026-09-09T00:01:00Z"}]
    handoff = WorkflowHandoff.model_validate(payload)
    assert not ReadinessGate().evaluate(handoff, target_state=WorkflowState.DELIVERED).eligible


@pytest.mark.parametrize("location", ["endpoint", "service"])
def test_declared_secret_free_manifest_does_not_serialize_synthetic_secret(location: str) -> None:
    payload = seed()
    sentinel = "SYNTHETIC_REVIEW_VALUE_NOT_A_CREDENTIAL"
    if location == "endpoint":
        payload["environment"]["endpoints"] = [{"endpoint_id": "health", "url": "https://example.invalid/?token=" + sentinel}]
    else:
        payload["environment"]["services"] = ["password=" + sentinel]
    try:
        handoff = WorkflowHandoff.model_validate(payload)
    except ValidationError:
        return
    assert sentinel not in handoff.model_dump_json()


def test_baseline_payload_roundtrips() -> None:
    """Control: serialization only; it does not certify trusted operation."""
    handoff = WorkflowHandoff.model_validate(copy.deepcopy(seed()))
    assert WorkflowHandoff.model_validate_json(handoff.model_dump_json()) == handoff
