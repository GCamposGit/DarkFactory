"""Reusable pilots and shadow pre-classification library (HF-23-02)."""

from __future__ import annotations

from core.pilots.contracts import (
    PilotObservation,
    PilotReport,
    PilotSpec,
    PilotVerdict,
)
from core.pilots.evaluator import (
    PilotEvaluator,
    compute_mcnemar_p_value,
    compute_paired_delta_ci,
    compute_wilson_interval,
)
from core.pilots.jev_adapter import (
    JEV_QUESTIONS,
    JevShadowAdapter,
)

__all__ = [
    "PilotSpec",
    "PilotObservation",
    "PilotReport",
    "PilotVerdict",
    "PilotEvaluator",
    "JevShadowAdapter",
    "JEV_QUESTIONS",
    "compute_wilson_interval",
    "compute_paired_delta_ci",
    "compute_mcnemar_p_value",
]
