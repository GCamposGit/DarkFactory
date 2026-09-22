"""Tests for the per-run git workspace layer (HF-27-02).

Every test uses a local `git init --bare` repository under `tmp_path` as the
"origin" remote — no network access. Each host/project is simulated by
pointing `DARKFAC_WORKSPACES` at a distinct local directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from core.line import workspace as ws_mod
from core.line.workspace import (
    RunWorkspace,
    WorkspaceError,
    checkout,
    cleanup,
    commit,
    context_dir,
    find_commit_by_job,
    push,
    workspace_root,
    write_context,
)
from core.projects.models import ProjectDescriptor


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path, dirname: str = "origin.git", default_branch: str = "main") -> Path:
    """Create a bare repo with one seed commit on `default_branch`, used as "origin"."""
    origin = tmp_path / dirname
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(["init", "--bare", str(origin)], cwd=tmp_path)

    seed_name = "_seed_" + "".join(c if c.isalnum() else "_" for c in dirname)
    seed = tmp_path / seed_name
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", default_branch], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", default_branch], cwd=seed)
    return origin


def _project(
    repo_url: str, project_id: str = "acme", default_branch: str = "main"
) -> ProjectDescriptor:
    return ProjectDescriptor(
        id=project_id,
        name="Acme Project",
        repo_url=repo_url,
        default_branch=default_branch,
    )


def _head_sha(path: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


# --------------------------------------------------------------------------
# workspace_root()
# --------------------------------------------------------------------------


def test_workspace_root_defaults_to_home_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_WORKSPACES", raising=False)
    assert workspace_root() == Path.home() / ".darkfac" / "workspaces"


def test_workspace_root_honors_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom root"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(custom))
    assert workspace_root() == custom


# --------------------------------------------------------------------------
# checkout() basics + validation
# --------------------------------------------------------------------------


def test_checkout_requires_repo_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))
    project = ProjectDescriptor(id="no-repo", name="No Repo")
    with pytest.raises(WorkspaceError):
        checkout(project, "run-x")


def test_checkout_creates_branch_from_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-1")

    assert ws.branch == "df/run-1"
    assert ws.path.is_dir()
    assert (ws.path / "README.md").is_file()
    assert ws.base_sha == _head_sha(ws.path)
    current_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=ws.path).stdout.strip()
    assert current_branch == "df/run-1"


def test_checkout_twice_same_host_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws1 = checkout(project, "run-2")
    write_context(ws1, "A.md", "v1\n")
    commit(ws1, "a", job_key="job-2a")

    ws2 = checkout(project, "run-2")

    assert ws2.path == ws1.path
    assert ws2.branch == ws1.branch
    assert (context_dir(ws2) / "A.md").read_text(encoding="utf-8") == "v1\n"


def test_checkout_respects_nonstandard_default_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path, default_branch="master")
    project = _project(str(origin), project_id="jarvis", default_branch="master")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-3")

    remote_master = _git(["rev-parse", "origin/master"], cwd=ws.path).stdout.strip()
    assert _head_sha(ws.path) == remote_master
    assert ws.base_sha == remote_master


def test_checkout_with_spaces_in_root_and_repo_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path, dirname="my repo.git")
    project = _project(str(origin))
    root = tmp_path / "space root" / "workspaces"
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(root))

    ws = checkout(project, "run-4")
    write_context(ws, "NOTE.md", "ok\n")
    sha = commit(ws, "note", job_key="job-4")

    assert sha
    assert workspace_root() == root
    assert (context_dir(ws) / "NOTE.md").is_file()


# --------------------------------------------------------------------------
# context files
# --------------------------------------------------------------------------


def test_context_dir_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-5")
    target = write_context(ws, "DEMAND.md", "hello\n")

    assert target == ws.path / ".darkfac" / "runs" / "run-5" / "DEMAND.md"
    assert context_dir(ws) == ws.path / ".darkfac" / "runs" / "run-5"
    assert target.read_text(encoding="utf-8") == "hello\n"


# --------------------------------------------------------------------------
# commit() + find_commit_by_job()
# --------------------------------------------------------------------------


def test_commit_uses_fixed_darkfac_identity_and_trailer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-6")
    write_context(ws, "E.md", "e\n")
    sha = commit(ws, "write e", job_key="job-6")

    author = _git(["log", "-1", "--format=%an <%ae>", sha], cwd=ws.path).stdout.strip()
    assert author == "DarkFac <darkfac@users.noreply.github.com>"
    body = _git(["log", "-1", "--format=%B", sha], cwd=ws.path).stdout
    assert "DarkFac-Job: job-6" in body


def test_commit_is_noop_without_diff_and_findable_by_job_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-7")
    write_context(ws, "SPEC.md", "spec v1\n")
    sha1 = commit(ws, "write spec", job_key="job-spec")
    sha2 = commit(ws, "write spec", job_key="job-spec")

    assert sha1 == sha2
    log_count = _git(["rev-list", "--count", "HEAD"], cwd=ws.path).stdout.strip()
    # seed commit + exactly one spec commit, never two.
    assert log_count == "2"
    assert find_commit_by_job(ws, "job-spec") == sha1


def test_find_commit_by_job_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-8")
    assert find_commit_by_job(ws, "does-not-exist") is None


def test_commit_requires_job_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-9")
    write_context(ws, "F.md", "f\n")
    with pytest.raises(WorkspaceError):
        commit(ws, "f", job_key="   ")


# --------------------------------------------------------------------------
# push() + retries
# --------------------------------------------------------------------------


def test_push_refuses_non_df_branch(tmp_path: Path) -> None:
    ws = RunWorkspace(
        project_id="p", run_id="r", branch="main", path=tmp_path, base_sha="deadbeef",
    )
    with pytest.raises(WorkspaceError):
        push(ws)


def test_push_retries_after_transient_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-10")
    write_context(ws, "G.md", "g\n")
    commit(ws, "g", job_key="job-10")

    real_run = subprocess.run
    calls = {"n": 0}

    def flaky_run(argv, *args, **kwargs):
        if "push" in argv:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(
                    argv, returncode=1, stdout="", stderr="simulated transient failure"
                )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", flaky_run)
    sleeps: list[float] = []

    push(ws, sleep=sleeps.append)

    assert calls["n"] == 2
    assert sleeps == [ws_mod._PUSH_BACKOFF_S]
    remote_sha = _git(["rev-parse", f"refs/heads/{ws.branch}"], cwd=origin).stdout.strip()
    assert remote_sha == _head_sha(ws.path)


def test_push_raises_after_exhausting_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-11")
    write_context(ws, "H.md", "h\n")
    commit(ws, "h", job_key="job-11")

    real_run = subprocess.run

    def always_fail(argv, *args, **kwargs):
        if "push" in argv:
            return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="permanent failure")
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(ws_mod.subprocess, "run", always_fail)

    with pytest.raises(WorkspaceError):
        push(ws, sleep=lambda _s: None)


# --------------------------------------------------------------------------
# Cross-host acceptance: same SHA + context visible on a second host
# --------------------------------------------------------------------------


def test_checkout_commit_push_visible_across_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "host_a"))
    ws_a = checkout(project, "run-12")
    write_context(ws_a, "DEMAND.md", "hello from host A\n")
    sha_a = commit(ws_a, "add demand context", job_key="job-12")
    push(ws_a)

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "host_b"))
    ws_b = checkout(project, "run-12")

    assert _head_sha(ws_b.path) == sha_a
    assert ws_b.path != ws_a.path
    ctx_file = context_dir(ws_b) / "DEMAND.md"
    assert ctx_file.read_text(encoding="utf-8") == "hello from host A\n"
    assert find_commit_by_job(ws_b, "job-12") == sha_a


def test_checkout_fast_forwards_when_remote_is_ahead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(root_a))
    ws_a = checkout(project, "run-13")
    write_context(ws_a, "C.md", "v1\n")
    commit(ws_a, "v1", job_key="job-13a")
    push(ws_a)

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(root_b))
    ws_b = checkout(project, "run-13")
    write_context(ws_b, "D.md", "v2\n")
    sha2 = commit(ws_b, "v2", job_key="job-13b")
    push(ws_b)

    monkeypatch.setenv("DARKFAC_WORKSPACES", str(root_a))
    ws_a2 = checkout(project, "run-13")

    assert _head_sha(ws_a2.path) == sha2
    assert (context_dir(ws_a2) / "D.md").is_file()


def test_checkout_preserves_local_commits_ahead_of_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws = checkout(project, "run-14")
    write_context(ws, "B.md", "unpushed\n")
    local_sha = commit(ws, "unpushed change", job_key="job-14")
    # Never pushed. Re-checking out the same run must not discard this commit.

    with caplog.at_level("WARNING", logger="core.line.workspace"):
        ws2 = checkout(project, "run-14")

    assert _head_sha(ws2.path) == local_sha


# --------------------------------------------------------------------------
# cleanup()
# --------------------------------------------------------------------------


def test_cleanup_removes_runs_older_than_keep_days_but_keeps_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws_old = checkout(project, "run-old")
    ws_current = checkout(project, "run-current")

    old_time = time.time() - 10 * 86400
    os.utime(ws_old.path, (old_time, old_time))

    removed = cleanup(ws_current, keep_days=7)

    assert removed == ["run-old"]
    assert not ws_old.path.exists()
    assert ws_current.path.exists()


def test_cleanup_keeps_recent_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = _init_bare_origin(tmp_path)
    project = _project(str(origin))
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "root"))

    ws_recent = checkout(project, "run-recent")
    ws_current = checkout(project, "run-current2")

    removed = cleanup(ws_current, keep_days=7)

    assert removed == []
    assert ws_recent.path.exists()


# --------------------------------------------------------------------------
# GITHUB_TOKEN handling (pure helpers; no network)
# --------------------------------------------------------------------------


def test_github_token_only_applied_for_https_github_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token-value")
    assert ws_mod._github_token_for("https://github.com/acme/repo.git") == "secret-token-value"
    assert ws_mod._github_token_for("https://gitlab.com/acme/repo.git") is None
    assert ws_mod._github_token_for(str(tmp_path / "origin.git")) is None
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert ws_mod._github_token_for("https://github.com/acme/repo.git") is None


def test_sanitize_redacts_token_value() -> None:
    text = "fatal: could not read Username: AUTHORIZATION: bearer secret-token-value"
    sanitized = ws_mod._sanitize(text, "secret-token-value")
    assert "secret-token-value" not in sanitized
