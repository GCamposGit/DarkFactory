"""Read the owner action ledger from origin/main with a short cache (USR-348).

Merges that only touch ``.factory/**`` do not trigger a redeploy, so the copy baked into the Hub
image goes stale. The Hub reads the ledger from GitHub instead, falling back to the local file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

ENV_ENABLE = "DARKHUB_OWNER_ACTIONS_REMOTE"
ENV_REPO = "DARKFAC_GITHUB_REPO"
ENV_TOKEN = "GITHUB_TOKEN"
DEFAULT_TTL_SEC = 120.0
HTTP_TIMEOUT_SEC = 4.0
LEDGER_PATH = ".factory/owner_actions/owner_actions.json"
DEMANDS_LEDGER_PATH = ".factory/demands/demands.json"


class RemoteLedger:
    """Cached fetcher; ``fetch`` is injectable for tests. Never raises."""

    def __init__(self, repo: str, *, ref: str = "main", token: str | None = None,
                 path: str = LEDGER_PATH, ttl: float = DEFAULT_TTL_SEC, fetch: Any = None,
                 clock: Any = time.monotonic) -> None:
        self.repo, self.ref, self.token = repo, ref, token
        self.path = path
        self.ttl, self._fetch, self._clock = ttl, fetch or self._http_fetch, clock
        self._lock = threading.Lock()
        self._payload: Any = None
        self._at: float | None = None
        self._last_fetch_succeeded = False

    def _http_fetch(self) -> Any:
        url = f"https://api.github.com/repos/{self.repo}/contents/{self.path}?ref={self.ref}"
        headers = {"Accept": "application/vnd.github.raw+json", "User-Agent": "darkhub-owner-actions"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_SEC) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(self) -> Any:
        """Latest payload, or the last good one, or None when never fetched."""
        with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < self.ttl:
                return self._payload
            try:
                self._payload = self._fetch()
                self._last_fetch_succeeded = True
            except Exception as exc:
                logger.warning("owner_actions remote ledger unavailable (%s)", type(exc).__name__)
                self._last_fetch_succeeded = False
            self._at = now  # also backs off after failures
            return self._payload

    def get_or_none(self) -> Any:
        """Return the cached payload only when its latest fetch succeeded.

        Demand readers use this to fall back to their local ledger after a failed
        refresh. ``get`` keeps its historical stale-last-good behavior for owner actions.
        """
        payload = self.get()
        with self._lock:
            return payload if self._last_fetch_succeeded else None


DEFAULT_REPO = "GCamposGit/DarkFactory"
ENV_TOKEN_FALLBACK = "GITHUB_PAT"


def from_env(*, path: str = LEDGER_PATH) -> RemoteLedger | None:
    """On by default in production; opt out with DARKHUB_OWNER_ACTIONS_REMOTE=0/false/off."""
    if os.environ.get(ENV_ENABLE, "").strip().lower() in {"0", "false", "off"}:
        return None
    repo = os.environ.get(ENV_REPO, "").strip() or DEFAULT_REPO
    token = os.environ.get(ENV_TOKEN, "").strip() or os.environ.get(ENV_TOKEN_FALLBACK, "").strip()
    return RemoteLedger(repo, token=token or None, path=path)
