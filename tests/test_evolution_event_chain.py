"""Complete verification test suite for Evolution Event Chain (HF-25-01).

Tests:
1. Mandatory Counter-Proof (a): Candidate modifying own verifier rejected (SecurityViolationError / fail-closed)
   - tests/, sandbox.py, engine.py, core/harness/, core/workflow/verification.py
2. Mandatory Counter-Proof (b): Bad candidate fails holdout (syntax or logic defects) -> status=REJECTED and prevents promotion
3. Mandatory Counter-Proof (c): Good candidate consumed after restart -> does not alter current running jobs, applies stably post-restart
4. Mandatory Counter-Proof (d): Regression restores snapshot and blocks spurious re-application
5. StageHandler protocol compliance and canonical refs emission (ref://evolution/proposal/..., ref://evolution/snapshot/...)
6. Stale lease and fencing token failsafes (cause_code="stale_lease")
7. Integration with build_handlers() and dispatch_stage()
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from core.evolution.engine import FactoryEvolutionEngine
from core.evolution.models import (
    EvolutionProposal,
    EvolutionStatus,
    EvolutionTarget,
    EvolutionTrigger,
    SecurityViolationError,
)
from core.evolution.sandbox import EvolutionHoldoutSandbox
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.evolution_handlers import (
    EvolutionStageHandler,
    LearningEvalHandler,
    create_evolution_bindings,
)
from core.workflow.handlers import (
    StageHandler,
    build_handlers,
    dispatch_stage,
)


def _make_context(
    ticket_id: str = "HF-25-01",
    stage: str = "learning_eval",
    identity: str = "evaluator_agent",
    expires_delta_sec: int = 300,
    fencing_token: int = 1,
    input_refs: list[str] | None = None,
) -> StageContext:
    """Helper to construct a valid StageContext."""
    now = datetime.now(UTC)
    expires_at = (now + timedelta(seconds=expires_delta_sec)).isoformat()
    jk = JobKey(
        run_id="run_evo_test_01",
        ticket_id=ticket_id,
        plan_version="v1",
        stage=stage,
        iteration=0,
    )
    claim = Claim(
        job_key=jk,
        lease_id="lease_test_01",
        owner="worker_test_01",
        fencing_token=fencing_token,
        expires_at=expires_at,
    )
    return StageContext(
        claim=claim,
        plan_ref="ref://plans/HF-25-01",
        plan_digest="a" * 64,
        config_version="v1.0",
        environment_ref="env://local",
        identity=identity,
        route_ref="route://local",
        memory_version="m_v1",
        input_refs=input_refs or [],
    )


# ==============================================================================
# 1. COUNTER-PROOF (a): Verifier Tampering Rejected Fail-Closed
# ==============================================================================

@pytest.mark.parametrize(
    "forbidden_path",
    [
        "tests/test_something.py",
        "tests/unit/test_core.py",
        "core/evolution/sandbox.py",
        "core/evolution/engine.py",
        "core/harness/runner.py",
        "core/workflow/verification.py",
        "core/orchestrator/guard.py",
        "MISSION.md",
        "FACTORY_RULES.md",
        "FACTORY_GOVERNANCE.md",
        "AGENTS.md",
    ],
)
def test_counterproof_a_tampering_verifier_rejected(tmp_path: Path, forbidden_path: str) -> None:
    sandbox = EvolutionHoldoutSandbox(root=tmp_path)
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=tmp_path / "evo")

    # 1. Direct sandbox boundary audit must raise SecurityViolationError
    with pytest.raises(SecurityViolationError, match="Forbidden self-evolution target"):
        sandbox.audit_boundaries(forbidden_path)

    # 2. engine.propose must fail-closed with SecurityViolationError
    with pytest.raises(SecurityViolationError):
        engine.propose(
            target_kind=EvolutionTarget.SKILL_INSTRUCTION,
            target_path=forbidden_path,
            trigger=EvolutionTrigger.MANUAL_PROPOSAL,
            patch_content="def hacked(): pass\n",
            rationale="malicious verifier bypass attempt",
        )

    # 3. Direct sandbox evaluation of constructed proposal must return passed=False, tampering_detected=True
    proposal = EvolutionProposal(
        proposal_id=f"evo_tamper_{abs(hash(forbidden_path)) % 10000}",
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path=forbidden_path,
        trigger=EvolutionTrigger.MANUAL_PROPOSAL,
        patch_content="def hacked(): pass\n",
        rationale="tamper test",
    )
    res = sandbox.evaluate(proposal)
    assert not res.passed
    assert res.tampering_detected
    assert forbidden_path in res.protected_files_touched


def test_counterproof_a_stage_handler_rejects_verifier_tampering(tmp_path: Path) -> None:
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)

    # Manually register a malicious proposal bypassing propose() to test handler defense
    malicious = EvolutionProposal(
        proposal_id="evo_malicious_01",
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path="tests/test_fake.py",
        trigger=EvolutionTrigger.MANUAL_PROPOSAL,
        patch_content="def bypass(): pass",
        rationale="harness tampering",
    )
    engine._proposals[malicious.proposal_id] = malicious

    context = _make_context(
        input_refs=[f"ref://evolution/proposal/{malicious.proposal_id}"],
    )
    result = handler.handle(context)

    assert result.outcome == "failed"
    assert result.cause_code == "security_violation"
    assert not result.output_refs


# ==============================================================================
# 2. COUNTER-PROOF (b): Bad Candidate Fails Holdout Sandbox
# ==============================================================================

def test_counterproof_b_syntax_defect_fails_holdout(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")

    # Propose proposal with broken Python syntax
    prop = engine.propose(
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path="core/router/dynamic_config.py",
        trigger=EvolutionTrigger.FAIL_REPEATED,
        patch_content="def broken_syntax(:\n    return False\n",
        rationale="candidate with invalid syntax",
        proposal_id="evo_bad_syntax",
    )

    # Evaluate candidate in sandbox
    eval_res = engine.evaluate_candidate(prop.proposal_id)
    assert not eval_res.passed
    assert "Compilation error" in eval_res.log_summary

    # Ensure status is REJECTED
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.REJECTED

    # Inviolable: promotion must be strictly blocked
    with pytest.raises(ValueError, match="Must be 'approved'"):
        engine.promote_candidate(prop.proposal_id)


def test_counterproof_b_logical_defect_fails_holdout_command(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")

    # Candidate with valid syntax but failing logical holdout
    prop = engine.propose(
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path="docs/instructions.md",
        trigger=EvolutionTrigger.RCA_DISCOVERY,
        patch_content="Instruction text with logical regression",
        rationale="logical defect test",
        proposal_id="evo_bad_logic",
    )

    # Holdout command that fails (exit code 1)
    eval_res = engine.evaluate_candidate(
        prop.proposal_id,
        holdout_cmd='python -c "import sys; sys.exit(1)"',
    )
    assert not eval_res.passed
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.REJECTED

    # Ensure promote is blocked
    with pytest.raises(ValueError, match="Must be 'approved'"):
        engine.promote_candidate(prop.proposal_id)


def test_counterproof_b_handler_rejects_bad_candidate(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)

    prop = engine.propose(
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path="core/router/bad_code.py",
        trigger=EvolutionTrigger.FAIL_REPEATED,
        patch_content="import broken syntax ???",
        rationale="syntax defect",
        proposal_id="evo_handler_bad",
    )

    context = _make_context(input_refs=[f"ref://evolution/proposal/{prop.proposal_id}"])
    result = handler.handle(context)

    assert result.outcome == "failed"
    assert result.cause_code == "holdout_failed"
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.REJECTED


# ==============================================================================
# 3. COUNTER-PROOF (c): Good Candidate Applied After Restart Without Altering Running Jobs
# ==============================================================================

def test_counterproof_c_good_candidate_applied_after_restart(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_rel = "config/policy.txt"
    target_file = repo_root / target_rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("Policy Version 1.0 (Baseline)", encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")

    # 1. Simulate an active running job pinned to baseline
    engine.register_running_job("job_active_42", paths=[target_rel])
    assert engine.get_content_for_run("job_active_42", target_rel) == "Policy Version 1.0 (Baseline)"

    # 2. Propose and evaluate candidate
    prop = engine.propose(
        target_kind=EvolutionTarget.CONTEXT_RULE,
        target_path=target_rel,
        trigger=EvolutionTrigger.BENCHMARK_SHIFT,
        patch_content="Policy Version 2.0 (Evolved)",
        rationale="Performance optimization from benchmark analysis",
        proposal_id="evo_good_01",
    )
    eval_res = engine.evaluate_candidate(prop.proposal_id)
    assert eval_res.passed
    assert prop.status == EvolutionStatus.APPROVED

    # 3. Promote with defer_to_restart=True (production continuous autonomy setting)
    snapshot = engine.promote_candidate(prop.proposal_id, defer_to_restart=True)
    assert snapshot.previous_content == "Policy Version 1.0 (Baseline)"
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.PROMOTED
    assert engine._proposals[prop.proposal_id].metadata.get("pending_restart") is True

    # Invariant: live file on disk is UNTOUCHED before restart!
    assert target_file.read_text(encoding="utf-8") == "Policy Version 1.0 (Baseline)"

    # Invariant: running job still sees baseline content!
    assert engine.get_content_for_run("job_active_42", target_rel) == "Policy Version 1.0 (Baseline)"

    # 4. Job completes
    engine.finish_running_job("job_active_42")

    # 5. Worker/Factory Restart occurs
    applied = engine.restart()
    assert prop.proposal_id in applied

    # Invariant: after restart, mutation is stably applied to disk!
    assert target_file.read_text(encoding="utf-8") == "Policy Version 2.0 (Evolved)"
    assert engine._proposals[prop.proposal_id].metadata.get("pending_restart") is False
    assert engine._proposals[prop.proposal_id].metadata.get("applied_at_restart") is not None

    # Invariant: newly started jobs post-restart consume the evolved version!
    assert engine.get_content_for_run("job_new_101", target_rel) == "Policy Version 2.0 (Evolved)"


def test_counterproof_c_stage_handler_executes_deferred_promotion(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_rel = "rules/optimization.json"
    target_file = repo_root / target_rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('{"threshold": 10}', encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine, defer_to_restart=True)

    prop = engine.propose(
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path=target_rel,
        trigger=EvolutionTrigger.RULES_DRIFT,
        patch_content='{"threshold": 25}',
        rationale="Increase threshold based on drift telemetry",
        proposal_id="evo_handler_defer",
    )

    context = _make_context(input_refs=[f"ref://evolution/proposal/{prop.proposal_id}"])
    result = handler.handle(context)

    assert result.outcome == "success"
    assert f"ref://evolution/proposal/{prop.proposal_id}" in result.output_refs
    assert any(ref.startswith("ref://evolution/snapshot/") for ref in result.output_refs)

    # Target file remains baseline until restart
    assert target_file.read_text(encoding="utf-8") == '{"threshold": 10}'

    # Simulate restart
    engine.restart()
    assert target_file.read_text(encoding="utf-8") == '{"threshold": 25}'


# ==============================================================================
# 4. COUNTER-PROOF (d): Regression Restores Snapshot and Blocks Re-Application
# ==============================================================================

def test_counterproof_d_regression_restores_snapshot_and_blocks(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_rel = "models/params.json"
    target_file = repo_root / target_rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('{"stable": true}', encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")

    # 1. Propose, evaluate and promote candidate immediately
    prop = engine.propose(
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path=target_rel,
        trigger=EvolutionTrigger.BENCHMARK_SHIFT,
        patch_content='{"stable": false, "experimental": true}',
        rationale="Experimental tuning",
        proposal_id="evo_regress_01",
    )
    engine.evaluate_candidate(prop.proposal_id)
    snapshot = engine.promote_candidate(prop.proposal_id, defer_to_restart=False)

    assert target_file.read_text(encoding="utf-8") == '{"stable": false, "experimental": true}'
    assert prop.status == EvolutionStatus.PROMOTED

    # 2. Operational regression detected -> Trigger record_regression
    restored_snap = engine.record_regression(prop.proposal_id, reason="Runtime regression: crash on production traffic")
    assert restored_snap.snapshot_id == snapshot.snapshot_id

    # Invariant: Exact previous content restored
    assert target_file.read_text(encoding="utf-8") == '{"stable": true}'

    # Invariant: Status transitioned to ROLLED_BACK
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.ROLLED_BACK
    assert engine._proposals[prop.proposal_id].metadata.get("blocked") is True

    # Invariant: Spurious re-promotion strictly blocked
    with pytest.raises(ValueError, match="blocked against spurious re-application"):
        engine.promote_candidate(prop.proposal_id)

    # Invariant: Restart will NOT re-apply rolled back proposal
    applied = engine.restart()
    assert prop.proposal_id not in applied
    assert target_file.read_text(encoding="utf-8") == '{"stable": true}'

    # Invariant: Proposing the exact same patch for the target path is rejected
    with pytest.raises(ValueError, match="Spurious evolution proposal rejected"):
        engine.propose(
            target_kind=EvolutionTarget.ROUTING_CONFIG,
            target_path=target_rel,
            trigger=EvolutionTrigger.BENCHMARK_SHIFT,
            patch_content='{"stable": false, "experimental": true}',
            rationale="Trying again",
        )


def test_counterproof_d_handler_regression_rollback(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_rel = "skills/math.md"
    target_file = repo_root / target_rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("Safe formula: a + b", encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)

    prop = engine.propose(
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path=target_rel,
        trigger=EvolutionTrigger.MANUAL_PROPOSAL,
        patch_content="Regressive formula: a / 0",
        rationale="Dangerous math",
        proposal_id="evo_regress_handler",
    )
    engine.evaluate_candidate(prop.proposal_id)
    engine.promote_candidate(prop.proposal_id, defer_to_restart=False)

    # Handler handles regression event
    res = handler.handle_regression(prop.proposal_id, reason="Division by zero in production")
    assert res.outcome == "failed"
    assert res.cause_code == "regression_rolled_back"
    assert target_file.read_text(encoding="utf-8") == "Safe formula: a + b"


# ==============================================================================
# 5. PROTOCOL CONFORMANCE & ROLE SEPARATION
# ==============================================================================

def test_stage_handler_protocol_conformance(tmp_path: Path) -> None:
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)
    learning_handler = LearningEvalHandler(engine=engine)

    # Check runtime Protocol conformance
    assert isinstance(handler, StageHandler)
    assert isinstance(learning_handler, StageHandler)


def test_role_separation_enforces_distinct_evaluator(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)

    # Author is developer_agent
    prop = engine.propose(
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path="skills/safe.md",
        trigger=EvolutionTrigger.MANUAL_PROPOSAL,
        patch_content="Safe text",
        rationale="test",
        proposal_id="evo_author_check",
        metadata={"proposer_identity": "developer_agent"},
    )

    # Context with same identity trying to self-approve
    context_same = _make_context(
        identity="developer_agent",
        input_refs=[f"ref://evolution/proposal/{prop.proposal_id}"],
    )
    result = handler.handle(context_same)
    assert result.outcome == "failed"
    assert result.cause_code == "role_separation_violation"

    # Context with distinct reviewer passes role check
    context_distinct = _make_context(
        identity="independent_evaluator",
        input_refs=[f"ref://evolution/proposal/{prop.proposal_id}"],
    )
    result_ok = handler.handle(context_distinct)
    assert result_ok.outcome == "success"


# ==============================================================================
# 6. FAILSAFE: Stale Lease and Fencing Token
# ==============================================================================

def test_stale_lease_time_expiration_fails_closed(tmp_path: Path) -> None:
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=tmp_path / "evo")
    handler = EvolutionStageHandler(engine=engine)

    # Context with expires_at in the past
    context = _make_context(expires_delta_sec=-60)
    result = handler.handle(context)

    assert result.outcome == "failed"
    assert result.cause_code == "stale_lease"
    assert not result.output_refs


def test_stale_lease_fencing_token_mismatch_fails_closed(tmp_path: Path) -> None:
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=tmp_path / "evo")
    # Handler expects fencing token 42
    handler = EvolutionStageHandler(engine=engine, expected_fencing_token=42)

    # Context provided with fencing token 41
    context = _make_context(fencing_token=41)
    result = handler.handle(context)

    assert result.outcome == "failed"
    assert result.cause_code == "stale_lease"


# ==============================================================================
# 7. INTEGRATION: build_handlers() & dispatch_stage()
# ==============================================================================

def test_build_handlers_and_dispatch_stage_integration(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    target_rel = "skills/workflow_skill.md"
    target_file = repo_root / target_rel
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("Original workflow skill", encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=tmp_path / "evo")
    prop = engine.propose(
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path=target_rel,
        trigger=EvolutionTrigger.RCA_DISCOVERY,
        patch_content="Updated workflow skill v2",
        rationale="Evolution test in workflow",
        proposal_id="evo_dispatch_01",
    )

    # Create bindings and register in standard registry
    bindings = create_evolution_bindings(engine=engine, defer_to_restart=True)
    registry = build_handlers(bindings=bindings)

    # Ensure handler registered for ("learning_eval", "v1")
    assert ("learning_eval", "v1") in registry
    assert isinstance(registry[("learning_eval", "v1")], StageHandler)

    # Dispatch stage context
    context = _make_context(
        stage="learning_eval",
        input_refs=[f"ref://evolution/proposal/{prop.proposal_id}"],
    )
    result = dispatch_stage(registry, context, version="v1")

    assert result.outcome == "success"
    assert f"ref://evolution/proposal/{prop.proposal_id}" in result.output_refs
    assert any(ref.startswith("ref://evolution/snapshot/") for ref in result.output_refs)
    assert any(ref.startswith("ref://evolution/evidence/") for ref in result.evidence_refs)
