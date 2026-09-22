"""Dark Factory Multi-Project Management subsystem."""

from .detect import detect_commands
from .models import (
    DeployConfig,
    DeployTargetType,
    ProjectCommands,
    ProjectDescriptor,
    ProjectKind,
    SmokeCheck,
)
from .registry import ProjectRegistry, get_project_registry, resolve_commands

__all__ = [
    "DeployConfig",
    "DeployTargetType",
    "ProjectCommands",
    "ProjectDescriptor",
    "ProjectKind",
    "SmokeCheck",
    "ProjectRegistry",
    "get_project_registry",
    "detect_commands",
    "resolve_commands",
]
