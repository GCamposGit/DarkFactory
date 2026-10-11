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
from core.owner_actions.models import ActionStatus
from core.owner_actions.store import OwnerActionStore
from core.roadmap.models import DeliveryStatus
from tests.test_git_delivery import FakeGh, _git, _ident, env  # noqa: F401  (env is a fixture)

LEDGER = ".factory/demands/demands.json"


def _ticket(ticket_id: str, title: str) -> UserTicket:
    return UserTicket(id=ticket_id, project_id="darkfac", title=title, status=DeliveryStatus.PLANNED)


def _main_ids(bare: Path) -> list[str]:
    shown = subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"refs/heads/main:{LEDGER}"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    return [row["id"] for row in json.loads(shown)]


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
    assert OwnerActionStore().list_actions(include_done=False) == []


def test_registration_that_never_merges_leaves_a_visible_alert(queue_env) -> None:
    local, bare, other = queue_env
    gh = MovingMainGh(bare, other, moves=10)
    rep = _register(local, gh)
    assert not rep.ok and rep.pr_url
    alerts = [a for a in OwnerActionStore().list_actions(include_done=False) if a.id.startswith("OA-QR-")]
    assert len(alerts) == 1
    assert alerts[0].id == f"OA-QR-{rep.pr_url.rsplit('/', 1)[1]}"
    assert "USR-50" in alerts[0].title and rep.pr_url in alerts[0].why and "not mergeable" in alerts[0].why
    assert alerts[0].blocks == ["USR-50"]
    # the ticket row is still intact on the PR branch, nothing was lost or half-merged
    assert "USR-50" not in _main_ids(bare)


class ListGh:
    """Runner answering only `gh pr list --label queue`."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.ok = True

    def __call__(self, args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        a = list(args)
        assert a[:2] == ["pr", "list"] and "queue" in a
        return subprocess.CompletedProcess(a, 0 if self.ok else 1, json.dumps(self.rows), "")


def _row(number: int, created: str, mergeable: str = "CONFLICTING", state: str = "DIRTY") -> dict:
    return {
        "number": number, "url": f"https://github.com/x/y/pull/{number}", "createdAt": created,
        "title": f"chore(backlog): registrar USR-{number} - Titulo", "headRefName": f"ticket/queue-{number}",
        "mergeable": mergeable, "mergeStateStatus": state,
    }


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


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
