"""Small, read-only GitHub adapter for delivery policy evaluation.

The adapter owns transport and GitHub payload normalization only. Delivery
eligibility remains in :mod:`core.orchestrator.delivery` so it can be tested
without credentials or network access.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitHubApiError(RuntimeError):
    """A safe, credential-free error from the GitHub API boundary."""


class JsonTransport(Protocol):
    def __call__(self, url: str, headers: Mapping[str, str]) -> Mapping[str, Any]: ...


_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_REPOSITORY_RE = re.compile(r"^[^/\\\s]+/[^/\\\s]+$")


def _validate_sha(value: str) -> str:
    normalized = value.strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise ValueError("must be a 7-64 character hexadecimal Git SHA")
    return normalized


class GitHubCheck(BaseModel):
    """Normalized check-run evidence attached to one commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    head_sha: str
    status: str = Field(min_length=1)
    conclusion: str | None = None

    _normalize_sha = field_validator("head_sha")(_validate_sha)

    @property
    def passed(self) -> bool:
        return self.status.lower() == "completed" and self.conclusion == "success"


class PullRequestSnapshot(BaseModel):
    """Only the immutable/current facts needed by delivery policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=3)
    number: int = Field(gt=0)
    base_sha: str
    head_sha: str
    state: str = Field(min_length=1)
    draft: bool = False
    mergeable: bool | None = None
    checks: tuple[GitHubCheck, ...] = ()
    updated_at: datetime | None = None

    _normalize_base_sha = field_validator("base_sha")(_validate_sha)
    _normalize_head_sha = field_validator("head_sha")(_validate_sha)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        normalized = value.strip()
        if not _REPOSITORY_RE.fullmatch(normalized):
            raise ValueError("repository must use the owner/name form")
        return normalized


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubClient:
    """Read-only GitHub API client with an injectable JSON transport."""

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        transport: JsonTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        normalized_base = base_url.rstrip("/")
        parsed = urllib.parse.urlparse(normalized_base)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("GitHub API base_url must use HTTPS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.base_url = normalized_base
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.transport = transport
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _validate_repository(repository: str) -> str:
        normalized = repository.strip()
        if not _REPOSITORY_RE.fullmatch(normalized):
            raise ValueError("repository must use the owner/name form")
        return normalized

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DarkFac-delivery-policy",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_json(self, path: str) -> Mapping[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers()
        if self.transport is not None:
            try:
                payload = self.transport(url, headers)
            except Exception as exc:
                raise GitHubApiError(f"GitHub transport failed: {type(exc).__name__}") from exc
            if not isinstance(payload, Mapping):
                raise GitHubApiError("GitHub returned an invalid JSON object")
            return payload

        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise GitHubApiError(f"GitHub API request failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubApiError("GitHub API request failed due to transport error") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError("GitHub returned malformed JSON") from exc
        if not isinstance(payload, Mapping):
            raise GitHubApiError("GitHub returned an invalid JSON object")
        return payload

    def get_pull_request_snapshot(self, repository: str, number: int) -> PullRequestSnapshot:
        """Fetch a PR and its checks, returning a stable policy snapshot."""
        normalized_repository = self._validate_repository(repository)
        if number <= 0:
            raise ValueError("pull request number must be positive")
        encoded_repository = urllib.parse.quote(normalized_repository, safe="/")
        pr = self._get_json(f"repos/{encoded_repository}/pulls/{number}")
        head = pr.get("head")
        base = pr.get("base")
        if not isinstance(head, Mapping) or not isinstance(base, Mapping):
            raise GitHubApiError("GitHub pull request payload lacks commit references")
        head_sha = head.get("sha")
        base_sha = base.get("sha")
        if not isinstance(head_sha, str) or not isinstance(base_sha, str):
            raise GitHubApiError("GitHub pull request payload has invalid commit references")

        checks_payload = self._get_json(f"repos/{encoded_repository}/commits/{head_sha}/check-runs")
        raw_checks = checks_payload.get("check_runs", [])
        if not isinstance(raw_checks, list):
            raise GitHubApiError("GitHub check-runs payload is invalid")
        checks: list[GitHubCheck] = []
        for raw_check in raw_checks:
            if not isinstance(raw_check, Mapping):
                continue
            try:
                checks.append(
                    GitHubCheck(
                        name=str(raw_check.get("name", "")),
                        head_sha=str(raw_check.get("head_sha", head_sha)),
                        status=str(raw_check.get("status", "")),
                        conclusion=raw_check.get("conclusion"),
                    )
                )
            except (TypeError, ValueError):
                continue

        try:
            return PullRequestSnapshot(
                repository=normalized_repository,
                number=number,
                base_sha=base_sha,
                head_sha=head_sha,
                state=str(pr.get("state", "")),
                draft=bool(pr.get("draft", False)),
                mergeable=pr.get("mergeable"),
                checks=tuple(checks),
                updated_at=_parse_datetime(pr.get("updated_at")),
            )
        except (TypeError, ValueError) as exc:
            raise GitHubApiError("GitHub pull request payload failed validation") from exc


__all__ = ["GitHubApiError", "GitHubCheck", "GitHubClient", "PullRequestSnapshot"]
