"""Static (no-docker, no-network) checks for the HF-27-09 VPS image and topology.

Docker cannot run in this environment, so these tests assert on the
Dockerfile/compose/env/PowerShell *text* instead of building or running
anything: pinned CLI versions are present, the worker publishes the expected
env-driven capabilities, and the on-prem launcher targets the right module.
The actual `docker run <img> claude --version && codex --version && ...`
acceptance check from the handoff must still be run manually after a real
build; see docs/runbooks/HF-27-09_topology.md section 7.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOKPLOY_DIR = REPO_ROOT / "deploy" / "dokploy"


def _read(path: Path) -> str:
    assert path.is_file(), f"expected file at {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Dockerfile.cloud
# --------------------------------------------------------------------------


def test_dockerfile_installs_git_gh_node_lftp():
    text = _read(DOKPLOY_DIR / "Dockerfile.cloud")
    assert re.search(r"^\s*git\s*\\?\s*$", text, re.MULTILINE) or " git " in text
    assert "lftp" in text
    assert "nodejs" in text
    # gh comes from the pinned release tarball, checksum-verified (the apt repo
    # only keeps the latest version, so a pinned apt install breaks on bump).
    assert "github.com/cli/cli/releases/download/v${GH_CLI_VERSION}" in text
    assert "sha256sum -c" in text
    assert "/usr/local/bin/gh" in text
    assert '"gh=${GH_CLI_VERSION}' not in text
    assert "githubcli-archive-keyring" not in text


def test_dockerfile_pins_claude_and_codex_versions():
    text = _read(DOKPLOY_DIR / "Dockerfile.cloud")
    assert "@anthropic-ai/claude-code@" in text
    assert "@openai/codex@" in text
    assert "ARG CLAUDE_CODE_VERSION=" in text
    assert "ARG CODEX_CLI_VERSION=" in text
    assert "ARG GH_CLI_VERSION=" in text
    # No unpinned "@latest" install for either agent CLI.
    assert "claude-code@latest" not in text
    assert "codex@latest" not in text


def test_dockerfile_declares_codex_auth_and_workspaces_volumes():
    text = _read(DOKPLOY_DIR / "Dockerfile.cloud")
    assert "/home/darkfac/.codex" in text
    assert "/workspaces" in text
    assert re.search(r"VOLUME\s*\[.*\.codex.*\]", text)


def test_dockerfile_never_hardcodes_oauth_token_value():
    text = _read(DOKPLOY_DIR / "Dockerfile.cloud")
    # The Dockerfile may *mention* the env var name for documentation, but
    # must never set it to a literal secret-looking value.
    assert not re.search(r"CLAUDE_CODE_OAUTH_TOKEN\s*=\s*['\"]?sk-", text)


# --------------------------------------------------------------------------
# docker-compose.cloud.yml
# --------------------------------------------------------------------------


def test_compose_worker_declares_hf2709_env_vars():
    text = _read(DOKPLOY_DIR / "docker-compose.cloud.yml")
    assert "DARKFAC_WORKER_CAPS" in text
    assert "DARKFAC_WORKER_PRIORITY" in text
    assert "DARKFAC_WORKSPACES=/workspaces" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text


def test_compose_worker_max_slots_defaults_to_one():
    text = _read(DOKPLOY_DIR / "docker-compose.cloud.yml")
    assert "DARKFAC_MAX_CONCURRENT_SLOTS:-1" in text


def test_compose_worker_memory_limit_is_2gb():
    text = _read(DOKPLOY_DIR / "docker-compose.cloud.yml")
    assert "memory: 2048M" in text


def test_compose_declares_codex_auth_and_workspace_volumes():
    text = _read(DOKPLOY_DIR / "docker-compose.cloud.yml")
    assert "darkfac-codex-auth:/home/darkfac/.codex" in text
    assert "darkfac-workspaces:/workspaces" in text
    assert "darkfac-codex-auth:" in text
    assert "darkfac-workspaces:" in text


def test_compose_is_valid_yaml():
    yaml = __import__("yaml") if _yaml_available() else None
    if yaml is None:
        return  # PyYAML not guaranteed as a dependency; skip structural parse.
    data = yaml.safe_load(_read(DOKPLOY_DIR / "docker-compose.cloud.yml"))
    assert "darkfac-worker" in data["services"]
    assert "darkfac-codex-auth" in data["volumes"]
    assert "darkfac-workspaces" in data["volumes"]


def _yaml_available() -> bool:
    try:
        import yaml  # noqa: F401

        return True
    except ImportError:
        return False


# --------------------------------------------------------------------------
# env.cloud.example
# --------------------------------------------------------------------------


def test_env_example_documents_hf2709_vars_without_real_secrets():
    text = _read(DOKPLOY_DIR / "env.cloud.example")
    for var in (
        "DARKFAC_WORKER_CAPS",
        "DARKFAC_WORKER_PRIORITY",
        "DARKFAC_WORKSPACES",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        assert var in text, f"missing {var} in env.cloud.example"
    # CLAUDE_CODE_OAUTH_TOKEN must ship blank, never a real-looking token.
    match = re.search(r"^CLAUDE_CODE_OAUTH_TOKEN=(.*)$", text, re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == ""


# --------------------------------------------------------------------------
# scripts/start_onprem_worker.ps1
# --------------------------------------------------------------------------


def test_start_onprem_worker_targets_cloud_worker_module():
    text = _read(REPO_ROOT / "scripts" / "start_onprem_worker.ps1")
    assert "core.orchestrator.cloud_worker" in text
    # Must no longer launch the old HTTP remote_worker daemon for this role.
    assert "remote_worker.py" not in text


def test_start_onprem_worker_sets_topology_env_vars():
    text = _read(REPO_ROOT / "scripts" / "start_onprem_worker.ps1")
    for var in (
        "DARKFAC_HF02_DATABASE_URL",
        "DARKFAC_WORKER_CAPS",
        "DARKFAC_WORKER_PRIORITY",
        "DARKFAC_MAX_CONCURRENT_SLOTS",
    ):
        assert var in text, f"missing {var} in start_onprem_worker.ps1"


def test_start_onprem_worker_priority_param_validated():
    text = _read(REPO_ROOT / "scripts" / "start_onprem_worker.ps1")
    assert "ValidateSet(\"primary\", \"secondary\", \"fallback\")" in text


def test_start_onprem_worker_fails_fast_without_database_url():
    text = _read(REPO_ROOT / "scripts" / "start_onprem_worker.ps1")
    assert "WORKER_START_FAIL" in text
    assert "exit 1" in text


def test_install_scripts_unchanged_still_point_at_launcher():
    # HF-27-09 must not touch these two installers (reused as-is per the
    # handoff); confirm they still just call start_onprem_worker.ps1.
    for name in ("install_onprem_worker_service.ps1", "install_onprem_worker_user_startup.ps1"):
        text = _read(REPO_ROOT / "scripts" / name)
        assert "start_onprem_worker.ps1" in text
