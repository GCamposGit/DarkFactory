from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts import pilot_m3_verify

EXTERNAL_ID = "tg-msg-42-101"
ENTRY_TEXT = "pagina inicial com o texto DarkFac pilot e endpoint /healthz que devolve status ok e a versao"
CREATED_AT = "2026-10-20T12:30:00+00:00"


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _ticket(ticket_id: str, project_id: str, *, external_id: str = EXTERNAL_ID) -> dict:
    return {
        "id": ticket_id,
        "project_id": project_id,
        "title": "Telegram: pedido de pagina e healthz",
        "problem_statement": ENTRY_TEXT,
        "core_journey": [f"Telegram external id {external_id}"],
        "tags": ["user-demand", external_id],
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }


def test_ph_entry_returns_zero_and_reports_id_project_time_and_message(tmp_path, capsys, monkeypatch) -> None:
    ledger = tmp_path / ".factory" / "demands" / "demands.json"
    _write_ledger(ledger, [_ticket("PH-001", "pilot-hello")])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "pilot_m3_verify.py",
            "--project",
            "pilot-hello",
            "--demands",
            str(ledger),
            "--projects",
            str(Path(__file__).resolve().parents[1] / ".factory" / "projects.json"),
            "--external-id",
            EXTERNAL_ID,
        ],
    )

    assert pilot_m3_verify.main() == 0
    output = capsys.readouterr().out
    assert "PH-001" in output
    assert "pilot-hello" in output
    assert CREATED_AT in output
    assert EXTERNAL_ID in output
    assert '"has_usr_ticket_for_entry": false' in output
    assert "action=accept, project_id=pilot-hello" in output


def test_missing_entry_returns_two_and_does_not_create_factory_files(tmp_path, capsys, monkeypatch) -> None:
    ledger = tmp_path / ".factory" / "demands" / "demands.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["pilot_m3_verify.py", "--project", "pilot-hello", "--demands", str(ledger)])

    assert pilot_m3_verify.main() == 2
    output = capsys.readouterr().out
    assert "entrada ausente" in output.lower()
    assert not (tmp_path / ".factory").exists()


def test_wrong_project_entry_returns_one_and_names_violation(tmp_path, capsys, monkeypatch) -> None:
    ledger = tmp_path / "demands.json"
    _write_ledger(ledger, [_ticket("USR-900", "darkfac")])
    monkeypatch.setattr(
        "sys.argv",
        ["pilot_m3_verify.py", "--project", "pilot-hello", "--demands", str(ledger), "--external-id", EXTERNAL_ID],
    )

    assert pilot_m3_verify.main() == 1
    output = capsys.readouterr().out
    assert "violacao" in output.lower()
    assert "USR-900" in output
    assert "darkfac" in output


def test_usr_ticket_for_same_entry_is_violation_even_if_ph_ticket_exists(tmp_path) -> None:
    ledger = tmp_path / "demands.json"
    _write_ledger(ledger, [_ticket("PH-001", "pilot-hello"), _ticket("USR-900", "darkfac")])

    report = pilot_m3_verify.verify_m3(
        demands_path=ledger,
        projects_file=None,
        external_id=EXTERNAL_ID,
        demand_text=ENTRY_TEXT,
        project_id="pilot-hello",
    )

    assert report.exit_code == 1
    assert report.has_usr_ticket_for_entry


def test_since_excludes_older_tickets(tmp_path) -> None:
    ledger = tmp_path / "demands.json"
    _write_ledger(ledger, [_ticket("PH-001", "pilot-hello")])

    report = pilot_m3_verify.verify_m3(
        demands_path=ledger,
        projects_file=None,
        external_id=EXTERNAL_ID,
        demand_text=ENTRY_TEXT,
        since=datetime(2026, 10, 21, tzinfo=UTC),
        project_id="pilot-hello",
    )

    assert report.exit_code == 2
    assert report.ticket_id is None


def test_verifier_does_not_modify_existing_factory_tree(tmp_path) -> None:
    factory = tmp_path / ".factory"
    ledger = factory / "demands" / "demands.json"
    _write_ledger(ledger, [])
    before = {path.relative_to(factory): path.read_bytes() for path in factory.rglob("*") if path.is_file()}

    pilot_m3_verify.verify_m3(
        demands_path=ledger,
        projects_file=None,
        external_id=EXTERNAL_ID,
        demand_text=ENTRY_TEXT,
        project_id="pilot-hello",
    )

    after = {path.relative_to(factory): path.read_bytes() for path in factory.rglob("*") if path.is_file()}
    assert after == before


def test_verifier_routes_unknown_project_only_in_temporary_state(tmp_path) -> None:
    factory = tmp_path / ".factory"
    projects = factory / "projects.json"
    factory.mkdir(parents=True)
    projects.write_text("[]", encoding="utf-8")
    ledger = factory / "demands" / "demands.json"
    _write_ledger(ledger, [])
    before = {path.relative_to(factory): path.read_bytes() for path in factory.rglob("*") if path.is_file()}

    report = pilot_m3_verify.verify_m3(
        demands_path=ledger,
        projects_file=projects,
        external_id=None,
        demand_text=ENTRY_TEXT,
        project_id="pilot-hello",
    )

    after = {path.relative_to(factory): path.read_bytes() for path in factory.rglob("*") if path.is_file()}
    assert report.exit_code == 2
    assert "action=onboarding" in report.routing_excerpt
    assert before == after
