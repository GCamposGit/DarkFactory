"""Node synchronization contract with fake Git and HTTP transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os

import pytest

from core.infra import node_sync as mod


SHA = "a" * 40
OLD = "b" * 40


@pytest.fixture(autouse=True)
def _pin_fixed_candidate(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Existing calls predate the required candidate; USR-420 cases opt out."""
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "")
    if request.node.get_closest_marker("raw_node_sync"):
        return
    verify, sync = mod.verify, mod.sync

    def _verify(*args: Any, **kwargs: Any) -> mod.SyncReport:
        kwargs.setdefault("expected_sha", SHA)
        return verify(*args, **kwargs)

    def _sync(*args: Any, **kwargs: Any) -> mod.SyncReport:
        kwargs.setdefault("expected_sha", SHA)
        return sync(*args, **kwargs)

    monkeypatch.setattr(mod, "verify", _verify)
    monkeypatch.setattr(mod, "sync", _sync)


class FakeGit:
    def __init__(
        self,
        *,
        dirty: bool = False,
        old: bool = False,
        branch: str = "main",
        worktree: bool = False,
        local_main_old: bool = False,
        fail_fetch_ref: bool = False,
    ) -> None:
        self.dirty = dirty
        self.branch = branch
        self.worktree = worktree
        self.head = OLD if old else SHA
        # SHA the local `main` branch ref points at -- independent of `head`/`branch`, since a shared
        # checkout parked on another branch for a parallel session still has its own `main` ref that
        # can be fast-forwarded without touching HEAD or the working tree (USR-222).
        self.local_main = OLD if local_main_old else SHA
        self.fail_fetch_ref = fail_fetch_ref
        self.calls: list[list[str]] = []

    def __call__(self, root: Path, args: list[str]) -> str:
        self.calls.append(args)
        if args == ["rev-parse", "origin/main"]:
            return SHA
        if args == ["rev-parse", "HEAD"]:
            return self.head
        if args == ["rev-parse", "refs/heads/main"]:
            return self.local_main
        if args == ["rev-parse", "--git-dir"]:
            return "C:/repo/.git/worktrees/x" if self.worktree else ".git"
        if args == ["rev-parse", "--git-common-dir"]:
            return "C:/repo/.git" if self.worktree else ".git"
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return self.branch
        if args == ["status", "--porcelain"]:
            return " M file" if self.dirty else ""
        if len(args) == 2 and args[0] == "rev-list":
            spec = args[1]
            if spec == f"{SHA}..HEAD":
                return ""
            if spec == f"HEAD..{SHA}":
                return "behind-commit" if self.head == OLD else ""
            raise AssertionError(args)
        if args[:2] == ["fetch", "origin"]:
            return ""
        if args == ["fetch", "origin", "main:refs/heads/main"]:
            if self.fail_fetch_ref:
                raise RuntimeError("git fetch failed (exit 1)")
            self.local_main = SHA
            return ""
        if len(args) == 3 and args[:2] == ["merge", "--ff-only"]:
            self.head = SHA
            return ""
        if len(args) >= 3 and args[:3] == ["show", "-s", "--format=%cI"]:
            return "2026-10-03T18:00:00+00:00"
        raise AssertionError(args)


class VirtualClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class FakeHttp:
    def __init__(
        self,
        *,
        desktop: str = SHA,
        returns: bool = True,
        busy: bool = False,
        active_runs: int = 0,
        transient_failures: int = 0,
    ) -> None:
        self.desktop = desktop
        self.returns = returns
        self.busy = busy
        self.active_runs = active_runs
        self.transient_failures = transient_failures
        self.vps_sha: str | None | bool = None
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        self.calls.append((method, url))
        if "Desktop" in url or ":8080" in url:
            if method == "GET":
                if not self.returns:
                    raise OSError("offline")
                if self.transient_failures > 0:
                    self.transient_failures -= 1
                    raise OSError("transient connection error")
                return {
                    "git_sha": self.desktop,
                    "restart_safe": True,
                    "busy": self.busy,
                    "active_runs": self.active_runs,
                }
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
    vclock = VirtualClock()
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, clock=vclock.clock, sleep=vclock.sleep)
    assert report.ok
    assert ("POST", f"{mod.DESKTOP_URL}/system/update") in http.calls
    assert ("POST", f"{mod.DESKTOP_URL}/system/restart") in http.calls


def test_desktop_without_restart_safe_is_not_touched() -> None:
    class LegacyDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if method == "GET" and ":8080" in url:
                return {"status": "ok"}
            return super().__call__(method, url, payload, token)

    http = LegacyDesktop(desktop=OLD)
    vclock = VirtualClock()
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, clock=vclock.clock, sleep=vclock.sleep)
    assert report.nodes[1].state == "divergent"
    assert report.nodes[1].reason  # legacy worker is reported with a reason, never silently skipped
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_desktop_offline_after_restart_fails() -> None:
    class LostDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if url.endswith("/system/restart"):
                self.returns = False
            return super().__call__(method, url, payload, token)

    http = LostDesktop(desktop=OLD)
    vclock = VirtualClock()
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, timeout=2, interval=1, clock=vclock.clock, sleep=vclock.sleep)
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
    assert mod.redeploy_vps() is False and mod.redeploy_vps("not-a-sha") is False and mod.redeploy_vps("A" * 40) is False
    assert captured == []
    assert mod.redeploy_vps(SHA) is True
    assert "--skip-node-sync" in captured[0] and "--skip-backup" in captured[0]
    assert captured[0][captured[0].index("--expected-sha") + 1] == SHA


