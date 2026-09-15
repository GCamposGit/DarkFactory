"""Project registry and discovery for Dark Factory."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import project_root
from .models import ProjectDescriptor, ProjectKind

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS = [
    ProjectDescriptor(
        id="darkfac",
        name="Dark Factory (Core)",
        description="Núcleo compartilhado da fábrica autônoma e do DarkHub.",
        path=str(project_root()),
        kind=ProjectKind.CORE,
        prefix="DF",
        domain="darkhub.ggcampos.com",
    ),
]


class ProjectRegistry:
    """Central registry of software projects and clients managed by Dark Factory."""

    def __init__(self, projects_file: Optional[Path] = None) -> None:
        self.projects_file = projects_file or (project_root() / ".factory" / "projects.json")
        self._projects: Dict[str, ProjectDescriptor] = {}
        self._load()

    def _load(self) -> None:
        """Load projects from disk or initialize with defaults."""
        self._projects = {p.id: p for p in DEFAULT_PROJECTS}

        if self.projects_file.is_file():
            try:
                data = json.loads(self.projects_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for item in data:
                        proj = ProjectDescriptor.model_validate(item)
                        self._projects[proj.id] = proj
            except Exception as exc:
                logger.error(f"Failed to load projects from {self.projects_file}: {exc}")

    def save(self) -> None:
        """Persist registered projects to disk."""
        self.projects_file.parent.mkdir(parents=True, exist_ok=True)
        items = [p.model_dump() for p in self.list_projects()]
        self.projects_file.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_projects(self) -> List[ProjectDescriptor]:
        """Return all managed projects sorted by ID."""
        return [self._projects[k] for k in sorted(self._projects.keys())]

    def get_project(self, project_id: str) -> Optional[ProjectDescriptor]:
        """Look up a project by slug ID."""
        return self._projects.get(project_id)

    def register_project(self, project: ProjectDescriptor) -> None:
        """Register or update a managed project and save."""
        self._projects[project.id] = project
        self.save()

    def get_ticket_prefix(self, project_id: str) -> str:
        """Get the ticket prefix for demand IDs (e.g. 'SIT', 'SC', 'USR')."""
        if project_id == "darkfac":
            return "USR"
        proj = self.get_project(project_id)
        if proj and proj.prefix:
            return proj.prefix.upper()
        return "USR"


_GLOBAL_REGISTRY: Optional[ProjectRegistry] = None


def get_project_registry() -> ProjectRegistry:
    """Return the global ProjectRegistry singleton."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = ProjectRegistry()
    return _GLOBAL_REGISTRY
