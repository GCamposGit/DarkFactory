"""USR-212: the Hub container must not identify itself as the Notebook."""

from core.infra import metrics
from core.infra.metrics import NOTEBOOK_NODE_ID, VPS_NODE_ID, resolve_host_node_id


def test_explicit_env_wins_over_hostname():
    assert resolve_host_node_id({"DARKFAC_NODE_ID": "custom-node"}, "darkhub-cloud-247") == "custom-node"


def test_hostname_fallback_for_hub_container():
    assert resolve_host_node_id({}, "darkhub-cloud-247") == VPS_NODE_ID
    assert resolve_host_node_id({"DARKFAC_NODE_ID": "  "}, "darkhub-cloud-247") == VPS_NODE_ID


def test_notebook_hostname_maps_case_insensitive():
    assert resolve_host_node_id({}, "AI_Notebook") == NOTEBOOK_NODE_ID
    assert resolve_host_node_id({}, "ai_notebook") == NOTEBOOK_NODE_ID


def test_unknown_host_never_defaults_to_notebook():
    assert resolve_host_node_id({}, "abc123def456") == "abc123def456"
    assert resolve_host_node_id({}, "") != NOTEBOOK_NODE_ID


def test_compose_sets_vps_node_id():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "deploy/dokploy/docker-compose.hub.yml").read_text(encoding="utf-8")
    assert "DARKFAC_NODE_ID=${DARKFAC_NODE_ID:-darkfac-vps-primary}" in text


def test_report_on_vps_marks_notebook_without_recent_data(monkeypatch):
    monkeypatch.setenv("DARKFAC_NODE_ID", VPS_NODE_ID)
    report = metrics.build_infra_metrics_report(timeout=0.05)
    assert report.host_node_id == VPS_NODE_ID
    assert report.host_hardware.node_id == VPS_NODE_ID
    nodes = {n.node_id: n for n in report.nodes}
    assert nodes[NOTEBOOK_NODE_ID].hardware is None
    assert nodes[NOTEBOOK_NODE_ID].status == "no_recent_data"
    assert nodes[VPS_NODE_ID].hardware is not None
