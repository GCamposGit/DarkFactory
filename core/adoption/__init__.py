"""Public API for the Dark Factory project-adoption gateway."""

from .models import AdoptionPlan, AdoptionResult, ProjectInspection, TaskPreparation, VerificationReport
from .service import (
    AdoptionBlockedError,
    AdoptionError,
    apply_adoption,
    initialize_project,
    inspect_project,
    plan_adoption,
    prepare_adoption_worktree,
    prepare_task,
    verify_adoption,
)

__all__ = [
    "AdoptionBlockedError",
    "AdoptionError",
    "AdoptionPlan",
    "AdoptionResult",
    "ProjectInspection",
    "TaskPreparation",
    "VerificationReport",
    "apply_adoption",
    "initialize_project",
    "inspect_project",
    "plan_adoption",
    "prepare_adoption_worktree",
    "prepare_task",
    "verify_adoption",
]
