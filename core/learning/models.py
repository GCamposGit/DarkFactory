"""
Domain models for DarkFac Continuous Learning and Self-Improvement Engine.
Strictly typed for compatibility with Universal Engineering Standards (AGENTS.md).
Incorporates SOTA Dual-Process (DPA), SICA, ExpeL, and Voyager constructs.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


class PreferenceCategory(str, Enum):
    ARCHITECTURE = "architecture"
    TOOLING = "tooling"
    COMMUNICATION = "communication"
    TESTING = "testing"
    STYLING = "styling"
    WORKFLOW = "workflow"


class MistakeCategory(str, Enum):
    COMPILATION_SYNTAX = "compilation_syntax"
    TYPE_ERROR = "type_error"
    TEST_REGRESSION = "test_regression"
    TOOL_MISUSE = "tool_misuse"
    GOVERNANCE_VIOLATION = "governance_violation"
    ASSUMPTION_ERROR = "assumption_error"
    TIMEOUT = "timeout"


@dataclass
class InteractionTurn:
    """Represents a single prompt-response turn in an agent-user session."""
    turn_id: int
    timestamp: str
    user_prompt_summary: str
    perceived_intent: str
    was_followup: bool = False
    one_shot_success: bool = True
    missing_context_or_gap: Optional[str] = None
    target_skill: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InteractionTurn":
        return cls(
            turn_id=data["turn_id"],
            timestamp=data["timestamp"],
            user_prompt_summary=data["user_prompt_summary"],
            perceived_intent=data["perceived_intent"],
            was_followup=data.get("was_followup", False),
            one_shot_success=data.get("one_shot_success", True),
            missing_context_or_gap=data.get("missing_context_or_gap"),
            target_skill=data.get("target_skill"),
        )


@dataclass
class UserPreference:
    """Represents an inferred or explicitly stated user preference/convention."""
    preference_id: str
    category: PreferenceCategory
    rule: str
    context_or_example: str
    confidence: float  # 0.0 to 1.0
    created_at: str
    times_reinforced: int = 1
    active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserPreference":
        return cls(
            preference_id=data["preference_id"],
            category=PreferenceCategory(data["category"]),
            rule=data["rule"],
            context_or_example=data["context_or_example"],
            confidence=float(data.get("confidence", 1.0)),
            created_at=data["created_at"],
            times_reinforced=int(data.get("times_reinforced", 1)),
            active=data.get("active", True),
        )


@dataclass
class TrajectoryContrast:
    """
    Captures contrastive trajectories (ExpeL / SICA pattern).
    Compares initial attempt vs user follow-up to extract the exact latent preference gap.
    """
    contrast_id: str
    timestamp: str
    initial_output_summary: str
    user_correction: str
    corrected_output_summary: str
    key_delta: str
    inferred_preference_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrajectoryContrast":
        return cls(
            contrast_id=data["contrast_id"],
            timestamp=data["timestamp"],
            initial_output_summary=data["initial_output_summary"],
            user_correction=data["user_correction"],
            corrected_output_summary=data["corrected_output_summary"],
            key_delta=data["key_delta"],
            inferred_preference_id=data.get("inferred_preference_id"),
        )


@dataclass
class VerificationGate:
    """
    Code Judge / Deterministic Verification Gate.
    Ensures no patch or rule modification is promoted without passing an executable test.
    """
    gate_id: str
    target_type: str  # "rca_patch", "skill_rule", "code_change"
    test_command: str
    passed: bool
    output_snippet: str
    executed_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VerificationGate":
        return cls(
            gate_id=data["gate_id"],
            target_type=data["target_type"],
            test_command=data["test_command"],
            passed=bool(data["passed"]),
            output_snippet=data.get("output_snippet", ""),
            executed_at=data["executed_at"],
        )


@dataclass
class MistakeRCA:
    """
    Root Cause Analysis (RCA) record for an error or failed attempt.
    Follows 5-Whys methodology to guarantee permanent resolution.
    """
    rca_id: str
    timestamp: str
    category: MistakeCategory
    symptom: str
    mechanism: str
    root_cause: str
    patch_description: str
    preventative_rule: str
    regression_test_file: Optional[str] = None
    verification_gate_id: Optional[str] = None
    status: str = "resolved"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MistakeRCA":
        return cls(
            rca_id=data["rca_id"],
            timestamp=data["timestamp"],
            category=MistakeCategory(data["category"]),
            symptom=data["symptom"],
            mechanism=data["mechanism"],
            root_cause=data["root_cause"],
            patch_description=data["patch_description"],
            preventative_rule=data["preventative_rule"],
            regression_test_file=data.get("regression_test_file"),
            verification_gate_id=data.get("verification_gate_id"),
            status=data.get("status", "resolved"),
        )


@dataclass
class AnalogousTransfer:
    """Cross-domain knowledge extrapolation from one module/context to others."""
    transfer_id: str
    source_domain: str
    target_domains: List[str]
    specific_lesson: str
    generalized_principle: str
    applied_actions: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AnalogousTransfer":
        return cls(
            transfer_id=data["transfer_id"],
            source_domain=data["source_domain"],
            target_domains=data.get("target_domains", []),
            specific_lesson=data["specific_lesson"],
            generalized_principle=data["generalized_principle"],
            applied_actions=data.get("applied_actions", []),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
        )


@dataclass
class PolicyDebtReport:
    """Report on duplicate, contradictory, or obsolete rules pruned by System 2."""
    redundant_count: int
    pruned_ids: List[str]
    consolidated_count: int
    active_clean_count: int


@dataclass
class LearningLedger:
    """Complete persistent learning ledger containing turns, preferences, RCAs, and transfers."""
    session_id: str
    prompt_count: int = 0
    turns: List[InteractionTurn] = field(default_factory=list)
    preferences: List[UserPreference] = field(default_factory=list)
    mistakes: List[MistakeRCA] = field(default_factory=list)
    transfers: List[AnalogousTransfer] = field(default_factory=list)
    contrasts: List[TrajectoryContrast] = field(default_factory=list)
    verifications: List[VerificationGate] = field(default_factory=list)
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "prompt_count": self.prompt_count,
            "turns": [t.to_dict() for t in self.turns],
            "preferences": [p.to_dict() for p in self.preferences],
            "mistakes": [m.to_dict() for m in self.mistakes],
            "transfers": [x.to_dict() for x in self.transfers],
            "contrasts": [c.to_dict() for c in self.contrasts],
            "verifications": [v.to_dict() for v in self.verifications],
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LearningLedger":
        return cls(
            session_id=data["session_id"],
            prompt_count=data.get("prompt_count", 0),
            turns=[InteractionTurn.from_dict(t) for t in data.get("turns", [])],
            preferences=[UserPreference.from_dict(p) for p in data.get("preferences", [])],
            mistakes=[MistakeRCA.from_dict(m) for m in data.get("mistakes", [])],
            transfers=[AnalogousTransfer.from_dict(x) for x in data.get("transfers", [])],
            contrasts=[TrajectoryContrast.from_dict(c) for c in data.get("contrasts", [])],
            verifications=[VerificationGate.from_dict(v) for v in data.get("verifications", [])],
            last_updated=data.get("last_updated", datetime.now(timezone.utc).isoformat()),
        )
