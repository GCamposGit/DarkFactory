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
    def __init__(self, *, dirty: bool = False, old: bool = False) -> None:
        self.dirty = dirty
        self.head = OLD if old else SHA
        self.calls: list[list[str]] = []

    def __call__(self, root: Path, args: list[str]) -> str:
        self.calls.append(args)
        if args == ["rev-parse", "origin/main"]:
            return SHA
        if args == ["rev-parse", "HEAD"]:
            return self.head
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
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        self.calls.append((method, url))
        if "Desktop" in url or ":8080" in url:
            if method == "GET":
                if not self.returns:
                    raise OSError("offline")
                return {"git_sha": self.desktop}
            if url.endswith("/system/update"):
                self.desktop = SHA
                return {"success": True, "current_commit": SHA}
            if url.endswith("/system/restart"):
                return {"status": "restarting"}
        return {"git_sha": SHA}


def test_all_nodes_converged() -> None:
    report = mod.verify(root=Path("."), git=FakeGit(), http=FakeHttp())
    assert report.ok


def test_desktop_update_and_restart_converges() -> None:
    http = FakeHttp(desktop=OLD)
    report = mod.sync(root=Path("."), git=FakeGit(), http=http)
    assert report.ok
    assert ("POST", f"{mod.DESKTOP_URL}/system/update") in http.calls
    assert ("POST", f"{mod.DESKTOP_URL}/system/restart") in http.calls


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


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("DARKFAC_LIVE_NODES") != "1", reason="opt-in live node probe")
def test_live_nodes_match_origin_main() -> None:
    report = mod.verify()
    assert report.ok, report.model_dump_json()
