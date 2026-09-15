"""Comprehensive tests for Dark Factory Archetypes subsystem (HF-20)."""

import json
from pathlib import Path
import pytest

from core.archetypes.models import (
    ArchetypeKind,
    ArchetypeManifest,
    ContentSchemaDescriptor,
    ScaffoldRequest,
    ScaffoldResult,
    StackDescriptor,
)
from core.archetypes.registry import ArchetypeRegistry, get_registry
from core.archetypes.scaffolder import ArchetypeScaffolder
from core.archetypes.cli import main as cli_main


def test_registry_builtins():
    """Verify built-in archetypes are properly configured according to HF-20."""
    registry = ArchetypeRegistry()
    archetypes = registry.list_archetypes()
    assert len(archetypes) >= 3

    ids = {item.id for item in archetypes}
    assert "personal_presence" in ids
    assert "internal_tool" in ids
    assert "second_brain" in ids

    # Check personal_presence details
    pp = registry.get_archetype("personal_presence")
    assert pp is not None
    assert pp.kind == ArchetypeKind.PERSONAL_PRESENCE
    assert pp.stack.framework == "Astro 5"
    assert "hostinger_ftp" in pp.stack.deployment_targets
    assert len(pp.content_schemas) == 2

    # Check collections
    collections = {c.collection_name for c in pp.content_schemas}
    assert "cases" in collections
    assert "thinking" in collections


def test_models_validation():
    """Test Pydantic models for archetype contracts."""
    req = ScaffoldRequest(
        archetype_id="personal_presence",
        project_name="My Portfolio",
        target_dir="/tmp/test",
        author_name="Guilherme Campos",
        domain="ggcampos.com",
    )
    assert req.project_name == "My Portfolio"
    assert req.deploy_target == "hostinger_ftp"

    manifest = ArchetypeManifest(
        id="custom_arch",
        kind=ArchetypeKind.INTERNAL_TOOL,
        title="Custom Archetype",
        description="Custom description",
        stack=StackDescriptor(
            framework="FastAPI",
            styling="Tailwind",
            content_format="JSON",
            deployment_targets=["docker"],
        ),
    )
    assert manifest.id == "custom_arch"
    assert manifest.version == "1.0.0"


def test_scaffolder_unknown_archetype(tmp_path: Path):
    """Test error handling when scaffolding an invalid archetype."""
    scaffolder = ArchetypeScaffolder()
    req = ScaffoldRequest(
        archetype_id="non_existent_archetype",
        project_name="Invalid",
        target_dir=str(tmp_path / "invalid"),
    )
    result = scaffolder.scaffold(req)
    assert not result.success
    assert "not found in registry" in result.error_message


def test_scaffolder_personal_presence_one_shot(tmp_path: Path):
    """Test end-to-end one-shot generation of personal_presence portfolio."""
    target_dir = tmp_path / "executive_site"
    scaffolder = ArchetypeScaffolder()
    req = ScaffoldRequest(
        archetype_id="personal_presence",
        project_name="Executive Presence",
        target_dir=str(target_dir),
        author_name="Guilherme Guidolin de Campos",
        author_title="Executive & AI Transformation Leader",
        author_bio="23 years in strategy, PE and AI implementation.",
        domain="ggcampos.com",
    )
    result = scaffolder.scaffold(req)

    assert result.success
    assert result.project_name == "Executive Presence"
    assert len(result.files_created) >= 20

    # Verify critical files exist
    assert (target_dir / "package.json").is_file()
    assert (target_dir / "astro.config.mjs").is_file()
    assert (target_dir / "tsconfig.json").is_file()
    assert (target_dir / "PROJECT_BIBLE.md").is_file()
    assert (target_dir / "AGENTS.md").is_file()
    assert (target_dir / "harness.config.json").is_file()
    assert (target_dir / "scripts" / "verify_build.mjs").is_file()
    assert (target_dir / "scripts" / "deploy.mjs").is_file()
    assert (target_dir / ".github" / "workflows" / "deploy.yml").is_file()
    assert (target_dir / "src" / "content" / "config.ts").is_file()
    assert (target_dir / "src" / "content" / "cases" / "sample-case.mdx").is_file()
    assert (target_dir / "src" / "content" / "thinking" / "sample-thought.mdx").is_file()
    assert (target_dir / "src" / "layouts" / "Layout.astro").is_file()
    assert (target_dir / "src" / "pages" / "index.astro").is_file()
    assert (target_dir / "src" / "pages" / "colophon.astro").is_file()
    assert (target_dir / "src" / "pages" / "404.astro").is_file()
    assert (target_dir / "src" / "pages" / "cases" / "[slug].astro").is_file()
    assert (target_dir / "src" / "pages" / "thinking" / "[slug].astro").is_file()
    assert (target_dir / "src" / "styles" / "global.css").is_file()

    # Verify package.json syntax and dependencies
    pkg = json.loads((target_dir / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == "executive-presence"
    assert "astro" in pkg["dependencies"]
    assert "tailwindcss" in pkg["dependencies"]
    assert "zod" in pkg["dependencies"]

    # Verify harness.config.json
    harness = json.loads((target_dir / "harness.config.json").read_text(encoding="utf-8"))
    assert len(harness["steps"]) == 2
    assert harness["steps"][0]["name"] == "astro_build"
    assert harness["steps"][1]["name"] == "routes_verification"


def test_scaffolder_internal_tool(tmp_path: Path):
    """Test scaffolding internal_tool archetype."""
    target_dir = tmp_path / "tool_app"
    scaffolder = ArchetypeScaffolder()
    req = ScaffoldRequest(
        archetype_id="internal_tool",
        project_name="AuditTool",
        target_dir=str(target_dir),
    )
    result = scaffolder.scaffold(req)
    assert result.success
    assert (target_dir / "pyproject.toml").is_file()
    assert (target_dir / "main.py").is_file()
    assert (target_dir / "harness.config.json").is_file()


def test_scaffolder_second_brain(tmp_path: Path):
    """Test scaffolding second_brain archetype."""
    target_dir = tmp_path / "brain_app"
    scaffolder = ArchetypeScaffolder()
    req = ScaffoldRequest(
        archetype_id="second_brain",
        project_name="SecondBrainCore",
        target_dir=str(target_dir),
    )
    result = scaffolder.scaffold(req)
    assert result.success
    assert (target_dir / "pyproject.toml").is_file()
    assert (target_dir / "service.py").is_file()
    assert (target_dir / "harness.config.json").is_file()


def test_cli_commands(tmp_path: Path, capsys):
    """Test CLI commands: list, inspect, and init."""
    # 1. list
    rc = cli_main(["list"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "personal_presence" in captured.out
    assert "internal_tool" in captured.out

    # 2. list --json
    rc = cli_main(["list", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) >= 3

    # 3. inspect
    rc = cli_main(["inspect", "personal_presence"])
    assert rc == 0
    captured = capsys.readouterr()
    arch_data = json.loads(captured.out)
    assert arch_data["id"] == "personal_presence"
    assert arch_data["stack"]["framework"] == "Astro 5"

    # 4. init
    target = tmp_path / "cli_scaffolded_site"
    rc = cli_main([
        "init",
        "personal_presence",
        str(target),
        "--name", "CLI Site",
        "--author", "Test Author",
        "--title", "Test Title",
        "--domain", "testsitedomain.com",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "[OK] Successfully scaffolded" in captured.out
    assert (target / "package.json").is_file()
    assert (target / "PROJECT_BIBLE.md").is_file()
