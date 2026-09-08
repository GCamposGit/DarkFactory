"""Typed contracts and Pydantic models for the User Demands module."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.roadmap.models import (
    DeliveryStatus,
    LifecycleStage,
    PlanningHorizon,
    RoadmapItemType,
    utc_now,
)

TAG_USER_DEMAND = "user-demand"
TAG_CODE_REVIEW = "code-review"
TAG_AGENT_FEATURE = "agent-feature"


class DemandOrigin(str, Enum):
    USER = "user"
    REVIEW = "review"
    AGENT = "agent"


class DemandInput(BaseModel):
    """Initial input provided by the user when submitting a new demand."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(default="darkfac", min_length=1)
    title: str = Field(..., min_length=3, max_length=200)
    problem_statement: str = Field(default="", description="The real user pain or problem to solve")
    core_journey: str = Field(default="", description="Observable user steps or desired journey")
    non_goals: list[str] = Field(default_factory=list, description="Explicit out-of-scope boundaries")
    acceptance_criteria: list[str] = Field(default_factory=list, description="Measurable criteria or tests")
    horizon: PlanningHorizon = Field(default=PlanningHorizon.NOW)
    item_type: RoadmapItemType = Field(default=RoadmapItemType.FEATURE)
    extra_tags: list[str] = Field(default_factory=list)
    suggested_files: list[str] = Field(default_factory=list)
    reachability_contract: str = Field(default="", description="Command or interface to test headlessly")


class UserTicket(BaseModel):
    """Canonical representation of a specified user demand in the development backlog."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, description="Deterministic ticket identifier (e.g. USR-01)")
    project_id: str = Field(default="darkfac", min_length=1)
    title: str = Field(..., min_length=3)
    origin: DemandOrigin = Field(default=DemandOrigin.USER)
    status: DeliveryStatus = Field(default=DeliveryStatus.PLANNED)
    item_type: RoadmapItemType = Field(default=RoadmapItemType.FEATURE)
    lifecycle_stage: LifecycleStage = Field(default=LifecycleStage.EXECUTION)
    horizon: PlanningHorizon = Field(default=PlanningHorizon.NOW)
    tags: list[str] = Field(default_factory=lambda: [TAG_USER_DEMAND], validate_default=True)

    problem_statement: str = Field(default="")
    core_journey: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    reachability_contract: str = Field(default="")
    acceptance_criteria: list[str] = Field(default_factory=list)
    suggested_files: list[str] = Field(default_factory=list)
    estimated_complexity: str = Field(default="medium")
    dependencies: list[str] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("tags")
    @classmethod
    def ensure_user_tag(cls, tags: list[str]) -> list[str]:
        cleaned = [t.strip() for t in tags if t.strip()]
        if TAG_USER_DEMAND not in cleaned:
            cleaned.insert(0, TAG_USER_DEMAND)
        return cleaned


class DemandSpecificationGuidance(BaseModel):
    """Feedback and auto-generated refinement proposals for a user demand."""

    model_config = ConfigDict(extra="forbid")

    is_ready: bool = Field(..., description="Whether the demand meets minimum execution standards")
    readiness_score: int = Field(..., ge=0, le=100, description="Readiness score from 0 to 100")
    missing_elements: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    suggested_ticket: UserTicket | None = Field(default=None)
    engine_used: str = Field(..., description="Engine that generated guidance (e.g. ollama:qwen-fast or heuristic_script)")
    cost_usd: float = Field(default=0.0, description="Cost of the guidance in USD ($0.00 guaranteed)")


class GrillQuestionOption(BaseModel):
    """A concrete selectable alternative for a clarifying question."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique option identifier (e.g. opt_1)")
    label: str = Field(..., description="User-friendly text of the option")
    is_recommended: bool = Field(default=False, description="Whether this is the recommended Staff+ architecture default")
    description: str | None = Field(default=None, description="Additional context or rationale for this option")


class GrillQuestion(BaseModel):
    """An essential, surgical question aimed at clarifying scope, non-goals, or reachability."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="Unique question identifier (e.g. q_scope_1)")
    question: str = Field(..., description="The direct question text")
    context_reason: str = Field(default="", description="Why this question is necessary to prevent rework")
    category: str = Field(default="scope", description="Category: scope, non_goals, architecture, or validation")
    options: list[GrillQuestionOption] = Field(default_factory=list, description="Pre-filled probable options")
    allow_custom_input: bool = Field(default=True, description="Whether the user can write a custom answer")


class GrillSession(BaseModel):
    """Full session state for a demand clarification grill."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(..., description="Target ticket ID (e.g. USR-09)")
    project_id: str = Field(default="darkfac")
    status: str = Field(default="pending", description="Session status: pending, answered, skipped, refined")
    doc_insights: list[str] = Field(default_factory=list, description="High-signal insights gathered from project docs")
    questions: list[GrillQuestion] = Field(default_factory=list, description="2 to 4 essential clarifying questions")
    answers: dict[str, str] = Field(default_factory=dict, description="Answers supplied or auto-accepted")
    engine_used: str = Field(default="heuristic_script")
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = Field(default=None)


class GrillAnswersPayload(BaseModel):
    """Answers submitted by user or client for a grill session."""

    model_config = ConfigDict(extra="forbid")

    answers: dict[str, str] = Field(..., description="Mapping of question_id to chosen option label or custom text")
    auto_accept_unanswered: bool = Field(default=True, description="Whether to adopt recommended options for unanswered questions")


class GrillRefinementResult(BaseModel):
    """Outcome of refining a ticket through grill answers."""

    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    original_title: str
    refined_ticket: UserTicket
    applied_answers: dict[str, str]
    summary_of_changes: list[str]
