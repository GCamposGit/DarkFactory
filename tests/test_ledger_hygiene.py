"""The checked-in backlog must never claim completion without provenance."""

import json
from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.roadmap.models import DeliveryStatus


def test_store_rejects_completed_without_evidence(tmp_path: Path) -> None:
    store = DemandsStore(tmp_path / "demands.json")
    ticket = UserTicket(id="USR-200", title="Ticket sem prova", status=DeliveryStatus.COMPLETED)
    with pytest.raises(ValueError, match="delivery_evidence"):
        store.save_ticket(ticket)
    with pytest.raises(ValueError, match="delivery_evidence"):
        store.save_ticket(ticket.model_copy(update={"delivery_evidence": "  "}))


def test_completed_tickets_have_delivery_evidence() -> None:
    ledger = Path(__file__).resolve().parents[1] / ".factory" / "demands" / "demands.json"
    tickets = json.loads(ledger.read_text(encoding="utf-8"))
    missing = [ticket["id"] for ticket in tickets
               if ticket.get("status") == "completed" and not (ticket.get("delivery_evidence") or "").strip()]
    assert not missing, f"Completed tickets lack delivery evidence: {missing}"
