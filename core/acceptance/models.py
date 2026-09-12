"""Data models for HF-15 Acceptance, Environment, Rollback, and Observability.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 5, 11, line 267 / HF-15)
- HYBRID_AUTONOMY_REQUIREMENTS (Sections 5, 7, Scenarios G1-G8)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class HF15Scenario(str, Enum):
    """Enumerate the eight mandatory acceptance scenarios (G1-G8) for HF-15."""

    G1 = "G1"  # Demanda ambígua vs clara (Grill)
    G2 = "G2"  # Chaves de API ausentes com/sem substitutos (Fail-closed)
    G3 = "G3"  # Bloqueio de prontidão por erro em worker real
    G4 = "G4"  # 9 slots: 4 dev + 5 testes independentes concorrentes
    G5 = "G5"  # Deduplicação, evento perdido e reinício resiliente
    G6 = "G6"  # Cota de assinatura esgotada e roteamento de Pareto
    G7 = "G7"  # Proveniência de pesquisa, memória durável e Learning Pack
    G8 = "G8"  # Aceite formal do owner, prova operacional e rollback isolado


class ScenarioStatus(str, Enum):
    """Lifecycle status of a scenario during acceptance execution."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class HF15EnvironmentConfig(BaseModel):
    """Configuration defining the isolated acceptance runtime environment."""

    model_config = ConfigDict(extra="forbid")

    sandbox_root: Path = Field(
        default_factory=lambda: Path(".factory/hf15/workspace").resolve()
    )
    db_type: str = "sqlite_sandbox"  # 'sqlite_sandbox' or 'postgres'
    db_url: Optional[str] = None
    n8n_url: str = "https://n8n.ggcampos.com"
    n8n_api_key: Optional[str] = None
    n8n_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_authorized_users: List[int] = Field(default_factory=list)
    worker_slots: int = Field(default=9, ge=1, le=32)
    coordinator_url: Optional[str] = None
    live_mode: bool = False


class HF15PreflightCheck(BaseModel):
    """Audit record for a single environment preflight verification step."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    passed: bool
    details: str = ""
    error: Optional[str] = None


class HF15PreflightReport(BaseModel):
    """Consolidated preflight report verifying environment readiness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: List[HF15PreflightCheck]
    all_passed: bool
    environment_mode: str = "sandbox"


class ScenarioDataFixture(BaseModel):
    """Deterministic test dataset for a specific scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: HF15Scenario
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = Field(min_length=1)
    tags: List[str] = Field(default_factory=list)


class RollbackExecutionRecord(BaseModel):
    """Auditable evidence of a rollback execution during acceptance testing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rollback_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    trigger_reason: str = Field(min_length=1)
    pre_rollback_digest: str = Field(min_length=1)
    post_rollback_digest: Optional[str] = None
    rpo_seconds: float = Field(ge=0.0)
    rto_seconds: float = Field(ge=0.0)
    success: bool
    evidence_hash: str = Field(min_length=64, max_length=64)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AcceptanceTelemetryEvent(BaseModel):
    """Structured audit log entry emitted during acceptance execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    duration_ms: float = Field(ge=0.0)
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HF15MetricsSummary(BaseModel):
    """Consolidated metrics summary of the HF-15 acceptance run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_scenarios: int = 8
    passed_scenarios: int = 0
    failed_scenarios: int = 0
    rolled_back_scenarios: int = 0
    avg_dispatch_latency_ms: float = 0.0
    avg_reconciliation_latency_ms: float = 0.0
    avg_rto_seconds: float = 0.0
    max_active_slots_used: int = 0
    total_budget_spent_usd: float = 0.0
    all_slas_met: bool = False
