"""Detect an open GitHub PR that already owns a ticket's implementation (USR-418)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from core.git import ci_checks


class PullRequestLookupError(RuntimeError):
    """The active-PR projection could not be checked safely."""


@dataclass(frozen=True)
class ActivePullRequest:
    number: int
    url: str
    title: str
    head_ref: str
    draft: bool = False
    cross_repository: bool = False

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
             "--json", "number,url,title,headRefName,body,isDraft,isCrossRepository", "--limit", "1000"],
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
                draft=entry.get("isDraft") is True,
                cross_repository=entry.get("isCrossRepository") is True,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PullRequestLookupError("GitHub PR lookup returned an incomplete matching PR") from exc
    return None


PullRequestAction = Literal["adopt_delivery", "fix", "wait"]


@dataclass(frozen=True)
class PullRequestDisposition:
    """What the dispatcher should do about a ticket's open PR (USR-545)."""

    action: PullRequestAction
    pr_number: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "pr_number": self.pr_number, "reason": self.reason}


def head_is_ticket_branch(head_ref: str, ticket_id: str) -> bool:
    """True for ``ticket/<slug>`` or ``ticket/<slug>-<suffix>`` (slug = lowercase ticket id)."""
    slug = re.escape(ticket_id.strip().lower())
    return re.fullmatch(rf"ticket/{slug}(?:-[A-Za-z0-9._-]+)?", head_ref.strip()) is not None


def _gh_runner(repo: str | None) -> ci_checks.GhRunner:
    def run(args: Any, cwd: Path) -> subprocess.CompletedProcess[str]:
        command = ["gh", *args]
        if repo:
            command += ["--repo", repo]
        return subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, check=False,
        )

    return run


def classify_pull_request_disposition(
    pr: ActivePullRequest,
    ticket_id: str,
    *,
    repo: str | None = None,
    cwd: Path | None = None,
    runner: ci_checks.GhRunner | None = None,
) -> PullRequestDisposition:
    """Decide ``adopt_delivery`` / ``fix`` / ``wait`` for a ticket's open PR; any doubt is ``wait``."""

    def verdict(action: PullRequestAction, reason: str) -> PullRequestDisposition:
        return PullRequestDisposition(action, pr.number, reason)

    if pr.cross_repository:
        return verdict("wait", "PR de fork nao e adotado")
    if not head_is_ticket_branch(pr.head_ref, ticket_id):
        return verdict("wait", "head do PR nao e a branch do ticket")
    if repo is None:
        from_url = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/\d+", pr.url)
        repo = from_url.group(1) if from_url else None
    try:
        snapshot = ci_checks.query_checks(pr.number, cwd or Path.cwd(), runner or _gh_runner(repo))
    except Exception:
        return verdict("wait", "consulta de CI falhou")
    if snapshot.state == "failed":
        labels = ", ".join(ci_checks.check_label(c) for c in snapshot.failing[:3])
        return verdict("fix", ci_checks.tail_chars(ci_checks.sanitize(f"CI vermelho: {labels}"), 200))
    if snapshot.state == "pending":
        return verdict("wait", "CI pendente")
    if snapshot.state in ("green", "no_checks"):
        return verdict("adopt_delivery", "CI verde" if snapshot.state == "green" else "PR sem checks")
    return verdict("wait", "consulta de CI inconclusiva")


__all__ = [
    "ActivePullRequest",
    "PullRequestDisposition",
    "PullRequestLookupError",
    "classify_pull_request_disposition",
    "find_active_pull_request",
    "head_is_ticket_branch",
]
