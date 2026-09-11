"""
Unit and integration tests for DarkFac Continuous Learning and Self-Improvement Engine.
Validates session tracking, checkpoint triggers, preference registration,
mistake Root Cause Analysis (RCA), and analogous transfer persistence.
"""

import os
import json
import pytest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.learning.models import (
    PreferenceCategory,
    MistakeCategory,
    PolicyOrigin,
    PolicyStatus,
    InteractionTurn,
    UserPreference,
    MistakeRCA,
    AnalogousTransfer,
    LearningLedger,
)
from core.learning.tracker import ContinuousLearningTracker


@pytest.fixture
def temp_tracker():
    with TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "learning" / "learning_ledger.json"
        tracker = ContinuousLearningTracker(ledger_file=ledger_path, session_id="test_session_001")
        yield tracker


def test_checkpoint_trigger_cadence():
    """Verify that the checkpoint triggers on every prompt from turn 1 onwards and on follow-ups."""
    # Turn 0: False
    assert ContinuousLearningTracker.is_checkpoint_turn(0, was_followup=False) is False
    # Turn 1: True (every prompt from turn 1 onwards evaluates)
    assert ContinuousLearningTracker.is_checkpoint_turn(1, was_followup=False) is True
    # Turn 2: True
    assert ContinuousLearningTracker.is_checkpoint_turn(2, was_followup=False) is True
    # Turn 3: True
    assert ContinuousLearningTracker.is_checkpoint_turn(3, was_followup=False) is True
    # Turn 4: True
    assert ContinuousLearningTracker.is_checkpoint_turn(4, was_followup=False) is True

    # Any follow-up triggers immediately
    assert ContinuousLearningTracker.is_checkpoint_turn(1, was_followup=True) is True
    assert ContinuousLearningTracker.is_checkpoint_turn(3, was_followup=True) is True


def test_record_turn_and_persistence(temp_tracker):
    """Verify recording interaction turns increments count and saves to disk."""
    turn1 = temp_tracker.record_turn(
        user_prompt_summary="Create audio transcriber",
        perceived_intent="Build GPU transcription module",
        was_followup=False,
        one_shot_success=True,
        target_skill="local-audio-transcription",
    )
    assert turn1.turn_id == 1
    assert temp_tracker.ledger.prompt_count == 1
    assert temp_tracker.ledger_file.exists()

    turn2 = temp_tracker.record_turn(
        user_prompt_summary="Fix audio silence cutoff",
        perceived_intent="Adjust VAD parameters",
        was_followup=True,
        one_shot_success=False,
        missing_context_or_gap="VAD threshold was too aggressive for whispered voices",
        target_skill="local-audio-transcription",
    )
    assert turn2.turn_id == 2
    assert temp_tracker.ledger.prompt_count == 2
    assert turn2.was_followup is True

    # Reload from disk
    reloaded = ContinuousLearningTracker(ledger_file=temp_tracker.ledger_file)
    assert reloaded.ledger.prompt_count == 2
    assert len(reloaded.ledger.turns) == 2
    assert reloaded.ledger.turns[1].missing_context_or_gap == "VAD threshold was too aggressive for whispered voices"


def test_register_and_reinforce_preference(temp_tracker):
    """Verify registering user preferences and reinforcing when seen again."""
    pref1 = temp_tracker.register_preference(
        category=PreferenceCategory.ARCHITECTURE,
        rule="Always design headless APIs with CLI/HTTP access before writing UI",
        context_or_example="DarkHub backend and audio transcriber",
        confidence=0.9,
    )
    assert pref1.times_reinforced == 1
    assert pref1.origin is PolicyOrigin.EXPLICIT_PREFERENCE
    assert pref1.status is PolicyStatus.ACTIVE
    assert len(temp_tracker.ledger.preferences) == 1

    # Reinforce identical rule
    pref2 = temp_tracker.register_preference(
        category=PreferenceCategory.ARCHITECTURE,
        rule="Always design headless APIs with CLI/HTTP access before writing UI",
        context_or_example="DarkHub local service backend",
        confidence=0.9,
    )
    assert pref2.preference_id == pref1.preference_id
    assert pref2.times_reinforced == 2
    assert pref2.confidence == 1.0
    assert len(temp_tracker.ledger.preferences) == 1


