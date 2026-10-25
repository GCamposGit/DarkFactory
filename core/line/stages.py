"""Canonical stage sequence and per-stage artifact contract (USR-375 S2).

Every stage of the line leaves one artifact under `.darkfac/runs/<run_id>/`
(`workspace.context_dir`). `require_artifact(stage, ws)` loads and validates it, so
a later stage (or the runner, from slice S4) can refuse to proceed without proof
that the previous one really finished. Not wired into `run_ticket` yet.

RESEARCH, DOCS and LEARNING are optional placeholders (USR-376): they return None
instead of raising until their artifacts are defined.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from core.line import workspace
from core.line.stage_planning import PlanningPlan, PlanningSpec, PlanningTicket
from core.line.stage_release import DEPLOY_REPORT_FILE, DeployReport
from core.line.stage_review import ReviewVerdict


class Stage(str, Enum):
    INTAKE = "INTAKE"
    GRILL = "GRILL"
    PLAN = "PLAN"
    RESEARCH = "RESEARCH"
    BUILD = "BUILD"
    VALIDATE = "VALIDATE"
    REVIEW = "REVIEW"
    MERGE = "MERGE"
    DEPLOY_SMOKE = "DEPLOY_SMOKE"
    DOCS = "DOCS"
    LEARNING = "LEARNING"


STAGE_SEQUENCE: tuple[Stage, ...] = tuple(Stage)
OPTIONAL_STAGES: frozenset[Stage] = frozenset({Stage.RESEARCH, Stage.DOCS, Stage.LEARNING})

BUILD_FILE = "progress.json"
VALIDATION_FILE = "validation.json"
VERDICT_FILE = "review_verdict.json"
MERGE_FILE = "merge.json"


class StageArtifactMissing(RuntimeError):
    def __init__(self, stage: Stage, reason: str) -> None:
        super().__init__(f"{stage.value}: {reason}")
        self.stage = stage
        self.reason = reason


class TextArtifact(BaseModel):
    name: str
    content: str = Field(min_length=1)


class BuildReport(BaseModel):
    """`progress.json` written by BUILD."""

    harness: Optional[str] = None
    tickets_done: list[str] = Field(min_length=1)


class ValidationReport(BaseModel):
    """`validation.json` written by VALIDATE."""

    passed: bool
    gate: str = ""
    detail: str = ""


class MergeReceipt(BaseModel):
    """`merge.json` written by MERGE."""

    pr_url: str = Field(min_length=1)
    merge_sha: str = Field(min_length=1)


def _read(stage: Stage, directory: Path, name: str) -> str:
    path = directory / name
    if not path.is_file():
        raise StageArtifactMissing(stage, f"artefato ausente: {name}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise StageArtifactMissing(stage, f"artefato vazio: {name}")
    return text


def _json(stage: Stage, directory: Path, name: str, cls: type) -> BaseModel:
    text = _read(stage, directory, name)
    try:
        return cls.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise StageArtifactMissing(stage, f"artefato invalido: {name}: {exc}") from exc


def _text(name: str) -> Callable[[Stage, Path], BaseModel]:
    return lambda stage, d: TextArtifact(name=name, content=_read(stage, d, name))


def _plan(stage: Stage, directory: Path) -> BaseModel:
    spec = _read(stage, directory, "SPEC.md")
    raw = _read(stage, directory, "tickets.json")
    try:
        tickets = [PlanningTicket.model_validate(t) for t in json.loads(raw)]
        return PlanningPlan(spec=PlanningSpec(objective=spec), tickets=tickets)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise StageArtifactMissing(stage, f"artefato invalido: tickets.json: {exc}") from exc


def _validated(stage: Stage, directory: Path) -> BaseModel:
    report = _json(stage, directory, VALIDATION_FILE, ValidationReport)
    if not report.passed:  # type: ignore[attr-defined]
        raise StageArtifactMissing(stage, "portao nao passou")
    return report


def _reviewed(stage: Stage, directory: Path) -> BaseModel:
    verdict = _json(stage, directory, VERDICT_FILE, ReviewVerdict)
    if verdict.verdict != "approve" or verdict.blocking:  # type: ignore[attr-defined]
        raise StageArtifactMissing(stage, "veredito nao aprovado")
    return verdict


def _deployed(stage: Stage, directory: Path) -> BaseModel:
    report = _json(stage, directory, DEPLOY_REPORT_FILE, DeployReport)
    if not report.smoke_ok:  # type: ignore[attr-defined]
        raise StageArtifactMissing(stage, "smoke falhou")
    return report


_LOADERS: dict[Stage, Callable[[Stage, Path], BaseModel]] = {
    Stage.INTAKE: _text("DEMAND.md"),
    Stage.GRILL: _text("GRILL.md"),
    Stage.PLAN: _plan,
    Stage.BUILD: lambda s, d: _json(s, d, BUILD_FILE, BuildReport),
    Stage.VALIDATE: _validated,
    Stage.REVIEW: _reviewed,
    Stage.MERGE: lambda s, d: _json(s, d, MERGE_FILE, MergeReceipt),
    Stage.DEPLOY_SMOKE: _deployed,
}


def require_artifact(stage: Stage, ws: workspace.RunWorkspace) -> Optional[BaseModel]:
    """Load and validate the artifact of `stage`; raise `StageArtifactMissing` if absent/invalid.

    Optional stages (RESEARCH, DOCS, LEARNING) are skipped (None) until USR-376.
    """
    stage = Stage(stage)
    if stage in OPTIONAL_STAGES:
        return None
    return _LOADERS[stage](stage, workspace.context_dir(ws))
