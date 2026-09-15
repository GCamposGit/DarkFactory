"""Registry of Dark Factory project archetypes (HF-20)."""

from __future__ import annotations

from typing import Dict, List, Optional
from .models import (
    ArchetypeKind,
    ArchetypeManifest,
    ContentSchemaDescriptor,
    ContentSchemaField,
    StackDescriptor,
)


class ArchetypeRegistry:
    """In-memory and filesystem registry for Dark Factory blueprints."""

    def __init__(self) -> None:
        self._archetypes: Dict[str, ArchetypeManifest] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register canonical archetypes specified in the Dark Factory roadmap."""
        # 1. Personal / Executive Presence (ATRIUM Blueprint)
        personal_presence = ArchetypeManifest(
            id="personal_presence",
            kind=ArchetypeKind.PERSONAL_PRESENCE,
            title="Executive & Personal Presence Portfolio",
            description=(
                "Astro 5 + Tailwind CSS 4 + MDX spec-driven executive portfolio with "
                "decoupled Zod content collections, instant SEO/OG, and FTP/Cloudflare/Dokploy deployment."
            ),
            version="1.0.0",
            stack=StackDescriptor(
                framework="Astro 5",
                styling="Tailwind CSS 4",
                content_format="MDX + Zod Content Collections",
                runtime="node",
                deployment_targets=["hostinger_ftp", "cloudflare_pages", "dokploy_static", "github_pages"],
            ),
            content_schemas=[
                ContentSchemaDescriptor(
                    collection_name="cases",
                    description="Case studies / deliverables with verifiable metrics and impact.",
                    path="src/content/cases",
                    fields=[
                        ContentSchemaField(name="title", field_type="string", description="Title of the case"),
                        ContentSchemaField(name="slug", field_type="string", required=False, description="Custom URL slug"),
                        ContentSchemaField(name="era", field_type="string", description="Career era or client organization"),
                        ContentSchemaField(name="tags", field_type="list[string]", description="Categorization tags"),
                        ContentSchemaField(name="anchorMetric", field_type="string", description="Primary metric (e.g. EBITDA, LLM latency)"),
                        ContentSchemaField(name="anchorValue", field_type="string", description="Value of primary metric (e.g. +R$ 140M)"),
                        ContentSchemaField(name="period", field_type="string", description="Execution time period"),
                        ContentSchemaField(name="featured", field_type="boolean", required=False, description="Show in hero/featured grid"),
                        ContentSchemaField(name="confidentiality", field_type="string", description="draft | pending-rights | approved"),
                    ],
                    example={
                        "title": "Autonomous Dark Factory Architecture",
                        "era": "darkfac",
                        "tags": ["AI", "Architecture", "Automation"],
                        "anchorMetric": "Test Pass Rate",
                        "anchorValue": "100%",
                        "period": "2026",
                        "featured": True,
                        "confidentiality": "approved",
                    },
                ),
                ContentSchemaDescriptor(
                    collection_name="thinking",
                    description="Long-form essays, technical notes and thought-leadership articles.",
                    path="src/content/thinking",
                    fields=[
                        ContentSchemaField(name="title", field_type="string", description="Article title"),
                        ContentSchemaField(name="date", field_type="datetime", description="Publication date"),
                        ContentSchemaField(name="description", field_type="string", required=False, description="Brief summary"),
                        ContentSchemaField(name="tags", field_type="list[string]", required=False, description="Tags"),
                        ContentSchemaField(name="featured", field_type="boolean", required=False, description="Featured flag"),
                        ContentSchemaField(name="lang", field_type="string", required=False, description="Language (pt, en)"),
                    ],
                    example={
                        "title": "Why Autonomous Coding Agents Require Spec-Driven Architecture",
                        "date": "2026-09-14",
                        "description": "Analysis of deterministic harnesses vs LLM conversational drift.",
                        "tags": ["AI", "Software Engineering"],
                        "featured": True,
                        "lang": "pt",
                    },
                ),
            ],
            required_inputs=["project_name", "target_dir", "author_name", "author_title"],
            default_files=[
                "package.json",
                "astro.config.mjs",
                "PROJECT_BIBLE.md",
                "AGENTS.md",
                "harness.config.json",
                "src/content/config.ts",
                "src/layouts/Layout.astro",
                "src/pages/index.astro",
                "src/pages/colophon.astro",
                "src/styles/global.css",
                ".github/workflows/deploy.yml",
            ],
        )
        self._archetypes[personal_presence.id] = personal_presence

        # 2. Internal Tooling & Micro SaaS Blueprint
        internal_tool = ArchetypeManifest(
            id="internal_tool",
            kind=ArchetypeKind.INTERNAL_TOOL,
            title="Internal Tooling & Micro SaaS",
            description="FastAPI + SQLite/Postgres + Tailwind/HTMX headless backend with worker queue and auth.",
            version="1.0.0",
            stack=StackDescriptor(
                framework="FastAPI + HTMX",
                styling="Tailwind CSS / DaisyUI",
                content_format="Pydantic + SQLite/Postgres",
                runtime="python",
                deployment_targets=["dokploy_docker", "systemd_service"],
            ),
            content_schemas=[],
            required_inputs=["project_name", "target_dir"],
            default_files=["pyproject.toml", "main.py", "harness.config.json"],
        )
        self._archetypes[internal_tool.id] = internal_tool

        # 3. Second Brain Knowledge Base Blueprint
        second_brain = ArchetypeManifest(
            id="second_brain",
            kind=ArchetypeKind.SECOND_BRAIN,
            title="Second Brain & Semantic Knowledge Base",
            description="Multi-modal ingestion (audio, pdf, markdown) with hybrid semantic search and citation audit.",
            version="1.0.0",
            stack=StackDescriptor(
                framework="Python + FastAPI / Faster-Whisper",
                styling="Minimalist WebUI / API Headless",
                content_format="Markdown + Vector Store",
                runtime="python",
                deployment_targets=["local_service", "dokploy_docker"],
            ),
            content_schemas=[],
            required_inputs=["project_name", "target_dir"],
            default_files=["pyproject.toml", "service.py", "harness.config.json"],
        )
        self._archetypes[second_brain.id] = second_brain

    def get_archetype(self, archetype_id: str) -> Optional[ArchetypeManifest]:
        """Retrieve an archetype by its unique ID."""
        return self._archetypes.get(archetype_id)

    def list_archetypes(self) -> List[ArchetypeManifest]:
        """Return all registered archetypes."""
        return list(self._archetypes.values())

    def register_archetype(self, manifest: ArchetypeManifest) -> None:
        """Register or override an archetype in the catalog."""
        self._archetypes[manifest.id] = manifest


_GLOBAL_REGISTRY = ArchetypeRegistry()


def get_registry() -> ArchetypeRegistry:
    """Return the global ArchetypeRegistry instance."""
    return _GLOBAL_REGISTRY
