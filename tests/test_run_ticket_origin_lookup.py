"""USR-314: --resume-delivery finds tickets registered on origin/main after the worktree was cut."""

from __future__ import annotations

import json
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


def _isolate_queue(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKFAC_DEMANDS_PATH", raising=False)
    from core.demands import queue_store

    queue_store._store.cache_clear()


def test_ticket_only_on_origin_main_is_found_and_rebase_brings_it(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = repos["wt"]
    _isolate_queue(monkeypatch, repos["shared"] / "state")
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


def _forbid_allocators() -> tuple[Any, Any]:
    """The lookup must not allocate an id or call the correction agent."""
    return (
        patch.object(DemandsStore, "next_ticket_id", side_effect=AssertionError("next_ticket_id")),
        patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")),
    )


def test_worktree_file_wins_over_the_queue(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worktree ledger is first: an open file row is not replaced by the queue."""
    wt = repos["wt"]
    _isolate_queue(monkeypatch, repos["shared"] / "state")
    from core.demands import queue_store

    ledger = wt / LEDGER
    before = ledger.read_bytes()
    DemandsStore(ledger).save_ticket(UserTicket(
        id="USR-902", project_id="darkfac", title="No arquivo",
        problem_statement="p", status=DeliveryStatus.PLANNED,
    ))
    written = ledger.read_bytes()
    assert written != before
    queue_store.register_ticket(UserTicket(
        id="USR-902", project_id="darkfac", title="Na fila",
        problem_statement="p", status=DeliveryStatus.IMPLEMENTING,
    ))
    seen: dict[str, str] = {}

    def fake_deliver(_git_mgr: Any, ticket: UserTicket, *_args: Any, **_kwargs: Any) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    forbid_id, forbid_agent = _forbid_allocators()
    with patch("run_ticket.PROJECT_ROOT", repos["shared"]), \
            patch("run_ticket._deliver_worktree", fake_deliver), forbid_id, forbid_agent:
        code = run_ticket.resume_delivery("USR-902", worktree_path=wt, skip_validation=True)

    assert code == 0
    assert seen == {"id": "USR-902", "title": "No arquivo"}
    assert ledger.read_bytes() == written
    queued = queue_store.get_ticket("USR-902")
    assert queued is not None and queued.title == "Na fila"


def test_queue_wins_over_the_shared_checkout_file(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable queue is materialized before the shared checkout file is read."""
    shared = repos["shared"]
    _isolate_queue(monkeypatch, shared / "state")
    from core.demands import queue_store

    shared_ledger = shared / LEDGER
    shared_ledger.parent.mkdir(parents=True)
    DemandsStore(shared_ledger).save_ticket(UserTicket(
        id="USR-903", project_id="darkfac", title="No checkout",
        problem_statement="p", status=DeliveryStatus.PLANNED,
    ))
    shared_before = shared_ledger.read_bytes()
    queue_store.register_ticket(UserTicket(
        id="USR-903", project_id="darkfac", title="Na fila",
        problem_statement="p", status=DeliveryStatus.IMPLEMENTING,
    ))
    wt = repos["wt"]
    seen: dict[str, str] = {}

    def fake_deliver(_git_mgr: Any, ticket: UserTicket, *_args: Any, **_kwargs: Any) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    forbid_id, forbid_agent = _forbid_allocators()
    with patch("run_ticket.PROJECT_ROOT", shared), \
            patch("run_ticket._deliver_worktree", fake_deliver), forbid_id, forbid_agent:
        code = run_ticket.resume_delivery("USR-903", worktree_path=wt, skip_validation=True)

    assert code == 0
    assert seen == {"id": "USR-903", "title": "Na fila"}
    ids = [row["id"] for row in json.loads((wt / LEDGER).read_text(encoding="utf-8"))]
    assert ids.count("USR-903") == 1
    assert shared_ledger.read_bytes() == shared_before


def test_ticket_only_in_shared_checkout_is_found(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ticket absent from the worktree, the queue and origin/main is still found in the shared file."""
    shared = repos["shared"]
    _isolate_queue(monkeypatch, shared / "state")
    ledger = shared / LEDGER
    ledger.parent.mkdir(parents=True)
    DemandsStore(ledger).save_ticket(UserTicket(
        id="USR-901", project_id="darkfac", title="So no checkout",
        problem_statement="p", status=DeliveryStatus.PLANNED,
    ))
    wt = repos["wt"]
    before = (wt / LEDGER).read_bytes()
    assert "USR-901" not in before.decode("utf-8")
    seen: dict[str, str] = {}

    def fake_deliver(_git_mgr: Any, ticket: UserTicket, *_args: Any, **_kwargs: Any) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    with patch("run_ticket.PROJECT_ROOT", shared), \
            patch("run_ticket._deliver_worktree", fake_deliver), \
            patch.object(DemandsStore, "next_ticket_id", side_effect=AssertionError("next_ticket_id")), \
            patch("run_ticket._invoke_fix_agent", side_effect=AssertionError("_invoke_fix_agent")):
        code = run_ticket.resume_delivery("USR-901", worktree_path=wt, skip_validation=True)

    assert code == 0
    assert seen == {"id": "USR-901", "title": "So no checkout"}
    assert (wt / LEDGER).read_bytes() == before
    from core.demands import queue_store

    assert queue_store.get_ticket("USR-901") is None


def test_ticket_missing_everywhere_is_ticket_absent(
    repos: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent from the worktree, the queue, the shared checkout and origin/main: no new ticket."""
    state = repos["shared"] / "state"
    _isolate_queue(monkeypatch, state)
    from core.demands import queue_store

    before = (repos["wt"] / LEDGER).read_bytes()
    with patch("run_ticket.PROJECT_ROOT", repos["shared"]):
        result = run_ticket.resume_delivery(
            "USR-999", worktree_path=repos["wt"], skip_validation=True, return_result=True
        )
    assert result["exit_code"] == 1
    assert result["ok"] is False
    assert result["cause"] == "ticket_absent"
    assert result["error"] == "Ticket USR-999 not found in demands store"
    err = capsys.readouterr().err
    assert "USR-999" in err
    assert "ledger da worktree" in err
    assert "control store" in err
    assert "checkout compartilhado" in err
    assert "origin/main" in err
    assert (repos["wt"] / LEDGER).read_bytes() == before
    assert "USR-999" not in (repos["wt"] / LEDGER).read_text(encoding="utf-8")
    assert queue_store.get_ticket("USR-999") is None
    assert not (state / "control.db").exists()


def test_missing_worktree_ledger_stays_missing_when_only_the_shared_file_has_the_id(
    repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared-checkout hit does not create the worktree ledger or a queue row."""
    shared = repos["shared"]
    state = shared / "state"
    _isolate_queue(monkeypatch, state)
    from core.demands import queue_store

    ledger = shared / LEDGER
    ledger.parent.mkdir(parents=True)
    DemandsStore(ledger).save_ticket(UserTicket(
        id="USR-904", project_id="darkfac", title="So no checkout",
        problem_statement="p", status=DeliveryStatus.PLANNED,
    ))
    wt = repos["wt"]
    wt_ledger = wt / LEDGER
    wt_ledger.unlink()
    seen: dict[str, str] = {}

    def fake_deliver(_git_mgr: Any, ticket: UserTicket, *_args: Any, **_kwargs: Any) -> int:
        seen["id"] = ticket.id
        seen["title"] = ticket.title
        return 0

    forbid_id, forbid_agent = _forbid_allocators()
    with patch("run_ticket.PROJECT_ROOT", shared), \
            patch("run_ticket._deliver_worktree", fake_deliver), forbid_id, forbid_agent:
        code = run_ticket.resume_delivery("USR-904", worktree_path=wt, skip_validation=True)

    assert code == 0
    assert seen == {"id": "USR-904", "title": "So no checkout"}
    assert not wt_ledger.exists()
    assert queue_store.get_ticket("USR-904") is None


def test_missing_everywhere_does_not_create_the_worktree_ledger(
    repos: dict[str, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ledger file and no control store are created when the id is absent from all four places."""
    state = repos["shared"] / "state"
    _isolate_queue(monkeypatch, state)
    wt_ledger = repos["wt"] / LEDGER
    wt_ledger.unlink()
    forbid_id, forbid_agent = _forbid_allocators()
    with patch("run_ticket.PROJECT_ROOT", repos["shared"]), forbid_id, forbid_agent:
        result = run_ticket.resume_delivery(
            "USR-998", worktree_path=repos["wt"], skip_validation=True, return_result=True
        )
    assert result["exit_code"] == 1
    assert result["ok"] is False
    assert result["cause"] == "ticket_absent"
    assert result["error"] == "Ticket USR-998 not found in demands store"
    err = capsys.readouterr().err
    assert "ledger da worktree" in err
    assert "control store" in err
    assert "checkout compartilhado" in err
    assert "origin/main" in err
    assert not wt_ledger.exists()
    assert not (state / "control.db").exists()
