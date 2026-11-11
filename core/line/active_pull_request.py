"""Detect an open GitHub PR that already owns a ticket's implementation (USR-418).

A failed `gh pr list` stays fail-closed, but the refusal names the cause: the
binary is missing, the CLI is not logged in, the call timed out, or GitHub
returned an error. Each cause carries the command that fixes it (USR-486).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable

GH_INSTALL_COMMAND = "winget install --id GitHub.cli -e"
GH_AUTH_LOGIN_COMMAND = "gh auth login"
GH_AUTH_STATUS_COMMAND = "gh auth status"

_HTTP_STATUS = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)
_DETAIL_LIMIT = 300


class PullRequestLookupError(RuntimeError):
    """The active-PR projection could not be checked safely.

    ``cause`` is ``gh_missing``, ``gh_unauthenticated``, ``gh_timeout`` or
    ``github_error`` when the failure is classified. ``fix_command`` is the
    operator command for that cause. A message-only error leaves both empty
    so older callers keep the generic ``active_pr_lookup_failed`` refusal.
    """

    cause: str | None
    fix_command: str | None

    def __init__(self, message: str, *, cause: str | None = None, fix_command: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.fix_command = fix_command


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


def _github_error_message(combined: str) -> str:
    """Keep the HTTP status visible even when the CLI text is long."""
    status = _HTTP_STATUS.search(combined)
    detail = " ".join(combined.split())
    if len(detail) > _DETAIL_LIMIT:
        detail = detail[:_DETAIL_LIMIT].rstrip()
    if status and status.group(1) not in detail:
        detail = f"HTTP {status.group(1)} {detail}".strip()
    if not detail:
        return "GitHub PR lookup failed; retry when GitHub is available"
    return f"GitHub PR lookup failed: {detail}"


def _classify_gh_result(
    result: Any,
    repo: str,
    *,
    github_fix_command: str | None = None,
) -> PullRequestLookupError:
    stderr = str(getattr(result, "stderr", "") or "")
    stdout = str(getattr(result, "stdout", "") or "")
    combined = f"{stderr}\n{stdout}"
    if "not logged in" in combined.casefold():
        return PullRequestLookupError(
            "gh is not authenticated; active PR status cannot be verified",
            cause="gh_unauthenticated",
            fix_command=GH_AUTH_LOGIN_COMMAND,
        )
    return PullRequestLookupError(
        _github_error_message(combined),
        cause="github_error",
        fix_command=github_fix_command or f"gh pr list --state open --repo {repo}",
    )


def classify_gh_call(
    outcome: BaseException | Any,
    *,
    repo: str,
    timeout_message: str = "gh pr list timed out; active PR status cannot be verified",
    github_fix_command: str | None = None,
) -> PullRequestLookupError | None:
    """Classify one gh invocation. None means the process succeeded.

    Missing binary, timeout, a logged-out CLI and any other non-zero exit share
    this classifier. Callers that are not `gh pr list` pass their own timeout
    message and the command that retries a GitHub error.
    """
    if isinstance(outcome, FileNotFoundError):
        return PullRequestLookupError(
            "gh is not installed or not on PATH; active PR status cannot be verified",
            cause="gh_missing",
            fix_command=GH_INSTALL_COMMAND,
        )
    if isinstance(outcome, subprocess.TimeoutExpired):
        return PullRequestLookupError(
            timeout_message,
            cause="gh_timeout",
            fix_command=GH_AUTH_STATUS_COMMAND,
        )
    if isinstance(outcome, OSError):
        return PullRequestLookupError("GitHub PR lookup failed; retry when GitHub is available")
    if getattr(outcome, "returncode", 0) != 0:
        return _classify_gh_result(outcome, repo, github_fix_command=github_fix_command)
    return None


def find_active_pull_request(
    project_id: str,
    ticket_id: str,
    *,
    project_registry: Any | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> ActivePullRequest | None:
    """Return an open PR mentioning this exact ticket, or fail closed when the lookup cannot be verified."""
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
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        classified = classify_gh_call(exc, repo=repo)
        if classified is None:
            raise PullRequestLookupError("GitHub PR lookup failed; retry when GitHub is available") from exc
        raise classified from exc
    if result.returncode != 0:
        classified = classify_gh_call(result, repo=repo)
        if classified is not None:
            raise classified
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


__all__ = [
    "ActivePullRequest",
    "GH_AUTH_LOGIN_COMMAND",
    "GH_AUTH_STATUS_COMMAND",
    "GH_INSTALL_COMMAND",
    "PullRequestLookupError",
    "classify_gh_call",
    "find_active_pull_request",
]
