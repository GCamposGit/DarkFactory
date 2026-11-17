"""USR-485 T1: one host identity source for workers and fleet helpers."""

import logging

from fastapi.testclient import TestClient

from core.harness import remote_worker
from core.harness.remote_dispatch import KNOWN_HOST_IDENTIFIERS
from core.infra.host_identity import (
    CANONICAL_NODE_IDS,
    HOST_ALIASES,
    canonical_node_id,
    logical_host,
    resolve_host_id_detail,
    resolve_node_identity,
)
from core.line import line_lock
from scripts import start_fleet_worker


def test_host_aliases_resolve_to_canonical_inventory_ids():
    assert KNOWN_HOST_IDENTIFIERS == HOST_ALIASES
    expected = {
        "predator-neo-16": ("notebook", "predator-neo-16"),
        "ai_notebook": ("notebook", "predator-neo-16"),
        "ai-notebook": ("notebook", "predator-neo-16"),
        "desktop-g45ipem": ("desktop", "onprem-z97-server"),
        "darkfac-desktop": ("desktop", "onprem-z97-server"),
        "grok-bot-vm-418374532": ("grokbox", "grokbox-test-worker"),
        "grokbox": ("grokbox", "grokbox-test-worker"),
        "100.81.84.124": ("notebook", "predator-neo-16"),
        "100.78.181.90": ("desktop", "onprem-z97-server"),
        "100.77.130.5": ("grokbox", "grokbox-test-worker"),
    }

    for alias, (host, node_id) in expected.items():
        assert logical_host(alias) == host
        assert canonical_node_id(alias) == node_id
        assert alias in HOST_ALIASES[host]
        assert CANONICAL_NODE_IDS[host] == node_id


def test_unknown_identifier_is_not_mapped_to_a_known_host():
    assert logical_host("custom-host") is None
    assert canonical_node_id("custom-host") == "custom-host"


def test_worker_without_configured_id_uses_local_canonical_host():
    identity = resolve_node_identity(env={}, hostname="AI_Notebook")

    assert identity.node_id == "predator-neo-16"
    assert identity.configured_node_id is None
    assert identity.source == "hostname"


def test_worker_corrects_configured_id_that_belongs_to_another_known_host():
    identity = resolve_node_identity("darkfac-desktop", env={}, hostname="AI_Notebook")

    assert identity.node_id == "predator-neo-16"
    assert identity.configured_node_id == "darkfac-desktop"
    assert identity.configured_logical_host == "desktop"
    assert identity.logical_host == "notebook"
    assert identity.source == "hostname"
    assert identity.corrected is True


def test_worker_keeps_configured_id_when_local_host_is_unknown():
    identity = resolve_node_identity("darkfac-desktop", env={}, hostname="unknown-vm")

    assert identity.node_id == "onprem-z97-server"
    assert identity.configured_node_id == "darkfac-desktop"
    assert identity.source == "cli"
    assert identity.corrected is False


def test_worker_reports_environment_source_and_cli_has_precedence():
    from_env = resolve_node_identity(env={"DARKFAC_NODE_ID": "grokbox"}, hostname="grokbox-test-worker")
    from_cli = resolve_node_identity(
        "darkfac-desktop",
        env={"DARKFAC_NODE_ID": "grokbox"},
        hostname="desktop-g45ipem",
    )

    assert (from_env.node_id, from_env.configured_node_id, from_env.source) == (
        "grokbox-test-worker",
        "grokbox",
        "env",
    )
    assert (from_cli.node_id, from_cli.configured_node_id, from_cli.source) == (
        "onprem-z97-server",
        "darkfac-desktop",
        "cli",
    )


def test_remote_worker_health_exposes_corrected_identity_and_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(remote_worker.socket, "gethostname", lambda: "AI_Notebook")
    monkeypatch.delenv("DARKFAC_NODE_ID", raising=False)

    with caplog.at_level(logging.WARNING, logger="core.harness.remote_worker"):
        app = remote_worker.create_worker_app(project_root=tmp_path, node_id="darkfac-desktop")

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert health["node_id"] == "predator-neo-16"
    assert health["configured_node_id"] == "darkfac-desktop"
    assert health["node_id_source"] == "hostname"
    assert "darkfac-desktop" in caplog.text
    assert "predator-neo-16" in caplog.text
    assert "AI_Notebook" in caplog.text


def test_remote_worker_health_preserves_config_for_unknown_local_host(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(remote_worker.socket, "gethostname", lambda: "custom-vm")
    monkeypatch.delenv("DARKFAC_NODE_ID", raising=False)

    with caplog.at_level(logging.WARNING, logger="core.harness.remote_worker"):
        app = remote_worker.create_worker_app(project_root=tmp_path, node_id="darkfac-desktop")

    with TestClient(app) as client:
        health = client.get("/health").json()

    assert health["node_id"] == "onprem-z97-server"
    assert health["configured_node_id"] == "darkfac-desktop"
    assert health["node_id_source"] == "cli"
    assert "Configured node id" not in caplog.text


def test_line_lock_delegates_without_changing_legacy_resolution(monkeypatch):
    monkeypatch.setattr(line_lock.socket, "gethostname", lambda: "AI_Notebook")
    monkeypatch.setenv("DARKFAC_HOST_ID", "  custom-host ")

    assert line_lock.resolve_host_id() == "custom-host"
    assert line_lock.resolve_host_id(" Custom-Override ") == "custom-override"

    monkeypatch.delenv("DARKFAC_HOST_ID")
    assert line_lock.resolve_host_id() == "notebook"
    assert resolve_host_id_detail(hostname="unmapped-machine").host_id == "unmapped-machine"


def test_fleet_host_discovery_delegates_with_existing_heuristics(monkeypatch):
    monkeypatch.setattr(start_fleet_worker.platform, "node", lambda: "grok-bot-vm-418374532")
    monkeypatch.setattr(start_fleet_worker.sys, "platform", "win32")
    monkeypatch.delenv("DARKFAC_HOST_ID", raising=False)
    assert start_fleet_worker.discover_local_host_id() == "grokbox"

    monkeypatch.setattr(start_fleet_worker.platform, "node", lambda: "unmapped-machine")
    monkeypatch.setattr(start_fleet_worker.sys, "platform", "linux")
    assert start_fleet_worker.discover_local_host_id() == "grokbox"

    monkeypatch.setattr(start_fleet_worker.sys, "platform", "win32")
    assert start_fleet_worker.discover_local_host_id() == "notebook"
