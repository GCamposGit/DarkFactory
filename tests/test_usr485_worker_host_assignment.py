"""USR-485 T2: disabled-host assignment uses the shared host identity map."""

import pytest

from core.harness import remote_dispatch
from core.infra.host_identity import HOST_ALIASES


@pytest.mark.parametrize(
    ("node_id", "hostname", "disabled", "expected"),
    [
        ("onprem-z97-server", "worker-host", {"desktop"}, True),
        ("darkfac-desktop", "worker-host", {"desktop"}, True),
        ("onprem-z97-server", "worker-host", {"darkfac-desktop"}, True),
        ("darkfac-desktop", "worker-host", {"onprem-z97-server"}, True),
        ("predator-neo-16", "worker-host", {"desktop"}, False),
        (None, "AI_Notebook", {"desktop"}, False),
    ],
)
def test_disabled_host_checks_resolve_worker_identity_first(
    node_id: str | None,
    hostname: str,
    disabled: set[str],
    expected: bool,
) -> None:
    assert remote_dispatch.is_worker_node_disabled(node_id, hostname, disabled) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://100.78.181.90:8080", True),
        ("http://onprem-z97-server:8080", True),
        ("http://darkfac-desktop:8080", True),
        ("http://100.81.84.124:8080", False),
    ],
)
def test_worker_url_filter_uses_inventory_host_identity(url: str, expected: bool) -> None:
    assert remote_dispatch.is_worker_disabled(url, {"desktop"}) is expected


def test_known_host_identifiers_exports_inventory_ids() -> None:
    assert remote_dispatch.KNOWN_HOST_IDENTIFIERS == HOST_ALIASES
    assert "onprem-z97-server" in remote_dispatch.KNOWN_HOST_IDENTIFIERS["desktop"]
    assert "predator-neo-16" in remote_dispatch.KNOWN_HOST_IDENTIFIERS["notebook"]


def test_desktop_environment_filter_matches_inventory_id_but_keeps_notebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop")

    assert remote_dispatch.is_worker_node_disabled("onprem-z97-server", "worker-host")
    assert not remote_dispatch.is_worker_node_disabled("predator-neo-16", "AI_Notebook")

