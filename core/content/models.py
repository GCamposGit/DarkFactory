"""
Domain models for DarkFac Anti-AI-Slop Content Engine.
Strictly typed for compatibility with Universal Engineering Standards (AGENTS.md).
Defines content types, persona profiles, slop classification, and lint reports.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
import uuid


class ContentType(str, Enum):
    LINKEDIN_POST = "linkedin_post"
    TECHNICAL_BLOG = "technical_blog"
    COMMERCIAL_PROPOSAL = "commercial_proposal"
    RELEASE_NOTES = "release_notes"
    EXECUTIVE_MEMO = "executive_memo"
    THOUGHT_LEADERSHIP = "thought_leadership"
    DOCUMENTATION = "documentation"


class SlopCategory(str, Enum):
    BUZZWORD = "buzzword"
    HOLLOW_SUPERLATIVE = "hollow_superlative"
    FORMULAIC_OPENER = "formulaic_opener"
    ROBOTIC_TRANSITION = "robotic_transition"
    CADENCE_MONOTONY = "cadence_monotony"
    HEDGE_FILLER = "hedge_filler"
    EM_DASH_ABUSE = "em_dash_abuse"
    FORCED_RHETORICAL = "forced_rhetorical"
    EMPTY_CONCLUSION = "empty_conclusion"


class SeverityLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CleanlinessRating(str, Enum):
    PRISTINE = "Pristine (Human-Grade Voice)"
    CLEAN = "Clean (High Signal)"
    MILD_SLOP = "Mild Slop (Minor Generic Patterns)"
    HIGH_SLOP = "High Slop (Noticeable AI Artifacts)"
    TOXIC_SLOP = "Toxic Slop (Unusable AI Cliché Mash)"


@dataclass
class ToneProfile:
    """Configurable tone and style profile to eliminate generic AI output."""
    formality: int = 3           # 1 (casual/raw) to 5 (rigorous/formal)
    brevity: int = 4             # 1 (expansive/narrative) to 5 (ultra-concise/terse)
    technical_depth: int = 4     # 1 (layman overview) to 5 (staff+ kernel level)
    contrarianism: int = 2       # 1 (diplomatic consensus) to 5 (spicy contrarian take)
    bullet_density: str = "moderate"  # none, sparse, moderate, high
    hook_style: str = "pattern_interrupt"  # pattern_interrupt, data_first, bold_statement, story
    call_to_action: bool = True
    custom_voice_sample: Optional[str] = None
    banned_words: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToneProfile":
        return cls(
            formality=data.get("formality", 3),
            brevity=data.get("brevity", 4),
            technical_depth=data.get("technical_depth", 4),
            contrarianism=data.get("contrarianism", 2),
            bullet_density=data.get("bullet_density", "moderate"),
            hook_style=data.get("hook_style", "pattern_interrupt"),
            call_to_action=data.get("call_to_action", True),
            custom_voice_sample=data.get("custom_voice_sample"),
            banned_words=data.get("banned_words", []),
        )


@dataclass
class SlopViolation:
    """Individual violation identified by the Anti-Slop Linter."""
    term: str
    category: SlopCategory
    severity: SeverityLevel
    context: str
    line_number: int
    suggestion: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "term": self.term,
            "category": self.category.value,
            "severity": self.severity.value,
            "context": self.context,
            "line_number": self.line_number,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlopViolation":
        return cls(
            term=data["term"],
            category=SlopCategory(data["category"]),
            severity=SeverityLevel(data["severity"]),
            context=data["context"],
            line_number=data["line_number"],
            suggestion=data["suggestion"],
        )


@dataclass
class SlopReport:
    """Full analytical audit produced by AntiSlopLinter."""
    slop_score: float             # 0.0 (pristine) to 100.0 (toxic slop)
    cleanliness_rating: CleanlinessRating
    violations_count: int
    violations: List[SlopViolation]
    sentence_count: int
    average_sentence_length: float
    sentence_length_variance: float
    cadence_rating: str           # "Punchy & Dynamic", "Balanced", "Monotonous"
    passive_voice_count: int
    top_fixes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slop_score": round(self.slop_score, 1),
            "cleanliness_rating": self.cleanliness_rating.value,
            "violations_count": self.violations_count,
            "violations": [v.to_dict() for v in self.violations],
            "sentence_count": self.sentence_count,
            "average_sentence_length": round(self.average_sentence_length, 1),
            "sentence_length_variance": round(self.sentence_length_variance, 1),
            "cadence_rating": self.cadence_rating,
            "passive_voice_count": self.passive_voice_count,
            "top_fixes": self.top_fixes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SlopReport":
        return cls(
            slop_score=data.get("slop_score", 0.0),
            cleanliness_rating=CleanlinessRating(data.get("cleanliness_rating", CleanlinessRating.CLEAN.value)),
            violations_count=data.get("violations_count", 0),
            violations=[SlopViolation.from_dict(v) for v in data.get("violations", [])],
            sentence_count=data.get("sentence_count", 0),
            average_sentence_length=data.get("average_sentence_length", 0.0),
            sentence_length_variance=data.get("sentence_length_variance", 0.0),
            cadence_rating=data.get("cadence_rating", "Balanced"),
            passive_voice_count=data.get("passive_voice_count", 0),
            top_fixes=data.get("top_fixes", []),
        )


@dataclass
class ContentRequest:
    """Request payload for anti-slop content synthesis."""
    topic: str
    content_type: ContentType = ContentType.LINKEDIN_POST
    target_audience: str = "software engineers, technical leaders, CTOs"
    key_points: List[str] = field(default_factory=list)
    raw_context: Optional[str] = None
    tone_profile: Optional[ToneProfile] = None
    max_length_words: Optional[int] = None
    max_slop_threshold: float = 15.0
    offline: bool = False
    model_override: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "content_type": self.content_type.value,
            "target_audience": self.target_audience,
            "key_points": self.key_points,
            "raw_context": self.raw_context,
            "tone_profile": self.tone_profile.to_dict() if self.tone_profile else None,
            "max_length_words": self.max_length_words,
            "max_slop_threshold": self.max_slop_threshold,
            "offline": self.offline,
            "model_override": self.model_override,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContentRequest":
        return cls(
            topic=data.get("topic", ""),
            content_type=ContentType(data.get("content_type", ContentType.LINKEDIN_POST.value)),
            target_audience=data.get("target_audience", "software engineers"),
            key_points=data.get("key_points", []),
            raw_context=data.get("raw_context"),
            tone_profile=ToneProfile.from_dict(data["tone_profile"]) if data.get("tone_profile") else None,
            max_length_words=data.get("max_length_words"),
            max_slop_threshold=data.get("max_slop_threshold", 15.0),
            offline=data.get("offline", False),
            model_override=data.get("model_override"),
        )


@dataclass
class ContentResponse:
    """Finished content artifact with generation metrics and quality score."""
    content_id: str
    title: str
    content_type: ContentType
    final_content: str
    initial_draft: Optional[str]
    initial_slop_score: float
    final_slop_score: float
    slop_report: SlopReport
    scrubbed: bool
    iterations_count: int
    provider: str
    model_used: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    word_count: int = 0
    quality_rejected: bool = False
    rejection_reason: Optional[str] = None
    fallback_occurred: bool = False
    original_provider_requested: Optional[str] = None
    fallback_reason: Optional[str] = None
    cost_usd: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "content_id": self.content_id,
            "title": self.title,
            "content_type": self.content_type.value,
            "final_content": self.final_content,
            "initial_draft": self.initial_draft,
            "initial_slop_score": round(self.initial_slop_score, 1),
            "final_slop_score": round(self.final_slop_score, 1),
            "slop_report": self.slop_report.to_dict(),
            "scrubbed": self.scrubbed,
            "iterations_count": self.iterations_count,
            "provider": self.provider,
            "model_used": self.model_used,
            "created_at": self.created_at,
            "word_count": self.word_count,
            "quality_rejected": self.quality_rejected,
            "rejection_reason": self.rejection_reason,
            "fallback_occurred": self.fallback_occurred,
            "original_provider_requested": self.original_provider_requested,
            "fallback_reason": self.fallback_reason,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContentResponse":
        return cls(
            content_id=data.get("content_id", str(uuid.uuid4())[:8]),
            title=data.get("title", ""),
            content_type=ContentType(data.get("content_type", ContentType.LINKEDIN_POST.value)),
            final_content=data.get("final_content", ""),
            initial_draft=data.get("initial_draft"),
            initial_slop_score=data.get("initial_slop_score", 0.0),
            final_slop_score=data.get("final_slop_score", 0.0),
            slop_report=SlopReport.from_dict(data["slop_report"]),
            scrubbed=data.get("scrubbed", False),
            iterations_count=data.get("iterations_count", 1),
            provider=data.get("provider", "local"),
            model_used=data.get("model_used", "deterministic"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            word_count=data.get("word_count", 0),
            quality_rejected=data.get("quality_rejected", False),
            rejection_reason=data.get("rejection_reason"),
            fallback_occurred=data.get("fallback_occurred", False),
            original_provider_requested=data.get("original_provider_requested"),
            fallback_reason=data.get("fallback_reason"),
            cost_usd=data.get("cost_usd"),
        )
