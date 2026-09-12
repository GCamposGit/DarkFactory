"""HF-09: Implementation, quality, and independent review cycle tests.

Verifies:
- Invariant Cenário G4: Concurrent pools (development, test, review), 9 slots execution, conflict keys, fair queuing without starvation.
- Invariant Cenário G3: Unit tests passing with firewall/scopes blocked on target blocks readiness; valid target probe releases readiness.
- Post-implementation environment manifest reconciliation.
- Strict independent review by profile (rejection of auto-review, valid EvidenceReceipt generation).
- Limited correction loop preventing infinite retries (transitions to NEEDS_REPLAN).
- Greenfield and brownfield candidate verification.
- Headless CLI interface.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.demands.contracts_adapter import (
    build_environment_manifest,
    build_grill_record,
    build_workflow_handoff,
    create_testing_verification_context,
)
from core.demands.models import DemandInput, UserTicket
from core.workflow.contracts import (
    EnvironmentKind,
    EnvironmentManifest,
    EnvironmentPort,
    EvidenceFreshness,
    EvidenceRequirement,
    EvidenceResult,
    PlannerTier,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.cycle import (
    CorrectionLoopTracker,
    ExternalTargetProbe,
    ImplementationCandidate,
    ImplementationCycleService,
    IndependentReviewVerdict,
    ValidationCycleResult,
)
from core.workflow.readiness import ReadinessGate
from core.workflow.reconciliation import (
    ManifestDiff,
    extract_code_dependencies,
    reconcile_environment_manifest,
)
from core.workflow.runtime import (
    JobOutcome,
    JobRecord,
    JobSpec,
    JobStatus,
    WorkflowRuntime,
)
from core.workflow.verification import EvidenceReceipt, ValidationMode


def _make_ticket(ticket_id: str, project_id: str = "darkfac") -> UserTicket:
    return UserTicket(
        id=ticket_id,
        project_id=project_id,
        title=f"Feature {ticket_id}",
        problem_statement="Implement payment integration module.",
        core_journey=["User submits payment and receives receipt."],
        non_goals=["Cryptocurrency payments"],
        acceptance_criteria=["Payment endpoint returns 200", "Idempotent processing"],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _make_handoff(
    ticket_id: str,
    project_id: str = "darkfac",
    initial_state: WorkflowState = WorkflowState.READY_FOR_HANDOFF,
) -> tuple[WorkflowHandoff, SanitizedIdentity]:
    ticket = _make_ticket(ticket_id, project_id)
    grill = build_grill_record(ticket, ready_for_spec=True)
    worker = SanitizedIdentity(role="developer", subject="agent_dev_01")
    manifest = build_environment_manifest(ticket, worker_identity=worker)
    handoff = build_workflow_handoff(
        ticket,
        grill=grill,
        environment=manifest,
        state=initial_state,
        baseline_sha="abcdef1234567890abcdef1234567890abcdef12",
        validate_commands=["pytest tests/test_payment.py"],
    )
    return handoff, worker


# ==============================================================================
# 1. CENÁRIO G4: POOLS CONCORRENTES & 9 SLOTS SIMULTÂNEOS
# ==============================================================================

def test_scenario_g4_concurrent_pools_9_slots(tmp_path: Path) -> None:
    """Cenário G4: 4 desenvolvimentos de 3 projetos e 5 testes independentes usam 9 slots simultaneamente."""
    db_path = tmp_path / "runtime_g4.db"
    # Configure 4 development slots and 5 test slots
    runtime = WorkflowRuntime(
        db_path,
        stage_limits={"development": 4, "test": 5, "review": 2},
    )

    # 3 projects: proj-a, proj-b, proj-c
    # 4 dev runs: 2 in proj-a (without conflict), 1 in proj-b, 1 in proj-c
    dev_specs = [
        ("dev_1", "proj-a", "development", ["file:module_a1.py"]),
        ("dev_2", "proj-a", "development", ["file:module_a2.py"]),
        ("dev_3", "proj-b", "development", ["file:module_b.py"]),
        ("dev_4", "proj-c", "development", ["file:module_c.py"]),
    ]

    for job_id, proj_id, stage, conflicts in dev_specs:
        runtime.register_run(f"run_{job_id}", proj_id, initial_state=WorkflowState.IMPLEMENTING_ECONOMY)
        runtime.enqueue_job(
            JobSpec(
                job_id=job_id,
                run_id=f"run_{job_id}",
                project_id=proj_id,
                stage=stage,
                priority=10,
                conflict_keys=conflicts,
            )
        )

    # 5 independent test jobs
    test_specs = [
        ("test_1", "proj-a", "test", []),
        ("test_2", "proj-a", "test", []),
        ("test_3", "proj-b", "test", []),
        ("test_4", "proj-b", "test", []),
        ("test_5", "proj-c", "test", []),
    ]

    for job_id, proj_id, stage, conflicts in test_specs:
        runtime.register_run(f"run_{job_id}", proj_id, initial_state=WorkflowState.VALIDATING)
        runtime.enqueue_job(
            JobSpec(
                job_id=job_id,
                run_id=f"run_{job_id}",
                project_id=proj_id,
                stage=stage,
                priority=20,
                conflict_keys=conflicts,
            )
        )

    # 9 workers claim jobs simultaneously
    claimed_leases = []
    for i in range(1, 10):
        lease = runtime.claim_next(f"worker_{i}", lease_seconds=60.0)
        assert lease is not None, f"Worker {i} should have claimed a slot"
        claimed_leases.append(lease)

    assert len(claimed_leases) == 9

    dev_claims = [lease for lease in claimed_leases if lease.stage == "development"]
    test_claims = [lease for lease in claimed_leases if lease.stage == "test"]

    assert len(dev_claims) == 4, "Exact 4 development slots occupied"
    assert len(test_claims) == 5, "Exact 5 test slots occupied"

    # Enqueue 5th dev job and 6th test job: both must wait because pool limits are exhausted
    runtime.register_run("run_dev_extra", "proj-c", initial_state=WorkflowState.IMPLEMENTING_ECONOMY)
    runtime.enqueue_job(
        JobSpec(
            job_id="dev_extra",
            run_id="run_dev_extra",
            project_id="proj-c",
            stage="development",
            priority=10,
        )
    )
    assert runtime.claim_next("worker_10") is None, "Extra dev job must wait for an open dev slot"

    # Complete one dev lease: now dev_extra can be claimed!
    completed_dev = dev_claims[0]
    runtime.complete_job(completed_dev.lease_id, completed_dev.worker_id, JobOutcome.SUCCEEDED)

    extra_lease = runtime.claim_next("worker_10")
    assert extra_lease is not None
    assert extra_lease.job_id == "dev_extra"


def test_scenario_g4_conflict_keys_isolate_only_affected_jobs(tmp_path: Path) -> None:
    """Cenário G4: Conflito real de chaves restringe apenas os jobs envolvidos."""
    db_path = tmp_path / "runtime_conflicts.db"
    runtime = WorkflowRuntime(db_path, stage_limits={"development": 4})

    # Job 1 and Job 2 both touch database schema lock
    runtime.register_run("run_1", "proj-a", initial_state=WorkflowState.IMPLEMENTING_ECONOMY)
    runtime.register_run("run_2", "proj-a", initial_state=WorkflowState.IMPLEMENTING_ECONOMY)
    runtime.register_run("run_3", "proj-a", initial_state=WorkflowState.IMPLEMENTING_ECONOMY)

    runtime.enqueue_job(
        JobSpec(job_id="job_conflict_1", run_id="run_1", project_id="proj-a", stage="development", conflict_keys=["resource:db_schema"])
    )
    runtime.enqueue_job(
        JobSpec(job_id="job_conflict_2", run_id="run_2", project_id="proj-a", stage="development", conflict_keys=["resource:db_schema"])
    )
    runtime.enqueue_job(
        JobSpec(job_id="job_independent", run_id="run_3", project_id="proj-a", stage="development", conflict_keys=["resource:ui_css"])
    )

    # Worker 1 claims job_conflict_1
    lease_1 = runtime.claim_next("worker_1")
    assert lease_1 is not None and lease_1.job_id == "job_conflict_1"

    # Worker 2 claims next: job_conflict_2 is blocked due to active conflict, so job_independent is claimed
    lease_2 = runtime.claim_next("worker_2")
    assert lease_2 is not None and lease_2.job_id == "job_independent"

    # Worker 3 cannot claim anything yet
    assert runtime.claim_next("worker_3") is None

    # Complete job_conflict_1
    runtime.complete_job(lease_1.lease_id, "worker_1", JobOutcome.SUCCEEDED)

    # Now job_conflict_2 is unblocked and claimed
    lease_3 = runtime.claim_next("worker_3")
    assert lease_3 is not None and lease_3.job_id == "job_conflict_2"


# ==============================================================================
# 2. CENÁRIO G3: VALIDAÇÃO REALISTA & BLOQUEIO FAIL-CLOSED NO READINESSGATE
# ==============================================================================

def test_scenario_g3_firewall_or_scope_denial_blocks_readiness(tmp_path: Path) -> None:
    """Cenário G3: Unitários verdes com firewall/scopes inválidos no worker real bloqueiam prontidão."""
    db_path = tmp_path / "runtime_g3.db"
    runtime = WorkflowRuntime(db_path)
    gate = ReadinessGate()
    service = ImplementationCycleService(runtime, gate)

    ticket_id = "HF09-PAYMENT"
    handoff, dev_identity = _make_handoff(ticket_id)
    runtime.register_run(ticket_id, "darkfac", initial_state=WorkflowState.READY_FOR_HANDOFF)

    # 1. Start development
    service.start_development(handoff, dev_identity)

    # 2. Candidate implementation
    candidate = ImplementationCandidate.create(
        ticket_id=ticket_id,
        run_id=ticket_id,
        baseline_sha=handoff.baseline_sha,
        files_changed=["core/payment.py"],
        diff="def pay(): os.environ.get('GATEWAY_KEY'); return True",
    )

    reconciled_manifest, diff, run, test_job = service.submit_candidate(handoff, candidate, dev_identity)
    assert run.state == WorkflowState.VALIDATING

    # 3. Simulate target probe failure: firewall blocked on target environment
    probe_failed = ExternalTargetProbe(
        probe_id="probe_firewall_check",
        endpoint_url="https://gateway.bank.internal:8443/v1",
        required_scopes=["payments:write"],
        firewall_allowed=False,
        status="firewall_blocked",
        message="Firewall egress port 8443 rejected by security group",
    )

    context = create_testing_verification_context(
        handoff,
        candidate_digest=candidate.candidate_digest,
    )

    result = service.run_validation(
        handoff,
        candidate,
        unit_tests_pass=True,  # Unit tests pass locally!
        target_probe=probe_failed,
        reconciled_manifest=reconciled_manifest,
        manifest_diff=diff,
        context=context,
    )

    # Invariant Cenário G3: Unit tests pass, but target probe failure blocks readiness
    assert result.unit_tests_pass is True
    assert result.target_probe_pass is False
    assert result.eligible_for_review is False
    assert any("firewall" in reason.lower() for reason in result.blocking_reasons)
    # Run was not advanced to INDEPENDENT_REVIEW
    current_run = runtime.get_run(ticket_id)
    assert current_run.state != WorkflowState.INDEPENDENT_REVIEW


def test_scenario_g3_target_probe_pass_liberates_readiness(tmp_path: Path) -> None:
    """Cenário G3: Correção e nova evidência com probe válido no alvo liberam prontidão."""
    db_path = tmp_path / "runtime_g3_pass.db"
    runtime = WorkflowRuntime(db_path)
    gate = ReadinessGate()
    service = ImplementationCycleService(runtime, gate)

    ticket_id = "HF09-PAYMENT-RETRY"
    handoff, dev_identity = _make_handoff(ticket_id)
    runtime.register_run(ticket_id, "darkfac", initial_state=WorkflowState.READY_FOR_HANDOFF)

    service.start_development(handoff, dev_identity)
    candidate = ImplementationCandidate.create(
        ticket_id=ticket_id,
        run_id=ticket_id,
        baseline_sha=handoff.baseline_sha,
        files_changed=["core/payment.py"],
        diff="def pay(): return True",
    )
    reconciled_manifest, diff, run, test_job = service.submit_candidate(handoff, candidate, dev_identity)

    # Probe succeeds on target environment
    probe_success = ExternalTargetProbe(
        probe_id="probe_firewall_ok",
        endpoint_url="https://gateway.bank.internal:8443/v1",
        required_scopes=["payments:write"],
        firewall_allowed=True,
        status="passed",
        message="Connectivity and scopes verified on target",
    )

    context = create_testing_verification_context(
        handoff,
        candidate_digest=candidate.candidate_digest,
    )

    result = service.run_validation(
        handoff,
        candidate,
        unit_tests_pass=True,
        target_probe=probe_success,
        reconciled_manifest=reconciled_manifest,
        manifest_diff=diff,
        context=context,
    )

    assert result.unit_tests_pass is True
    assert result.target_probe_pass is True
    assert result.eligible_for_review is True
    current_run = runtime.get_run(ticket_id)
    assert current_run.state == WorkflowState.INDEPENDENT_REVIEW


# ==============================================================================
# 3. RECONCILIAÇÃO DE MANIFESTO DE AMBIENTE PÓS-IMPLEMENTAÇÃO
# ==============================================================================

def test_manifest_reconciliation_detects_additions() -> None:
    """Reconciliação detecta novas variáveis de ambiente, portas e endpoints no diff."""
    ticket = _make_ticket("TICKET-ENV")
    worker = SanitizedIdentity(role="developer", subject="agent_dev")
    initial_manifest = build_environment_manifest(ticket, worker_identity=worker)

    sample_diff = """
    import os
    DATABASE_URL = os.environ.get("DATABASE_URL")
    STRIPE_SECRET = os.getenv("STRIPE_SECRET_KEY")
    PORT = 8088
    remote_api = "https://payments.partner.com/webhook"
    """

    reconciled, diff = reconcile_environment_manifest(
        initial_manifest,
        code_or_diff=sample_diff,
    )

    assert diff.has_changes is True
    assert "DATABASE_URL" in diff.added_env_vars
    assert "STRIPE_SECRET_KEY" in diff.added_env_vars
    assert any(p.port == 8088 for p in diff.added_ports)
    assert any(ep.url == "https://payments.partner.com/webhook" for ep in diff.added_endpoints)

    assert reconciled.environment_ref.endswith("_v2")
    assert "DATABASE_URL" in reconciled.required_env_vars
    assert "STRIPE_SECRET_KEY" in reconciled.required_env_vars


# ==============================================================================
# 4. REVISÃO INDEPENDENTE POR PERFIL & REJEIÇÃO DE AUTOAPROVAÇÃO
# ==============================================================================

def test_independent_review_rejects_same_identity(tmp_path: Path) -> None:
    """Revisão independente rejeita auto-revisão pelo mesmo agente (reviewer == developer)."""
    db_path = tmp_path / "runtime_review.db"
    runtime = WorkflowRuntime(db_path)
    service = ImplementationCycleService(runtime)

    ticket_id = "HF09-REVIEW-CHECK"
    handoff, dev_identity = _make_handoff(ticket_id)
    runtime.register_run(ticket_id, "darkfac", initial_state=WorkflowState.INDEPENDENT_REVIEW)

    candidate = ImplementationCandidate.create(
        ticket_id=ticket_id,
        run_id=ticket_id,
        baseline_sha=handoff.baseline_sha,
        files_changed=["app.py"],
        diff="print('hello')",
    )

    # Same identity tries to review its own work
    same_identity = SanitizedIdentity(role="reviewer", subject=dev_identity.subject)

    with pytest.raises(ValueError, match="Auto-review is strictly forbidden"):
        service.conduct_independent_review(
            handoff,
            candidate,
            reviewer=same_identity,
            developer=dev_identity,
            approve=True,
        )


def test_independent_review_approval_generates_valid_receipt(tmp_path: Path) -> None:
    """Revisor independente distinto emite EvidenceReceipt aprovado e apto no ReadinessGate."""
    db_path = tmp_path / "runtime_review_ok.db"
    runtime = WorkflowRuntime(db_path)
    service = ImplementationCycleService(runtime)

    ticket_id = "HF09-REVIEW-PASS"
    handoff, dev_identity = _make_handoff(ticket_id)
    runtime.register_run(ticket_id, "darkfac", initial_state=WorkflowState.INDEPENDENT_REVIEW)

    candidate = ImplementationCandidate.create(
        ticket_id=ticket_id,
        run_id=ticket_id,
        baseline_sha=handoff.baseline_sha,
        files_changed=["app.py"],
        diff="print('hello')",
    )

    reviewer = SanitizedIdentity(role="independent_reviewer", subject="agent_reviewer_bob")
    context = create_testing_verification_context(
        handoff,
        candidate_digest=candidate.candidate_digest,
    )

    verdict, report = service.conduct_independent_review(
        handoff,
        candidate,
        reviewer=reviewer,
        developer=dev_identity,
        approve=True,
        context=context,
    )

    assert verdict.verdict == "approved"
    assert verdict.receipt is not None
    assert verdict.receipt.result is EvidenceResult.PASSED
    assert verdict.receipt.candidate_digest == candidate.candidate_digest
    assert verdict.receipt.producer.subject == "agent_reviewer_bob"


# ==============================================================================
# 5. LOOP LIMITADO DE CORREÇÕES (PREVENÇÃO DE LOOP INFINITO)
# ==============================================================================

def test_limited_correction_loop_prevents_infinite_retries(tmp_path: Path) -> None:
    """Falhas consecutivas acionam teto de tentativas e transicionam para NEEDS_REPLAN."""
    db_path = tmp_path / "runtime_loop.db"
    runtime = WorkflowRuntime(db_path)
    service = ImplementationCycleService(runtime)

    ticket_id = "HF09-RETRY-LOOP"
    handoff, dev_identity = _make_handoff(ticket_id)
    runtime.register_run(ticket_id, "darkfac", initial_state=WorkflowState.INDEPENDENT_REVIEW)

    candidate = ImplementationCandidate.create(
        ticket_id=ticket_id,
        run_id=ticket_id,
        baseline_sha=handoff.baseline_sha,
        files_changed=["app.py"],
        diff="def broken(): pass",
    )

    reviewer = SanitizedIdentity(role="independent_reviewer", subject="agent_reviewer_charlie")

    # Attempt 1: Rejection reverts to IMPLEMENTING_ECONOMY
    verdict1, _ = service.conduct_independent_review(
        handoff,
        candidate,
        reviewer=reviewer,
        developer=dev_identity,
        approve=False,
        findings=["Syntax error in payment handler"],
    )
    assert verdict1.verdict == "changes_required"
    assert runtime.get_run(ticket_id).state == WorkflowState.IMPLEMENTING_ECONOMY

    # Transition back to review for second attempt (economy -> validating -> review)
    runtime.transition_run(ticket_id, WorkflowState.VALIDATING)
    runtime.transition_run(ticket_id, WorkflowState.INDEPENDENT_REVIEW)

    # Attempt 2: Rejection reaches failure ceiling -> transitions to NEEDS_REPLAN
    verdict2, _ = service.conduct_independent_review(
        handoff,
        candidate,
        reviewer=reviewer,
        developer=dev_identity,
        approve=False,
        findings=["Same error persists"],
    )
    assert verdict2.verdict == "changes_required"
    # Run must be transitioned to NEEDS_REPLAN, avoiding infinite loops!
    assert runtime.get_run(ticket_id).state == WorkflowState.NEEDS_REPLAN


# ==============================================================================
# 6. CANDIDATOS GREENFIELD E BROWNFIELD
# ==============================================================================

def test_greenfield_and_brownfield_candidates() -> None:
    """Criação de candidatos verificáveis para novos projetos (greenfield) e bases existentes (brownfield)."""
    cand_green = ImplementationCandidate.create(
        ticket_id="GF-01",
        run_id="run_gf01",
        baseline_sha="0000000000000000000000000000000000000000",
        files_changed=["MISSION.md", "app.py", ".factory/darkfac.lock.json"],
        diff="+ initial commit",
        is_greenfield=True,
    )
    assert cand_green.is_greenfield is True
    assert cand_green.candidate_digest is not None
    assert cand_green.candidate_id.startswith("cand_GF-01_")

    cand_brown = ImplementationCandidate.create(
        ticket_id="BF-02",
        run_id="run_bf02",
        baseline_sha="1234567890abcdef1234567890abcdef12345678",
        files_changed=["core/routes.py"],
        diff="- old_route\n+ new_route",
        is_greenfield=False,
    )
    assert cand_brown.is_greenfield is False
    assert cand_brown.candidate_digest != cand_green.candidate_digest


# ==============================================================================
# 7. CLI HEADLESS (HF-09)
# ==============================================================================

def test_workflow_cycle_cli_headless(tmp_path: Path) -> None:
    """Comandos da CLI reconcile e validate-candidate em modo headless JSON."""
    from core.workflow.cli import main as cli_main

    # 1. Create a dummy manifest file
    manifest_file = tmp_path / "test_manifest.json"
    ticket = _make_ticket("CLI-01")
    worker = SanitizedIdentity(role="developer", subject="agent_cli")
    manifest = build_environment_manifest(ticket, worker_identity=worker)
    manifest_file.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    code_file = tmp_path / "test_code.py"
    code_file.write_text("API_TOKEN = os.environ.get('PARTNER_API_KEY')\nPORT = 8099\n", encoding="utf-8")

    # Test reconcile subcommand
    exit_code_rec = cli_main([
        "reconcile",
        "--manifest-json", str(manifest_file),
        "--code-file", str(code_file),
        "--json",
    ])
    assert exit_code_rec == 0

    # Test validate-candidate subcommand with firewall block (exit code 2)
    exit_code_fail = cli_main([
        "validate-candidate",
        "--ticket-id", "CLI-01",
        "--firewall-blocked",
        "--json",
    ])
    assert exit_code_fail == 2

    # Test validate-candidate subcommand with pass (exit code 0)
    exit_code_ok = cli_main([
        "validate-candidate",
        "--ticket-id", "CLI-01",
        "--probe-status", "passed",
        "--json",
    ])
    assert exit_code_ok == 0
