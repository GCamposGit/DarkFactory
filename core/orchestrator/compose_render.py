"""Shared render of the DarkHub raw compose (USR-144).

One place owns how ``deploy/dokploy/docker-compose.hub.yml`` becomes the raw
``composeFile`` installed in Dokploy, so the manual redeploy script
(``scripts/dokploy_redeploy.py``) and the production line's
``DokployDeploymentAdapter`` can never diverge again.

Also provides :func:`fetch_compose_at_sha`, which reads the compose file at an
exact commit through the GitHub contents API (``gh api``), with no local
checkout. Every failure is a :class:`ComposeSourceError`: callers must fail
closed and never deploy a compose they could not read at the merged SHA.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

HUB_COMPOSE_PATH = "deploy/dokploy/docker-compose.hub.yml"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_URL_RE = re.compile(
    r"^(?:https?://(?:[^/@]+@)?github\.com/|git@github\.com:)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_GIT_CONTEXT = "DarkFactory.git#${DARKFAC_GIT_SHA}"
_SHA_PLACEHOLDER = "${DARKFAC_GIT_SHA}"
FETCH_TIMEOUT_SECONDS = 30.0

# (argv, timeout_seconds) -> CompletedProcess. Injected by tests; the default
# shells out to ``gh`` without a shell.
GhRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
# (repository "owner/name", full sha, repo-relative path) -> file text.
ComposeFetcher = Callable[[str, str, str], str]


class ComposeRenderError(ValueError):
    """The compose source does not satisfy the pinned-build contract."""


class ComposeSourceError(RuntimeError):
    """The compose source could not be read at the requested commit."""


IMAGE_SOURCE_ENV = "DARKHUB_IMAGE_SOURCE"
IMAGE_SOURCE_BUILD = "build"
IMAGE_SOURCE_GHCR = "ghcr"
GHCR_IMAGE_REPOSITORY = "gcamposgit/darkhub"
GHCR_IMAGE_PREFIX = f"ghcr.io/{GHCR_IMAGE_REPOSITORY}"

_BUILD_BLOCK_RE = re.compile(
    r"^    build:\n(?:      .*\n)+    image: darkhub:v2\.1\n    pull_policy: build\n", re.M
)
_CONTAINER_NAME_RE = re.compile(r"^    container_name: .*\n", re.M)
_DEPLOY_RE = re.compile(r"^    deploy:\n", re.M)
_START_FIRST = (
    "    deploy:\n"
    "      # USR-339: start-first only takes effect under Swarm/Stack mode; plain `docker compose`\n"
    "      # ignores update_config. The healthcheck gates the traffic cut-over.\n"
    "      update_config:\n"
    "        order: start-first\n"
    "        parallelism: 1\n"
    "        monitor: 20s\n"
    "        failure_action: rollback\n"
    "      rollback_config:\n"
    "        order: start-first\n"
)


def resolve_hub_image_source(env: Optional[Mapping[str, str]] = None) -> str:
    """``ghcr`` (default); ``build`` only with explicit ``DARKHUB_IMAGE_SOURCE=build`` (USR-365). Unknown values fail closed."""
    raw = ((env if env is not None else os.environ).get(IMAGE_SOURCE_ENV) or "").strip().lower()
    if not raw:
        return IMAGE_SOURCE_GHCR
    if raw not in (IMAGE_SOURCE_BUILD, IMAGE_SOURCE_GHCR):
        raise ComposeRenderError(f"{IMAGE_SOURCE_ENV} must be 'build' or 'ghcr'")
    return raw


def ghcr_image_ref(sha: str) -> str:
    return f"{GHCR_IMAGE_PREFIX}:{sha}"


GHCR_WAIT_TIMEOUT_SECONDS = 600.0
GHCR_WAIT_POLL_SECONDS = 15.0
GhcrManifestProbe = Callable[[str, str], int]


class GhcrImageUnavailableError(ComposeRenderError):
    """The GHCR tag for the SHA did not appear in time; the deploy must not start."""


def _ghcr_registry_token() -> str:
    """Return the configured GHCR credential without exposing it to callers."""
    for name in ("GHCR_TOKEN", "GITHUB_PAT"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    if sys.platform == "win32":
        for name in ("GHCR_TOKEN", "GITHUB_PAT"):
            value = _windows_user_env(name)
            if value:
                return value
    return ""


def _windows_user_env(name: str) -> str:
    """Read a Windows user-level environment variable (registry HKCU, Environment); never logged."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except Exception:  # noqa: BLE001 - missing key/value or non-Windows
        return ""
    return str(value or "").strip()


def _ghcr_manifest_status(repository: str, tag: str) -> int:
    """HEAD the GHCR manifest (token: GHCR_TOKEN or GITHUB_PAT with read:packages)."""
    import base64

    token = _ghcr_registry_token()
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


