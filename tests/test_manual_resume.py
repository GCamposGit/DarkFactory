"""Tests for manual dependency resolution, probe execution, and workflow resumption.

Governed by HF-08-05 and Universal Engineering Standards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import pytest
from pydantic import ValidationError

from core.workflow.contracts import (
    Direction,
    EnvironmentManifest,
    EnvironmentPort,
    ManualDependency,
    ManualDependencyStatus,
    ReadinessState,
    SecretReference,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import ReadinessGate, VerificationContext
from core.workflow.verification import EvidenceReceipt, EvidenceResult, ValidationMode
from core.workflow.manual_resolution import (
    ProbeJob,
    ProbeJobStatus,
    ResolutionSubmission,
    create_resolution_receipt,
    execute_manual_probe,
    is_ticket_unblocked_by_resolution,
    submit_resolution,
)
from core.workflow.reconciliation import (
    reconcile_environment_manifest,
    reconcile_portfolio_state,
)
from tests.test_workflow_contracts import (
    make_environment,
    make_handoff,
    make_simulation_context,
)


def _make_dependency(
    status: ManualDependencyStatus = ManualDependencyStatus.WAITING,
    *,
    dependency_id: str = "dep-hf08-01",
    ticket_ids: list[str] | None = None,
    final_probe: str = "python -m core.workflow.probe --check dep-hf08-01",
    receipt_ref: str | None = None,
    created_at: datetime | None = None,
    resolved_at: datetime | None = None,
    blocked_stages: list[WorkflowState] | None = None,
    help_route: str = "help://runbook/manual-tokens",
    secret_ref: SecretReference | None = None,
) -> ManualDependency:
    now = created_at or datetime.now(UTC)
    return ManualDependency(
        dependency_id=dependency_id,
        ticket_ids=ticket_ids or ["HF-08-05"],
        status=status,
        reason="Manual token provisioning required by human operator",
        alternatives_attempted=[
            {
                "alternative_id": "alt-auto-token",
                "description": "Attempt automatic OAuth token fetch",
                "tested": True,
                "equivalent": False,
                "reason_unusable": "Requires human biometric 2FA",
            }
        ],
        configuration_location="secret://vault/tokens/production",
        steps=[{"number": 1, "instruction": "Generate production token in portal", "expected_result": "Token generated"}],
        final_probe=final_probe,
        resume_criteria="Probe verifies target connectivity with provided credentials",
        help_route=help_route,
        secret_ref=secret_ref,
        created_at=now,
        resolved_at=resolved_at if resolved_at is not None else (now if status is ManualDependencyStatus.RESOLVED else None),
        resolution_receipt_ref=(
            receipt_ref
            if receipt_ref is not None
            else (f"receipt://manual-resolution/{dependency_id}" if status is ManualDependencyStatus.RESOLVED else None)
        ),
        blocked_stages=blocked_stages or [],
    )


class MockControlStore:
    """Mock store supporting outbox events and probe recording."""

    def __init__(self) -> None:
        self.outbox: list[Any] = []
        self.recorded_probes: dict[str, ProbeJob] = {}

    def emit_outbox(self, event: Any, now: datetime) -> Any:
        self.outbox.append(event)
        return event

    def record_probe(self, probe_job: ProbeJob) -> None:
        self.recorded_probes[probe_job.probe_id] = probe_job


def test_submit_resolution_creates_probe_job() -> None:
    """Verify submit_resolution constructs a valid pending ProbeJob."""
    store = MockControlStore()
    job = submit_resolution(
        dependency_id="dep-hf08-01",
        response_ref="ref://responses/resp-101",
        version=1,
        operator_identity="operator-alice",
        help_route="help://runbook/manual-tokens",
        payload={"secret_ref": SecretReference(ref_id="sec-1", provider="vault", locator="app/key")},
        store=store,
    )

    assert isinstance(job, ProbeJob)
    assert job.dependency_id == "dep-hf08-01"
    assert job.status is ProbeJobStatus.PENDING
    assert job.created_at is not None
    assert job.created_at.tzinfo is not None
    assert job.receipt_ref is None
    assert "resp-101" in job.response_ref


def test_probe_failure_keeps_waiting_status_and_does_not_unblock() -> None:
    """Verify probe failure leaves ManualDependency WAITING and preserves WAITING_HUMAN in readiness."""
    dep = _make_dependency(status=ManualDependencyStatus.WAITING, ticket_ids=["HF-04-01"])
    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/failed-resp",
        version=1,
        operator_identity="operator-alice",
        help_route=dep.help_route,
        payload={"result": "failed"},
    )

    # Execute probe with failure signaled
    res_dep, res_job = execute_manual_probe(job, dep, probe_pass=False)

    assert res_job.status is ProbeJobStatus.FAILED
    assert res_dep.status is ManualDependencyStatus.WAITING
    assert res_dep.resolved_at is None
    assert res_dep.resolution_receipt_ref is None

    # Readiness remains WAITING_HUMAN
    gate = ReadinessGate()
    ctx = make_simulation_context()
    handoff = make_handoff(dependencies=[res_dep])
    report = gate.evaluate(handoff, context=ctx)
    assert report.eligible is False
    assert report.readiness is ReadinessState.WAITING_HUMAN
    assert dep.dependency_id in report.blocking_dependency_ids


def test_stale_submission_rejected_without_effect() -> None:
    """Verify submission with older version than dependency is rejected fail-closed without modifying dependency."""
    dep = _make_dependency()
    # Assume dependency is at version 2 (simulated via attribute or metadata)
    object.__setattr__(dep, "version", 2)

    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/stale",
        version=1,  # older version
        operator_identity="operator-alice",
        help_route=dep.help_route,
    )

    res_dep, res_job = execute_manual_probe(job, dep)
    assert res_job.status is ProbeJobStatus.REJECTED
    assert "stale" in (res_job.detail or "").lower()
    assert res_dep.status is ManualDependencyStatus.WAITING
    assert res_dep.resolved_at is None


def test_unauthorized_route_or_identity_rejected_fail_closed() -> None:
    """Verify divergent help_route or unauthorized identity is rejected fail-closed."""
    dep = _make_dependency(help_route="help://runbook/manual-tokens")

    # 1. Divergent help_route
    job_bad_route = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/resp-1",
        version=1,
        operator_identity="operator-alice",
        help_route="help://malicious/phishing-route",
    )
    res_dep, res_job = execute_manual_probe(job_bad_route, dep)
    assert res_job.status is ProbeJobStatus.REJECTED
    assert "unauthorized" in (res_job.detail or "").lower() or "divergent" in (res_job.detail or "").lower()
    assert res_dep.status is ManualDependencyStatus.WAITING

    # 2. Unauthorized operator identity
    job_bad_identity = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/resp-1",
        version=1,
        operator_identity="unauthorized-anonymous-user",
        help_route=dep.help_route,
    )
    res_dep2, res_job2 = execute_manual_probe(job_bad_identity, dep)
    assert res_job2.status is ProbeJobStatus.REJECTED
    assert res_dep2.status is ManualDependencyStatus.WAITING


def test_successful_probe_resolves_and_populates_receipt_and_timestamp() -> None:
    """Verify successful probe execution resolves dependency and populates receipts and timestamp."""
    store = MockControlStore()
    dep = _make_dependency()
    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/resp-success",
        version=1,
        operator_identity="operator-authorized",
        help_route=dep.help_route,
        store=store,
    )

    res_dep, res_job = execute_manual_probe(job, dep, store=store, probe_pass=True)

    assert res_job.status is ProbeJobStatus.SUCCEEDED
    assert res_dep.status is ManualDependencyStatus.RESOLVED
    assert res_dep.resolved_at is not None
    assert res_dep.resolved_at.tzinfo is not None
    assert res_dep.resolution_receipt_ref is not None
    assert res_dep.resolution_receipt_ref.startswith("receipt://manual-resolution/")
    assert res_job.receipt_ref == res_dep.resolution_receipt_ref

    # Outbox event was emitted
    assert len(store.outbox) == 1
    event = store.outbox[0]
    assert event.event_type == "dependency_resolved"
    assert event.aggregate_id == dep.dependency_id
    assert event.payload["receipt_ref"] == res_dep.resolution_receipt_ref


def test_selective_unblocking_only_declared_tickets_and_blocked_stages() -> None:
    """Verify resolved dependency only unblocks declared ticket_ids and blocked_stages; siblings unaffected."""
    dep = _make_dependency(
        ticket_ids=["HF-08-05"],
        blocked_stages=[WorkflowState.DELIVERED],
    )
    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/resp-1",
        version=1,
        operator_identity="operator-authorized",
        help_route=dep.help_route,
    )
    resolved_dep, _ = execute_manual_probe(job, dep, probe_pass=True)

    # 1. Ticket in ticket_ids and stage in blocked_stages is unblocked
    assert is_ticket_unblocked_by_resolution("HF-08-05", WorkflowState.DELIVERED, resolved_dep) is True

    # 2. Stage NOT in blocked_stages (e.g. INDEPENDENT_REVIEW) was never blocked by this dependency
    assert is_ticket_unblocked_by_resolution("HF-08-05", WorkflowState.INDEPENDENT_REVIEW, resolved_dep) is False

    # 3. Independent sibling ticket is not unblocked (not governed by this dependency)
    assert is_ticket_unblocked_by_resolution("SIBLING-TICKET-99", WorkflowState.DELIVERED, resolved_dep) is False

    # 4. Another project is completely isolated
    assert is_ticket_unblocked_by_resolution("OTHER-PROJ-01", WorkflowState.DELIVERED, resolved_dep) is False


def test_strict_secret_hygiene_sanitizes_or_rejects_cleartext_secrets() -> None:
    """Verify plaintext passwords/tokens are sanitized or rejected, while SecretReference is preserved."""
    # 1. Plaintext secret in response_ref is rejected
    with pytest.raises(ValidationError, match="secret"):
        ResolutionSubmission(
            dependency_id="dep-hf08-01",
            response_ref="password=supersecretpassword123",
            version=1,
            operator_identity="operator-alice",
            help_route="help://runbook",
        )

    # 2. Plaintext secrets in payload are strictly sanitized to [REDACTED]
    valid_sec_ref = SecretReference(ref_id="sec-1", provider="env", locator="VAR_KEY")
    submission = ResolutionSubmission(
        dependency_id="dep-hf08-01",
        response_ref="ref://safe/resp-200",
        version=1,
        operator_identity="operator-alice",
        help_route="help://runbook",
        payload={
            "api_key": "raw_token_xyz_12345",
            "password": "mypassword",
            "secret_ref": valid_sec_ref.model_dump(),
            "regular_info": "safe_data",
        },
    )
    sanitized = submission.payload
    assert sanitized is not None
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["password"] == "[REDACTED]"
    assert sanitized["regular_info"] == "safe_data"
    assert sanitized["secret_ref"]["ref_id"] == "sec-1"


def test_reconciliation_integration_with_manual_resolution() -> None:
    """Verify environment manifest and portfolio state reconciliation cycles integrate manual resolution."""
    # 1. Environment manifest reconciliation with resolved dependency
    initial_manifest = make_environment()
    resolved_sec = SecretReference(ref_id="sec-manual", provider="vault", locator="app/token")
    resolved_dep = _make_dependency(
        status=ManualDependencyStatus.RESOLVED,
        final_probe="python -m probe --check",
        secret_ref=resolved_sec,
    )

    reconciled_manifest, diff = reconcile_environment_manifest(
        initial_manifest,
        resolved_dependencies=[resolved_dep],
    )
    assert diff.has_changes is True
    assert any(s.ref_id == "sec-manual" for s in reconciled_manifest.secret_refs)
    assert any("sec-manual" in s.ref_id for s in diff.added_secret_refs)

    # 2. Portfolio state reconciliation with manual resolution
    store = MockControlStore()
    waiting_dep = _make_dependency(status=ManualDependencyStatus.WAITING)
    job = submit_resolution(
        dependency_id=waiting_dep.dependency_id,
        response_ref="ref://responses/reconcile-resp",
        version=1,
        operator_identity="operator-reconciler",
        help_route=waiting_dep.help_route,
        store=store,
    )

    class StoreWithPending(MockControlStore):
        def reconcile(self, now: datetime, cursor: str | None = None, limit: int = 100) -> Any:
            from core.workflow.control_contracts import ReconcilePage
            return ReconcilePage(
                cursor=None,
                visited_projects=["DarkFac"],
                repaired_keys=[],
                next_cursor=None,
                cycle_id="cycle-1",
            )

    rich_store = StoreWithPending()
    report = reconcile_portfolio_state(
        rich_store,
        manual_probes=[(job, waiting_dep)],
    )
    assert report.resolved_dependencies_count == 1
    assert waiting_dep.dependency_id in report.resolved_dependency_ids


def test_resolved_manual_dependency_unblocks_readiness_gate_with_receipt() -> None:
    """Verify resolved dependency with a valid receipt unblocks ReadinessGate evaluation."""
    from tests.test_workflow_contracts import NOW

    dep = _make_dependency(status=ManualDependencyStatus.WAITING, ticket_ids=["HF-04-01"], created_at=NOW)
    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/pass",
        version=1,
        operator_identity="operator-alice",
        help_route=dep.help_route,
    )
    job = job.model_copy(update={"created_at": NOW})
    resolved_dep, succeeded_job = execute_manual_probe(job, dep, probe_pass=True)
    resolved_dep = resolved_dep.model_copy(update={"resolved_at": NOW})

    # Create receipt from the probe job
    receipt = create_resolution_receipt(
        succeeded_job,
        resolved_dep,
        environment_ref="hf04-target-local",
        plan_digest="plan-sha-1",
        candidate_digest="sha256:" + "a" * 64,
        config_version="config-v1",
        route="local-test-process",
    )

    base_ctx = make_simulation_context()
    merged_receipts = dict(base_ctx.receipts)
    merged_receipts[receipt.receipt_id] = receipt

    ctx = make_simulation_context(
        receipts=merged_receipts,
    )
    handoff = make_handoff(dependencies=[resolved_dep])
    report = ReadinessGate().evaluate(handoff, context=ctx)

    assert report.eligible is True
    assert report.readiness is ReadinessState.READY_FOR_RELEASE
    assert dep.dependency_id not in report.blocking_dependency_ids


def test_dependency_id_mismatch_rejected() -> None:
    """Verify executing a probe job against a mismatched dependency is rejected."""
    dep = _make_dependency(dependency_id="dep-expected")
    job = submit_resolution(
        dependency_id="dep-mismatched",
        response_ref="ref://responses/test",
        version=1,
        operator_identity="operator-alice",
        help_route=dep.help_route,
    )
    res_dep, res_job = execute_manual_probe(job, dep)
    assert res_job.status is ProbeJobStatus.REJECTED
    assert "mismatch" in (res_job.detail or "").lower()
    assert res_dep.status is ManualDependencyStatus.WAITING


def test_reconcile_manual_dependencies_helper() -> None:
    """Verify reconcile_manual_dependencies matches dependencies and jobs and resolves them."""
    store = MockControlStore()
    dep1 = _make_dependency(dependency_id="dep-batch-1")
    dep2 = _make_dependency(dependency_id="dep-batch-2")

    job1 = submit_resolution(
        dependency_id="dep-batch-1",
        response_ref="ref://responses/1",
        version=1,
        operator_identity="operator-alice",
        help_route=dep1.help_route,
        store=store,
    )
    job2 = submit_resolution(
        dependency_id="dep-batch-2",
        response_ref="ref://responses/2",
        version=1,
        operator_identity="operator-bob",
        help_route=dep2.help_route,
        store=store,
    )

    from core.workflow.reconciliation import reconcile_manual_dependencies

    results = reconcile_manual_dependencies([dep1, dep2], [job1, job2], store=store)
    assert len(results) == 2
    assert all(d.status is ManualDependencyStatus.RESOLVED for d, _ in results)
    assert all(j.status is ProbeJobStatus.SUCCEEDED for _, j in results)
    assert len(store.outbox) == 2


def test_apply_resolution_to_handoff_scoping() -> None:
    """Verify apply_resolution_to_handoff updates only the matching dependency."""
    from core.workflow.manual_resolution import apply_resolution_to_handoff

    dep1 = _make_dependency(dependency_id="dep-h1", status=ManualDependencyStatus.WAITING)
    dep2 = _make_dependency(dependency_id="dep-h2", status=ManualDependencyStatus.WAITING)
    handoff = make_handoff(dependencies=[dep1, dep2])

    resolved_dep1 = dep1.model_copy(
        update={
            "status": ManualDependencyStatus.RESOLVED,
            "resolved_at": datetime.now(UTC),
            "resolution_receipt_ref": "receipt://manual-resolution/dep-h1",
        }
    )

    updated_handoff = apply_resolution_to_handoff(handoff, resolved_dep1)
    deps_by_id = {d.dependency_id: d for d in updated_handoff.manual_dependencies}

    assert deps_by_id["dep-h1"].status is ManualDependencyStatus.RESOLVED
    assert deps_by_id["dep-h2"].status is ManualDependencyStatus.WAITING


def test_plaintext_secret_rejection_in_help_route_and_operator_identity() -> None:
    """Verify plaintext secrets in help_route or operator_identity are rejected with ValidationError."""
    with pytest.raises(ValidationError, match="secret"):
        ResolutionSubmission(
            dependency_id="dep-test",
            response_ref="ref://safe",
            version=1,
            operator_identity="operator-alice",
            help_route="https://vault.local/api?token=secret12345",
        )

    with pytest.raises(ValidationError, match="secret"):
        ResolutionSubmission(
            dependency_id="dep-test",
            response_ref="ref://safe",
            version=1,
            operator_identity="token=secretvalue12345",
            help_route="help://runbook",
        )


def test_evaluator_exception_fails_closed() -> None:
    """Verify that an exception in probe_evaluator results in fail-closed probe failure."""
    dep = _make_dependency()
    job = submit_resolution(
        dependency_id=dep.dependency_id,
        response_ref="ref://responses/test",
        version=1,
        operator_identity="operator-alice",
        help_route=dep.help_route,
    )

    def faulty_evaluator(j: ProbeJob, d: ManualDependency) -> bool:
        raise RuntimeError("Network timeout contacting verification service")

    res_dep, res_job = execute_manual_probe(job, dep, probe_evaluator=faulty_evaluator)
    assert res_job.status is ProbeJobStatus.FAILED
    assert "exception" in (res_job.detail or "").lower()
    assert res_dep.status is ManualDependencyStatus.WAITING

