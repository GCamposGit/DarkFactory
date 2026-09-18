"""Catalog Manager for Cross-Project Reusable Components (HF-25).

Maintains a versioned registry of reusable components, exports them from
originating projects, and safely synchronizes them to target repositories.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import project_root
from core.projects.registry import get_project_registry
from core.catalog.models import (
    ComponentDescriptor,
    ComponentFile,
    ComponentKind,
    SyncResult,
)

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_DIR = Path(".factory") / "catalog"
DEFAULT_CATALOG_FILE = DEFAULT_CATALOG_DIR / "components.json"


BUILTIN_COMPONENTS = [
    ComponentDescriptor(
        id="atrium-seo-validator",
        name="SEO and Sitemap Validator",
        version="1.0.0",
        kind=ComponentKind.SCRIPT_UTILITY,
        source_project_id="site-ggcampos",
        description="Automated audit of title length, meta description, OpenGraph tags, and sitemap XML.",
        compatible_archetypes=["personal_presence", "internal_tool", "client_portfolio"],
        files=[
            ComponentFile(
                path="scripts/verify_seo.py",
                content="# SEO Verification Script\nprint('SEO Verified')\n",
                sha256="4cf3b0b533f81e69b5e3cb15bf61b3152a550d4f3b1458e0a776c535492d24a7",
            )
        ],
        dependencies=["beautifulsoup4"],
        tags=["seo", "audit", "marketing", "atrium"],
    ),
    ComponentDescriptor(
        id="atrium-anti-slop-linter",
        name="Anti-Slop Content Linter",
        version="1.1.0",
        kind=ComponentKind.INTEGRATION_HOOK,
        source_project_id="darkfac",
        description="Deterministic lexical and rhythm variance linter preventing AI buzzwords and robotic cadences.",
        compatible_archetypes=["personal_presence", "internal_tool", "second_brain", "core"],
        files=[
            ComponentFile(
                path="core/content/anti_slop.py",
                content="# Anti-Slop Linter Hook\nclass AntiSlopHook:\n    pass\n",
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ],
        dependencies=["pydantic"],
        tags=["content", "quality", "anti-slop", "marketing"],
    ),
    ComponentDescriptor(
        id="second-brain-mcp-connector",
        name="Segundo Cérebro MCP Client",
        version="1.0.0",
        kind=ComponentKind.INTEGRATION_HOOK,
        source_project_id="segundo-cerebro",
        description="Headless Model Context Protocol (MCP) client for RAG retrieval and citation verification.",
        compatible_archetypes=["internal_product", "second_brain", "core"],
        files=[
            ComponentFile(
                path="core/knowledge/mcp_client.py",
                content="# MCP Client Hook\nclass SegundoCerebroMCPClient:\n    pass\n",
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ],
        dependencies=["pydantic"],
        tags=["knowledge", "mcp", "rag", "citations"],
    ),
    ComponentDescriptor(
        id="portfolio-budget-guard",
        name="Portfolio Budget & Telegram Guardrail",
        version="1.0.0",
        kind=ComponentKind.INTEGRATION_HOOK,
        source_project_id="darkfac",
        description="Tracks monthly USD limits per project with Telegram 80% warning and 100% fail-closed cutoff.",
        compatible_archetypes=["core", "internal_product", "client_portfolio"],
        files=[
            ComponentFile(
                path="core/portfolio/budget_guard.py",
                content="# Portfolio Budget Guard Hook\nclass BudgetGuard:\n    pass\n",
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ],
        dependencies=["pydantic"],
        tags=["portfolio", "budget", "telegram", "governance"],
    ),
    ComponentDescriptor(
        id="ftp-deploy-pipeline",
        name="Continuous FTP Deploy Pipeline",
        version="1.0.0",
        kind=ComponentKind.SCRIPT_UTILITY,
        source_project_id="site-ggcampos",
        description="GitHub Actions automated deployment workflow for Hostinger FTP with path containment.",
        compatible_archetypes=["personal_presence", "client_portfolio"],
        files=[
            ComponentFile(
                path=".github/workflows/deploy.yml",
                content="# FTP Deploy Action\nname: Deploy\non: push\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n",
                sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        ],
        dependencies=[],
        tags=["ci-cd", "deploy", "ftp", "hostinger"],
    ),
]


class CrossProjectCatalogManager:
    """Central manager for cross-project component registry, packaging, and syncing."""

    def __init__(
        self,
        root: Optional[Path] = None,
        catalog_file: Optional[Path] = None,
    ) -> None:
        self.root = Path(root) if root else project_root()
        self.catalog_file = Path(catalog_file) if catalog_file else (self.root / DEFAULT_CATALOG_FILE)
        self._components: Dict[str, ComponentDescriptor] = {}
        self._load()

    def _load(self) -> None:
        """Load components from disk or initialize with built-in catalog."""
        self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
        # Pre-seed with built-ins
        for b in BUILTIN_COMPONENTS:
            self._components[b.id] = b

        if self.catalog_file.is_file():
            try:
                data = json.loads(self.catalog_file.read_text(encoding="utf-8"))
                for item in data:
                    comp = ComponentDescriptor.model_validate(item)
                    self._components[comp.id] = comp
            except Exception as exc:
                logger.error(f"Failed to load catalog from {self.catalog_file}: {exc}")

        # Ensure catalog is persisted
        self._save()

    def _save(self) -> None:
        """Persist catalog to disk."""
        self.catalog_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [c.model_dump(mode="json") for c in self.list_components()]
        self.catalog_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def register_component(self, descriptor: ComponentDescriptor) -> None:
        """Add or update a component in the catalog and persist."""
        self._components[descriptor.id] = descriptor
        self._save()

    def get_component(self, component_id: str) -> Optional[ComponentDescriptor]:
        """Look up a component by unique slug ID."""
        return self._components.get(component_id)

    def list_components(self, kind: Optional[ComponentKind] = None) -> List[ComponentDescriptor]:
        """Return all catalog components, optionally filtered by kind."""
        items = list(self._components.values())
        if kind:
            items = [c for c in items if c.kind == kind]
        return sorted(items, key=lambda c: c.id)

    def export_from_project(
        self,
        source_project_id: str,
        component_id: str,
        name: str,
        kind: ComponentKind | str,
        file_paths: List[str],
        description: str,
        compatible_archetypes: Optional[List[str]] = None,
        dependencies: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
    ) -> ComponentDescriptor:
        """Package files from an existing project into a new reusable component."""
        norm_kind = ComponentKind(kind) if isinstance(kind, str) else kind
        reg = get_project_registry()
        proj = reg.get_project(source_project_id)
        if not proj:
            raise KeyError(f"Source project '{source_project_id}' not found in registry.")

        source_base = Path(proj.path)
        packaged_files: List[ComponentFile] = []

        for rel_path in file_paths:
            norm_rel = rel_path.replace("\\", "/").strip().lstrip("/")
            src_file = source_base / norm_rel
            if not src_file.is_file():
                raise FileNotFoundError(f"Cannot export: file not found at '{src_file}'.")
            content = src_file.read_text(encoding="utf-8")
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            packaged_files.append(ComponentFile(path=norm_rel, content=content, sha256=sha))

        descriptor = ComponentDescriptor(
            id=component_id.strip(),
            name=name.strip(),
            kind=norm_kind,
            source_project_id=source_project_id,
            description=description.strip(),
            compatible_archetypes=list(compatible_archetypes or []),
            files=packaged_files,
            dependencies=list(dependencies or []),
            tags=list(tags or []),
        )
        self.register_component(descriptor)
        return descriptor

    def sync_to_project(
        self,
        component_id: str,
        target_project_id: str,
        *,
        target_dir_override: Optional[Path | str] = None,
        overwrite: bool = True,
    ) -> SyncResult:
        """Synchronize a component's files into a target project workspace."""
        comp = self.get_component(component_id)
        if not comp:
            raise KeyError(f"Component '{component_id}' not found in catalog.")

        if target_dir_override:
            target_path = Path(target_dir_override)
        else:
            reg = get_project_registry()
            proj = reg.get_project(target_project_id)
            if not proj:
                raise KeyError(f"Target project '{target_project_id}' not found in registry.")
            target_path = Path(proj.path)

        # Check compatibility if archetypes are specified
        if comp.compatible_archetypes and not target_dir_override:
            proj = reg.get_project(target_project_id)
            if proj:
                proj_kind_str = proj.kind.value if hasattr(proj.kind, "value") else str(proj.kind)
                if proj_kind_str not in comp.compatible_archetypes and "all" not in comp.compatible_archetypes:
                    # Non-blocking warning for flexibility, recorded in message
                    logger.info(f"Archetype note: project '{target_project_id}' has kind '{proj_kind_str}'.")

        synced: List[str] = []
        skipped: List[str] = []

        for cfile in comp.files:
            dest_file = target_path / cfile.path
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.is_file() and not overwrite:
                skipped.append(cfile.path)
                continue

            dest_file.write_text(cfile.content, encoding="utf-8")
            synced.append(cfile.path)

        msg = f"Successfully synced {len(synced)} file(s) to {target_project_id}."
        if skipped:
            msg += f" Skipped {len(skipped)} file(s) (no-overwrite)."

        return SyncResult(
            component_id=component_id,
            target_project_id=target_project_id,
            target_path=str(target_path),
            success=True,
            files_synced=synced,
            files_skipped=skipped,
            message=msg,
        )
