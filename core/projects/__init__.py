"""Dark Factory Multi-Project Management subsystem."""

from .models import ProjectDescriptor, ProjectKind
from .registry import ProjectRegistry, get_project_registry

__all__ = [
    "ProjectDescriptor",
    "ProjectKind",
    "ProjectRegistry",
    "get_project_registry",
]
