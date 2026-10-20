from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/evidence/USR-279-M7-falhas.md"
LINK_RE = re.compile(r"(?<![A-Za-z0-9])(?:[0-9a-fA-F]{7,40}|#[0-9]+|(?:USR|PH)-[0-9]+|OA-[0-9]+)(?![A-Za-z0-9])")


def _inventory_rows() -> list[list[str]]:
    lines = INVENTORY.read_text(encoding="utf-8").splitlines()
    table_start = next(i for i, line in enumerate(lines) if line.startswith("| Marco |"))
    rows: list[list[str]] = []
    for line in lines[table_start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def test_every_inventory_row_has_evidence_and_resolution_reference() -> None:
    rows = _inventory_rows()

    assert rows, "the M7 failure inventory must contain at least one finding"
    for row in rows:
        assert len(row) >= 4, f"malformed inventory row: {row}"
        assert row[2].strip(), f"finding has no evidence: {row}"
        assert LINK_RE.search(row[3]), f"finding has no commit, PR, ticket, or owner action: {row}"


def test_ticket_and_owner_action_references_exist_in_factory_records() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    owner_actions = json.loads((ROOT / ".factory/owner_actions/owner_actions.json").read_text(encoding="utf-8"))["actions"]
    demand_ids = {item["id"] for item in demands}
    owner_action_ids = {item["id"] for item in owner_actions}

    for row in _inventory_rows():
        for ticket_id in re.findall(r"\b(?:USR|PH)-[0-9]+\b", row[3]):
            assert ticket_id in demand_ids, f"unknown ticket reference {ticket_id} in row {row}"
        for action_id in re.findall(r"\bOA-[0-9]+\b", row[3]):
            assert action_id in owner_action_ids, f"unknown owner action {action_id} in row {row}"
