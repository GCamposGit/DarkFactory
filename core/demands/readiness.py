"""Canonical G1 readiness evaluation (USR-305).

Single source of truth shared by the Grill service, the backlog feeder and the
live executable-queue projection, so eligible counts always agree.

Missing non-goals / acceptance criteria are *documentary gaps*: they still block
admission (G1 is not weakened) but are classified separately from a missing
human decision, and are never auto-filled with invented content.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

# Placeholders must be explicit tokens: "TODO"/"TBD" never match inside words such as
# "todos", "todas" or "metodo". Lowercase "todo" is a plain Portuguese word and only
# counts when written as a marker ("todo:", "[todo]").
_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:TODO|TBD)\b"),
    re.compile(r"\b(?:todo|tbd)\s*[:\]\)]", re.IGNORECASE),
    re.compile(r"\[\s*(?:todo|tbd|preencher|definir|fill\s*in)[^\]]*\]", re.IGNORECASE),
    re.compile(r"\ba\s+definir\b", re.IGNORECASE),
    re.compile(r"\bnão\s+sei\b", re.IGNORECASE),
    re.compile(r"\bambíguo\b", re.IGNORECASE),
    re.compile(r"\?\?\?"),
)

READINESS_READY = "ready"
READINESS_DOCUMENTARY_GAP = "documentary_gap"
READINESS_HUMAN_DECISION = "human_decision_missing"

MSG_PROBLEM_SHORT = "Problem statement muito breve ou ausente"
MSG_PROBLEM_PLACEHOLDER = "Problem statement contém marcadores explícitos de indefinição (TODO/TBD)"
MSG_NON_GOALS = "Fronteiras explícitas de escopo (non-goals) não foram delimitadas"
MSG_CRITERIA = "Critérios de aceitação verificáveis ausentes"
MSG_CLEAR = (
    "Demanda clara e contextualizada; escopo, non-goals e critérios delimitados sem lacunas materiais."
)


class ReadinessReport(BaseModel):
    """Result of the canonical G1 readiness evaluation."""

    model_config = ConfigDict(extra="forbid")

    is_clear: bool
    classification: str
    reasons: list[str]
    human_decision_reasons: list[str] = Field(default_factory=list)
    documentary_reasons: list[str] = Field(default_factory=list)


def has_placeholder_marker(text: str | None) -> bool:
    """True only for explicit placeholders (not substrings such as 'todos')."""
    return any(p.search(text or "") for p in _PLACEHOLDER_PATTERNS)


def evaluate_readiness(
    problem_statement: str | None,
    non_goals: Sequence[str] | None,
    acceptance_criteria: Sequence[str] | None,
) -> ReadinessReport:
    """Classify a demand as ready, documentary gap or missing human decision."""
    prob = (problem_statement or "").strip()
    human: list[str] = []
    doc: list[str] = []
    if len(prob) < 20:
        human.append(MSG_PROBLEM_SHORT)
    elif has_placeholder_marker(prob):
        human.append(MSG_PROBLEM_PLACEHOLDER)
    if not [g for g in (non_goals or []) if str(g).strip()]:
        doc.append(MSG_NON_GOALS)
    if not [c for c in (acceptance_criteria or []) if str(c).strip()]:
        doc.append(MSG_CRITERIA)
    reasons = human + doc
    if not reasons:
        return ReadinessReport(is_clear=True, classification=READINESS_READY, reasons=[MSG_CLEAR])
    return ReadinessReport(
        is_clear=False,
        classification=READINESS_HUMAN_DECISION if human else READINESS_DOCUMENTARY_GAP,
        reasons=reasons,
        human_decision_reasons=human,
        documentary_reasons=doc,
    )
