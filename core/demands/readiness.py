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
MSG_OBJECTIVE = "Objetivo da demanda ausente ou vago"
MSG_SUCCESS = "Criterio de sucesso objetivo e verificavel ausente (o que prova que deu certo?)"
MSG_ACCEPTANCE_MODE = "Modo de aceite invalido (use auto ou owner)"
ACCEPTANCE_MODES = ("auto", "owner")
# Small fixes (correction/adjustment) are specified by the defect itself: no grill.
_SMALL_FIX = re.compile(
    r"\b(?:corrig\w*|ajust\w*|conserta\w*|fix(?:ar|es)?|bug|typo|hotfix|regress[aã]o)\b",
    re.IGNORECASE,
)
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


def is_small_fix(text: str | None) -> bool:
    """True for short correction/adjustment demands, which skip the grill."""
    t = (text or "").strip()
    return 20 <= len(t) <= 400 and bool(_SMALL_FIX.search(t))


def _is_verifiable(criterion: str) -> bool:
    """A success criterion must be concrete: at least two words, not a placeholder."""
    c = criterion.strip()
    return len(c.split()) >= 2 and not has_placeholder_marker(c)


def evaluate_readiness(
    problem_statement: str | None,
    non_goals: Sequence[str] | None,
    acceptance_criteria: Sequence[str] | None,
    acceptance_mode: str | None = None,
) -> ReadinessReport:
    """Classify a demand as ready, documentary gap or missing human decision.

    G1 semantics (USR-375 S6): a non-trivial demand needs an objective, a verifiable
    success criterion and an acceptance mode (``auto`` default, or ``owner``). Small
    fixes pass on the defect description alone.
    """
    prob = (problem_statement or "").strip()
    human: list[str] = []
    doc: list[str] = []
    mode_bad = acceptance_mode is not None and acceptance_mode not in ACCEPTANCE_MODES
    if len(prob) < 20:
        human.append(MSG_PROBLEM_SHORT)
    elif has_placeholder_marker(prob):
        human.append(MSG_PROBLEM_PLACEHOLDER)
    elif is_small_fix(prob) and not mode_bad:
        return ReadinessReport(is_clear=True, classification=READINESS_READY, reasons=[MSG_CLEAR])
    if not human:
        criteria = [str(c) for c in (acceptance_criteria or []) if str(c).strip()]
        if not any(_is_verifiable(c) for c in criteria):
            human.append(MSG_SUCCESS)
        if not [g for g in (non_goals or []) if str(g).strip()]:
            doc.append(MSG_NON_GOALS)
    if mode_bad:
        human.append(MSG_ACCEPTANCE_MODE)
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
