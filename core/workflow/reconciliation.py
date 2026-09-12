"""Environment manifest reconciliation post-implementation.

Conforms to Sections 2 and 7 of HYBRID_AUTONOMY_REQUIREMENTS and HF-09:
- Mandatory reconciliation after implementation, before acceptance, and at release.
- Inspects code, diffs, or explicit runtime changes to detect new dependencies,
  environment variables, ports, endpoints, and permission scopes.
- Generates a new sanitized versioned EnvironmentManifest and a ManifestDiff audit record.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.workflow.contracts import (
    Direction,
    EnvironmentEndpoint,
    EnvironmentManifest,
    EnvironmentPort,
    EnvironmentTool,
    SecretReference,
)


class ManifestDiff(BaseModel):
    """Detailed diff between original and reconciled environment manifests."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    has_changes: bool
    summary: str
    added_tools: list[EnvironmentTool] = Field(default_factory=list)
    added_services: list[str] = Field(default_factory=list)
    added_ports: list[EnvironmentPort] = Field(default_factory=list)
    added_env_vars: list[str] = Field(default_factory=list)
    added_endpoints: list[EnvironmentEndpoint] = Field(default_factory=list)
    added_secret_refs: list[SecretReference] = Field(default_factory=list)
    added_permission_scopes: list[str] = Field(default_factory=list)


_ENV_VAR_REGEX = re.compile(
    r"""(?:os\.(?:environ\.get|environ\[|getenv)|process\.env(?:\.|\[))\s*\(?['"]([A-Z0-9_]{2,64})['"]""",
    re.IGNORECASE,
)
_PORT_REGEX = re.compile(
    r"""(?:port\s*[:=]\s*|EXPOSE\s+|listen\s*\(\s*|--port\s+)(\d{2,5})""",
    re.IGNORECASE,
)
_URL_REGEX = re.compile(
    r"""(https?://[a-zA-Z0-9.\-_]+(?::\d+)?(?:/[^\s'"<>]*)?)""",
    re.IGNORECASE,
)


def extract_code_dependencies(code_or_diff: str) -> dict[str, Any]:
    """Inspect source code or git diff to extract potential environment requirements."""
    env_vars: set[str] = set()
    ports: set[int] = set()
    endpoints: set[str] = set()

    for match in _ENV_VAR_REGEX.finditer(code_or_diff):
        var_name = match.group(1).strip()
        if var_name:
            env_vars.add(var_name)

    for match in _PORT_REGEX.finditer(code_or_diff):
        try:
            port_num = int(match.group(1))
            if 1 <= port_num <= 65535:
                ports.add(port_num)
        except ValueError:
            continue

    for match in _URL_REGEX.finditer(code_or_diff):
        raw_url = match.group(1).strip()
        # Clean up any trailing punctuation
        cleaned = raw_url.rstrip(").,;'\"")
        if cleaned.startswith("http://") or cleaned.startswith("https://"):
            endpoints.add(cleaned)

    return {
        "env_vars": sorted(env_vars),
        "ports": sorted(ports),
        "endpoints": sorted(endpoints),
    }


