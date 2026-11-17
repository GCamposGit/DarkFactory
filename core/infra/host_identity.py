"""Canonical host identity shared by workers, fleet scheduling, and line locks."""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass
from typing import Literal, Mapping

NODE_ID_ENV = "DARKFAC_NODE_ID"

LOGICAL_HOSTS = ("grokbox", "notebook", "desktop")
CANONICAL_NODE_IDS: dict[str, str] = {
    "grokbox": "grokbox-test-worker",
    "notebook": "predator-neo-16",
    "desktop": "onprem-z97-server",
}

# This is the single alias table for logical hosts, inventory ids, real hostnames,
# legacy worker ids, and the Tailscale addresses used by remote dispatch.
HOST_ALIASES: dict[str, frozenset[str]] = {
    "grokbox": frozenset(
        {
            "grokbox",
            "grokbox-test-worker",
            "grok-bot-vm-418374532",
            "100.77.130.5",
        }
    ),
    "notebook": frozenset(
        {
            "notebook",
            "predator-neo-16",
            "ai_notebook",
            "ai-notebook",
            "100.81.84.124",
        }
    ),
    "desktop": frozenset(
        {
            "desktop",
            "desktop-g45ipem",
            "darkfac-desktop",
            "onprem-z97-server",
            "100.78.181.90",
        }
    ),
}

_ALIAS_TO_LOGICAL_HOST = {
    alias.casefold(): host for host, aliases in HOST_ALIASES.items() for alias in aliases
}

NodeIdSource = Literal["cli", "env", "hostname", "fallback"]
HostIdSource = Literal["argument", "environment", "hostname", "fallback"]


@dataclass(frozen=True)
class NodeIdentity:
    """Worker identity and the evidence used to choose it."""

    node_id: str
    logical_host: str | None
    configured_node_id: str | None
    source: NodeIdSource
    hostname: str
    configured_logical_host: str | None = None
    corrected: bool = False


@dataclass(frozen=True)
class HostIdDetail:
    """Result of the legacy line-lock or fleet-host resolution rules."""

    host_id: str
    logical_host: str | None
    source: HostIdSource


def logical_host(identifier: str | None) -> str | None:
    """Return the logical fleet host for any known alias, or ``None``."""
    if identifier is None:
        return None
    return _ALIAS_TO_LOGICAL_HOST.get(identifier.strip().casefold())


def canonical_node_id(identifier: str | None) -> str | None:
    """Return a known host's canonical inventory node id; preserve unknown ids."""
    if identifier is None or not identifier.strip():
        return None
    host = logical_host(identifier)
    if host is not None:
        return CANONICAL_NODE_IDS[host]
    return identifier.strip()


def resolve_node_identity(
    configured_node_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> NodeIdentity:
    """Resolve a worker node id, correcting only a provable cross-host mismatch.

    An explicit CLI id takes precedence over ``DARKFAC_NODE_ID``. Known aliases
    are normalized to the canonical inventory id. A known configured id is
    overridden only when the local hostname also identifies a different known
    host; unknown local hosts retain configured ids because there is no safe
    evidence that they belong elsewhere.
    """
    environ = os.environ if env is None else env
    resolved_hostname = (socket.gethostname() if hostname is None else hostname).strip()
    local_host = logical_host(resolved_hostname)

    cli_id = (configured_node_id or "").strip()
    env_id = (environ.get(NODE_ID_ENV) or "").strip()
    configured_id = cli_id or env_id or None
    configured_source: NodeIdSource = "cli" if cli_id else "env"
    configured_host = logical_host(configured_id)

    if configured_id is not None:
        if local_host is not None and configured_host is not None and local_host != configured_host:
            return NodeIdentity(
                node_id=CANONICAL_NODE_IDS[local_host],
                logical_host=local_host,
                configured_node_id=configured_id,
                source="hostname",
                hostname=resolved_hostname,
                configured_logical_host=configured_host,
                corrected=True,
            )
        if configured_host is not None:
            return NodeIdentity(
                node_id=CANONICAL_NODE_IDS[configured_host],
                logical_host=configured_host,
                configured_node_id=configured_id,
                source=configured_source,
                hostname=resolved_hostname,
                configured_logical_host=configured_host,
            )
        return NodeIdentity(
            node_id=configured_id,
            logical_host=local_host,
            configured_node_id=configured_id,
            source=configured_source,
            hostname=resolved_hostname,
        )

    if local_host is not None:
        return NodeIdentity(
            node_id=CANONICAL_NODE_IDS[local_host],
            logical_host=local_host,
            configured_node_id=None,
            source="hostname",
            hostname=resolved_hostname,
        )

    if resolved_hostname:
        return NodeIdentity(
            node_id=resolved_hostname.casefold(),
            logical_host=None,
            configured_node_id=None,
            source="hostname",
            hostname=resolved_hostname,
        )

    return NodeIdentity(
        node_id="unknown-host",
        logical_host=None,
        configured_node_id=None,
        source="fallback",
        hostname="",
    )


def resolve_host_id_detail(
    host_id: str | None = None,
    *,
    env_host_id: str | None = None,
    hostname: str | None = None,
    fleet_compat: bool = False,
    platform_name: str | None = None,
) -> HostIdDetail:
    """Resolve legacy host ids while retaining the line-lock and fleet rules.

    ``fleet_compat`` preserves ``start_fleet_worker``'s environment allowlist,
    hostname heuristics, and OS fallback. Without it, resolution preserves the
    existing line-lock precedence and hostname substring behavior exactly.
    """
    if not fleet_compat:
        if host_id and host_id.strip():
            value = host_id.strip().lower()
            return HostIdDetail(value, logical_host(value), "argument")
        if env_host_id and env_host_id.strip():
            value = env_host_id.strip().lower()
            return HostIdDetail(value, logical_host(value), "environment")
        raw_name = (socket.gethostname() if hostname is None else hostname).lower()
        for known in LOGICAL_HOSTS:
            if known in raw_name:
                return HostIdDetail(known, logical_host(raw_name), "hostname")
        return HostIdDetail(raw_name, logical_host(raw_name), "hostname")

    env_host = (env_host_id or "").strip().lower()
    if env_host in LOGICAL_HOSTS:
        return HostIdDetail(env_host, logical_host(env_host), "environment")

    raw_name = (socket.gethostname() if hostname is None else hostname).lower()
    if "grok" in raw_name or "vm-418" in raw_name:
        return HostIdDetail("grokbox", logical_host(raw_name), "hostname")
    if "desktop" in raw_name or "g45ipem" in raw_name:
        return HostIdDetail("desktop", logical_host(raw_name), "hostname")
    if "notebook" in raw_name or "ai_notebook" in raw_name or "ai-notebook" in raw_name:
        return HostIdDetail("notebook", logical_host(raw_name), "hostname")

    current_platform = sys.platform if platform_name is None else platform_name
    fallback_host = "grokbox" if current_platform.startswith("linux") else "notebook"
    return HostIdDetail(fallback_host, fallback_host, "fallback")

