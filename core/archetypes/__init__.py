"""Dark Factory Archetypes Subsystem (HF-20)."""

from .models import (
    ArchetypeKind,
    ArchetypeManifest,
    ContentSchemaDescriptor,
    ContentSchemaField,
    ScaffoldRequest,
    ScaffoldResult,
    StackDescriptor,
)
from .registry import ArchetypeRegistry, get_registry
from .scaffolder import ArchetypeScaffolder

__all__ = [
    "ArchetypeKind",
    "ArchetypeManifest",
    "ArchetypeRegistry",
    "ArchetypeScaffolder",
    "ContentSchemaDescriptor",
    "ContentSchemaField",
    "ScaffoldRequest",
    "ScaffoldResult",
    "StackDescriptor",
    "get_registry",
]
