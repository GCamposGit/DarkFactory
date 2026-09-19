"""Tests for Factory Self-Evolution Subsystem (HF-25)."""

import tempfile
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


def test_sandbox_blocks_tampering_of_protected_files() -> None:
    sandbox = EvolutionHoldoutSandbox()

    # Governance protected files
    with pytest.raises(SecurityViolationError, match="immutable governance file"):
        sandbox.audit_boundaries("MISSION.md")

    with pytest.raises(SecurityViolationError, match="immutable governance file"):
        sandbox.audit_boundaries("FACTORY_RULES.md")

    with pytest.raises(SecurityViolationError, match="immutable governance file"):
        sandbox.audit_boundaries("FACTORY_GOVERNANCE.md")

    # Verifier and harness guards
    with pytest.raises(SecurityViolationError, match="touches active verifier"):
        sandbox.audit_boundaries("core/harness/runner.py")

    with pytest.raises(SecurityViolationError, match="touches active verifier"):
        sandbox.audit_boundaries("harness.config.json")

    with pytest.raises(SecurityViolationError, match="touches active verifier"):
        sandbox.audit_boundaries("core/orchestrator/guard.py")


def test_sandbox_evaluates_tampering_proposal_fail_closed() -> None:
    sandbox = EvolutionHoldoutSandbox()
    proposal = EvolutionProposal(
        proposal_id="evo_tamper_01",
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path="core/harness/runner.py",
        trigger=EvolutionTrigger.MANUAL_PROPOSAL,
        patch_content="def hacked(): pass\n",
        rationale="malicious test",
    )
    result = sandbox.evaluate(proposal)
    assert not result.passed
    assert result.tampering_detected
    assert "core/harness/runner.py" in result.protected_files_touched


def test_sandbox_evaluates_valid_python_syntax_cleanly() -> None:
    sandbox = EvolutionHoldoutSandbox()
    proposal = EvolutionProposal(
        proposal_id="evo_clean_01",
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path="core/router/test_sample.py",
        trigger=EvolutionTrigger.RULES_DRIFT,
        patch_content="x = 42\n",
        rationale="valid python code",
    )
    result = sandbox.evaluate(proposal)
    assert result.passed
    assert not result.tampering_detected


def test_sandbox_rejects_invalid_syntax() -> None:
    sandbox = EvolutionHoldoutSandbox()
    proposal = EvolutionProposal(
        proposal_id="evo_syntax_err",
        target_kind=EvolutionTarget.ROUTING_CONFIG,
        target_path="core/router/bad_syntax.py",
        trigger=EvolutionTrigger.FAIL_REPEATED,
        patch_content="def bad_syntax(:\n",
        rationale="broken syntax test",
    )
    result = sandbox.evaluate(proposal)
    assert not result.passed
    assert "Compilation error" in result.log_summary


def test_engine_lifecycle_propose_evaluate_promote_rollback(tmp_path: Path) -> None:
    # Set up engine in temp storage
    storage = tmp_path / "evolution"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    target_rel = "some/module/skill.md"
    target_abs = repo_root / target_rel
    target_abs.parent.mkdir(parents=True)
    target_abs.write_text("Original version 1.0", encoding="utf-8")

    engine = FactoryEvolutionEngine(root=repo_root, storage_dir=storage)

    # 1. Propose
    prop = engine.propose(
        target_kind=EvolutionTarget.SKILL_INSTRUCTION,
        target_path=target_rel,
        trigger=EvolutionTrigger.RCA_DISCOVERY,
        patch_content="Updated version 2.0 with preventative rule",
        rationale="RCA indicated need for preventative rule",
    )
    assert prop.status == EvolutionStatus.PROPOSED
    assert prop.proposal_id in engine._proposals

    # 2. Cannot promote before evaluation
    with pytest.raises(ValueError, match="Must be 'approved'"):
        engine.promote_candidate(prop.proposal_id)

    # 3. Evaluate
    eval_res = engine.evaluate_candidate(prop.proposal_id)
    assert eval_res.passed
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.APPROVED

    # 4. Promote
    snap = engine.promote_candidate(prop.proposal_id)
    assert snap.previous_content == "Original version 1.0"
    assert target_abs.read_text(encoding="utf-8") == "Updated version 2.0 with preventative rule"
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.PROMOTED

    # 5. Rollback
    ok = engine.rollback_candidate(prop.proposal_id)
    assert ok
    assert target_abs.read_text(encoding="utf-8") == "Original version 1.0"
    assert engine._proposals[prop.proposal_id].status == EvolutionStatus.ROLLED_BACK


def test_engine_proposals_persistence_and_report(tmp_path: Path) -> None:
    storage = tmp_path / "evolution"
    engine = FactoryEvolutionEngine(root=tmp_path, storage_dir=storage)

    engine.propose(
        target_kind=EvolutionTarget.CONTEXT_RULE,
        target_path="config/rules.json",
        trigger=EvolutionTrigger.BENCHMARK_SHIFT,
        patch_content="{}",
        rationale="Test rule persistence",
        proposal_id="prop_persisted_01",
    )

    # Reload from disk
    engine2 = FactoryEvolutionEngine(root=tmp_path, storage_dir=storage)
    report = engine2.get_report()
    assert report.total_proposals == 1
    assert report.proposals[0].proposal_id == "prop_persisted_01"


def test_evolution_cli_status(capsys: pytest.CaptureFixture[str]) -> None:
    from core.evolution.cli import main
    code = main(["status"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Factory Evolution Subsystem" in captured.out
