"""Detect an open GitHub PR that already owns a ticket's implementation (USR-418)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


class PullRequestLookupError(RuntimeError):
    """The active-PR projection could not be checked safely."""


@dataclass(frozen=True)
class ActivePullRequest:
    number: int
    url: str
    title: str
    head_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "url": self.url, "title": self.title, "head_ref": self.head_ref}


def _repo_slug(repo_url: str) -> str:
    match = re.search(r"github\.com[:/]([^/]+/[^/#?]+?)(?:\.git)?/?$", repo_url.strip(), re.IGNORECASE)
    if not match:
        raise PullRequestLookupError("project repository is not a GitHub repository")
    return match.group(1).removesuffix(".git")


def _contains_ticket(value: object, ticket_id: str) -> bool:
    if not isinstance(value, str):
        return False
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(ticket_id)}(?![A-Za-z0-9])", value, re.IGNORECASE) is not None


def find_active_pull_request(
    project_id: str,
    ticket_id: str,
    *,
    project_registry: Any | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> ActivePullRequest | None:
    """Return an open PR mentioning this exact ticket, or fail closed when GitHub is unavailable."""
    if project_registry is None:
        from core.projects.registry import ProjectRegistry

        project_registry = ProjectRegistry()
    project = project_registry.get_project(project_id)
    repo_url = getattr(project, "repo_url", None) if project is not None else None
    if not repo_url:
        raise PullRequestLookupError(
            f"project '{project_id}' has no registered GitHub repository; active PR status cannot be verified"
        )
    repo = _repo_slug(str(repo_url))
    try:
        result = run(
            ["gh", "pr", "list", "--state", "open", "--repo", repo,
             "--json", "number,url,title,headRefName,body", "--limit", "1000"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PullRequestLookupError("GitHub PR lookup failed; retry when GitHub is available") from exc
    if result.returncode != 0:
        raise PullRequestLookupError("GitHub PR lookup failed; retry when GitHub is available")
    try:
        entries = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise PullRequestLookupError("GitHub PR lookup returned invalid data") from exc
    if not isinstance(entries, list):
        raise PullRequestLookupError("GitHub PR lookup returned invalid data")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if not any(_contains_ticket(entry.get(field), ticket_id) for field in ("headRefName", "title", "body")):
            continue
        try:
            return ActivePullRequest(
                number=int(entry["number"]),
                url=str(entry["url"]),
                title=str(entry.get("title") or ""),
                head_ref=str(entry.get("headRefName") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PullRequestLookupError("GitHub PR lookup returned an incomplete matching PR") from exc
    return None


__all__ = ["ActivePullRequest", "PullRequestLookupError", "find_active_pull_request"]
