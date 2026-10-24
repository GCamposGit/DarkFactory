import json

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.owner_actions.remote_ledger import RemoteLedger


def _ticket(ticket_id: str, title: str) -> dict:
    return UserTicket(id=ticket_id, title=title).model_dump(mode="json")


def test_remote_demands_ledger_refreshes_after_ttl_and_is_shared_with_roadmap(tmp_path):
    from pathlib import Path

    from core.roadmap.service import build_repository_roadmap_service

    local = tmp_path / ".factory" / "demands" / "demands.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps([_ticket("USR-10", "Local demand")]), encoding="utf-8")
    remote = {"items": [_ticket("USR-11", "Remote demand one")]}
    now = {"value": 0.0}
    calls = []

    def fetch():
        calls.append(True)
        return remote["items"]

    ledger = RemoteLedger("org/repo", path=".factory/demands/demands.json", ttl=10,
                          fetch=fetch, clock=lambda: now["value"])
    store = DemandsStore(local, remote_ledger=ledger)
    roadmap = build_repository_roadmap_service(
        Path.cwd(), demands_path=local, demands_payload_provider=ledger.get,
        include_demands=True,
    )

    assert [ticket.id for ticket in store.list_tickets()] == ["USR-11"]
    assert any(item.id == "USR-11" for item in roadmap.get_snapshot("darkfac").items)
    remote["items"] = [_ticket("USR-12", "Remote demand two")]
    assert [ticket.id for ticket in store.list_tickets()] == ["USR-11"]
    assert any(item.id == "USR-11" for item in roadmap.get_snapshot("darkfac").items)
    now["value"] = 11
    assert [ticket.id for ticket in store.list_tickets()] == ["USR-12"]
    assert any(item.id == "USR-12" for item in roadmap.get_snapshot("darkfac").items)
    assert len(calls) == 2


def test_remote_demands_failure_uses_local_and_local_writes_remain_visible(tmp_path):
    local = tmp_path / "demands.json"
    local.write_text(json.dumps([_ticket("USR-20", "Local demand")]), encoding="utf-8")

    def fail():
        raise OSError("offline")

    store = DemandsStore(local, remote_ledger=RemoteLedger(
        "org/repo", path=".factory/demands/demands.json", fetch=fail,
    ))
    assert [ticket.id for ticket in store.list_tickets()] == ["USR-20"]

    created = UserTicket(id="USR-21", title="Created locally")
    store.save_ticket(created)
    assert [ticket.id for ticket in store.list_tickets()] == ["USR-20", "USR-21"]
    assert {ticket.id for ticket in map(UserTicket.model_validate, json.loads(local.read_text(encoding="utf-8")))} == {
        "USR-20", "USR-21"
    }


def test_remote_ledger_defaults_to_owner_actions_path_and_accepts_demands_path():
    default = RemoteLedger("org/repo")
    demands = RemoteLedger("org/repo", path=".factory/demands/demands.json")

    assert default.path == ".factory/owner_actions/owner_actions.json"
    assert demands.path == ".factory/demands/demands.json"