def test_record_mistake_rca_without_evidence_remains_candidate(temp_tracker):
    """An RCA without executable evidence must not become an active rule."""
    rca = temp_tracker.record_mistake_rca(
        category=MistakeCategory.TOOL_MISUSE,
        symptom="write_to_file rejected workspace path when ArtifactMetadata was provided",
        mechanism="ArtifactMetadata is strictly validated only for paths in brain directory",
        root_cause="Caller assumed all files could carry metadata, but workspace files cannot",
        patch_description="Omit ArtifactMetadata when writing to codebase paths outside brain",
        preventative_rule="Never include ArtifactMetadata for target files outside artifact dir",
        regression_test_file="tests/test_learning_engine.py",
    )
    assert rca.status is PolicyStatus.PROPOSED
    assert rca.active is False
    assert rca.preventative_rule not in temp_tracker.get_active_system1_context()
    assert len(temp_tracker.ledger.mistakes) == 1

    # Check persistence
    with open(temp_tracker.ledger_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["mistakes"][0]["rca_id"] == rca.rca_id
    assert data["mistakes"][0]["category"] == "tool_misuse"
    assert data["mistakes"][0]["status"] == "proposed"


def test_rca_verification_controls_activation(temp_tracker):
    failing = temp_tracker.record_mistake_rca(
        category=MistakeCategory.TEST_REGRESSION,
        symptom="Regression repeated",
        mechanism="The guard was missing",
        root_cause="The proposed policy had not been evaluated",
        patch_description="Add a deterministic guard",
        preventative_rule="Never activate rejected policies",
        test_verification_cmd='python -c "raise SystemExit(1)"',
    )
    assert failing.status is PolicyStatus.EVALUATED
    assert failing.active is False
    assert failing.preventative_rule not in temp_tracker.get_active_system1_context()

    passing = temp_tracker.record_mistake_rca(
        category=MistakeCategory.TEST_REGRESSION,
        symptom="A separately verified regression",
        mechanism="The guard was absent",
        root_cause="No executable evidence existed",
        patch_description="Add and execute the regression check",
        preventative_rule="Activate only policies supported by passing evidence",
        test_verification_cmd='python -c "raise SystemExit(0)"',
    )
    assert passing.status is PolicyStatus.ACTIVE
    assert passing.active is True
    assert passing.preventative_rule in temp_tracker.get_active_system1_context()


def test_inferred_preference_requires_evaluation_before_activation(temp_tracker):
    contrast = temp_tracker.record_trajectory_contrast(
        initial_output_summary="Verbose prose",
        user_correction="Use a compact table",
        corrected_output_summary="Compact table",
        key_delta="prose to table",
        inferred_preference_rule="Prefer compact tables",
    )
    pref = next(
        item
        for item in temp_tracker.ledger.preferences
        if item.preference_id == contrast.inferred_preference_id
    )
    assert pref.origin is PolicyOrigin.INFERRED_PREFERENCE
    assert pref.status is PolicyStatus.PROPOSED
    assert pref.rule not in temp_tracker.get_active_system1_context()

    gate = temp_tracker.verify_patch_with_code_judge(
        target_type="preference_rule",
        test_command='python -c "raise SystemExit(0)"',
    )
    evaluated = temp_tracker.evaluate_preference(pref.preference_id, gate.gate_id)
    assert evaluated.status is PolicyStatus.ACTIVE
    assert evaluated.rule in temp_tracker.get_active_system1_context()


def test_extrapolate_analogy(temp_tracker):
    """Verify transferring lessons from one domain to analogous modules."""
    transfer = temp_tracker.extrapolate_analogy(
        source_domain="audio_transcriber",
        target_domains=["benchmarks_cli", "research_cli", "hub_backend"],
        specific_lesson="Timeout expired on subprocess run during test",
        generalized_principle="All background tasks and subprocesses must have explicit timeouts and non-blocking status polling",
        applied_actions=["Added default timeouts", "Verified tests use mock execution"],
    )
    assert len(transfer.target_domains) == 3
    assert len(temp_tracker.ledger.transfers) == 1
    assert "hub_backend" in transfer.target_domains


def test_summary_report(temp_tracker):
    """Verify summary metrics calculation."""
    temp_tracker.record_turn(
        user_prompt_summary="Turn 1",
        perceived_intent="Initial task",
        was_followup=False,
        one_shot_success=True,
    )
    temp_tracker.record_turn(
        user_prompt_summary="Turn 2",
        perceived_intent="Follow-up revision",
        was_followup=True,
        one_shot_success=False,
    )
    summary = temp_tracker.get_summary_report()
    assert summary["prompt_count"] == 2
    assert summary["total_turns"] == 2
    assert summary["followup_turns"] == 1
    assert summary["one_shot_rate_pct"] == 50.0