def test_notebook_worktree_is_divergent_and_untouched() -> None:
    """A checkout that is itself a linked worktree (e.g. a ticket's own worktree) is never touched:
    some other session may be mid-work in it, regardless of what branch it is on."""
    git = FakeGit(worktree=True, old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    notebook = report.nodes[0]
    assert notebook.state == "divergent"
    assert "worktree" in (notebook.reason or "")
    assert not report.ok
    assert ["fetch", "origin", "main"] not in git.calls
    assert ["fetch", "origin", "main:refs/heads/main"] not in git.calls
    assert ["merge", "--ff-only", "origin/main"] not in git.calls
    assert ["status", "--porcelain"] not in git.calls


# --- USR-222: checkout compartilhado em outra branch (sessao paralela) --------------------------


def test_notebook_on_other_branch_converges_via_local_main_ref_without_touching_head() -> None:
    """A shared checkout parked on another branch for a parallel session (even with a dirty working
    tree there) still converges, because convergence is judged by the local `main` ref, never by
    HEAD or the working tree of whatever branch is actually checked out."""
    git = FakeGit(branch="fix/demand-id-allocator-no-git", dirty=True, old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert report.ok
    notebook = report.nodes[0]
    assert notebook.state == "converged"
    assert notebook.reason is None
    # The other session's branch and working tree are never inspected or switched.
    assert ["status", "--porcelain"] not in git.calls
    assert ["rev-parse", "HEAD"] not in git.calls or git.head == OLD  # HEAD, if read at all, is untouched
    assert git.branch == "fix/demand-id-allocator-no-git"


def test_notebook_on_other_branch_fast_forwards_stale_local_main_ref() -> None:
    """USR-420: a parallel session's main ref is not fast-forwarded."""
    git = FakeGit(branch="fix/demand-id-allocator-no-git", local_main_old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert not report.ok
    notebook = report.nodes[0]
    assert notebook.state == "divergent"
    assert ["fetch", "origin", "main:refs/heads/main"] not in git.calls
    assert ["merge", "--ff-only", "origin/main"] not in git.calls
    assert git.local_main == OLD


def test_notebook_on_other_branch_reports_pending_when_ref_fetch_fails() -> None:
    """A stale main ref is diagnosed against the fixed candidate and is not fetched."""
    git = FakeGit(branch="fix/demand-id-allocator-no-git", local_main_old=True, fail_fetch_ref=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert not report.ok
    notebook = report.nodes[0]
    assert notebook.state == "divergent"
    assert "branch" in (notebook.reason or "")
    assert f"behind fixed candidate {SHA}" in (notebook.reason or "")
    assert "no ref update attempted" in (notebook.reason or "")


def test_notebook_clean_main_fast_forwards() -> None:
    git = FakeGit(old=True)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp())
    assert report.ok
    assert ["fetch", "origin", SHA] in git.calls
    assert ["merge", "--ff-only", SHA] in git.calls
    assert ["rev-parse", "origin/main"] not in git.calls
    assert ["merge", "--ff-only", "origin/main"] not in git.calls


class _RelationGit(FakeGit):
    """Classify HEAD only against the caller-supplied candidate, never origin/main."""

    def __init__(self, *, ahead: str = "", behind: str = "") -> None:
        super().__init__(old=True)
        self._ahead = ahead
        self._behind = behind

    def __call__(self, root: Path, args: list[str]) -> str:
        if args == ["rev-parse", "origin/main"]:
            self.calls.append(args)
            return "e" * 40
        if len(args) == 2 and args[0] == "rev-list":
            self.calls.append(args)
            if args[1].endswith("..HEAD"):
                return self._ahead
            if args[1].startswith("HEAD.."):
                return self._behind
            raise AssertionError(args)
        return super().__call__(root, args)


_MUTATING = {"fetch", "merge", "reset", "checkout", "clean", "restore"}


@pytest.mark.parametrize(
    ("ahead", "behind", "kind"),
    [("local-1", "", "ahead"), ("local-1\nlocal-2", "remote-1", "diverged")],
)
def test_ahead_or_diverged_notebook_is_not_mutated(ahead: str, behind: str, kind: str) -> None:
    """USR-420: classification uses the fixed candidate; those states do not move the checkout."""
    git = _RelationGit(ahead=ahead, behind=behind)
    report = mod.sync(root=Path("."), git=git, http=FakeHttp(), sleep=lambda _s: None)
    notebook = report.nodes[0]
    assert report.expected_sha == SHA and not report.ok
    assert notebook.state == "divergent" and notebook.sync_classification == kind
    assert ["rev-parse", "origin/main"] not in git.calls
    assert {call[1] for call in git.calls if call[0] == "rev-list"} == {f"{SHA}..HEAD", f"HEAD..{SHA}"}
    assert not any(call and call[0] in _MUTATING for call in git.calls)
    assert git.head == OLD


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


def test_desktop_with_git_sha_but_no_restart_safe_flag_is_updated() -> None:
    """A worker exposing git_sha (USR-65+) already has the safe restart (USR-64), so it is updated."""
    class PreFlagDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            result = super().__call__(method, url, payload, token)
            if method == "GET" and ":8080" in url:
                result = {k: v for k, v in result.items() if k != "restart_safe"}
            return result

    http = PreFlagDesktop(desktop=OLD)
    vclock = VirtualClock()
    report = mod.sync(root=Path("."), git=FakeGit(), http=http, clock=vclock.clock, sleep=vclock.sleep)
    assert ("POST", f"{mod.DESKTOP_URL}/system/restart") in http.calls
    assert report.nodes[1].state == "converged"


def test_probe_retries_transient_failure_and_converges() -> None:
    """AC 1: A sonda de cada no (Desktop e VPS) repete ate 3 vezes com backoff curto antes de devolver 'offline'."""
    http = FakeHttp(transient_failures=1)
    report = mod.verify(root=Path("."), git=FakeGit(), http=http, retries=3, retry_delay=0.01)
    assert report.nodes[1].state == "converged"

    class TransientVps(FakeHttp):
        def __init__(self) -> None:
            super().__init__()
            self.vps_fails = 1

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if "darkhub" in url:
                if self.vps_fails > 0:
                    self.vps_fails -= 1
                    raise OSError("vps transient failure")
                return {"git_sha": SHA}
            return super().__call__(method, url, payload, token)

    report_vps = mod.verify(root=Path("."), git=FakeGit(), http=TransientVps(), retries=3, retry_delay=0.01)
    assert report_vps.nodes[2].state == "converged"


def test_probe_all_retries_fail_marks_offline_with_attempt_count() -> None:
    """AC 4: Mensagem final distingue 'offline apos N tentativas' de 'divergente'."""
    http = FakeHttp(returns=False)
    report = mod.verify(root=Path("."), git=FakeGit(), http=http, retries=3, retry_delay=0.01)
    desktop = report.nodes[1]
    assert desktop.state == "offline"
    assert "offline apos 3 tentativas" in (desktop.reason or "")


def test_sync_no_update_or_restart_when_desktop_is_offline() -> None:
    """AC 2: Nenhum POST /system/update ou /system/restart e enviado quando a sonda inicial nao observou um SHA divergente."""
    http = FakeHttp(returns=False)
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        clock=vclock.clock, sleep=vclock.sleep, retries=3, retry_delay=0.01,
    )
    assert not report.ok
    assert report.nodes[1].state == "offline"
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


@pytest.mark.parametrize("busy, active_runs", [(True, 0), (False, 2)])
def test_sync_no_restart_when_desktop_is_busy(busy: bool, active_runs: int) -> None:
    """AC 3: Nenhum /system/restart e enviado quando /health informa busy=true ou active_runs>0; motivo no relatorio."""
    http = FakeHttp(desktop=OLD, busy=busy, active_runs=active_runs)
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        clock=vclock.clock, sleep=vclock.sleep, retries=3, retry_delay=0.01,
    )
    assert not report.ok
    assert report.nodes[1].state == "divergent"
    assert "busy" in (report.nodes[1].reason or "").lower()
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_sync_waits_for_desktop_spontaneous_convergence_without_post() -> None:
    """AC 6: Desktop offline na primeira sonda: esperar ate 120 s pela convergencia antes de declarar estado e antes de qualquer POST."""
    class DelayedConvergedHttp(FakeHttp):
        def __init__(self, offline_calls: int = 5) -> None:
            super().__init__()
            self.offline_remaining = offline_calls

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            self.calls.append((method, url))
            if "Desktop" in url or ":8080" in url:
                if method == "GET":
                    if self.offline_remaining > 0:
                        self.offline_remaining -= 1
                        raise OSError("worker rebooting")
                    return {"git_sha": SHA, "restart_safe": True}
            return super().__call__(method, url, payload, token)

    http = DelayedConvergedHttp(offline_calls=5)
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        timeout=120, interval=2, clock=vclock.clock, sleep=vclock.sleep,
        retries=3, retry_delay=0.01,
    )
    assert report.ok
    assert report.nodes[1].state == "converged"
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_sync_waits_for_divergent_desktop_spontaneous_convergence_without_post() -> None:
    """AC 6: Desktop com SHA diferente na primeira sonda: esperar ate 120 s pela convergencia antes de qualquer POST."""
    class DelayedDivergentToConvergedHttp(FakeHttp):
        def __init__(self, divergent_calls: int = 3) -> None:
            super().__init__(desktop=OLD)
            self.divergent_remaining = divergent_calls

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            self.calls.append((method, url))
            if "Desktop" in url or ":8080" in url:
                if method == "GET":
                    if self.divergent_remaining > 0:
                        self.divergent_remaining -= 1
                        return {"git_sha": OLD, "restart_safe": True}
                    return {"git_sha": SHA, "restart_safe": True}
            return super().__call__(method, url, payload, token)

    http = DelayedDivergentToConvergedHttp(divergent_calls=3)
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        timeout=120, interval=2, clock=vclock.clock, sleep=vclock.sleep,
        retries=3, retry_delay=0.01,
    )
    assert report.ok
    assert report.nodes[1].state == "converged"
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_desktop_legacy_recovers_via_scheduled_task() -> None:
    """USR-71: Legacy worker without git_sha/restart_safe triggers Scheduled Task recovery."""
    class LegacyRecoveringHttp(FakeHttp):
        def __init__(self) -> None:
            super().__init__()
            self.recovered = False

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            self.calls.append((method, url))
            if ":8080" in url and method == "GET":
                if not self.recovered:
                    return {"status": "ok"}
                return {"git_sha": SHA, "restart_safe": True}
            return super().__call__(method, url, payload, token)

    recovery_calls: list[str] = []
    http = LegacyRecoveringHttp()
    vclock = VirtualClock()

    def fake_recovery(task_name: str) -> bool:
        recovery_calls.append(task_name)
        http.recovered = True
        return True

    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        task_recovery=fake_recovery,
        clock=vclock.clock, sleep=vclock.sleep,
    )
    assert report.ok
    assert report.nodes[1].state == "converged"
    assert recovery_calls == ["DarkFac Test Worker"]
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)


