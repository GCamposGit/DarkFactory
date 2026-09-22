"""Per-run git workspace for the DarkFac production line (HF-27-02).

The workspace layer prepares the directory a stage's agent CLI works in and
carries state between stages/hosts through the run's own branch, per the
"contrato comum da linha" in `docs/handoffs/production-line/INDEX.md`:

- Each project gets a shared mirror clone at
  `<root>/<project_id>/.mirror`, created once and refreshed with
  `git fetch --prune` on every `checkout()`.
- Each run gets its own `git worktree` at
  `<root>/<project_id>/runs/<run_id>`, checked out on branch
  `df/<run_id>`. If `origin/df/<run_id>` already exists the worktree
  continues from it (another host's work); otherwise it branches from
  `origin/<default_branch>`.
- `checkout()` is idempotent: calling it again reuses the existing
  worktree, fast-forwards it to the remote tip when the remote is ahead,
  and — critically — never discards local commits that have not been
  pushed yet.
- Context files for a run live under `.darkfac/runs/<run_id>/` inside the
  worktree and are versioned on the branch itself, so any stage on any
  host can read what a previous stage wrote by checking out the branch.
- `commit()` stamps a `DarkFac-Job: <job_key>` trailer used by
  `find_commit_by_job()` for idempotency: retrying a step whose effect is
  already committed is a safe no-op.

All git calls go through a single subprocess helper (`_run_git`) using
argument lists (never `shell=True`), UTF-8 text I/O, `CREATE_NO_WINDOW` on
Windows, and raise a structured `WorkspaceError` with sanitized stderr on
failure.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel

from core.projects.models import ProjectDescriptor
from core.projects.registry import normalize_repo_url

logger = logging.getLogger(__name__)

_JOB_TRAILER = "DarkFac-Job"
_DEFAULT_IDENTITY_NAME = "DarkFac"
_DEFAULT_IDENTITY_EMAIL = "darkfac@users.noreply.github.com"
_PUSH_RETRIES = 3
_PUSH_BACKOFF_S = 2.0
_MIRROR_DIRNAME = ".mirror"
_RUNS_DIRNAME = "runs"
_CONTEXT_DIRNAME = ".darkfac/runs"

_AUTH_HEADER_PATTERN = re.compile(r"(AUTHORIZATION:\s*basic\s+)\S+", re.IGNORECASE)


class WorkspaceError(RuntimeError):
    """Raised when a git operation for a run workspace fails.

    The message always carries sanitized stderr (any GITHUB_TOKEN value or
    `http.extraheader` content is redacted), so it is safe to log as-is.
    """


class RunWorkspace(BaseModel):
    """A checked-out, ready-to-use run worktree."""

    project_id: str
    run_id: str
    branch: str
    path: Path
    base_sha: str


def workspace_root() -> Path:
    """Root directory holding every project's mirror and run worktrees.

    Controlled by `DARKFAC_WORKSPACES`; defaults to `~/.darkfac/workspaces`.
    """
    override = os.environ.get("DARKFAC_WORKSPACES")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".darkfac" / "workspaces"


# --------------------------------------------------------------------------
# Low-level git subprocess helper
# --------------------------------------------------------------------------


def _win_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _sanitize(text: str, token: Optional[str]) -> str:
    sanitized = text
    if token:
        sanitized = sanitized.replace(token, "***")
        sanitized = sanitized.replace(_basic_auth_value(token), "***")
    return _AUTH_HEADER_PATTERN.sub(r"\1***", sanitized)


def _github_token_for(repo_url: Optional[str]) -> Optional[str]:
    """Return `GITHUB_TOKEN` only when the remote is `https://github.com/...`."""
    if not repo_url:
        return None
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    if repo_url.startswith("https://github.com/"):
        return token
    return None


def _basic_auth_value(token: str) -> str:
    """Base64 of `x-access-token:<token>` — the form `actions/checkout` uses for HTTPS."""
    return base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")


def _auth_args(token: Optional[str]) -> list[str]:
    if not token:
        return []
    # Never written to disk or into the remote URL; passed per-command only.
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {_basic_auth_value(token)}"]


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    repo_url: Optional[str] = None,
    check: bool = True,
    timeout: int = 300,
) -> "subprocess.CompletedProcess[str]":
    """Run one git subcommand as an explicit arg list (never `shell=True`)."""
    token = _github_token_for(repo_url)
    argv = ["git", *_auth_args(token), *args]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **_win_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {' '.join(args[:2])} timed out after {timeout}s") from exc
    except OSError as exc:
        raise WorkspaceError(f"failed to launch git: {_sanitize(str(exc), token)}") from exc
    if check and proc.returncode != 0:
        stderr = _sanitize((proc.stderr or "").strip(), token)
        raise WorkspaceError(f"git {' '.join(args)} failed (exit {proc.returncode}): {stderr}")
    return proc


def _identity_args(cwd: Path) -> list[str]:
    """Fixed DarkFac identity, unless the repo already has one configured locally."""
    name = _run_git(["config", "--local", "--get", "user.name"], cwd=cwd, check=False)
    email = _run_git(["config", "--local", "--get", "user.email"], cwd=cwd, check=False)
    if (name.stdout or "").strip() and (email.stdout or "").strip():
        return []
    return [
        "-c", f"user.name={_DEFAULT_IDENTITY_NAME}",
        "-c", f"user.email={_DEFAULT_IDENTITY_EMAIL}",
    ]


def _rev_parse(cwd: Path, ref: str) -> str:
    proc = _run_git(["rev-parse", ref], cwd=cwd)
    return (proc.stdout or "").strip()


def _merge_base(cwd: Path, a: str, b: str) -> Optional[str]:
    proc = _run_git(["merge-base", a, b], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _ref_exists(cwd: Path, ref: str) -> bool:
    proc = _run_git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=cwd, check=False)
    return proc.returncode == 0


def _remote_url(cwd: Path) -> Optional[str]:
    proc = _run_git(["remote", "get-url", "origin"], cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    url = (proc.stdout or "").strip()
    return url or None


# --------------------------------------------------------------------------
# Mirror + worktree management
# --------------------------------------------------------------------------


def _mirror_dir(root: Path, project_id: str) -> Path:
    return root / project_id / _MIRROR_DIRNAME


def _run_dir(root: Path, project_id: str, run_id: str) -> Path:
    return root / project_id / _RUNS_DIRNAME / run_id


def _ensure_mirror(project_id: str, repo_url: str, root: Path) -> Path:
    mirror = _mirror_dir(root, project_id)
    if (mirror / ".git").exists():
        _run_git(["fetch", "--prune", "origin"], cwd=mirror, repo_url=repo_url, timeout=600)
        return mirror
    if mirror.exists():
        shutil.rmtree(mirror, ignore_errors=True)  # leftover/partial clone
    mirror.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["clone", "--filter=blob:none", "--origin", "origin", repo_url, str(mirror)],
        cwd=root,
        repo_url=repo_url,
        timeout=900,
    )
    return mirror


def _sync_existing_worktree(
    run_dir: Path,
    branch: str,
    remote_ref: Optional[str],
    repo_url: Optional[str],
) -> None:
    """Reconcile an already-checked-out run worktree with the remote branch.

    Fast-forwards to the remote tip when the remote is strictly ahead.
    Never discards local commits: if local is ahead of (or has diverged
    from) the remote, it is left untouched and a warning is logged.
    """
    if remote_ref is None:
        return  # nothing pushed for this run yet; local state is authoritative
    local_sha = _rev_parse(run_dir, "HEAD")
    remote_sha = _rev_parse(run_dir, remote_ref)
    if not remote_sha or local_sha == remote_sha:
        return
    ancestor_check = _run_git(
        ["merge-base", "--is-ancestor", "HEAD", remote_ref], cwd=run_dir, check=False
    )
    if ancestor_check.returncode == 0:
        _run_git(["reset", "--hard", remote_ref], cwd=run_dir, repo_url=repo_url)
        logger.info(
            "workspace %s: fast-forwarded %s from %s to remote tip %s",
            run_dir, branch, local_sha, remote_sha,
        )
    else:
        logger.warning(
            "workspace %s: local %s (%s) is ahead of or has diverged from %s (%s); "
            "keeping local commits, not resetting",
            run_dir, branch, local_sha, remote_ref, remote_sha,
        )


def checkout(project: ProjectDescriptor, run_id: str) -> RunWorkspace:
    """Prepare (or reuse) the run worktree for `project` on branch `df/<run_id>`.

    Idempotent: safe to call repeatedly, including from different hosts
    sharing the same remote, as long as each host points `DARKFAC_WORKSPACES`
    at its own local root.
    """
    if not run_id or not run_id.strip():
        raise WorkspaceError("run_id must be non-empty")
    if not project.repo_url:
        raise WorkspaceError(f"project '{project.id}' has no repo_url configured")

    root = workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    repo_url = normalize_repo_url(project.repo_url)
    default_branch = project.default_branch or "main"
    branch = f"df/{run_id}"

    mirror = _ensure_mirror(project.id, repo_url, root)

    remote_default_ref = f"origin/{default_branch}"
    if not _ref_exists(mirror, remote_default_ref):
        raise WorkspaceError(
            f"remote default branch '{default_branch}' not found at {repo_url} "
            f"for project '{project.id}'"
        )
    remote_branch_ref = f"origin/{branch}"
    has_remote_branch = _ref_exists(mirror, remote_branch_ref)

    run_dir = _run_dir(root, project.id, run_id)
    _run_git(["worktree", "prune"], cwd=mirror, check=False)

    if (run_dir / ".git").exists():
        _sync_existing_worktree(
            run_dir, branch, remote_branch_ref if has_remote_branch else None, repo_url,
        )
    else:
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)  # stale non-worktree leftover
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        start_point = remote_branch_ref if has_remote_branch else remote_default_ref
        _run_git(
            ["worktree", "add", "-B", branch, str(run_dir), start_point],
            cwd=mirror,
            repo_url=repo_url,
        )
        if has_remote_branch:
            _run_git(
                ["branch", f"--set-upstream-to={remote_branch_ref}", branch],
                cwd=run_dir, check=False,
            )

    head_sha = _rev_parse(run_dir, "HEAD")
    base_sha = _merge_base(run_dir, "HEAD", remote_default_ref) or head_sha

    return RunWorkspace(
        project_id=project.id, run_id=run_id, branch=branch, path=run_dir, base_sha=base_sha,
    )


# --------------------------------------------------------------------------
# Context files, commit, push
# --------------------------------------------------------------------------


def context_dir(ws: RunWorkspace) -> Path:
    """`.darkfac/runs/<run_id>/` inside the worktree — versioned on the branch."""
    return ws.path / ".darkfac" / "runs" / ws.run_id


def write_context(ws: RunWorkspace, name: str, content: str) -> Path:
    """Write (or overwrite) a context file, e.g. `DEMAND.md`, `tickets.json`."""
    directory = context_dir(ws)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(content, encoding="utf-8")
    return target


def commit(ws: RunWorkspace, message: str, job_key: str) -> str:
    """Stage everything and commit with a `DarkFac-Job: <job_key>` trailer.

    A no-op (nothing staged) returns the current HEAD SHA without creating a
    new commit, which makes retrying a step with the same `job_key` safe.
    """
    if not job_key or not job_key.strip():
        raise WorkspaceError("job_key must be non-empty")
    _run_git(["add", "-A"], cwd=ws.path)
    diff_check = _run_git(["diff", "--cached", "--quiet"], cwd=ws.path, check=False)
    if diff_check.returncode == 0:
        return _rev_parse(ws.path, "HEAD")
    trailer = f"{_JOB_TRAILER}: {job_key.strip()}"
    full_message = f"{message.rstrip()}\n\n{trailer}\n"
    argv = [*_identity_args(ws.path), "commit", "-m", full_message]
    _run_git(argv, cwd=ws.path)
    return _rev_parse(ws.path, "HEAD")


def push(ws: RunWorkspace, *, sleep: Callable[[float], None] = time.sleep) -> None:
    """`git push -u origin df/<run_id>`, retried up to 3 times with backoff.

    Only ever pushes `df/*` branches; `sleep` is injectable for tests.
    """
    if not ws.branch.startswith("df/"):
        raise WorkspaceError(f"refusing to push non-df branch '{ws.branch}'")
    repo_url = _remote_url(ws.path)
    last_error: Optional[WorkspaceError] = None
    for attempt in range(1, _PUSH_RETRIES + 1):
        try:
            _run_git(["push", "-u", "origin", ws.branch], cwd=ws.path, repo_url=repo_url, timeout=300)
            return
        except WorkspaceError as exc:
            last_error = exc
            if attempt < _PUSH_RETRIES:
                logger.warning(
                    "push attempt %d/%d for %s failed: %s", attempt, _PUSH_RETRIES, ws.branch, exc,
                )
                sleep(_PUSH_BACKOFF_S * attempt)
    assert last_error is not None
    raise last_error


def find_commit_by_job(ws: RunWorkspace, job_key: str) -> Optional[str]:
    """Return the SHA of the commit carrying an exact `DarkFac-Job: <job_key>` trailer.

    Matches the trailer *value* exactly (via `%(trailers:key=...,valueonly)`),
    not as a substring — job keys like `<run>:T1` and `<run>:T10` must not
    collide, since HF-27-05 uses per-ticket keys of exactly that shape.
    """
    needle = job_key.strip()
    if not needle:
        return None
    proc = _run_git(
        ["log", f"--format=%H%x00%(trailers:key={_JOB_TRAILER},valueonly)%x1e"],
        cwd=ws.path,
        check=False,
    )
    if proc.returncode != 0:
        return None
    for record in (proc.stdout or "").split("\x1e"):
        record = record.strip("\n")
        if "\x00" not in record:
            continue
        sha, _, value = record.partition("\x00")
        if value.strip() == needle:
            return sha.strip()
    return None


def cleanup(ws: RunWorkspace, keep_days: int = 7) -> list[str]:
    """Remove other run worktrees for `ws.project_id` last touched > `keep_days` ago.

    Never removes `ws` itself. Returns the list of removed `run_id`s.
    """
    root = workspace_root()
    mirror = _mirror_dir(root, ws.project_id)
    runs_root = root / ws.project_id / _RUNS_DIRNAME
    removed: list[str] = []
    if not runs_root.is_dir():
        return removed
    cutoff = time.time() - keep_days * 86400
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir() or entry.name == ws.run_id:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        branch = f"df/{entry.name}"
        if mirror.is_dir():
            _run_git(["worktree", "remove", "--force", str(entry)], cwd=mirror, check=False)
        if entry.exists():
            shutil.rmtree(entry, ignore_errors=True)
        if mirror.is_dir():
            _run_git(["worktree", "prune"], cwd=mirror, check=False)
            _run_git(["branch", "-D", branch], cwd=mirror, check=False)
        removed.append(entry.name)
    return removed
