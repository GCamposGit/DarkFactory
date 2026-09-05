"""
DarkFac Anti-AI-Slop Content Engine Package.
Public exports for models, linter, presets, and generation engine.
"""

from core.content.models import (
    ContentType,
    ToneProfile,
    SlopCategory,
    SeverityLevel,
    CleanlinessRating,
    SlopViolation,
    SlopReport,
    ContentRequest,
    ContentResponse,
)
from core.content.anti_slop_linter import AntiSlopLinter
from core.content.presets import get_preset, CONTENT_PRESETS
from core.content.engine import ContentEngine

__all__ = [
    "ContentType",
    "ToneProfile",
    "SlopCategory",
    "SeverityLevel",
    "CleanlinessRating",
    "SlopViolation",
    "SlopReport",
    "ContentRequest",
    "ContentResponse",
    "AntiSlopLinter",
    "get_preset",
    "CONTENT_PRESETS",
    "ContentEngine",
]
