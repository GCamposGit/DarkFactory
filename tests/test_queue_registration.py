"""Queue-registration PRs must reach main under concurrency, and must never rot in silence (USR-238)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.git import queue_registration
from core.git.autonomy import GitAutonomyManager
from core.git.ticket_workspace import shared_checkout_dirty_paths
from core.owner_actions.models import ActionStatus
from core.owner_actions.store import OwnerActionStore
from core.roadmap.models import DeliveryStatus
from tests.test_git_delivery import FakeGh, _git, _ident, env  # noqa: F401  (env is a fixture)

LEDGER = ".factory/demands/demands.json"
OA_LEDGER = ".factory/owner_actions/owner_actions.json"


def _ticket(ticket_id: str, title: str) -> UserTicket:
    return UserTicket(id=ticket_id, project_id="darkfac", title=title, status=DeliveryStatus.PLANNED)


def _main_ids(bare: Path) -> list[str]:
    shown = subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"refs/heads/main:{LEDGER}"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return [row["id"] for row in json.loads(shown)]


def _main_owner_actions(bare: Path) -> list[dict]:
    """The owner-action backlog as committed on ``bare``'s main, or ``[]`` if it was never written."""
    shown = subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"refs/heads/main:{OA_LEDGER}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if shown.returncode != 0:
        return []
    return json.loads(shown.stdout).get("actions", [])


def _control_actions() -> list[dict]:
    """The owner-action backlog as held by the control store (USR-343 S4b: alerts no longer open PRs)."""
    return [a.to_record() for a in OwnerActionStore(control=True).list_actions()]


class MovingMainGh(FakeGh):
    """FakeGh where a concurrent registration lands on main right before the first N merge attempts.

    This is what the 15 minute Windows CI window does in production: the gate turns green, but by
    then another registration changed demands.json on main and GitHub reports the PR as not mergeable.
    """

    def __init__(self, bare: Path, other: Path, moves: int) -> None:
        super().__init__(bare)
        self.other = other
        self.moves = moves
        self.moved = 0

    def _land_other_registration(self) -> None:
        self.moved += 1
        _git(self.other, "fetch", "--quiet", "origin")
        _git(self.other, "reset", "--hard", "--quiet", "origin/main")
        DemandsStore(self.other / LEDGER).save_ticket(_ticket(f"USR-9{self.moved}", f"Registro concorrente {self.moved}"))
        _git(self.other, "add", "-A")
        _git(self.other, "commit", "--quiet", "-m", f"chore(backlog): registrar USR-9{self.moved}")
        _git(self.other, "push", "--quiet", "origin", "main")

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        a = list(args)
        if a[:2] == ["pr", "list"] and "--label" in a:  # the stalled-PR watchdog
            self.calls.append(a)
            return subprocess.CompletedProcess(a, 0, "[]", "")
        if a[:2] == ["pr", "merge"] and self.moved < self.moves:
            self._land_other_registration()
        return super().__call__(args, cwd)


@pytest.fixture
def queue_env(env: tuple[Path, Path, FakeGh], tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    local, bare, _ = env
    monkeypatch.setenv("DARKFAC_OWNER_ACTIONS_PATH", str(tmp_path / "oa" / "owner_actions.json"))
    (local / ".factory" / "demands").mkdir(parents=True)
    DemandsStore(local / LEDGER).save_ticket(_ticket("USR-1", "Ticket ja na main"))
    _git(local, "add", "-A")
    _git(local, "commit", "--quiet", "-m", "chore(backlog): registrar USR-1")
    _git(local, "push", "--quiet", "origin", "main")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(other)], check=True, capture_output=True)
    _ident(other)
    return local, bare, other


def _register(local: Path, gh: FakeGh):
    DemandsStore(local / LEDGER).save_ticket(_ticket("USR-50", "Meu registro de fila"))
    return GitAutonomyManager(local).deliver_branch("USR-50", "Meu registro de fila", cwd=local, gh_runner=gh, kind="queue")


def test_concurrent_registration_survives_main_moving_during_every_ci_window(queue_env) -> None:
    """Two registrations land on main while ours waits for CI: ours is re-applied each time and still merges."""
    local, bare, other = queue_env
    gh = MovingMainGh(bare, other, moves=2)
    rep = _register(local, gh)
    assert rep.ok and rep.action == "merged", rep.message
    assert sorted(_main_ids(bare)) == ["USR-1", "USR-50", "USR-91", "USR-92"]
    assert sum(1 for call in gh.calls if call[:2] == ["pr", "merge"]) == 3
    assert _main_owner_actions(bare) == []


