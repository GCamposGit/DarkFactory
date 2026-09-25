"""Tests for ticket DH-15 (USR-46): Demands Ledger Hygiene.

Guarantees:
1. demands.json is valid JSON and parses cleanly.
2. Every item in demands.json conforms to the UserTicket schema.
3. No duplicate ticket IDs exist in demands.json.
4. No duplicate demand titles exist in demands.json.
5. All historical real demands are strictly preserved:
   USR-09, USR-AUTO, USR-12, USR-13, USR-15, USR-16, USR-17, USR-18,
   USR-01, JRV-01, USR-42, USR-43, USR-44, USR-45.
6. The 23 duplicate entries (USR-19 through USR-41) are absent.
7. Current wave tickets (USR-46, USR-47, USR-48, USR-49) are properly registered.
8. DemandsStore successfully loads all tickets from demands.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore

DEMANDS_FILE = Path(__file__).resolve().parents[1] / ".factory" / "demands" / "demands.json"

HISTORICAL_REAL_IDS = [
    "USR-09",
    "USR-AUTO",
    "USR-12",
    "USR-13",
    "USR-15",
    "USR-16",
    "USR-17",
    "USR-18",
    "USR-01",
    "JRV-01",
    "USR-42",
    "USR-43",
    "USR-44",
    "USR-45",
]

CLEANED_DUPLICATE_IDS = [f"USR-{i}" for i in range(19, 42)]

WAVE_3_IDS = ["USR-46", "USR-47", "USR-48", "USR-49"]
WAVE_4_IDS = ["USR-50", "USR-51", "USR-52", "USR-53"]
WAVE_5_IDS = ["USR-54", "USR-55"]
WAVE_6_IDS = ["USR-56"]
WAVE_7_IDS = ["USR-57", "USR-58"]
CURRENT_WAVE_IDS = WAVE_3_IDS + WAVE_4_IDS + WAVE_5_IDS + WAVE_6_IDS + WAVE_7_IDS


def test_demands_json_is_valid_json() -> None:
    """Ensure demands.json exists and is a strictly valid JSON file."""
    assert DEMANDS_FILE.exists(), f"Demands file not found at {DEMANDS_FILE}"
    content = DEMANDS_FILE.read_text(encoding="utf-8")
    assert len(content.strip()) > 0, "Demands file is empty"

    data = json.loads(content)
    assert isinstance(data, list), "Root of demands.json must be a JSON array"
    assert len(data) > 0, "Demands array should not be empty"


def test_demands_json_items_conform_to_schema() -> None:
    """Ensure every entry in demands.json validates against the UserTicket Pydantic model."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    for item in data:
        ticket = UserTicket(**item)
        assert ticket.id, "Ticket ID cannot be empty"
        assert ticket.title, "Ticket title cannot be empty"


def test_no_duplicate_ticket_ids() -> None:
    """Ensure no ticket ID is duplicated in demands.json."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    ids = [item["id"] for item in data]
    duplicates = [tid for tid in ids if ids.count(tid) > 1]
    assert len(ids) == len(set(ids)), f"Duplicate ticket IDs found: {set(duplicates)}"


def test_no_duplicate_demand_titles() -> None:
    """Ensure no demand title is duplicated in demands.json."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    titles = [item["title"] for item in data]
    duplicates = [title for title in titles if titles.count(title) > 1]
    assert len(titles) == len(set(titles)), f"Duplicate demand titles found: {set(duplicates)}"


def test_historical_real_demands_preserved() -> None:
    """Ensure all historical real demands remain present in demands.json."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    present_ids = {item["id"] for item in data}

    for expected_id in HISTORICAL_REAL_IDS:
        assert expected_id in present_ids, f"Historical demand {expected_id} is missing from demands.json"


def test_duplicate_fixture_demands_removed() -> None:
    """Ensure the 23 duplicate entries (USR-19 to USR-41) are completely removed."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    present_ids = {item["id"] for item in data}

    for dup_id in CLEANED_DUPLICATE_IDS:
        assert dup_id not in present_ids, f"Duplicate fixture demand {dup_id} was not removed"


def test_current_wave_tickets_registered() -> None:
    """Ensure tickets USR-46 to USR-53 are registered with correct metadata."""
    data = json.loads(DEMANDS_FILE.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in data}

    for wave_id in CURRENT_WAVE_IDS:
        assert wave_id in by_id, f"Wave ticket {wave_id} is not registered"

    # Wave 3 completed checks
    for wave_id in WAVE_3_IDS:
        assert by_id[wave_id]["status"] == "completed", f"{wave_id} must be completed"

    # USR-50 (DH-16) check
    assert by_id["USR-50"]["status"] == "completed"
    assert "DH-16" in by_id["USR-50"]["title"]


def test_demands_store_loads_cleanly() -> None:
    """Ensure DemandsStore loads all tickets without error and respects ledger count."""
    store = DemandsStore(DEMANDS_FILE)
    tickets = store.list_tickets()
    assert len(tickets) == len(HISTORICAL_REAL_IDS) + len(CURRENT_WAVE_IDS)
    assert len(tickets) == 27
