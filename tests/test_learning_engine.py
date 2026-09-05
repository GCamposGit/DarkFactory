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
    assert len(temp_tracker.ledger.preferences) == 1

    # Reinforce identical rule
    pref2 = temp_tracker.register_preference(
        category=PreferenceCategory.ARCHITECTURE,
        rule="Always design headless APIs with CLI/HTTP access before writing UI",
        context_or_example="Canaletto gallery backend",
        confidence=0.9,
    )
    assert pref2.preference_id == pref1.preference_id
    assert pref2.times_reinforced == 2
    assert pref2.confidence == 1.0
    assert len(temp_tracker.ledger.preferences) == 1


def test_record_mistake_rca(temp_tracker):
    """Verify 5-Whys Root Cause Analysis recording and preventative rule formation."""
    rca = temp_tracker.record_mistake_rca(
        category=MistakeCategory.TOOL_MISUSE,
        symptom="write_to_file rejected workspace path when ArtifactMetadata was provided",
        mechanism="ArtifactMetadata is strictly validated only for paths in brain directory",
        root_cause="Caller assumed all files could carry metadata, but workspace files cannot",
        patch_description="Omit ArtifactMetadata when writing to codebase paths outside brain",
        preventative_rule="Never include ArtifactMetadata for target files outside artifact dir",
        regression_test_file="tests/test_learning_engine.py",
    )
    assert rca.status == "resolved"
    assert len(temp_tracker.ledger.mistakes) == 1

    # Check persistence
    with open(temp_tracker.ledger_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["mistakes"][0]["rca_id"] == rca.rca_id
    assert data["mistakes"][0]["category"] == "tool_misuse"


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
