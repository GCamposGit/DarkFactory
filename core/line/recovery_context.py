"""Keep run context reachable after integration strips it from the product branch.

The pre-strip commit is pinned by a per-run Git ref in the target repository.
It is absent from the merged product tree and can be fetched by another worker
after the run branch has been deleted. Only the planning inputs needed for a
new development attempt are restored; progress from the failed candidate is not.
"""

from __future__ import annotations

import json
import re

from core.git.safe_show import safe_show
from core.line import workspace
from core.line.workspace import RunWorkspace, WorkspaceError

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REQUIRED_FILES = ("tickets.json",)
_OPTIONAL_FILES = ("SPEC.md", "DEMAND.md", "GRILL.md")


def context_ref(run_id: str) -> str:
    """A Git ref scoped to one run; reject values that could escape its namespace."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"Invalid run id for recovery context: {run_id!r}")
    # GitHub branch refs can be fetched by remote workers through normal Git access.
    return f"refs/heads/darkfac-context/{run_id}"


def _remote_sha(ws: RunWorkspace, ref: str) -> str | None:
    result = workspace._run_git(
        ["ls-remote", "origin", ref], cwd=ws.path, repo_url=workspace._remote_url(ws.path)
    )
    for line in (result.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name == ref:
            return sha
    return None


def preserve_context(ws: RunWorkspace) -> str | None:
    """Pin the current pre-strip commit remotely when a tickets list exists."""
    directory = workspace.context_dir(ws)
    if not (directory / "tickets.json").is_file():
        return None
    ref = context_ref(ws.run_id)
    sha = workspace._rev_parse(ws.path, "HEAD")
    prefix = f".darkfac/runs/{ws.run_id}/"
    for name in _REQUIRED_FILES:
        if safe_show(sha, prefix + name, cwd=ws.path).returncode != 0:
            raise WorkspaceError(f"Run {ws.run_id} has uncommitted {name} before integration")
    previous = _remote_sha(ws, ref)
    if previous == sha:
        return sha
    workspace._run_git(
        ["push", f"--force-with-lease={ref}:{previous or ''}", "origin", f"{sha}:{ref}"],
        cwd=ws.path,
        repo_url=workspace._remote_url(ws.path),
    )
    return sha


def restore_context(ws: RunWorkspace) -> bool:
    """Restore planning inputs from the pinned commit into the recreated run branch.

    All required content is read and checked before writing any file. Missing
    or malformed context returns False, so release can fail with a structured
    cause rather than looping back to development with no tickets.
    """
    ref = context_ref(ws.run_id)
    expected = _remote_sha(ws, ref)
    if not expected:
        return False
    fetched = workspace._run_git(
        ["fetch", "origin", ref], cwd=ws.path, repo_url=workspace._remote_url(ws.path)
    )
    if fetched.returncode != 0 or workspace._rev_parse(ws.path, "FETCH_HEAD") != expected:
        return False
    prefix = f".darkfac/runs/{ws.run_id}/"
    contents: dict[str, str] = {}
    for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES):
        shown = safe_show(expected, prefix + name, cwd=ws.path)
        if shown.returncode == 0:
            contents[name] = shown.stdout or ""
        elif name in _REQUIRED_FILES:
            return False
    try:
        tickets = json.loads(contents["tickets.json"])
    except (KeyError, json.JSONDecodeError):
        return False
    if not isinstance(tickets, list) or not tickets:
        return False
    for name, content in contents.items():
        workspace.write_context(ws, name, content)
    return True