def wait_for_ghcr_image(
    sha: str,
    *,
    probe: Optional[GhcrManifestProbe] = None,
    timeout: float = GHCR_WAIT_TIMEOUT_SECONDS,
    interval: float = GHCR_WAIT_POLL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Block until ``ghcr.io/gcamposgit/darkhub:<sha>`` exists; raise GhcrImageUnavailableError on timeout."""
    check = probe or _ghcr_manifest_status
    deadline = clock_fn() + timeout
    last: object = "no response"
    while True:
        try:
            status = check(GHCR_IMAGE_REPOSITORY, sha)
            if status == 200:
                return
            last = f"HTTP {status}"
            if status in (401, 403) and not _ghcr_registry_token():
                raise GhcrImageUnavailableError(
                    f"Cannot verify GHCR image {ghcr_image_ref(sha)}: HTTP {status} and no GHCR token is configured; "
                    "set GHCR_TOKEN with read:packages permission (OA-053). Deploy refused, no fallback to build."
                )
        except GhcrImageUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
        remaining = deadline - clock_fn()
        if remaining <= 0:
            raise GhcrImageUnavailableError(
                f"GHCR image {ghcr_image_ref(sha)} not published within {int(timeout)}s (last: {last}); "
                "deploy refused, no fallback to build (check the hub-image workflow)"
            )
        sleep_fn(min(interval, remaining))


def render_darkhub_compose(source: str, sha: str, image_source: Optional[str] = None) -> str:
    """Bind the Hub image (Git build context or GHCR tag) and container environment to one SHA.

    ``image_source`` defaults to :func:`resolve_hub_image_source` (``ghcr`` unless the owner opted into ``build``).
    """
    if not SHA_PATTERN.fullmatch(sha):
        raise ComposeRenderError("Darkhub build requires a full origin/main SHA")
    mode = image_source if image_source is not None else resolve_hub_image_source()
    if mode not in (IMAGE_SOURCE_BUILD, IMAGE_SOURCE_GHCR):
        raise ComposeRenderError("image source must be 'build' or 'ghcr'")
    if (
        _GIT_CONTEXT not in source
        or source.count(_SHA_PLACEHOLDER) != 3
        or "no_cache: true" not in source
        or "pull_policy: build" not in source
    ):
        raise ComposeRenderError("Darkhub compose lacks the pinned Git build and SHA contract")
    if mode == IMAGE_SOURCE_BUILD:
        return source.replace(_SHA_PLACEHOLDER, sha)
    image_lines = f"    image: {GHCR_IMAGE_PREFIX}:{_SHA_PLACEHOLDER}\n    pull_policy: always\n"
    transformed, n_build = _BUILD_BLOCK_RE.subn(lambda _m: image_lines, source, count=1)
    transformed, n_deploy = _DEPLOY_RE.subn(lambda _m: _START_FIRST, transformed, count=1)
    if n_build != 1 or n_deploy != 1:
        raise ComposeRenderError("Darkhub compose cannot be converted to the GHCR image layout")
    transformed = _CONTAINER_NAME_RE.sub("", transformed)
    return transformed.replace(_SHA_PLACEHOLDER, sha)


def repository_from_url(repo_url: Optional[str]) -> Optional[str]:
    """``owner/name`` from a GitHub remote URL, or ``None`` when not GitHub."""
    match = _GITHUB_URL_RE.fullmatch((repo_url or "").strip())
    return match.group("repo") if match else None


def _default_gh_runner(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **kwargs,  # type: ignore[arg-type]
    )


def fetch_compose_at_sha(
    repository: str,
    sha: str,
    path: str = HUB_COMPOSE_PATH,
    *,
    runner: Optional[GhRunner] = None,
    gh_executable: str = "gh",
) -> str:
    """Read ``path`` at commit ``sha`` of ``repository`` through ``gh api``.

    No checkout is involved. Raises :class:`ComposeSourceError` (never returns
    an empty string) when the repository/sha/path are malformed, ``gh`` is
    missing or fails, or the file is empty.
    """
    if not _REPOSITORY_RE.fullmatch(repository or ""):
        raise ComposeSourceError("repository must use the owner/name form")
    if not SHA_PATTERN.fullmatch(sha or ""):
        raise ComposeSourceError("a full 40-character commit SHA is required")
    if (
        not path
        or path.startswith("/")
        or ".." in path.split("/")
        or any(ch in path for ch in "\\?#:")
    ):
        raise ComposeSourceError("invalid repository-relative compose path")
    argv = [
        gh_executable,
        "api",
        "-H",
        "Accept: application/vnd.github.raw",
        f"repos/{repository}/contents/{path}?ref={sha}",
    ]
    try:
        result = (runner or _default_gh_runner)(argv, FETCH_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("compose fetch at %s failed to run gh: %s", sha[:12], type(exc).__name__)
        raise ComposeSourceError(f"gh unavailable ({type(exc).__name__})") from None
    if result.returncode != 0:
        logger.warning("compose fetch at %s returned gh exit code %s", sha[:12], result.returncode)
        raise ComposeSourceError(f"gh api exited with code {result.returncode}")
    if not (result.stdout or "").strip():
        raise ComposeSourceError("compose file is empty at the requested commit")
    return result.stdout


__all__ = [
    "GHCR_IMAGE_PREFIX",
    "GhcrImageUnavailableError",
    "wait_for_ghcr_image",
    "GHCR_IMAGE_REPOSITORY",
    "IMAGE_SOURCE_BUILD",
    "IMAGE_SOURCE_ENV",
    "IMAGE_SOURCE_GHCR",
    "ghcr_image_ref",
    "resolve_hub_image_source",
    "ComposeFetcher",
    "ComposeRenderError",
    "ComposeSourceError",
    "HUB_COMPOSE_PATH",
    "SHA_PATTERN",
    "fetch_compose_at_sha",
    "render_darkhub_compose",
    "repository_from_url",
]
