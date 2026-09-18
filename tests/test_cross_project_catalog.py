"""Tests for Cross-Project Reusable Catalog Subsystem (HF-25)."""

from pathlib import Path
import pytest

from core.catalog.manager import CrossProjectCatalogManager
from core.catalog.models import (
    ComponentDescriptor,
    ComponentFile,
    ComponentKind,
)
from core.projects.models import ProjectDescriptor, ProjectKind
from core.projects.registry import ProjectRegistry


def test_catalog_manager_initializes_with_builtins(tmp_path: Path) -> None:
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)
    components = manager.list_components()
    assert len(components) >= 5

    # Verify key builtins exist
    comp_ids = {c.id for c in components}
    assert "atrium-seo-validator" in comp_ids
    assert "atrium-anti-slop-linter" in comp_ids
    assert "second-brain-mcp-connector" in comp_ids
    assert "portfolio-budget-guard" in comp_ids
    assert "ftp-deploy-pipeline" in comp_ids


def test_catalog_filter_by_kind(tmp_path: Path) -> None:
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    utilities = manager.list_components(kind=ComponentKind.SCRIPT_UTILITY)
    assert all(c.kind == ComponentKind.SCRIPT_UTILITY for c in utilities)
    assert any(c.id == "atrium-seo-validator" for c in utilities)

    hooks = manager.list_components(kind=ComponentKind.INTEGRATION_HOOK)
    assert all(c.kind == ComponentKind.INTEGRATION_HOOK for c in hooks)
    assert any(c.id == "atrium-anti-slop-linter" for c in hooks)


def test_catalog_sync_to_project(tmp_path: Path) -> None:
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    target_dir = tmp_path / "target_app"
    target_dir.mkdir(parents=True)

    result = manager.sync_to_project(
        component_id="atrium-seo-validator",
        target_project_id="test-target",
        target_dir_override=target_dir,
    )
    assert result.success
    assert "scripts/verify_seo.py" in result.files_synced

    dest_file = target_dir / "scripts" / "verify_seo.py"
    assert dest_file.is_file()
    assert "SEO Verified" in dest_file.read_text(encoding="utf-8")


def test_catalog_sync_no_overwrite(tmp_path: Path) -> None:
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    target_dir = tmp_path / "target_app"
    dest_file = target_dir / "scripts" / "verify_seo.py"
    dest_file.parent.mkdir(parents=True)
    dest_file.write_text("Custom existing script", encoding="utf-8")

    result = manager.sync_to_project(
        component_id="atrium-seo-validator",
        target_project_id="test-target",
        target_dir_override=target_dir,
        overwrite=False,
    )
    assert result.success
    assert "scripts/verify_seo.py" in result.files_skipped
    assert dest_file.read_text(encoding="utf-8") == "Custom existing script"


def test_catalog_export_from_project(tmp_path: Path) -> None:
    # Setup mock source project
    source_dir = tmp_path / "source_app"
    source_dir.mkdir(parents=True)
    file_rel = "widgets/header.html"
    source_file = source_dir / file_rel
    source_file.parent.mkdir(parents=True)
    source_file.write_text("<header>Logo</header>", encoding="utf-8")

    # Setup isolated registry with this project
    projects_file = tmp_path / "projects.json"
    reg = ProjectRegistry(projects_file=projects_file)
    reg.register_project(
        ProjectDescriptor(
            id="mock-source",
            name="Mock Source Project",
            description="Testing export",
            path=str(source_dir),
            kind=ProjectKind.INTERNAL_PRODUCT,
        )
    )

    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    import core.catalog.manager as cat_mod
    orig_get_reg = cat_mod.get_project_registry
    cat_mod.get_project_registry = lambda: reg

    try:
        desc = manager.export_from_project(
            source_project_id="mock-source",
            component_id="custom-header-widget",
            name="Custom Header",
            kind=ComponentKind.UI_COMPONENT,
            file_paths=[file_rel],
            description="Reusable navigation header widget",
            compatible_archetypes=["personal_presence"],
        )
        assert desc.id == "custom-header-widget"
        assert len(desc.files) == 1
        assert desc.files[0].path == file_rel
        assert desc.files[0].content == "<header>Logo</header>"

        # Verify persisted and retrievable
        retrieved = manager.get_component("custom-header-widget")
        assert retrieved is not None
        assert retrieved.name == "Custom Header"
    finally:
        cat_mod.get_project_registry = orig_get_reg


def test_catalog_cli_list_and_inspect(capsys: pytest.CaptureFixture[str]) -> None:
    from core.catalog.cli import main

    code = main(["list"])
    assert code == 0
    captured = capsys.readouterr()
    assert "atrium-seo-validator" in captured.out

    code_inspect = main(["inspect", "--id", "atrium-seo-validator"])
    assert code_inspect == 0
    captured_inspect = capsys.readouterr()
    assert "SEO and Sitemap Validator" in captured_inspect.out