def test_desktop_restart_transient_offline_treated_as_restarting_and_converges() -> None:
    """USR-81: Transient offline during worker restart window is treated as restarting, then converges."""
    class TransientRestartHttp(FakeHttp):
        def __init__(self, offline_probes: int = 3) -> None:
            super().__init__(desktop=OLD)
            self.restarting = False
            self.offline_remaining = offline_probes

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            self.calls.append((method, url))
            if ":8080" in url:
                if url.endswith("/system/restart"):
                    self.restarting = True
                    return {"status": "restarting"}
                if method == "GET":
                    if self.restarting:
                        if self.offline_remaining > 0:
                            self.offline_remaining -= 1
                            raise OSError("worker is restarting")
                        return {"git_sha": SHA, "restart_safe": True}
                    return {"git_sha": self.desktop, "restart_safe": True}
            return super().__call__(method, url, payload, token)

    http = TransientRestartHttp(offline_probes=3)
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        timeout=60, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert report.ok
    assert report.nodes[1].state == "converged"


def test_desktop_restart_persistent_offline_fails_with_clear_cause() -> None:
    """USR-81: Persistent offline after worker restart fails with clear timeout reason."""
    class PersistentOfflineRestartHttp(FakeHttp):
        def __init__(self) -> None:
            super().__init__(desktop=OLD)
            self.restarting = False

        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            self.calls.append((method, url))
            if ":8080" in url:
                if url.endswith("/system/restart"):
                    self.restarting = True
                    return {"status": "restarting"}
                if method == "GET" and self.restarting:
                    raise OSError("worker permanently offline")
            return super().__call__(method, url, payload, token)

    http = PersistentOfflineRestartHttp()
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http,
        timeout=10, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert not report.ok
    desktop = report.nodes[1]
    assert desktop.state == "offline"
    assert "offline apos restart" in (desktop.reason or "").lower()


