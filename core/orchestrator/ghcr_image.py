"""GHCR manifest probing and wait logic shared by deployment entry points."""

from __future__ import annotations

import base64
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from core.orchestrator.compose_render import GHCR_IMAGE_REPOSITORY, ghcr_image_ref

GHCR_TAG_TIMEOUT_SECONDS = 900.0
GHCR_TAG_POLL_SECONDS = 15.0

# (repository, tag) -> HTTP status of the manifest lookup (200 = exists).
GhcrManifestProbe = Callable[[str, str], int]


class GhcrTagUnavailableError(RuntimeError):
    """The immutable image tag did not become available before the deadline."""


def probe_ghcr_manifest(repository: str, tag: str) -> int:
    """HEAD a GHCR manifest, authenticating with GHCR_TOKEN or GITHUB_PAT."""
    token = (os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_PAT") or "").strip()
    headers = {
        "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json"
    }
    if token:
        headers["Authorization"] = "Bearer " + base64.b64encode(token.encode()).decode()
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{repository}/manifests/{tag}", headers=headers, method="HEAD"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def wait_for_ghcr_tag(
    sha: str,
    *,
    probe: Optional[GhcrManifestProbe] = None,
    timeout: float = GHCR_TAG_TIMEOUT_SECONDS,
    interval: float = GHCR_TAG_POLL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for the SHA tag; transient probe errors do not abort polling."""
    check = probe or probe_ghcr_manifest
    deadline = clock_fn() + timeout
    last: object = "no response"
    while True:
        try:
            status = check(GHCR_IMAGE_REPOSITORY, sha)
            if status == 200:
                return
            last = f"HTTP {status}"
        except Exception as exc:
            last = type(exc).__name__
        remaining = deadline - clock_fn()
        if remaining <= 0:
            raise GhcrTagUnavailableError(
                f"GHCR image {ghcr_image_ref(sha)} not published within {int(timeout)}s (last: {last}); "
                "deploy refused, no fallback to build"
            )
        sleep_fn(min(interval, remaining))

