from pathlib import Path

from core.archetypes.deploy_pattern import (
    PROBE_ACCEPTANCE_CRITERION,
    DeploySpec,
    render_image_workflow,
    render_initial_compose,
)
from core.archetypes.models import ScaffoldRequest
from core.archetypes.scaffolder import ArchetypeScaffolder
from core.orchestrator.bluegreen import parse_state

SPEC = DeploySpec(app_name="meu-app", host="meu.example.com", ghcr_owner="Acme", port=9000)


def test_workflow_tags_by_sha():
    assert "ghcr.io/acme/meu-app:${{ github.sha }}" in render_image_workflow(SPEC)


def test_compose_bluegreen_with_healthcheck_and_state():
    text = render_initial_compose(SPEC)
    assert parse_state(text) is not None
    assert "meu-app-blue:" in text and "meu-app-green:" in text
    assert text.count("healthcheck:") == 2
    assert "replicas: 1" in text and "replicas: 0" in text
    assert "Host(`meu.example.com`)" in text and "server.port=9000" in text


def test_scaffold_writes_pattern_for_dokploy(tmp_path):
    r = ArchetypeScaffolder().scaffold(
        ScaffoldRequest(
            archetype_id="internal_tool", project_name="Meu App", target_dir=str(tmp_path), deploy_target="dokploy"
        )
    )
    assert r.success, r.error_message
    assert (tmp_path / ".github/workflows/image.yml").exists()
    doc = (tmp_path / "docs/DEPLOY_ZERO_DOWNTIME.md").read_text(encoding="utf-8")
    assert PROBE_ACCEPTANCE_CRITERION in doc


def test_scaffold_skips_pattern_by_default(tmp_path):
    r = ArchetypeScaffolder().scaffold(
        ScaffoldRequest(archetype_id="internal_tool", project_name="x", target_dir=str(tmp_path))
    )
    assert r.success
    assert not Path(tmp_path / "deploy").exists()
