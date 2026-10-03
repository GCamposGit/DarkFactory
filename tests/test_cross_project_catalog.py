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


def test_catalog_sync_fails_when_target_missing_without_create_flag(tmp_path: Path) -> None:
    """USR-101: sync_to_project must fail with FileNotFoundError if target directory does not exist and create=False."""
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    non_existent_target = tmp_path / "does_not_exist_workspace"
    assert not non_existent_target.exists()

    with pytest.raises(FileNotFoundError, match="does not exist"):
        manager.sync_to_project(
            component_id="atrium-seo-validator",
            target_project_id="test-target",
            target_dir_override=non_existent_target,
            create=False,
        )

    # Assert no directory was created
    assert not non_existent_target.exists()


def test_catalog_sync_creates_directory_when_create_true(tmp_path: Path) -> None:
    """USR-101: sync_to_project creates missing target directory only when create=True."""
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    target_to_create = tmp_path / "created_workspace"
    assert not target_to_create.exists()

    result = manager.sync_to_project(
        component_id="atrium-seo-validator",
        target_project_id="test-target",
        target_dir_override=target_to_create,
        create=True,
    )
    assert result.success
    assert target_to_create.is_dir()
    assert (target_to_create / "scripts" / "verify_seo.py").is_file()


def test_catalog_sync_prevents_path_traversal(tmp_path: Path) -> None:
    """USR-101: sync_to_project prevents path traversal outside target directory."""
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    # Register component with path traversal file
    malicious = ComponentDescriptor(
        id="malicious-comp",
        name="Malicious Component",
        version="1.0.0",
        kind=ComponentKind.SCRIPT_UTILITY,
        source_project_id="darkfac",
        description="Attempts path traversal",
        files=[
            ComponentFile(
                path="../../escaped.txt",
                content="malicious payload",
                sha256="abc",
            )
        ],
    )
    manager.register_component(malicious)

    target_dir = tmp_path / "sandbox"
    target_dir.mkdir(parents=True)

    with pytest.raises(PermissionError, match="Path traversal detected"):
        manager.sync_to_project(
            component_id="malicious-comp",
            target_project_id="test-target",
            target_dir_override=target_dir,
        )

    assert not (tmp_path / "escaped.txt").exists()


def test_project_descriptor_resolve_path_platform_awareness() -> None:
    """USR-101: ProjectDescriptor resolves darkfac via project_root() and respects OS-specific paths."""
    from core.paths import project_root

    # 1. darkfac always resolves to project_root()
    darkfac_proj = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory",
        path="C:\\dev\\DarkFac",
    )
    assert darkfac_proj.resolve_path(platform_name="windows") == project_root()
    assert darkfac_proj.resolve_path(platform_name="linux") == project_root()

    # 2. paths mapping per OS
    multi_os_proj = ProjectDescriptor(
        id="multi-os",
        name="Multi OS Project",
        path="C:\\dev\\MultiOS",
        paths={"windows": "C:\\dev\\MultiOS", "linux": "/var/www/multios"},
    )
    win_res = multi_os_proj.resolve_path(platform_name="windows")
    assert win_res is not None
    assert str(win_res).replace("\\", "/") == "C:/dev/MultiOS"
    assert multi_os_proj.resolve_path(platform_name="linux") == Path("/var/www/multios")

    # 3. Unresolved Windows path on Linux returns None
    win_only_proj = ProjectDescriptor(
        id="win-only",
        name="Windows Only Project",
        path="C:\\dev\\WinOnly",
    )
    assert win_only_proj.resolve_path(platform_name="linux") is None


def test_catalog_sync_linux_simulation_with_windows_path_fails_cleanly(tmp_path: Path) -> None:
    """USR-101: Simulating Linux runtime with registered Windows path raises structured ValueError
    and does NOT create a rogue 'C:\\...' directory in current working directory."""
    catalog_file = tmp_path / "components.json"
    manager = CrossProjectCatalogManager(root=tmp_path, catalog_file=catalog_file)

    # Setup project registry with a Windows-only path project
    projects_file = tmp_path / "projects.json"
    reg = ProjectRegistry(projects_file=projects_file)
    reg.register_project(
        ProjectDescriptor(
            id="remote-win-proj",
            name="Remote Windows Project",
            path="C:\\dev\\RemoteProject",
            paths={},  # No linux path provided
        )
    )

    import core.catalog.manager as cat_mod
    orig_get_reg = cat_mod.get_project_registry
    cat_mod.get_project_registry = lambda: reg

    # Mock ProjectDescriptor.resolve_path to simulate Linux behavior
    orig_resolve = ProjectDescriptor.resolve_path
    try:
        def fake_resolve(self: ProjectDescriptor, platform_name: str | None = None) -> Path | None:
            return orig_resolve(self, platform_name="linux")

        ProjectDescriptor.resolve_path = fake_resolve  # type: ignore[assignment]

        with pytest.raises(ValueError, match="cannot be resolved"):
            manager.sync_to_project(
                component_id="atrium-seo-validator",
                target_project_id="remote-win-proj",
            )

        # Verify no bogus directory named 'C:\dev\RemoteProject' was created in cwd
        assert not Path("C:").exists() or not (Path(".") / "C:\\dev\\RemoteProject").exists()
    finally:
        cat_mod.get_project_registry = orig_get_reg
        ProjectDescriptor.resolve_path = orig_resolve  # type: ignore[assignment]


def test_api_catalog_sync_status_codes(tmp_path: Path) -> None:
    """USR-101: /api/catalog/sync returns 400 when target is missing without create=True, 200 with create=True, and 404 on missing component."""
    from fastapi.testclient import TestClient
    from hub.backend.main import app
    from hub.backend.api import require_owner_session
    from hub.backend.service import HubService

    client = TestClient(app)
    app.dependency_overrides[require_owner_session] = lambda: HubService(data_dir=tmp_path)

    # Setup mock target project with non-existent path
    projects_file = tmp_path / "projects.json"
    reg = ProjectRegistry(projects_file=projects_file)
    non_existent = tmp_path / "api_target_nonexistent"
    reg.register_project(
        ProjectDescriptor(
            id="api-test-proj",
            name="API Test Project",
            path=str(non_existent),
        )
    )

    import core.catalog.manager as cat_mod
    orig_get_reg = cat_mod.get_project_registry
    cat_mod.get_project_registry = lambda: reg

    try:
        # 1. 404 when component does not exist
        res_404 = client.post(
            "/api/catalog/sync",
            json={"component_id": "nonexistent-comp-123", "target_project_id": "api-test-proj"},
        )
        assert res_404.status_code == 404

        # 2. 400 when target directory does not exist and create is False
        res_400 = client.post(
            "/api/catalog/sync",
            json={
                "component_id": "atrium-seo-validator",
                "target_project_id": "api-test-proj",
                "create": False,
            },
        )
        assert res_400.status_code == 400
        assert "does not exist" in res_400.json()["detail"]
        assert not non_existent.exists()

        # 3. 200 when create is True
        res_200 = client.post(
            "/api/catalog/sync",
            json={
                "component_id": "atrium-seo-validator",
                "target_project_id": "api-test-proj",
                "create": True,
            },
        )
        assert res_200.status_code == 200
        assert res_200.json()["success"] is True
        assert non_existent.is_dir()
    finally:
        cat_mod.get_project_registry = orig_get_reg
        app.dependency_overrides.pop(require_owner_session, None)

