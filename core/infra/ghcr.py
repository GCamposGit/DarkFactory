"""Shared, fail-closed probes for immutable GHCR release images."""

from __future__ import annotations

import base64
import os
import re
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

GHCR_IMAGE_REPOSITORY = "gcamposgit/darkhub"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

GhcrManifestProbe = Callable[[str, str], int]


class GhcrError(RuntimeError):
    """Base class for safe GHCR wait failures."""


class GhcrImageNotPublished(GhcrError):
    """The requested manifest was still absent when the wait expired."""


class GhcrCredentialsError(GhcrError):
    """GHCR rejected the configured package read credentials."""


class GhcrWaitTimeout(GhcrError):
    """The manifest probe did not converge before its deadline."""


def ghcr_image_ref(sha: str) -> str:
    return f"ghcr.io/{GHCR_IMAGE_REPOSITORY}:{sha}"


def probe_ghcr_manifest(repository: str, tag: str) -> int:
    """HEAD one GHCR manifest. Credential values are never included in errors."""
    token = (os.environ.get("GHCR_TOKEN") or os.environ.get("GITHUB_PAT") or "").strip()
    headers = {
        "Accept": "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json",
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
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Wait for an exact SHA tag; distinguish absent images, bad credentials and other timeouts."""
    if not _SHA_RE.fullmatch(sha or ""):
        raise ValueError("a full 40-character SHA is required for a GHCR image")
    if timeout < 0 or interval <= 0:
        raise ValueError("timeout must be non-negative and interval must be positive")

    check = probe or probe_ghcr_manifest
    deadline = clock_fn() + timeout
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    while True:
        try:
            status = int(check(GHCR_IMAGE_REPOSITORY, sha))
        except Exception as exc:
            last_error = type(exc).__name__
            last_status = None
        else:
            if status == 200:
                return
            if status in (401, 403):
                raise GhcrCredentialsError(
                    f"GHCR credentials rejected (HTTP {status}); configure GHCR_TOKEN or GITHUB_PAT "
                    "with read:packages"
                ) from None
            last_status = status
            last_error = None

        remaining = deadline - clock_fn()
        if remaining <= 0:
            if last_status == 404:
                raise GhcrImageNotPublished(
                    f"GHCR image {ghcr_image_ref(sha)} not published within {int(timeout)}s"
                )
            detail = f"HTTP {last_status}" if last_status is not None else (last_error or "no response")
            raise GhcrWaitTimeout(
                f"GHCR image {ghcr_image_ref(sha)} not published within {int(timeout)}s (last: {detail})"
            )
        sleep_fn(min(interval, remaining))


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "GHCR_IMAGE_REPOSITORY",
    "GhcrCredentialsError",
    "GhcrImageNotPublished",
    "GhcrManifestProbe",
    "GhcrWaitTimeout",
    "ghcr_image_ref",
    "probe_ghcr_manifest",
    "wait_for_ghcr_tag",
]
