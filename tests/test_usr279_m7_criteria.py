from __future__ import annotations

import json
from pathlib import Path

from core.evidence.greenfield_pilot import verify_report


ROOT = Path(__file__).resolve().parents[1]


def test_usr279_criteria_two_and_three_remain_pending_without_m3_m6_evidence() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    assert verify_report(report, root=ROOT, demand=ticket) == []
    evidence = ticket["delivery_evidence"].lower()
    assert "criterio 2: pendente" in evidence
    assert "criterio 3: pendente" in evidence
    assert "oa-043" in evidence and "oa-047" in evidence


def test_verifier_rejects_ticket_criteria_marked_confirmed_without_report_evidence() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    ticket["delivery_evidence"] = "Criterio 2: confirmado; criterio 3: confirmado."
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    problems = verify_report(report, root=ROOT, demand=ticket)
    assert any("criterio 2" in problem.lower() for problem in problems)
    assert any("criterio 3" in problem.lower() for problem in problems)
