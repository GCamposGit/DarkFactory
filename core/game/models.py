"""Typed contracts for the Echo Garden domain."""

from enum import Enum
from typing import Dict, List, Literal, Optional, Tuple
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


EnergyTriple = Tuple[int, int, int]


class GameMove(str, Enum):
    """Player actions. Each action strengthens one channel and drains another."""

    WEAVE = "weave"
    ECHO = "echo"
    GROUND = "ground"


class GameStatus(str, Enum):
    """Terminal state of a game."""

    RUNNING = "running"
    WON = "won"
    LOST = "lost"


class GameState(BaseModel):
    """Complete serializable state for one deterministic game."""

    model_config = ConfigDict(frozen=True)

    seed: int = 0
    turn: int = Field(default=0, ge=0, le=6)
    energies: EnergyTriple
    pending_echo: Optional[GameMove] = None
    history: Tuple[GameMove, ...] = ()
    status: GameStatus = GameStatus.RUNNING

    @field_validator("energies")
    @classmethod
    def validate_energies(cls, value: EnergyTriple) -> EnergyTriple:
        if any(level < 0 or level > 8 for level in value):
            raise ValueError("energy levels must stay between 0 and 8")
        return value


class TurnTrace(BaseModel):
    """Observable transition evidence for tests, CLI, and generated clients."""

    before: GameState
    move: GameMove
    direct_delta: EnergyTriple
    echo_delta: EnergyTriple
    after: GameState
    spread: int = Field(ge=0)
    score: int


class SequenceResult(BaseModel):
    """Result of replaying a bounded sequence."""

    initial: GameState
    traces: List[TurnTrace]
    final: GameState


class LocalGameContribution(BaseModel):
    """Low-tier naming and presentation configuration."""

    tagline: str = Field(min_length=8, max_length=90)
    move_labels: Dict[GameMove, str]


class StrategyContribution(BaseModel):
    """Balanced-tier candidate solution, verified by the domain engine."""

    moves: List[GameMove] = Field(min_length=3, max_length=6)
    rationale: str = Field(min_length=8, max_length=240)


class ReviewContribution(BaseModel):
    """Frontier-tier independent review verdict."""

    verdict: Literal["APPROVE", "REJECT"]
    risks_checked: List[str] = Field(min_length=3, max_length=6)
    summary: str = Field(min_length=8, max_length=300)


class ModelEvidence(BaseModel):
    """Auditable evidence for one real model call."""

    tier: Literal["local_fast", "balanced_cloud", "frontier_cloud"]
    provider: Literal["ollama", "openrouter"]
    requested_model: str
    returned_model: str
    response_sha256: str
    duration_ms: float = Field(ge=0)
    tokens_used: Optional[int] = Field(default=None, ge=0)
    cost_usd: Optional[float] = Field(default=None, ge=0)
    validation: Literal["passed"] = "passed"


class AssetEvidence(BaseModel):
    """Hash-bound proof for one generated visual artifact."""

    tier: Literal["local_procedural", "fast_cloud", "frontier_cloud"]
    provider: str
    requested_model: str
    returned_model: str
    relative_path: str
    sha256: str
    size_bytes: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    cost_usd: float = Field(default=0.0, ge=0)


class GameManifest(BaseModel):
    """Complete portable result of the one-shot Dark Factory run."""

    game_id: Literal["echo-garden"] = "echo-garden"
    title: Literal["Echo Garden"] = "Echo Garden"
    seed: int = 0
    local_contribution: LocalGameContribution
    strategy_contribution: StrategyContribution
    review_contribution: ReviewContribution
    model_evidence: List[ModelEvidence] = Field(min_length=3, max_length=3)
    assets: List[AssetEvidence] = Field(min_length=1)
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

