"""Shared GHCR image-tag readiness gate (USR-339, phase 2 of USR-207).

One place owns "block until ``ghcr.io/<repository>:<tag>`` exists" so
``scripts/dokploy_redeploy.py`` (manual CLI redeploy) and the production
line's ``DokployDeploymentAdapter`` (``core/orchestrator/deployment_adapter.py``)
can never diverge on what counts as "the image is published" before a
``ghcr``-mode DarkHub deploy is allowed to touch Dokploy.

There is no fallback to ``build`` anywhere in this module: a caller that
gets :class:`GhcrTagUnavailableError` must fail closed.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

GHCR_TAG_TIMEOUT_SECONDS = 900.0
GHCR_TAG_POLL_SECONDS = 15.0

# (repository, tag) -> HTTP status of the manifest lookup (200 = exists, 404 = not yet).
GhcrManifestProbe = Callable[[str, str], int]


class GhcrTagUnavailableError(RuntimeError):
    """``ghcr.io/<repository>:<tag>`` was not published within the deadline."""


def ghcr_manifest_url(repository: str, tag: str) -> str:
    return f"https://ghcr.io/v2/{repository}/manifests/{tag}"


def default_manifest_probe(repository: str, tag: str) -> int:
    """HEAD the GHCR manifest; needs GHCR_TOKEN or GITHUB_PAT (read:packages) for the private package."""
    token = (os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_PAT") or "").strip()
    headers = {
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    }
    if token:
        headers["Authorization"] = "Bearer " + base64.b64encode(token.encode()).decode()
    request = urllib.request.Request(ghcr_manifest_url(repository, tag), headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def wait_for_tag(
    repository: str,
    tag: str,
    *,
    probe: Optional[GhcrManifestProbe] = None,
    timeout: float = GHCR_TAG_TIMEOUT_SECONDS,
    interval: float = GHCR_TAG_POLL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Blocks until ``ghcr.io/<repository>:<tag>`` exists.

    Probe errors (network blips, non-200/404 responses) are tolerated and
    retried until ``timeout`` elapses, at which point this raises
    :class:`GhcrTagUnavailableError`. Never returns a fallback value and
    never raises for anything other than a confirmed timeout -- there is no
    silent "assume it's there" path.
    """
    check = probe or default_manifest_probe
    deadline = clock_fn() + timeout
    last: object = "no response"
    while True:
        try:
            status = check(repository, tag)
            if status == 200:
                return
            last = f"HTTP {status}"
        except Exception as exc:
            last = type(exc).__name__
        remaining = deadline - clock_fn()
        if remaining <= 0:
            raise GhcrTagUnavailableError(
                f"ghcr.io/{repository}:{tag} not published within {int(timeout)}s (last: {last})"
            )
        sleep_fn(min(interval, remaining))


__all__ = [
    "GHCR_TAG_POLL_SECONDS",
    "GHCR_TAG_TIMEOUT_SECONDS",
    "GhcrManifestProbe",
    "GhcrTagUnavailableError",
    "default_manifest_probe",
    "ghcr_manifest_url",
    "wait_for_tag",
]
