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
import re
import subprocess
import sys
from typing import Callable, Optional, Sequence

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


def render_darkhub_compose(source: str, sha: str) -> str:
    """Bind both the Git build context and container environment to one SHA."""
    if not SHA_PATTERN.fullmatch(sha):
        raise ComposeRenderError("Darkhub build requires a full origin/main SHA")
    if (
        _GIT_CONTEXT not in source
        or source.count(_SHA_PLACEHOLDER) != 3
        or "no_cache: true" not in source
        or "pull_policy: build" not in source
    ):
        raise ComposeRenderError("Darkhub compose lacks the pinned Git build and SHA contract")
    return source.replace(_SHA_PLACEHOLDER, sha)


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
    "ComposeFetcher",
    "ComposeRenderError",
    "ComposeSourceError",
    "HUB_COMPOSE_PATH",
    "SHA_PATTERN",
    "fetch_compose_at_sha",
    "render_darkhub_compose",
    "repository_from_url",
]