def test_registration_that_never_merges_leaves_a_visible_alert(queue_env) -> None:
    """USR-343 S4b: the alert goes to the control store (no PR, no push) and the shared checkout stays clean."""
    local, bare, other = queue_env
    # Exactly enough moves to defeat the ticket's own merge + 3 re-sync attempts (USR-238); any
    # further "pr merge" call -- namely the alert's own tiny single-file PR -- lands cleanly, so
    # the test isolates "the ticket is stuck" from "nothing else can ever merge either".
    gh = MovingMainGh(bare, other, moves=4)
    rep = _register(local, gh)
    assert not rep.ok and rep.pr_url
    assert _main_owner_actions(bare) == []
    alerts = [a for a in _control_actions() if a["id"].startswith("OA-QR-")]
    assert len(alerts) == 1
    assert alerts[0]["id"] == f"OA-QR-{rep.pr_url.rsplit('/', 1)[1]}"
    assert alerts[0]["status"] == "open"
    assert "USR-50" in alerts[0]["title"] and rep.pr_url in alerts[0]["why"] and "not mergeable" in alerts[0]["why"]
    assert alerts[0]["blocks"] == ["USR-50"]
    # the ticket row is still intact on the PR branch, nothing was lost or half-merged
    assert "USR-50" not in _main_ids(bare)
    # raising the alert never left the shared checkout dirty (the bug behind USR-246): the next
    # `run_ticket <ID>` run from `local` would not be refused with SharedCheckoutDirty
    assert shared_checkout_dirty_paths(local) == []


class ListGh:
    """Runner answering only `gh pr list --label queue`."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.ok = True

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        a = list(args)
        assert a[:2] == ["pr", "list"] and "queue" in a
        return subprocess.CompletedProcess(a, 0 if self.ok else 1, json.dumps(self.rows), "")


class QueueWatchGh(FakeGh):
    """FakeGh (full PR lifecycle against ``bare``) plus a controllable watchdog `pr list` response.

    Used to exercise the real USR-246 path end to end: the watchdog's own alert/resolution commit
    needs ``pr create``/``pr checks``/``pr merge`` to work exactly like a ticket's own registration,
    which plain ``ListGh`` (ignorant of everything but ``pr list``) cannot provide.
    """

    def __init__(self, bare: Path, rows: list[dict]) -> None:
        super().__init__(bare)
        self.rows = rows

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        a = list(args)
        if a[:2] == ["pr", "list"] and "--label" in a:
            self.calls.append(a)
            return subprocess.CompletedProcess(a, 0, json.dumps(self.rows), "")
        return super().__call__(args, cwd)


def _row(number: int, created: str, mergeable: str = "CONFLICTING", state: str = "DIRTY") -> dict:
    return {
        "number": number, "url": f"https://github.com/x/y/pull/{number}", "createdAt": created,
        "title": f"chore(backlog): registrar USR-{number} - Titulo", "headRefName": f"ticket/queue-{number}",
        "mergeable": mergeable, "mergeStateStatus": state,
    }


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_watchdog_writes_alert_to_control_store_then_closes_it_once_the_pr_merges(queue_env) -> None:
    """USR-246: without an injected store, raise and close each commit via an isolated worktree.

    No `--allow-dirty-shared-checkout` escape hatch is needed afterwards: `local` -- the shared
    checkout the watchdog ran from -- is never left dirty by either the alert or its resolution.
    """
    local, bare, _other = queue_env
    old_pr = _row(99, "2026-10-11T11:00:00Z")  # 60 min old: past the 30-minute stall threshold
    gh = QueueWatchGh(bare, [old_pr])

    raised = queue_registration.check_stalled_queue_prs(local, gh, now=NOW)
    assert [a.id for a in raised] == ["OA-QR-99"]
    assert any(a["id"] == "OA-QR-99" and a["status"] == "open" for a in _control_actions())
    assert _main_owner_actions(bare) == []  # no push, no PR for the alert
    assert shared_checkout_dirty_paths(local) == []

    gh.rows = []  # PR #99 merged (or closed) since the last scan
    closed = queue_registration.check_stalled_queue_prs(local, gh, now=NOW)
    assert closed == []
    assert any(a["id"] == "OA-QR-99" and a["status"] == "done" for a in _control_actions())
    assert shared_checkout_dirty_paths(local) == []


def test_watchdog_alerts_only_prs_older_than_thirty_minutes(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "oa.json")
    gh = ListGh([_row(10, "2026-10-11T11:20:00Z"), _row(11, "2026-10-11T11:45:00Z")])  # 40 min and 15 min old
    raised = queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store)
    assert [a.id for a in raised] == ["OA-QR-10"]
    action = store.get("OA-QR-10")
    assert action is not None and action.status is ActionStatus.OPEN
    assert "conflita com a main" in action.why and "40 min" in action.title
    assert action.blocks == ["USR-10"]
    assert store.get("OA-QR-11") is None


def test_watchdog_is_idempotent_and_closes_alerts_of_prs_that_are_gone(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "oa.json")
    gh = ListGh([_row(10, "2026-10-11T11:00:00Z")])
    queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store)
    queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store)
    assert len(store.list_actions()) == 1
    gh.rows = []  # merged or closed
    queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store)
    assert store.get("OA-QR-10").status is ActionStatus.DONE


def test_watchdog_does_nothing_when_gh_fails(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "oa.json")
    gh = ListGh([_row(10, "2026-10-11T11:00:00Z")])
    queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store)
    gh.ok = False
    assert queue_registration.check_stalled_queue_prs(tmp_path, gh, now=NOW, store=store) == []
    assert store.get("OA-QR-10").status is ActionStatus.OPEN


def test_alert_never_stores_credential_shaped_text(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "oa.json")
    leaked = "gh" + "p_" + "A" * 36
    saved = queue_registration.alert_queue_pr_stuck(
        "USR-5", "https://github.com/x/y/pull/5", f"token {leaked} rejected", store=store
    )
    assert saved is not None and leaked not in saved.why
