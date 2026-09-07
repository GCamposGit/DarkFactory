"""
Pytest integration tests for DarkFac Self-Learning Empirical Benchmark.
Ensures that all 4 learning dynamics (Error Extinction, One-Shot Convergence,
Cross-Domain Transfer, Policy Debt Pruning) maintain 100% deterministic pass.
"""

import pytest
from core.learning.benchmark import SelfLearningBenchmark
from core.learning.tracker import ContinuousLearningTracker
from core.learning.models import PreferenceCategory


def test_benchmark_full_suite():
    """Verify all synthetic scenarios pass and are labelled as synthetic evidence."""
    bench = SelfLearningBenchmark(verbose=False)
    summary = bench.run_all()
    assert summary["all_passed"] is True
    assert summary["passed_count"] == 4
    assert summary["total_count"] == 4
    assert summary["overall_score_pct"] == 100.0
    assert summary["metric_provenance"] == "synthetic"
    assert summary["synthetic"] is True
    assert all(item["metric_provenance"] == "synthetic" for item in summary["scenarios"])


def test_code_judge_verification_failure_blocks_promotion(tmp_path):
    """Verify that Code Judge blocks patch promotion if the verification test fails."""
    tracker = ContinuousLearningTracker(
        ledger_file=tmp_path / "judge_ledger.json",
        session_id="test_judge_session",
    )
    # Command that intentionally exits with non-zero exit code
    failing_cmd = "python -c \"import sys; sys.exit(1)\""
    gate = tracker.verify_patch_with_code_judge(
        target_type="rca_patch",
        test_command=failing_cmd,
    )
    assert gate.passed is False
    assert len(tracker.ledger.verifications) >= 1


def test_system1_context_filtering_by_domain(tmp_path):
    """Verify System 1 context can filter and prioritize rules by domain."""
    tracker = ContinuousLearningTracker(
        ledger_file=tmp_path / "domain_ledger.json",
        session_id="test_domain_session",
    )
    tracker.extrapolate_analogy(
        source_domain="core.audio",
        target_domains=["core.benchmarks"],
        specific_lesson="VRAM leak",
        generalized_principle="Release model memory on test exit",
    )
    context_benchmarks = tracker.get_active_system1_context(domain="core.benchmarks")
    assert "Release model memory on test exit" in context_benchmarks

    context_unrelated = tracker.get_active_system1_context(domain="unrelated_domain_xyz")
    assert "Release model memory on test exit" not in context_unrelated
