"""
DarkFac Continuous Self-Improvement & Learning Package.
Provides session-level prompt tracking, 2nd-prompt reflection triggers,
failure Root Cause Analysis (RCA), and cross-domain insight extrapolation.
"""

from core.learning.models import (
    InteractionTurn,
    UserPreference,
    MistakeRCA,
    AnalogousTransfer,
    LearningLedger,
)
from core.learning.tracker import ContinuousLearningTracker
from core.learning.service import PersistentMemoryService

__all__ = [
    "InteractionTurn",
    "UserPreference",
    "MistakeRCA",
    "AnalogousTransfer",
    "LearningLedger",
    "ContinuousLearningTracker",
    "PersistentMemoryService",
]
