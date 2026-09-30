"""Resolve the full git commit SHA a running node was built from (USR-65)."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Mapping

logger = logging.getLogger(__name__)

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GIT_SHA_ENV = "DARKFAC_GIT_SHA"
GIT_TIMEOUT_SECONDS = 3.0

GitHeadReader = Callable[[Path, float], str]


def _read_head(root: Path, timeout: float) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_sha(value: object) -> str | None:
    """Return ``value`` lower-cased when it is a full 40-hex SHA, else ``None``."""
    candidate = str(value or "").strip().lower()
    return candidate if SHA_PATTERN.fullmatch(candidate) else None


def current_git_sha(
    root: Path,
    *,
    use_env: bool = False,
    env: Mapping[str, str] | None = None,
    read_head: GitHeadReader = _read_head,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str | None:
    """Full HEAD SHA of ``root`` or ``None`` when it cannot be determined.

    With ``use_env`` (deployed containers have no ``.git``) a valid
    ``DARKFAC_GIT_SHA`` wins; otherwise ``git rev-parse HEAD`` runs with a
    ``timeout``-second limit. Never raises.
    """
    if use_env:
        sha = normalize_sha((env if env is not None else os.environ).get(GIT_SHA_ENV))
        if sha:
            return sha
    try:
        return normalize_sha(read_head(root, timeout))
    except Exception as exc:  # git missing, timeout, not a repo
        logger.debug("git rev-parse HEAD failed for %s: %s", root, type(exc).__name__)
        return None
