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
    assert ticket["status"] == "planned"


def test_all_three_ticket_criteria_match_report_and_have_real_references_or_blockers() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    owner_actions = json.loads((ROOT / ".factory/owner_actions/owner_actions.json").read_text(encoding="utf-8"))["actions"]
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    assert verify_report(report, root=ROOT, demand=ticket) == []
    owner_action_ids = {action["id"] for action in owner_actions}
    demand_ids = {item["id"] for item in demands}
    for number, criterion in enumerate(ticket["acceptance_criteria"], start=1):
        status = "confirmado" if number == 1 else "pendente"
        assert f"criterio {number}: {status}" in ticket["delivery_evidence"].lower()
        assert f"| {number} —" in report
        if status == "confirmado":
            assert "docs/plans/USR-279-greenfield-pilot.md" in report
        else:
            blockers = [ref for ref in ("OA-043", "OA-047", "USR-279") if ref in ticket["delivery_evidence"]]
            assert blockers
            assert all(ref in owner_action_ids or ref in demand_ids for ref in blockers)
            assert "M3-M6" in ticket["delivery_evidence"]
    assert ticket["status"] == "planned"


def test_d4_retention_action_exists_once_with_steps_and_verification() -> None:
    owner_actions = json.loads((ROOT / ".factory/owner_actions/owner_actions.json").read_text(encoding="utf-8"))["actions"]
    d4_actions = [
        action for action in owner_actions
        if action["kind"] == "action" and "d4:" in action["title"].lower() and "arquivado" in action["title"].lower()
    ]

    assert len(d4_actions) == 1
    action = d4_actions[0]
    assert action["priority"] == "low"
    assert "USR-279" in action["blocks"]
    assert len(action["steps"]) >= 2
    steps_text = " ".join(step["text"] for step in action["steps"])
    assert "Settings > General" in steps_text and "Danger Zone" in steps_text
    assert "Archive this repository" in steps_text and "pilot-hello" in steps_text
    assert "isArchived" in action["verify"] and "true" in action["verify"]


def test_verifier_rejects_ticket_criteria_marked_confirmed_without_report_evidence() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    ticket["delivery_evidence"] = "Criterio 2: confirmado; criterio 3: confirmado."
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    problems = verify_report(report, root=ROOT, demand=ticket)
    assert any("criterio 2" in problem.lower() for problem in problems)
    assert any("criterio 3" in problem.lower() for problem in problems)


def test_verifier_rejects_criterion_one_claim_not_confirmed_by_report() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    ticket["delivery_evidence"] = ticket["delivery_evidence"].replace(
        "Criterio 1: confirmado", "Criterio 1: pendente"
    )
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    problems = verify_report(report, root=ROOT, demand=ticket)
    assert any("criterio 1" in problem.lower() for problem in problems)


def test_verifier_rejects_pending_ticket_criterion_without_existing_blocker() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    ticket["delivery_evidence"] = (
        "Criterio 1: confirmado por docs/plans/USR-279-greenfield-pilot.md. "
        "Criterio 2: pendente; bloqueador OA-999. "
        "Criterio 3: pendente; bloqueador USR-999."
    )
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    problems = verify_report(report, root=ROOT, demand=ticket)

    assert any("bloqueador" in problem.lower() and "criterio 2" in problem.lower() for problem in problems)
    assert any("bloqueador" in problem.lower() and "criterio 3" in problem.lower() for problem in problems)


def test_verifier_requires_each_ticket_criterion_to_cite_the_m7_report() -> None:
    demands = json.loads((ROOT / ".factory/demands/demands.json").read_text(encoding="utf-8"))
    ticket = next(item for item in demands if item["id"] == "USR-279")
    ticket["delivery_evidence"] = ticket["delivery_evidence"].replace(
        "docs/evidence/USR-279-M7.md", "docs/plans/USR-279-greenfield-pilot.md", 1
    )
    report = (ROOT / "docs/evidence/USR-279-M7.md").read_text(encoding="utf-8")

    problems = verify_report(report, root=ROOT, demand=ticket)

    assert any("criterio 1" in problem.lower() and "m7" in problem.lower() for problem in problems)
