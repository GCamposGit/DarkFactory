"""Node synchronization contract with fake Git and HTTP transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os

import pytest

from core.infra import node_sync as mod


SHA = "a" * 40
OLD = "b" * 40


class FakeGit:
    def __init__(self, *, dirty: bool = False, old: bool = False, branch: str = "main", worktree: bool = False) -> None:
        self.dirty = dirty
        self.branch = branch
        self.worktree = worktree
        self.head = OLD if old else SHA
        self.calls: list[list[str]] = []

    def __call__(self, root: Path, args: list[str]) -> str:
        self.calls.append(args)
        if args == ["rev-parse", "origin/main"]:
            return SHA
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args == ["rev-parse", "--git-dir"]:
            return "C:/repo/.git/worktrees/x" if self.worktree else ".git"
        if args == ["rev-parse", "--git-common-dir"]:
            return "C:/repo/.git" if self.worktree else ".git"
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return self.branch
        if args == ["status", "--porcelain"]:
            return " M file" if self.dirty else ""
        if args == ["fetch", "origin", "main"]:
            return ""
        if args == ["merge", "--ff-only", "origin/main"]:
            self.head = SHA
            return ""
        raise AssertionError(args)


class FakeHttp:
    def __init__(self, *, desktop: str = SHA, returns: bool = True) -> None:
        self.desktop = desktop
        self.returns = returns
        self.vps_sha: str | None | bool = None
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        self.calls.append((method, url))
        if "Desktop" in url or ":8080" in url:
            if method == "GET":
                if not self.returns:
                    raise OSError("offline")
                return {"git_sha": self.desktop, "restart_safe": True}
            if url.endswith("/system/update"):
                self.desktop = SHA
                return {"success": True, "current_commit": SHA}
            if url.endswith("/system/restart"):
                return {"status": "restarting"}
        if self.vps_sha is False:
            return {"status": "ok", "git_sha": None}
        return {"git_sha": self.vps_sha or SHA}


def test_all_nodes_converged() -> None:
    report = mod.verify(root=Path("."), git=FakeGit(), http=FakeHttp())
    assert report.ok


def test_desktop_update_and_restart_converges() -> None:
    http = FakeHttp(desktop=OLD)
    report = mod.sync(root=Path("."), git=FakeGit(), http=http)
    assert report.ok
    assert ("POST", f"{mod.DESKTOP_URL}/system/update") in http.calls
    assert ("POST", f"{mod.DESKTOP_URL}/system/restart") in http.calls


def test_desktop_without_restart_safe_is_not_touched() -> None:
    class LegacyDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if method == "GET" and ":8080" in url:
                return {"git_sha": OLD}
            return super().__call__(method, url, payload, token)

    http = LegacyDesktop(desktop=OLD)
    report = mod.sync(root=Path("."), git=FakeGit(), http=http)
    assert report.nodes[1].state == "divergent"
    assert "restart_safe=true" in (report.nodes[1].reason or "")
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_desktop_offline_after_restart_fails() -> None:
    class LostDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if url.endswith("/system/restart"):
                self.returns = False
            return super().__call__(method, url, payload, token)

    http = LostDesktop(desktop=OLD)
    ticks = iter((0.0, 0.0, 1.0, 2.0, 3.0))
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, timeout=2, interval=1, clock=lambda: next(ticks))
    assert not report.ok
    assert report.nodes[1].state == "offline"


def test_dirty_notebook_is_not_touched() -> None:
    git = FakeGit(dirty=True, old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert not report.ok
    assert report.nodes[0].state == "divergent"
    assert ["fetch", "origin", "main"] not in git.calls
    assert ["merge", "--ff-only", "origin/main"] not in git.calls


def test_missing_sha_is_never_success() -> None:
    class LegacyHttp(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            return {"status": "ok"}

    report = mod.verify(root=Path("."), git=FakeGit(), http=LegacyHttp())
    assert not report.ok
    assert report.nodes[1].state == "divergent"


def test_vps_without_git_sha_is_unknown_not_divergent() -> None:
    http = FakeHttp()
    http.vps_sha = False
    report = mod.verify(root=Path("."), git=FakeGit(), http=http)
    vps = report.nodes[2]
    assert vps.state == "unknown"
    assert "git_sha" in (vps.reason or "")
    assert not report.ok


def test_sync_does_not_redeploy_vps_when_unknown() -> None:
    http = FakeHttp()
    http.vps_sha = False
    calls: list[int] = []
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, vps_redeploy=lambda: calls.append(1) or True)
    assert calls == []
    assert report.nodes[2].state == "unknown"


def test_sync_redeploys_divergent_vps_once() -> None:
    http = FakeHttp()
    http.vps_sha = OLD
    calls: list[int] = []

    def redeploy() -> bool:
        calls.append(1)
        http.vps_sha = None
        return True

    report = mod.sync(root=Path("."), git=FakeGit(), http=http, vps_redeploy=redeploy)
    assert calls == [1]
    assert report.ok


def test_redeploy_vps_passes_skip_node_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    class Done:
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: captured.append(cmd) or Done())
    assert mod.redeploy_vps() is True
    assert "--skip-node-sync" in captured[0] and "--skip-backup" in captured[0]


@pytest.mark.parametrize("kwargs, needle", [
    ({"branch": "feature/x", "old": True}, "branch"),
    ({"worktree": True, "old": True}, "worktree"),
    ({"branch": "feature/x"}, "branch"),  # same SHA on another branch is still not converged
])
def test_notebook_outside_main_is_divergent_and_untouched(kwargs: dict[str, Any], needle: str) -> None:
    git = FakeGit(**kwargs)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    notebook = report.nodes[0]
    assert notebook.state == "divergent"
    assert needle in (notebook.reason or "")
    assert not report.ok
    assert ["fetch", "origin", "main"] not in git.calls
    assert ["merge", "--ff-only", "origin/main"] not in git.calls
    assert ["status", "--porcelain"] not in git.calls


def test_notebook_clean_main_fast_forwards() -> None:
    git = FakeGit(old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert report.ok
    assert ["merge", "--ff-only", "origin/main"] in git.calls


def test_current_git_sha_env_then_git_then_none() -> None:
    from core.infra import git_sha

    full = "c" * 40
    boom = lambda root, timeout: (_ for _ in ()).throw(TimeoutError())  # noqa: E731
    assert git_sha.current_git_sha(Path("."), use_env=True, env={"DARKFAC_GIT_SHA": full}, read_head=boom) == full
    assert git_sha.current_git_sha(Path("."), use_env=True, env={"DARKFAC_GIT_SHA": ""}, read_head=lambda r, t: SHA) == SHA
    assert git_sha.current_git_sha(Path("."), env={"DARKFAC_GIT_SHA": full}, read_head=lambda r, t: "") is None
    assert git_sha.current_git_sha(Path("."), read_head=boom) is None
    assert git_sha.current_git_sha(Path("."), read_head=lambda r, t: "abc") is None


def test_worker_health_exposes_git_sha(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from core.harness import remote_worker

    monkeypatch.setattr(remote_worker, "current_git_sha", lambda root: SHA)
    client = TestClient(remote_worker.create_worker_app(project_root=tmp_path))
    assert client.get("/health").json()["git_sha"] == SHA
    monkeypatch.setattr(remote_worker, "current_git_sha", lambda root: None)
    assert client.get("/health").json()["git_sha"] is None


def test_hub_health_exposes_git_sha_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from hub.backend.main import app

    monkeypatch.setenv("DARKFAC_GIT_SHA", SHA)
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "git_sha": SHA}


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DARKFAC_LIVE_NODES") != "1", reason="opt-in live node probe")
def test_live_nodes_match_origin_main() -> None:
    report = mod.verify()
    assert report.ok, report.model_dump_json()
