"""Compatibility adapter for reading the owner action ledger through shared remote data."""
from __future__ import annotations

import os
from typing import Any, Callable

from core.infra.remote_data import (
    DEFAULT_REPO,
    DEFAULT_TTL_SEC,
    ENV_REPO,
    ENV_TOKEN,
    ENV_TOKEN_FALLBACK,
    RemoteData,
)

ENV_ENABLE = "DARKHUB_OWNER_ACTIONS_REMOTE"
LEDGER_PATH = ".factory/owner_actions/owner_actions.json"


class RemoteLedger:
    """Keep the USR-348 no-argument fetch API while using the shared cache implementation."""

    def __init__(
        self,
        repo: str,
        *,
        ref: str = "main",
        token: str | None = None,
        ttl: float = DEFAULT_TTL_SEC,
        fetch: Callable[[], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"ref": ref, "token": token, "ttl": ttl}
        if clock is not None:
            kwargs["clock"] = clock
        self._reader = RemoteData(repo, fetch=(lambda _path: fetch()) if fetch else None, **kwargs)

    @property
    def repo(self) -> str:
        return self._reader.repo

    @property
    def ref(self) -> str:
        return self._reader.ref

    @property
    def token(self) -> str | None:
        return self._reader.token

    def get(self) -> Any:
        return self._reader.get(LEDGER_PATH)


def from_env() -> RemoteLedger | None:
    """On by default; opt out with the legacy DARKHUB_OWNER_ACTIONS_REMOTE switch."""
    if os.environ.get(ENV_ENABLE, "").strip().lower() in {"0", "false", "off"}:
        return None
    if os.environ.get("DARKHUB_REMOTE_DATA", "").strip().lower() in {"0", "false", "off"}:
        return None
    repo = os.environ.get(ENV_REPO, "").strip() or DEFAULT_REPO
    token = os.environ.get(ENV_TOKEN, "").strip() or os.environ.get(ENV_TOKEN_FALLBACK, "").strip()
    return RemoteLedger(repo, token=token or None)
