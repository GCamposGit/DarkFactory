"""A replayed queue registration reuses the open PR of the same identity (USR-307)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git.autonomy import GitAutonomyManager
from core.roadmap.models import DeliveryStatus
from tests.test_git_delivery import FakeGh, _git, _ident, env  # noqa: F401  (env is a fixture)

LEDGER = ".factory/demands/demands.json"
TITLE = "Varredura repetida"


def _ticket(ticket_id: str, title: str) -> UserTicket:
    return UserTicket(id=ticket_id, project_id="darkfac", title=title, status=DeliveryStatus.PLANNED)


class TitledGh(FakeGh):
    """FakeGh that also tracks PR titles/labels and answers ``pr list --label queue``."""

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        a = list(args)
        if a[:2] == ["pr", "list"] and "--label" in a:
            self.calls.append(a)
            rows = [
                {"number": n, "url": f"https://github.com/x/y/pull/{n}", "title": p.get("title", ""),
                 "headRefName": p["head"]}
                for n, p in self.prs.items() if p["state"] == "OPEN"
            ]
            return subprocess.CompletedProcess(a, 0, json.dumps(rows), "")
        res = super().__call__(args, cwd)
        if a[:2] == ["pr", "create"]:
            self.prs[max(self.prs)]["title"] = a[a.index("--title") + 1]
        elif a[:2] == ["pr", "edit"] and "--title" in a:
            self.prs[int(a[2])]["title"] = a[a.index("--title") + 1]
        return res


def _clone(bare: Path, dest: Path) -> Path:
    subprocess.run(["git", "clone", "--quiet", str(bare), str(dest)], check=True, capture_output=True)
    _ident(dest)
    return dest


def _deliver(repo: Path, branch: str, gh: FakeGh, *, merge: bool):
    _git(repo, "fetch", "--quiet", "origin")
    _git(repo, "checkout", "--quiet", "-B", branch, "origin/main")
    DemandsStore(repo / LEDGER).save_ticket(_ticket("USR-60", TITLE))
    return GitAutonomyManager(repo).deliver_branch(
        "USR-60", TITLE, cwd=repo, gh_runner=gh, kind="queue", merge=merge, supervise_queue=False
    )


def _creates(gh: FakeGh) -> int:
    return sum(1 for c in gh.calls if c[:2] == ["pr", "create"])


def _ref(bare: Path, branch: str) -> str:
    res = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                         capture_output=True, text=True)
    return res.stdout.strip()


@pytest.fixture
def dup_env(env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    local, bare, _ = env
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(tmp_path / "oa" / "owner_actions.json"))
    (local / ".factory" / "demands").mkdir(parents=True)
    DemandsStore(local / LEDGER).save_ticket(_ticket("USR-1", "Ticket ja na main"))
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "chore(backlog): registrar USR-1")
    _git(local, "push", "--quiet", "origin", "main")
    return local, bare, TitledGh(bare), tmp_path


def test_replay_reuses_the_open_pr_instead_of_opening_a_chain(dup_env) -> None:
    local, bare, gh, tmp_path = dup_env
    first = _deliver(local, "reg-a", gh, merge=False)
    assert first.ok and first.pr_url
    second = _deliver(_clone(bare, tmp_path / "replay"), "reg-b", gh, merge=False)
    assert second.ok and second.pr_url == first.pr_url
    assert _creates(gh) == 1
    assert _ref(bare, "reg-b") == ""  # no parallel remote branch was created
    assert _ref(bare, "reg-a") != ""


def test_closed_pr_is_not_reused(dup_env) -> None:
    local, bare, gh, tmp_path = dup_env
    first = _deliver(local, "reg-a", gh, merge=False)
    gh.prs[int(first.pr_url.rsplit("/", 1)[1])]["state"] = "CLOSED"
    second = _deliver(_clone(bare, tmp_path / "replay"), "reg-b", gh, merge=False)
    assert second.ok and second.pr_url != first.pr_url
    assert _creates(gh) == 2


def test_replay_after_main_moved_updates_the_head_and_merges_the_same_pr(dup_env) -> None:
    """Registration conflict: main changed demands.json meanwhile; the reused PR is re-applied and merges."""
    local, bare, gh, tmp_path = dup_env
    first = _deliver(local, "reg-a", gh, merge=False)
    other = _clone(bare, tmp_path / "other")
    DemandsStore(other / LEDGER).save_ticket(_ticket("USR-91", "Registro concorrente"))
    _git(other, "add", "-A")
    _git(other, "commit", "--quiet", "-m", "chore(backlog): registrar USR-91")
    _git(other, "push", "--quiet", "origin", "main")
    second = _deliver(_clone(bare, tmp_path / "replay"), "reg-b", gh, merge=True)
    assert second.ok and second.action == "merged", second.message
    assert second.pr_url == first.pr_url
    assert _creates(gh) == 1
    shown = subprocess.run(["git", "--git-dir", str(bare), "show", f"refs/heads/main:{LEDGER}"],
                           capture_output=True, text=True, encoding="utf-8", check=True).stdout
    assert sorted(r["id"] for r in json.loads(shown)) == ["USR-1", "USR-60", "USR-91"]
