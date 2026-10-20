from __future__ import annotations

from core.demands.models import DemandInput
from core.demands.service import DemandsService
from core.demands.store import DemandsStore
from scripts.pilot_m3_verify import verify_m3


def test_verifier_reports_correlated_project_ticket_and_missing_entry(tmp_path, monkeypatch) -> None:
    demands = DemandsService(store=DemandsStore(tmp_path / "demands.json"))
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(tmp_path / "demands.json"))
    ticket = demands.create_ticket_from_input(
        DemandInput(
            project_id="pilot-hello",
            title="Telegram: M3 request",
            problem_statement="landing page",
            core_journey="Telegram external id tg-msg-42-101",
            extra_tags=["tg-msg-42-101"],
        ),
        force_heuristic=True,
    )

    report = verify_m3(
        demands_path=tmp_path / "demands.json",
        projects_file=None,
        external_id="tg-msg-42-101",
        demand_text="pagina inicial",
    )

    assert report.ticket_id == ticket.id
    assert report.project_id == "pilot-hello"
    assert report.has_ph_ticket
    assert not report.has_usr_ticket_for_entry
    assert "resolve_demand" in report.routing_excerpt


def test_verifier_keeps_oa_open_when_real_entry_is_absent(tmp_path) -> None:
    report = verify_m3(
        demands_path=tmp_path / "empty.json",
        projects_file=None,
        external_id="tg-msg-missing",
        demand_text="pagina inicial",
    )
    assert report.ticket_id is None
    assert not report.has_ph_ticket
    assert report.oa_047_should_remain_open