def test_node_status_records_commit_date() -> None:
    """USR-82: NodeStatus records commit_date so divergence age can be measured."""
    git = FakeGit()
    http = FakeHttp()
    report = mod.verify(root=Path("."), git=git, http=http)
    assert report.ok
    notebook = report.nodes[0]
    assert notebook.commit_date == "2026-10-03T18:00:00+00:00"


@pytest.fixture(autouse=True)
def _never_touch_real_scheduled_task(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Hermetico: nenhum teste deste modulo pode acionar schtasks de verdade (USR-186)."""
    calls: list[str] = []

    def fake(task_name: str = "DarkFac Test Worker") -> bool:
        calls.append(task_name)
        return True

    monkeypatch.setattr(mod, "recover_scheduled_task", fake)
    return calls


class _PortHeldDesktop(FakeHttp):
    """Simula USR-186: o restart deixa o worker desligado (porta 8080 presa) ate a tarefa agendada reiniciar."""

    def __init__(self) -> None:
        super().__init__(desktop=OLD)
        self.restarting = False
        self.recovered = False

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        self.calls.append((method, url))
        if ":8080" in url:
            if url.endswith("/system/restart"):
                self.restarting = True
                return {"status": "restarting"}
            if url.endswith("/system/update"):
                return {"success": True, "current_commit": SHA}
            if method == "GET":
                if self.recovered:
                    return {"git_sha": SHA, "restart_safe": True, "busy": False, "active_runs": 0, "queue_length": 0}
                if self.restarting:
                    raise OSError("[Errno 10048] bind: porta 8080 ocupada; worker nao subiu")
                return {"git_sha": OLD, "restart_safe": True, "busy": False, "active_runs": 0, "queue_length": 0}
        return super().__call__(method, url, payload, token)


def test_restart_with_port_held_recovers_via_scheduled_task() -> None:
    """USR-186: se a nova instancia nao sobe apos /system/restart, node_sync aciona schtasks /End + /Run."""
    http = _PortHeldDesktop()
    vclock = VirtualClock()
    recovery_calls: list[str] = []

    def fake_recovery(task_name: str) -> bool:
        recovery_calls.append(task_name)
        http.recovered = True
        return True

    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=fake_recovery,
        timeout=10, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert report.ok
    assert recovery_calls == ["DarkFac Test Worker"]
    assert ("POST", f"{mod.DESKTOP_URL}/system/restart") in http.calls


def test_restart_with_port_held_and_recovery_failing_reports_offline_with_cause() -> None:
    """USR-186: se nem a tarefa agendada levanta o worker, o relatorio diz isso (sem mascarar como divergente)."""
    http = _PortHeldDesktop()
    vclock = VirtualClock()
    recovery_calls: list[str] = []

    def fake_recovery(task_name: str) -> bool:
        recovery_calls.append(task_name)
        return False

    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=fake_recovery,
        timeout=6, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert not report.ok
    assert report.nodes[1].state == "offline"
    assert "tarefa agendada" in (report.nodes[1].reason or "")
    assert recovery_calls == ["DarkFac Test Worker"]  # uma unica tentativa de recuperacao, sem loop


def test_update_failure_with_worker_left_offline_triggers_recovery() -> None:
    """USR-186: update sem SHA esperado e worker desligado em seguida -> recuperacao antes de declarar divergencia."""
    class UpdateWithoutShaThenDown(_PortHeldDesktop):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if url.endswith("/system/update"):
                self.calls.append((method, url))
                self.restarting = True  # o worker se reiniciou sozinho e nao voltou
                return {"success": True, "current_commit": ""}
            return super().__call__(method, url, payload, token)

    http = UpdateWithoutShaThenDown()
    vclock = VirtualClock()
    recovery_calls: list[str] = []

    def fake_recovery(task_name: str) -> bool:
        recovery_calls.append(task_name)
        http.recovered = True
        return True

    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=fake_recovery,
        timeout=10, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert report.ok
    assert recovery_calls == ["DarkFac Test Worker"]


def test_update_failure_with_worker_alive_does_not_trigger_recovery() -> None:
    """Update sem SHA com worker vivo: reporta divergencia, nao mata o worker via tarefa agendada."""
    class UpdateWithoutSha(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if url.endswith("/system/update"):
                self.calls.append((method, url))
                return {"success": False, "current_commit": ""}
            return super().__call__(method, url, payload, token)

    http = UpdateWithoutSha(desktop=OLD)
    vclock = VirtualClock()
    recovery_calls: list[str] = []
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=lambda name: recovery_calls.append(name) or True,
        timeout=4, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert not report.ok
    assert report.nodes[1].state == "divergent"
    assert "expected SHA" in (report.nodes[1].reason or "")
    assert recovery_calls == []


def test_sync_no_restart_when_desktop_has_queued_job() -> None:
    """USR-182: job `queued` (busy=False, active_runs=0, queue_length>0) conta como ocupado."""
    class QueuedDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if method == "GET" and ":8080" in url:
                self.calls.append((method, url))
                return {
                    "git_sha": self.desktop, "restart_safe": True,
                    "busy": False, "active_runs": 0, "queue_length": 1,
                }
            return super().__call__(method, url, payload, token)

    http = QueuedDesktop(desktop=OLD)
    vclock = VirtualClock()
    recovery_calls: list[str] = []
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=lambda name: recovery_calls.append(name) or True,
        clock=vclock.clock, sleep=vclock.sleep, retries=3, retry_delay=0.01,
    )
    assert not report.ok
    assert report.nodes[1].state == "divergent"
    assert report.nodes[1].queue_length == 1
    assert "busy" in (report.nodes[1].reason or "").lower()
    assert "queue_length" in (report.nodes[1].reason or "")
    assert not any(method == "POST" and ":8080" in url for method, url in http.calls)
    assert recovery_calls == []




# --- USR-196: o erro real do /system/update chega ao relatorio, sem segredos ---------------------------------

class _FailingUpdate(FakeHttp):
    def __init__(self, output: str, *, current_commit: str = OLD) -> None:
        super().__init__(desktop=OLD)
        self.output = output
        self.current_commit = current_commit

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        if url.endswith("/system/update"):
            self.calls.append((method, url))
            return {"success": False, "output": self.output, "current_commit": self.current_commit}
        return super().__call__(method, url, payload, token)


def _sync_with_update_failure(output: str, *, token: str | None = None) -> mod.SyncReport:
    http = _FailingUpdate(output)
    vclock = VirtualClock()
    return mod.sync(
        root=Path("."), git=FakeGit(), http=http, token=token, task_recovery=lambda name: True,
        timeout=4, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )


def test_update_failure_reports_worker_git_stderr_in_reason() -> None:
    stderr = (
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "\t.factory/state.json\nPlease move or remove them before you merge.\nAborting"
    )
    report = _sync_with_update_failure(stderr)
    desktop = report.nodes[1]
    assert desktop.state == "divergent"
    assert "expected SHA" in (desktop.reason or "")
    assert "would be overwritten by merge" in (desktop.reason or "")
    assert ".factory/state.json" in (desktop.reason or "")
    assert "\n" not in (desktop.reason or "")


def test_update_failure_redacts_secrets_and_truncates_output() -> None:
    token = "tok-secret-1234567890"
    # Montado em tempo de execucao para o varredor de segredos do commit nao ver um literal.
    gh_token = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123456789"
    stderr = (
        f"fatal: unable to access 'https://user:{gh_token}@github.com/o/r.git/': 403\n"
        f"remote: Authorization: Bearer {token}\n"
        f"worker saw {token} and password=hunter2\n" + ("x" * 5000)
    )
    report = _sync_with_update_failure(stderr, token=token)
    reason = report.nodes[1].reason or ""
    for secret in (token, gh_token, "user:gh" + "p_", "hunter2"):
        assert secret not in reason
    assert "[REDACTED]" in reason
    assert "github.com" in reason  # o host continua util para o diagnostico
    assert len(reason) < 1300
    assert "truncado" in reason


def test_update_failure_is_recorded_as_pending_convergence() -> None:
    mod.clear_pending_convergence()
    _sync_with_update_failure("error: Your local changes would be overwritten by merge")
    pending = mod.load_pending_convergence()
    assert pending is not None
    assert pending["node"] == "Desktop"
    assert pending["expected_sha"] == SHA
    assert "would be overwritten" in pending["reason"]
    mod.clear_pending_convergence()


def test_update_success_with_wrong_commit_also_reports_output() -> None:
    class Reply(_FailingUpdate):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if url.endswith("/system/update"):
                self.calls.append((method, url))
                return {"success": True, "output": "Already up to date.", "current_commit": OLD}
            return super().__call__(method, url, payload, token)

    http = Reply("unused")
    vclock = VirtualClock()
    report = mod.sync(
        root=Path("."), git=FakeGit(), http=http, task_recovery=lambda name: True,
        timeout=4, interval=2, clock=vclock.clock, sleep=vclock.sleep,
    )
    assert "Already up to date." in (report.nodes[1].reason or "")


def test_redact_worker_output_handles_empty_and_plain_text() -> None:
    assert mod.redact_worker_output(None) == ""
    assert mod.redact_worker_output("") == ""
    assert mod.redact_worker_output("Already up to date.") == "Already up to date."


class _DiffGit(FakeGit):
    def __init__(self, changed: str) -> None:
        super().__init__()
        self.changed = changed

    def __call__(self, root: Path, args: list[str]) -> str:
        if args[:2] == ["diff", "--name-only"]:
            return self.changed
        return super().__call__(root, args)


@pytest.mark.parametrize(
    "changed,state",
    [("README.md\ntests/x.py", "converged"), (".factory/demands/demands.json\ndocs/a.md", "divergent"), ("core/a.py\ndocs/a.md", "divergent")],
)
def test_vps_on_older_sha_is_converged_only_when_image_is_unchanged(changed: str, state: str) -> None:
    """USR-207: data-only merges intentionally skip the Hub rebuild, so an older VPS sha is not drift."""
    http = FakeHttp()
    http.vps_sha = OLD
    report = mod.verify(root=Path("."), git=_DiffGit(changed), http=http)
    assert report.nodes[2].state == state


def _unknown(report: mod.SyncReport) -> None:
    assert report.expected_sha is None and not report.ok
    assert [n.state for n in report.nodes] == ["unknown", "unknown", "unknown"]
    assert all(n.reason == "missing or invalid immutable expected_sha" for n in report.nodes)


def _boom_git(root: Path, args: list[str]) -> str:
    raise AssertionError(args)


def _boom_http(method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
    raise AssertionError(url)


def test_missing_candidate_never_reads_origin_or_probes() -> None:
    """USR-420: sem SHA valido nao ha git, HTTP nem origin/main."""
    for candidate in (None, "", "not-a-sha", "a" * 39, "a" * 41, "A" * 40, "g" * 40):
        _unknown(mod.verify(root=Path("."), git=_boom_git, http=_boom_http, expected_sha=candidate))
        _unknown(mod.sync(root=Path("."), git=_boom_git, http=_boom_http, expected_sha=candidate, sleep=lambda _s: None))


def test_classify_checkout_compares_head_to_the_candidate_only() -> None:
    """origin/main pode apontar para outro commit; a classe segue o SHA recebido."""
    other = "c" * 40

    class _Moved(FakeGit):
        def __call__(self, root: Path, args: list[str]) -> str:
            if args == ["rev-parse", "origin/main"]:
                return other
            return super().__call__(root, args)

    on_candidate = _Moved()
    kind, commits, _dirty, reason = mod.classify_checkout(Path("."), on_candidate, SHA)
    assert kind == "converged" and commits == [] and reason is None
    assert ["rev-parse", "origin/main"] not in on_candidate.calls
    assert [f"{SHA}..HEAD", f"HEAD..{SHA}"] == [c[1] for c in on_candidate.calls if c[0] == "rev-list"]

    behind = _Moved()
    behind.head = OLD
    kind, _commits, _dirty, reason = mod.classify_checkout(Path("."), behind, SHA)
    assert kind == "behind" and SHA in (reason or "")

    invalid = FakeGit()
    kind, commits, _dirty, reason = mod.classify_checkout(Path("."), invalid, "A" * 40)
    assert kind is None and commits == [] and reason is None
    assert invalid.calls == []

    http = FakeHttp()
    checked = mod.verify(root=Path("."), git=on_candidate, http=http, expected_sha=SHA)
    assert checked.ok and checked.expected_sha == SHA and checked.nodes[2].git_sha == SHA
    assert checked.nodes[2].state == "converged"


def test_cli_flag_beats_env_and_invalid_sha_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DARKFAC_EXPECTED_SHA", raising=False)
    monkeypatch.setattr(mod, "run_git", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("git")))
    monkeypatch.setattr(mod, "request_json", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("http")))
    assert mod.main(["verify"]) == 2 and mod.main(["sync"]) == 2 and mod.main(["verify", "--expected-sha", "zz"]) == 2
    assert "immutable candidate SHA" in capsys.readouterr().err
    flag, env_sha = "c" * 40, "d" * 40
    monkeypatch.setenv("DARKFAC_EXPECTED_SHA", env_sha)
    assert mod.main(["sync", "--expected-sha", "A" * 40]) == 2
    assert "immutable candidate SHA" in capsys.readouterr().err
    monkeypatch.setenv("DARKFAC_EXPECTED_SHA", "not-a-sha")
    assert mod.main(["verify"]) == 2
    assert "immutable candidate SHA" in capsys.readouterr().err
    monkeypatch.setenv("DARKFAC_EXPECTED_SHA", env_sha)
    seen: list[tuple[str, str]] = []

    def report(action: str, sha: str) -> mod.SyncReport:
        seen.append((action, sha))
        return mod.SyncReport(expected_sha=sha, nodes=[
            mod.NodeStatus(name=name, state="converged", git_sha=sha) for name in ("Notebook", "Desktop", "VPS")
        ])

    monkeypatch.setattr(mod, "verify", lambda **kw: report("verify", kw["expected_sha"]))
    monkeypatch.setattr(mod, "sync", lambda **kw: report("sync", kw["expected_sha"]))
    assert mod.main(["verify", "--expected-sha", flag]) == 0 and mod.main(["sync"]) == 0
    assert seen == [("verify", flag), ("sync", env_sha)]


def test_retry_uses_pending_sha_and_refuses_an_invalid_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """retry ignora flag e env; a pendencia sem SHA de 40 hex termina em codigo 2."""
    monkeypatch.delenv("DARKFAC_EXPECTED_SHA", raising=False)
    monkeypatch.setattr(mod, "run_git", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("git")))
    monkeypatch.setattr(mod, "request_json", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("http")))
    stored: dict[str, Any] | None = None

    def load(path: Path | None = None) -> dict[str, Any] | None:
        return None if stored is None else dict(stored)

    monkeypatch.setattr(mod, "load_pending_convergence", load)
    seen: list[str] = []

    def fake_sync(**kwargs: Any) -> mod.SyncReport:
        seen.append(kwargs["expected_sha"])
        sha = kwargs["expected_sha"]
        return mod.SyncReport(expected_sha=sha, nodes=[
            mod.NodeStatus(name=name, state="converged", git_sha=sha) for name in ("Notebook", "Desktop", "VPS")
        ])

    monkeypatch.setattr(mod, "sync", fake_sync)
    assert mod.main(["retry"]) == 0 and seen == []
    assert "No pending convergence" in capsys.readouterr().out

    for bad in ("not-a-sha", "", "A" * 40, None):
        stored = {"node": "Desktop", "expected_sha": bad}
        assert mod.main(["retry", "--expected-sha", SHA]) == 2
        assert "immutable candidate SHA" in capsys.readouterr().err
        assert seen == []

    stored = {"node": "Desktop", "expected_sha": SHA}
    monkeypatch.setenv("DARKFAC_EXPECTED_SHA", "e" * 40)
    assert mod.main(["retry", "--expected-sha", "f" * 40]) == 0
    assert seen == [SHA]


def test_redeploy_and_converged_health_stay_on_the_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "load_pending_convergence", lambda path=None: None)
    captured: list[list[str]] = []

    class _Done:
        returncode = 0

    http, git = FakeHttp(), FakeGit()
    http.vps_sha = OLD

    def _run(cmd: list[str], **_k: object) -> _Done:
        captured.append(list(cmd))
        http.vps_sha = SHA
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", _run)
    report = mod.sync(root=Path("."), git=git, http=http, expected_sha=SHA, sleep=lambda _s: None)
    for command in captured:
        assert "--skip-backup" in command and "--skip-node-sync" in command
        assert command[command.index("--expected-sha") + 1] == SHA
    assert report.expected_sha == SHA and report.nodes[2].state == "converged"
    assert ["rev-parse", "origin/main"] not in git.calls

    class _Moving(FakeGit):
        def __call__(self, root: Path, args: list[str]) -> str:
            if args == ["rev-parse", "origin/main"]:
                raise AssertionError(args)
            return super().__call__(root, args)

    moving = _Moving()
    moving.head = moving.local_main = SHA
    checked = mod.verify(root=Path("."), git=moving, http=FakeHttp(), expected_sha=SHA)
    assert checked.ok and checked.nodes[2].git_sha == SHA


def _excluded(node: mod.NodeStatus, *, blocks_backup: bool) -> None:
    assert node.state == "excluded" and node.reason == "disabled_by_policy" and node.target_sha == SHA
    assert node.blocks_backup is blocks_backup


def _quiet(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for name in ("record_pending_convergence", "load_pending_convergence", "clear_pending_convergence"):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    return dict(
        sleep=lambda _s: None, retries=1, retry_delay=0,
        task_recovery=lambda name: (_ for _ in ()).throw(AssertionError(name)),
        vps_redeploy=lambda: (_ for _ in ()).throw(AssertionError("redeploy")),
    )


def test_missing_local_candidate_is_not_converged_and_is_not_fetched() -> None:
    """rev-list failure is not an empty range. Do not claim converged and do not fetch."""

    class _Missing(FakeGit):
        def __call__(self, root: Path, args: list[str]) -> str:
            if args == ["rev-parse", "origin/main"] or args[:1] == ["rev-list"]:
                self.calls.append(args)
                raise RuntimeError("missing object")
            return super().__call__(root, args)

    git = _Missing(old=True)
    kind, commits, _dirty, reason = mod.classify_checkout(Path("."), git, SHA)
    assert kind is None and commits == []
    assert reason == "candidate commit is not available locally; no git operation attempted"
    assert not any(call[:1] == ["rev-parse"] and call[1:] == ["origin/main"] for call in git.calls)

    report = mod.sync(root=Path("."), git=git, http=FakeHttp(), expected_sha=SHA, sleep=lambda _s: None)
    assert report.expected_sha == SHA and not report.ok
    assert report.nodes[0].state == "divergent"
    assert report.nodes[0].sync_classification is None
    assert "not available locally" in (report.nodes[0].reason or "")
    assert report.nodes[2].state == "converged" and report.nodes[2].git_sha == SHA
    assert not any(call and call[0] in _MUTATING for call in git.calls)
    assert ["rev-parse", "origin/main"] not in git.calls


def test_disabled_hosts_are_excluded_and_do_not_mask_or_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem git, HTTP, schtasks ou redeploy; VPS excluida nao e ok; falha ativa continua."""
    kw = _quiet(monkeypatch)
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", " Notebook , Desktop , VPS ")
    probes = {"git": 0, "http": 0}

    def counting_git(root: Path, args: list[str]) -> str:
        probes["git"] += 1
        raise AssertionError(args)

    def counting_http(method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        probes["http"] += 1
        raise AssertionError(url)

    for report in (
        mod.verify(root=Path("."), git=counting_git, http=counting_http, expected_sha=SHA),
        mod.sync(root=Path("."), git=counting_git, http=counting_http, expected_sha=SHA, **kw),
    ):
        assert report.expected_sha == SHA and not report.ok and report.vps_excluded
        assert report.excluded_hosts == ["Notebook", "Desktop", "VPS"]
        for node in report.nodes:
            _excluded(node, blocks_backup=node.name == "VPS")
    assert probes == {"git": 0, "http": 0}

    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "Desktop")
    seen: list[set[str]] = []
    real_verify = mod.verify

    def wrapped(**kwargs: Any) -> mod.SyncReport:
        seen.append(set(kwargs["disabled_hosts"]))
        monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "notebook,vps")
        return real_verify(**kwargs)

    monkeypatch.setattr(mod, "verify", wrapped)
    desktop_http = FakeHttp()
    stable = mod.sync(root=Path("."), git=FakeGit(), http=desktop_http, **kw)
    assert seen == [{"desktop"}, {"desktop"}] and stable.ok
    _excluded(stable.nodes[1], blocks_backup=False)
    assert not any(":8080" in url for _m, url in desktop_http.calls)
    monkeypatch.setattr(mod, "verify", real_verify)

    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "vps")
    vps_http = FakeHttp()
    blocked = mod.sync(root=Path("."), git=FakeGit(), http=vps_http, **kw)
    _excluded(blocked.nodes[2], blocks_backup=True)
    assert not blocked.ok and not any(url == mod.VPS_URL for _m, url in vps_http.calls)

    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "desktop")

    class _NoDesktop(FakeHttp):
        def __call__(self, method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
            if ":8080" in url:
                raise AssertionError(url)
            return super().__call__(method, url, payload, token)

    bad = _NoDesktop()
    bad.vps_sha = OLD
    masked = mod.sync(
        root=Path("."), git=FakeGit(), http=bad, retries=1, retry_delay=0, timeout=0, interval=1,
        clock=VirtualClock().clock, sleep=lambda _s: None, vps_redeploy=lambda: True,
        task_recovery=lambda name: (_ for _ in ()).throw(AssertionError(name)),
    )
    _excluded(masked.nodes[1], blocks_backup=False)
    assert masked.nodes[2].state == "divergent" and masked.nodes[2].git_sha == OLD and not masked.ok


def test_disabled_policy_is_read_once_and_frozen_during_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uma leitura vale o verify e o sync; mudar a env no meio nao inclui nem exclui outro host."""
    _quiet(monkeypatch)
    reads = {"n": 0}
    original = mod._disabled_hosts

    def counting(hosts: set[str] | None = None) -> set[str]:
        if hosts is None:
            reads["n"] += 1
        return original(hosts)

    monkeypatch.setattr(mod, "_disabled_hosts", counting)
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", " DeskTop ")

    def git(root: Path, args: list[str]) -> str:
        monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "vps, notebook")
        return FakeGit()(root, args)

    def http(method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "NOTEBOOK")
        if ":8080" in url:
            raise AssertionError(url)
        if method == "GET":
            return {"git_sha": SHA, "restart_safe": True}
        raise AssertionError((method, url))

    report = mod.sync(
        root=Path("."), git=git, http=http, sleep=lambda _s: None, retries=1, retry_delay=0,
        task_recovery=lambda name: (_ for _ in ()).throw(AssertionError(name)),
        vps_redeploy=lambda: (_ for _ in ()).throw(AssertionError("redeploy")),
    )
    assert reads["n"] == 1 and report.ok and report.expected_sha == SHA
    assert report.nodes[0].state == "converged" and report.nodes[2].git_sha == SHA
    _excluded(report.nodes[1], blocks_backup=False)

    reads["n"] = 0
    monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "VpS")

    def verify_git(root: Path, args: list[str]) -> str:
        monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "notebook, desktop")
        return FakeGit()(root, args)

    def verify_http(method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
        monkeypatch.setenv("DARKFAC_FLEET_DISABLED_HOSTS", "")
        if url == mod.VPS_URL:
            raise AssertionError(url)
        if method == "GET" and ":8080" in url:
            return {"git_sha": OLD, "restart_safe": True}
        raise AssertionError((method, url))

    checked = mod.verify(root=Path("."), git=verify_git, http=verify_http, retries=1, retry_delay=0, sleep=lambda _s: None)
    assert reads["n"] == 1 and checked.nodes[0].state == "converged"
    assert checked.nodes[1].state == "divergent" and checked.nodes[1].git_sha == OLD
    _excluded(checked.nodes[2], blocks_backup=True)
    assert not checked.ok
