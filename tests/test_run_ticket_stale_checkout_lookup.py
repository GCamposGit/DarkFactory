"""USR-335: launching a ticket resolves it from origin/main when the shared checkout is stale."""

from __future__ import annotations

import subprocess
from pathlib import Path

import run_ticket
from core.demands.models import DeliveryStatus, UserTicket
from core.demands.store import DemandsStore

LEDGER = Path(".factory") / "demands" / "demands.json"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _ticket(ticket_id: str) -> UserTicket:
    return UserTicket(
        id=ticket_id, project_id="darkfac", title="Ticket", problem_statement="p", status=DeliveryStatus.PLANNED
    )


def _setup(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", str(bare), str(seed))
    for k, v in (("user.name", "T"), ("user.email", "t@darkfac.local")):
        _git(seed, "config", k, v)
    _git(seed, "checkout", "-b", "main")
    DemandsStore(seed / LEDGER).save_ticket(_ticket("USR-001"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "origin", "main")
    shared = tmp_path / "shared"
    _git(tmp_path, "clone", str(bare), str(shared))
    # Ticket registered on main only after the shared checkout was cloned (stale).
    DemandsStore(seed / LEDGER).save_ticket(_ticket("USR-328"))
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "register USR-328")
    _git(seed, "push", "origin", "main")
    return seed, shared


def test_stale_checkout_finds_ticket_only_on_main(tmp_path: Path) -> None:
    _seed, shared = _setup(tmp_path)
    before = (shared / LEDGER).read_bytes()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=shared, capture_output=True, text=True).stdout
    assert DemandsStore(shared / LEDGER).get_ticket("USR-328") is None
    found = run_ticket._ticket_from_origin_main("USR-328", shared)
    assert found is not None and found.id == "USR-328"
    # shared checkout untouched
    assert (shared / LEDGER).read_bytes() == before
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=shared, capture_output=True, text=True).stdout == head


def test_ticket_missing_on_main_stays_refused(tmp_path: Path) -> None:
    _seed, shared = _setup(tmp_path)
    assert run_ticket._ticket_from_origin_main("USR-999", shared) is None
