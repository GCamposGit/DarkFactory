"""Cached, failure-tolerant reads of versioned Hub data from GitHub (USR-207)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

ENV_ENABLE = "DARKHUB_REMOTE_DATA"
ENV_REPO = "DARKFAC_GITHUB_REPO"
ENV_TOKEN = "GITHUB_TOKEN"
ENV_TOKEN_FALLBACK = "GITHUB_PAT"
DEFAULT_REPO = "GCamposGit/DarkFactory"
DEFAULT_TTL_SEC = 120.0
HTTP_TIMEOUT_SEC = 4.0
DEMANDS_PATH = ".factory/demands/demands.json"
OWNER_ACTIONS_PATH = ".factory/owner_actions/owner_actions.json"

# Every versioned factory/document file currently read by Hub roadmap and operations panels.
HUB_DATA_INVENTORY = frozenset({
    DEMANDS_PATH,
    OWNER_ACTIONS_PATH,
    ".factory/roadmap/darkfac.json",
    ".factory/infra/roadmap.json",
    ".factory/planning/continuous-autonomy/plan.json",
    "docs/DEVELOPMENT_PLAN_2026-09-05.md",
})
HUB_REMOTE_DATA_FILES = HUB_DATA_INVENTORY


class RemoteData:
    """Fetch GitHub content with per-path TTL cache, failure backoff, and last-good retention."""

    def __init__(
        self,
        repo: str,
        *,
        ref: str = "main",
        token: str | None = None,
        ttl: float = DEFAULT_TTL_SEC,
        fetch: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo, self.ref, self.token, self.ttl = repo, ref, token, max(0.0, float(ttl))
        self._fetch = fetch or self._http_fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._payloads: dict[str, Any] = {}
        self._attempted_at: dict[str, float] = {}

    def _http_fetch(self, path: str) -> Any:
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        url = f"https://api.github.com/repos/{self.repo}/contents/{quoted}?ref={self.ref}"
        headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "darkhub-remote-data"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_SEC) as response:
            content = response.read().decode("utf-8")
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content

    def get(self, path: str) -> Any:
        """Return cached/current content, last successful content, or None. Never raises."""
        key = str(path).replace("\\", "/").removeprefix("./")
        with self._lock:
            now = self._clock()
            attempted = self._attempted_at.get(key)
            if attempted is not None and now - attempted < self.ttl:
                return self._payloads.get(key)
            self._attempted_at[key] = now
            try:
                self._payloads[key] = self._fetch(key)
            except Exception as exc:
                logger.warning("remote Hub data unavailable for %s (%s)", key, type(exc).__name__)
            return self._payloads.get(key)


def from_env() -> RemoteData | None:
    """Create the shared Hub reader; DARKHUB_REMOTE_DATA=0/false/off disables it."""
    if os.environ.get(ENV_ENABLE, "").strip().lower() in {"0", "false", "off"}:
        return None
    repo = os.environ.get(ENV_REPO, "").strip() or DEFAULT_REPO
    token = os.environ.get(ENV_TOKEN, "").strip() or os.environ.get(ENV_TOKEN_FALLBACK, "").strip()
    return RemoteData(repo, token=token or None)


def read_json(
    local_path: Path,
    remote: RemoteData | None,
    remote_path: str,
    *,
    validator: Callable[[Any], bool] | None = None,
) -> Any:
    """Prefer valid remote JSON and fall back to the embedded/local JSON on every failure."""
    if remote is not None:
        try:
            payload = remote.get(remote_path)
            if isinstance(payload, (dict, list)) and (validator is None or validator(payload)):
                return payload
        except Exception as exc:  # defensive boundary for injected/custom readers
            logger.warning("remote Hub data read failed for %s (%s)", remote_path, type(exc).__name__)
    try:
        content = Path(local_path).read_text(encoding="utf-8")
        payload = json.loads(content) if content.strip() else []
        return payload if isinstance(payload, (dict, list)) else []
    except Exception as exc:
        logger.warning("local Hub data read failed for %s (%s)", local_path, type(exc).__name__)
        return []


def read_text(local_path: Path, remote: RemoteData | None, remote_path: str) -> str:
    """Prefer remote text and fall back to the local embedded copy."""
    if remote is not None:
        try:
            payload = remote.get(remote_path)
            if isinstance(payload, str):
                return payload
            if isinstance(payload, (dict, list)):
                return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.warning("remote Hub text read failed for %s (%s)", remote_path, type(exc).__name__)
    try:
        return Path(local_path).read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("local Hub text read failed for %s (%s)", local_path, type(exc).__name__)
        return ""
