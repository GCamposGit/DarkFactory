"""USR-314: --resume-delivery finds tickets registered on origin/main after the worktree was cut."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import run_ticket
from core.demands.models import DeliveryStatus, UserTicket
from core.demands.store import DemandsStore

LEDGER = Path(".factory") / "demands" / "demands.json"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _config(repo: Path) -> None:
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@darkfac.local")


def _ticket(ticket_id: str) -> UserTicket:
    return UserTicket(
        id=ticket_id, project_id="darkfac", title="Ticket", problem_statement="p", status=DeliveryStatus.PLANNED
    )


@pytest.fixture()
def repos(tmp_path: Path) -> dict[str, Path]:
    """bare origin + 'seed' clone (plays the main checkout) + a ticket worktree cut before USR-900 exists."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    _config(seed)
    _git(seed, "checkout", "-b", "main")
    store = DemandsStore(seed / LEDGER)
    store.save_ticket(_ticket("USR-001"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "origin", "main")
    wt = tmp_path / "wt"
    _git(seed, "worktree", "add", "-b", "ticket/usr-900", str(wt), "main")
    # Ticket registered on main only after the worktree was created.
    DemandsStore(seed / LEDGER).save_ticket(_ticket("USR-900"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "register USR-900")
    _git(seed, "push", "origin", "main")
    shared = tmp_path / "shared"
    shared.mkdir()
    return {"seed": seed, "wt": wt, "shared": shared}


def test_ticket_only_on_origin_main_is_found_and_rebase_brings_it(repos: dict[str, Path]) -> None:
    wt = repos["wt"]
    assert DemandsStore(wt / LEDGER).get_ticket("USR-900") is None
    seen: dict[str, Any] = {}

    def fake_deliver(git_mgr: Any, ticket: UserTicket, *args: Any, **kwargs: Any) -> int:
        seen["id"] = ticket.id
        ok, _conf, _msg = run_ticket.rebase_on_origin_main(wt)  # lookup happens before this rebase
        seen["rebased"] = ok
        return 0

    with patch("run_ticket.PROJECT_ROOT", repos["shared"]), patch("run_ticket._deliver_worktree", fake_deliver):
        code = run_ticket.resume_delivery("USR-900", worktree_path=wt, skip_validation=True)
    assert code == 0
    assert seen == {"id": "USR-900", "rebased": True}
    assert DemandsStore(wt / LEDGER).get_ticket("USR-900") is not None


def test_ticket_missing_everywhere_lists_three_places(
    repos: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("run_ticket.PROJECT_ROOT", repos["shared"]):
        result = run_ticket.resume_delivery(
            "USR-999", worktree_path=repos["wt"], skip_validation=True, return_result=True
        )
    assert result["exit_code"] == 1
    err = capsys.readouterr().err
    assert "USR-999" in err
    assert "ledger da worktree" in err
    assert "checkout compartilhado" in err
    assert "origin/main" in err