def reconcile_environment_manifest(
    manifest: EnvironmentManifest,
    *,
    code_or_diff: str | None = None,
    declared_additions: dict[str, Any] | None = None,
) -> tuple[EnvironmentManifest, ManifestDiff]:
    """Reconcile an existing EnvironmentManifest against candidate code and declared changes.

    Returns the reconciled EnvironmentManifest (with updated version and timestamp)
    along with a structured ManifestDiff audit record.
    """
    inspected = extract_code_dependencies(code_or_diff or "")
    additions = declared_additions or {}

    # Merge inspected and explicit additions
    new_env_vars = set(inspected["env_vars"]).union(additions.get("env_vars", []))
    new_ports_raw = set(inspected["ports"]).union(additions.get("ports", []))
    new_endpoints_raw = set(inspected["endpoints"]).union(additions.get("endpoints", []))
    new_tools_raw: Sequence[dict[str, Any] | EnvironmentTool] = additions.get("tools", [])
    new_services: list[str] = additions.get("services", [])
    new_scopes: list[str] = additions.get("permission_scopes", [])
    new_secrets: Sequence[dict[str, Any] | SecretReference] = additions.get("secret_refs", [])

    # Calculate differences against current manifest
    existing_vars = set(manifest.required_env_vars)
    diff_vars = sorted(v for v in new_env_vars if v not in existing_vars)

    existing_port_nums = {p.port for p in manifest.ports}
    diff_ports: list[EnvironmentPort] = []
    for p in new_ports_raw:
        if isinstance(p, EnvironmentPort):
            if p.port not in existing_port_nums:
                diff_ports.append(p)
        elif isinstance(p, int):
            if p not in existing_port_nums:
                diff_ports.append(
                    EnvironmentPort(port=p, direction=Direction.INBOUND, purpose=f"Service port {p}")
                )

    existing_endpoint_urls = {e.url for e in manifest.endpoints}
    diff_endpoints: list[EnvironmentEndpoint] = []
    for idx, ep in enumerate(new_endpoints_raw, start=len(manifest.endpoints) + 1):
        if isinstance(ep, EnvironmentEndpoint):
            if ep.url not in existing_endpoint_urls:
                diff_endpoints.append(ep)
        elif isinstance(ep, str):
            if ep not in existing_endpoint_urls:
                protocol = "https" if ep.startswith("https") else "http"
                diff_endpoints.append(
                    EnvironmentEndpoint(
                        endpoint_id=f"ep_{idx}",
                        url=ep,
                        protocol=protocol,
                    )
                )

    existing_tools = {t.name for t in manifest.tools}
    diff_tools: list[EnvironmentTool] = []
    for t in new_tools_raw:
        if isinstance(t, EnvironmentTool):
            if t.name not in existing_tools:
                diff_tools.append(t)
        elif isinstance(t, dict):
            if t["name"] not in existing_tools:
                diff_tools.append(EnvironmentTool(**t))

    existing_services = set(manifest.services)
    diff_services = sorted(s for s in new_services if s not in existing_services)

    existing_scopes = set(manifest.permission_scopes)
    diff_scopes = sorted(s for s in new_scopes if s not in existing_scopes)

    existing_secrets = {s.secret_id for s in manifest.secret_refs}
    diff_secrets: list[SecretReference] = []
    for s in new_secrets:
        if isinstance(s, SecretReference):
            if s.secret_id not in existing_secrets:
                diff_secrets.append(s)
        elif isinstance(s, dict):
            if s["secret_id"] not in existing_secrets:
                diff_secrets.append(SecretReference(**s))

    has_changes = any([
        diff_vars,
        diff_ports,
        diff_endpoints,
        diff_tools,
        diff_services,
        diff_scopes,
        diff_secrets,
    ])

    if not has_changes:
        return manifest, ManifestDiff(
            has_changes=False,
            summary="Manifest is up-to-date with candidate code; no additions detected.",
        )

    # Compute next version ref
    base_ref = manifest.environment_ref
    match = re.search(r"_v(\d+)$", base_ref)
    if match:
        version_num = int(match.group(1)) + 1
        new_ref = f"{base_ref[:match.start()]}_v{version_num}"
    else:
        new_ref = f"{base_ref}_v2"

    merged_manifest = EnvironmentManifest(
        schema_version=1,
        environment_ref=new_ref,
        ticket_id=manifest.ticket_id,
        kind=manifest.kind,
        tools=list(manifest.tools) + diff_tools,
        system=manifest.system,
        architecture=manifest.architecture,
        services=list(manifest.services) + diff_services,
        accounts=manifest.accounts,
        secret_refs=list(manifest.secret_refs) + diff_secrets,
        permission_scopes=list(manifest.permission_scopes) + diff_scopes,
        required_env_vars=sorted(existing_vars.union(diff_vars)),
        endpoints=list(manifest.endpoints) + diff_endpoints,
        ports=list(manifest.ports) + diff_ports,
        connection_origins=manifest.connection_origins,
        network_policy=manifest.network_policy,
        proxy=manifest.proxy,
        tailscale=manifest.tailscale,
        volumes=manifest.volumes,
        quotas=manifest.quotas,
        worker_identity=manifest.worker_identity,
        installation=manifest.installation,
        probes=manifest.probes,
        rollback=manifest.rollback,
        cleanup=manifest.cleanup,
        target_differences=manifest.target_differences,
        observed_at=datetime.now(UTC),
    )

    summary_parts: list[str] = []
    if diff_vars:
        summary_parts.append(f"+{len(diff_vars)} env vars ({', '.join(diff_vars)})")
    if diff_ports:
        summary_parts.append(f"+{len(diff_ports)} ports ({', '.join(str(p.port) for p in diff_ports)})")
    if diff_endpoints:
        summary_parts.append(f"+{len(diff_endpoints)} endpoints")
    if diff_tools:
        summary_parts.append(f"+{len(diff_tools)} tools")
    if diff_services:
        summary_parts.append(f"+{len(diff_services)} services")
    if diff_scopes:
        summary_parts.append(f"+{len(diff_scopes)} scopes")
    if diff_secrets:
        summary_parts.append(f"+{len(diff_secrets)} secret refs")

    summary = f"Reconciliation updated {manifest.environment_ref} -> {new_ref}: " + "; ".join(summary_parts)

    diff = ManifestDiff(
        has_changes=True,
        summary=summary,
        added_tools=diff_tools,
        added_services=diff_services,
        added_ports=diff_ports,
        added_env_vars=diff_vars,
        added_endpoints=diff_endpoints,
        added_secret_refs=diff_secrets,
        added_permission_scopes=diff_scopes,
    )

    return merged_manifest, diff
