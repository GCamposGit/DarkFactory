"""
Domain models for DarkFac Session Learning Pack & Cognitive Uplift Engine.
Strictly typed for compatibility with Universal Engineering Standards (AGENTS.md).
Grounded in Sweller's Cognitive Load Theory and the Feynman Multi-Tier Protocol.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone


class ConceptCategory(str, Enum):
    ARCHITECTURE = "Architecture"
    ALGORITHMS = "Algorithms & Data Structures"
    CONCURRENCY = "Concurrency & Async"
    RELIABILITY = "Reliability & Testing"
    AI_ORCHESTRATION = "AI & LLM Orchestration"
    SECURITY = "Security & Governance"
    SYSTEM_DESIGN = "System Design & Scaling"


@dataclass
class ExplanationTier:
    """Multi-tiered explanation protocol grounded in the Feynman Technique."""
    pitch_30s: str          # Level 1: Layman / Stakeholder / Client (Value, zero jargon)
    staff_architect: str    # Level 2: Staff+ Engineer (Trade-offs, invariants, non-functional guarantees)
    under_the_hood: str     # Level 3: Engine Room (Mechanics, algorithms, data structures)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExplanationTier":
        return cls(
            pitch_30s=data.get("pitch_30s", ""),
            staff_architect=data.get("staff_architect", ""),
            under_the_hood=data.get("under_the_hood", ""),
        )


@dataclass
class DefenseQA:
    """Skeptic defense point: anticipated tough questions from reviewers or CTOs and bulletproof responses."""
    question: str
    bulletproof_answer: str
    context: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DefenseQA":
        return cls(
            question=data.get("question", ""),
            bulletproof_answer=data.get("bulletproof_answer", ""),
            context=data.get("context"),
        )


@dataclass
class TradeOffOption:
    """Structured architectural trade-off comparison."""
    option: str
    pros: str
    cons: str
    why_chosen: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeOffOption":
        return cls(
            option=data.get("option", ""),
            pros=data.get("pros", ""),
            cons=data.get("cons", ""),
            why_chosen=data.get("why_chosen", ""),
        )


@dataclass
class LearningConcept:
    """A single technical concept or architectural pattern learned/applied in a session."""
    concept_id: str
    name: str
    category: ConceptCategory
    mental_anchor: str                  # Sticky real-world analogy
    tiers: ExplanationTier
    trade_offs: List[TradeOffOption] = field(default_factory=list)
    defense: List[DefenseQA] = field(default_factory=list)
    code_anchor: Optional[str] = None   # File/function reference

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "name": self.name,
            "category": self.category.value if isinstance(self.category, ConceptCategory) else str(self.category),
            "mental_anchor": self.mental_anchor,
            "tiers": self.tiers.to_dict(),
            "trade_offs": [t.to_dict() for t in self.trade_offs],
            "defense": [d.to_dict() for d in self.defense],
            "code_anchor": self.code_anchor,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LearningConcept":
        raw_cat = data.get("category", ConceptCategory.ARCHITECTURE.value)
        try:
            category = ConceptCategory(raw_cat)
        except ValueError:
            category = ConceptCategory.ARCHITECTURE

        return cls(
            concept_id=data["concept_id"],
            name=data["name"],
            category=category,
            mental_anchor=data.get("mental_anchor", ""),
            tiers=ExplanationTier.from_dict(data.get("tiers", {})),
            trade_offs=[TradeOffOption.from_dict(t) for t in data.get("trade_offs", [])],
            defense=[DefenseQA.from_dict(d) for d in data.get("defense", [])],
            code_anchor=data.get("code_anchor"),
        )


@dataclass
class ActiveRecallCard:
    """High-yield flashcard for spaced repetition and rapid self-quizzing."""
    card_id: str
    concept_id: str
    front_prompt: str       # Question, scenario, or fill-in-the-blank
    back_solution: str      # Clear, crisp technical explanation
    why_it_matters: str     # Germane schema reinforcement
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ActiveRecallCard":
        return cls(
            card_id=data["card_id"],
            concept_id=data["concept_id"],
            front_prompt=data["front_prompt"],
            back_solution=data["back_solution"],
            why_it_matters=data.get("why_it_matters", ""),
            tags=data.get("tags", []),
        )


@dataclass
class SessionLearningPack:
    """The master session synthesis pack combining concepts, defense shields, and active recall cards."""
    pack_id: str
    session_id: str
    timestamp: str
    title: str
    executive_summary: str
    concepts: List[LearningConcept] = field(default_factory=list)
    flashcards: List[ActiveRecallCard] = field(default_factory=list)
    files_analyzed: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    discussion_topics: List[str] = field(default_factory=list)
    reading_is_optional: bool = True
    blocks_production: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "title": self.title,
            "executive_summary": self.executive_summary,
            "concepts": [c.to_dict() for c in self.concepts],
            "flashcards": [f.to_dict() for f in self.flashcards],
            "files_analyzed": self.files_analyzed,
            "metrics": self.metrics,
            "discussion_topics": list(self.discussion_topics),
            "reading_is_optional": self.reading_is_optional,
            "blocks_production": self.blocks_production,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionLearningPack":
        return cls(
            pack_id=data["pack_id"],
            session_id=data.get("session_id", "session_default"),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            title=data.get("title", "Session Technical Learning Pack"),
            executive_summary=data.get("executive_summary", ""),
            concepts=[LearningConcept.from_dict(c) for c in data.get("concepts", [])],
            flashcards=[ActiveRecallCard.from_dict(f) for f in data.get("flashcards", [])],
            files_analyzed=data.get("files_analyzed", []),
            metrics=data.get("metrics", {}),
            discussion_topics=list(data.get("discussion_topics", [])),
            reading_is_optional=data.get("reading_is_optional", True),
            blocks_production=data.get("blocks_production", False),
        )
