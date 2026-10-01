"""Tests for autonomous branch delivery (push, PR, merge, cleanup, sweep) in core.git.autonomy."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git.autonomy import DeliveryReport, GitAutonomyManager
from core.roadmap.models import DeliveryStatus


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


def _ident(repo: Path) -> None:
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")


class FakeGh:
    """In-memory GitHub CLI simulating PRs against a local bare repository."""

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.prs: dict[int, dict] = {}
        self.calls: list[list[str]] = []

    def add_open_pr(self, branch: str) -> int:
        number = len(self.prs) + 1
        self.prs[number] = {"head": branch, "state": "OPEN", "oid": None, "tip": None}
        return number

    def _bare(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "--git-dir", str(self.bare), *args], capture_output=True, text=True, encoding="utf-8"
        )

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        a = list(args)
        self.calls.append(a)
        ok = lambda out="": subprocess.CompletedProcess(a, 0, out, "")  # noqa: E731
        if a[:2] == ["auth", "status"]:
            return ok()
        if a[:2] == ["pr", "list"]:
            head = a[a.index("--head") + 1]
            state = a[a.index("--state") + 1].upper()
            rows = [
                {"number": n, "url": f"https://github.com/x/y/pull/{n}", "headRefOid": p["tip"]}
                for n, p in self.prs.items()
                if p["head"] == head and p["state"] == state
            ]
            return ok(json.dumps(rows))
        if a[:2] == ["pr", "create"]:
            head = a[a.index("--head") + 1]
            n = self.add_open_pr(head)
            return ok(f"https://github.com/x/y/pull/{n}\n")
        if a[:2] == ["pr", "checks"]:
            # USR-85: merges are gated on CI; the fake repository reports one green check.
            return ok(json.dumps([{"bucket": "pass", "name": "build", "link": "", "workflow": "CI"}]))
        if a[:2] == ["pr", "view"]:
            p = self.prs[int(a[2])]
            return ok(json.dumps({"state": p["state"], "mergeCommit": {"oid": p["oid"]} if p["oid"] else None}))
        if a[:2] == ["pr", "merge"]:
            n = int(a[2])
            p = self.prs[n]
            sha = self._bare("rev-parse", f"refs/heads/{p['head']}").stdout.strip()
            main = self._bare("rev-parse", "refs/heads/main").stdout.strip()
            if self._bare("merge-base", "--is-ancestor", main, sha).returncode != 0:
                return subprocess.CompletedProcess(a, 1, "", "Pull request is not mergeable")
            self._bare("update-ref", "refs/heads/main", sha)
            self._bare("update-ref", "-d", f"refs/heads/{p['head']}")
            p.update(state="MERGED", oid=sha, tip=sha)
            return ok()
        return subprocess.CompletedProcess(a, 1, "", "unsupported")


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path, FakeGh]:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _ident(local)
    (local / "README.md").write_text("base\n", encoding="utf-8")
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "chore: initial")
    _git(local, "remote", "add", "origin", "https://github.com/x/y.git")
    _git(local, "config", f"url.{bare.as_posix()}.insteadOf", "https://github.com/x/y.git")
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return local, bare, FakeGh(bare)


def _other_clone(bare: Path, tmp_path: Path) -> Path:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(other)], check=True, capture_output=True)
    _ident(other)
    return other


def test_happy_path_merges_and_cleans(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    (local / "feature.py").write_text("x = 1\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-70", "Nova feature", cwd=local, gh_runner=gh)
    assert rep.ok and rep.action == "merged", rep.message
    assert rep.pr_url and rep.merge_sha and rep.cleaned
    assert _git(local, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (local / "feature.py").exists()
    assert _git(local, "rev-parse", "main") == _git(local, "rev-parse", "origin/main") == rep.merge_sha
    assert _git(local, "branch", "--list", "ticket/*") == ""
    assert any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_rebases_when_behind_origin(env: tuple[Path, Path, FakeGh], tmp_path: Path) -> None:
    local, bare, gh = env
    other = _other_clone(bare, tmp_path)
    (other / "other.txt").write_text("o\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "feat: other")
    _git(other, "push", "--quiet", "origin", "main")
    (local / "mine.txt").write_text("m\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-71", "Mine", cwd=local, gh_runner=gh)
    assert rep.ok and rep.action == "merged", rep.message
    assert (local / "other.txt").exists() and (local / "mine.txt").exists()


def test_conflict_is_reported_and_tree_left_clean(env: tuple[Path, Path, FakeGh], tmp_path: Path) -> None:
    local, bare, gh = env
    other = _other_clone(bare, tmp_path)
    (other / "README.md").write_text("theirs\n", encoding="utf-8")
    _git(other, "commit", "--quiet", "-am", "feat: theirs")
    _git(other, "push", "--quiet", "origin", "main")
    (local / "README.md").write_text("mine\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-72", "Conflito", cwd=local, gh_runner=gh)
    assert not rep.ok and rep.action == "conflict"
    assert rep.conflict_files == ["README.md"]
    assert _git(local, "status", "--porcelain") == ""
    assert not (local / ".git" / "rebase-merge").exists()
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_existing_pr_is_reused(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    number = gh.add_open_pr("ticket/usr-73")
    (local / "f.txt").write_text("f\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-73", "Reuso", cwd=local, gh_runner=gh)
    assert rep.ok and rep.action == "merged", rep.message
    assert rep.pr_url.endswith(f"/pull/{number}")
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_commercial_acceptance_opens_pr_without_merge(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    (local / "f.txt").write_text("f\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-74", "Comercial", cwd=local, gh_runner=gh, merge=False)
    assert rep.ok and rep.action == "awaiting_commercial_acceptance"
    assert rep.pr_url and not rep.cleaned
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)
    assert _git(local, "branch", "--list", "ticket/usr-74")


def test_cleanup_removes_secondary_worktree(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    wt = local / ".worktrees" / "usr-75"
    _git(local, "worktree", "add", "-q", "-b", "ticket/usr-75", str(wt), "main")
    (wt / "w.txt").write_text("w\n", encoding="utf-8")
    rep = GitAutonomyManager(local).deliver_branch("USR-75", "Worktree", cwd=wt, gh_runner=gh)
    assert rep.ok and rep.action == "merged", rep.message
    assert rep.cleaned
    assert not wt.exists()
    assert _git(local, "branch", "--list", "ticket/usr-75") == ""
    assert (local / "w.txt").exists()  # main checkout fast-forwarded


def test_sweep_removes_only_merged(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    _git(local, "branch", "ticket/merged-one", "main")
    _git(local, "branch", "df/merged-two", "main")
    _git(local, "checkout", "-q", "-b", "ticket/unmerged")
    (local / "u.txt").write_text("u\n", encoding="utf-8")
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "wip")
    _git(local, "checkout", "-q", "main")
    wt_merged = local / ".claude" / "worktrees" / "old"
    _git(local, "worktree", "add", "-q", "-b", "ticket/old-wt", str(wt_merged), "main")
    wt_open = local / ".worktrees" / "open"
    _git(local, "worktree", "add", "-q", str(wt_open), "ticket/unmerged")

    # sweep_grace_s=0: the fixture's worktrees are brand new, and a fresh worktree with no commits of
    # its own is normally spared (it may still be being set up, USR-69).
    rep = GitAutonomyManager(local, sweep_grace_s=0).sweep_stale(gh_runner=gh)
    assert sorted(rep.removed_branches) == ["df/merged-two", "ticket/merged-one", "ticket/old-wt"]
    assert str(wt_merged.resolve()) in rep.removed_worktrees or str(wt_merged) in rep.removed_worktrees
    assert not wt_merged.exists()
    assert wt_open.exists()
    assert _git(local, "branch", "--list", "ticket/unmerged")


def _seed_ticket(local: Path, ticket_id: str) -> None:
    (local / ".factory" / "demands").mkdir(parents=True)
    DemandsStore(local / ".factory" / "demands" / "demands.json").save_ticket(
        UserTicket(id=ticket_id, project_id="darkfac", title="Ticket de teste", status=DeliveryStatus.PLANNED)
    )


def test_complete_ticket_uses_delivery(env: tuple[Path, Path, FakeGh]) -> None:
    local, bare, gh = env
    _seed_ticket(local, "USR-76")
    (local / "c.txt").write_text("c\n", encoding="utf-8")
    rep = GitAutonomyManager(local).complete_ticket("USR-76", cwd=local, gh_runner=gh)
    assert rep.ok and rep.delivery is not None and rep.delivery.action == "merged"
    assert rep.sync_result is not None and rep.sync_result.ok


def test_complete_ticket_rejects_ledger_only_branch(env: tuple[Path, Path, FakeGh]) -> None:
    """A committed ledger-only branch cannot complete or open a PR."""
    local, bare, gh = env
    _seed_ticket(local, "USR-98")
    _git(local, "add", ".factory/demands/demands.json")
    _git(local, "commit", "--quiet", "-m", "ledger only")
    before = (local / ".factory" / "demands" / "demands.json").read_bytes()

    rep = GitAutonomyManager(local).complete_ticket("USR-98", cwd=local, gh_runner=gh)

    assert not rep.ok and rep.delivery is not None and rep.delivery.action == "no_changes"
    assert (local / ".factory" / "demands" / "demands.json").read_bytes() == before
    assert DemandsStore(local / ".factory" / "demands" / "demands.json").get_ticket("USR-98").status == DeliveryStatus.PLANNED
    assert not any(call[:2] == ["pr", "create"] for call in gh.calls)


def test_complete_ticket_commercial_project_awaits(env: tuple[Path, Path, FakeGh], monkeypatch: pytest.MonkeyPatch) -> None:
    local, bare, gh = env
    _seed_ticket(local, "USR-77")
    monkeypatch.setattr(GitAutonomyManager, "_requires_commercial_acceptance", staticmethod(lambda _pid: True))
    (local / "c.txt").write_text("c\n", encoding="utf-8")
    rep = GitAutonomyManager(local).complete_ticket("USR-77", cwd=local, gh_runner=gh)
    assert rep.ok and rep.delivery is not None
    assert rep.delivery.action == "awaiting_commercial_acceptance"
    assert not any(c[:2] == ["pr", "merge"] for c in gh.calls)


def test_delivery_report_model() -> None:
    assert DeliveryReport(ok=True, action="merged").cleaned is False
