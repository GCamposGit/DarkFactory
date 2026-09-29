"""Bridge from owner demands (Telegram /demand, /linha, DarkHub action) into the production line.

No network, no real Postgres, no real Telegram: the control store is a temp-file
`SQLiteControlStore`, and Telegram updates are fed straight to `TelegramGateway`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.line import owner_intake
from core.roadmap.models import DeliveryStatus
from core.workflow.control_contracts import StoreUnavailableError
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*owner_intake.DATABASE_URL_ENVS, owner_intake.AUTOSUBMIT_ENV, owner_intake.PROJECTS_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


@pytest.fixture
def demands(tmp_path: Path) -> DemandsStore:
    return DemandsStore(path=tmp_path / "demands.json")


def _ticket(demands: DemandsStore, ticket_id: str = "USR-62", project_id: str = "darkfac", **extra) -> UserTicket:
    return demands.save_ticket(
        UserTicket(
            id=ticket_id,
            project_id=project_id,
            title="Menu vertical esquerdo do DarkHub",
            problem_statement="Adicionar menu vertical a esquerda no DarkHub",
            core_journey=["Owner abre o DarkHub e ve o menu"],
            acceptance_criteria=["Menu visivel em desktop"],
            **extra,
        )
    )


def _run_count(store: SQLiteControlStore) -> int:
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM runs")
        return cur.fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------
# submit_ticket_to_line
# --------------------------------------------------------------------------


def test_existing_ticket_becomes_a_line_run_with_an_initial_grill_job(store, demands) -> None:
    _ticket(demands)

    result = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert result.ok is True and result.replayed is False
    assert result.run_id and result.demand_id
    assert _run_count(store) == 1
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT project_id FROM runs WHERE run_id = ?", (result.run_id,))
        assert cur.fetchone()[0] == "darkfac"
        cur.execute("SELECT stage, status FROM jobs WHERE run_id = ?", (result.run_id,))
        assert cur.fetchall()[0]["stage"] == "grill"
        cur.execute("SELECT channel, external_id FROM intake_commands WHERE run_id = ?", (result.run_id,))
        row = cur.fetchone()
        assert (row["channel"], row["external_id"]) == ("owner", "ticket:USR-62")
    finally:
        conn.close()
    # No duplicate `dem-*` ticket is projected back into demands.json.
    assert [t.id for t in demands.list_tickets()] == ["USR-62"]
    assert demands.get_ticket("USR-62").status == DeliveryStatus.IMPLEMENTING


def test_resubmitting_the_same_ticket_is_idempotent(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    second = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert second.ok is True and second.replayed is True
    assert second.run_id == first.run_id
    assert _run_count(store) == 1


def test_editing_a_submitted_ticket_is_refused_not_duplicated(store, demands) -> None:
    ticket = _ticket(demands)
    owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    demands.save_ticket(ticket.model_copy(update={"problem_statement": "Outro problema totalmente diferente"}))

    result = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert result.ok is False and result.replayed is True
    assert "outro conteudo" in result.message
    assert _run_count(store) == 1


def test_unknown_completed_and_out_of_scope_tickets_are_refused(store, demands) -> None:
    assert owner_intake.submit_ticket_to_line("USR-404", demands_store=demands, store=store).ok is False

    _ticket(demands, "USR-70", status=DeliveryStatus.COMPLETED)
    assert "nada a enviar" in owner_intake.submit_ticket_to_line("USR-70", demands_store=demands, store=store).message

    _ticket(demands, "USR-71", project_id="site-ggcampos")
    refused = owner_intake.submit_ticket_to_line("USR-71", demands_store=demands, store=store)
    assert refused.ok is False and "nao esta habilitado" in refused.message
    assert _run_count(store) == 0


def test_store_failure_is_reported_never_raised(demands) -> None:
    _ticket(demands)

    class BrokenStore:
        def accept(self, command, now):
            raise StoreUnavailableError("db down")

    result = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=BrokenStore())
    assert result.ok is False and "db down" in result.message
    assert demands.get_ticket("USR-62").status == DeliveryStatus.PLANNED  # not marked implementing


def test_configured_but_unreachable_postgres_fails_closed_never_mock(monkeypatch) -> None:
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://u:p@127.0.0.1:1/none")
    with pytest.raises(StoreUnavailableError):
        owner_intake.open_line_store()


def test_line_store_falls_back_when_no_database_url(store) -> None:
    assert owner_intake.open_line_store(fallback=store) is store


def test_database_url_precedence_and_autosubmit_switch(monkeypatch) -> None:
    assert owner_intake.line_database_url() is None and owner_intake.autosubmit_enabled() is False
    monkeypatch.setenv("DARKHUB_CONTROL_DATABASE_URL", "postgresql://control")
    assert owner_intake.line_database_url() == "postgresql://control"
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://hf02")
    assert owner_intake.line_database_url() == "postgresql://hf02"
    monkeypatch.setenv("DARKHUB_LINE_DATABASE_URL", "postgresql://line")
    assert owner_intake.line_database_url() == "postgresql://line"
    assert owner_intake.autosubmit_enabled() is True  # URL present -> on by default
    monkeypatch.setenv(owner_intake.AUTOSUBMIT_ENV, "false")
    assert owner_intake.autosubmit_enabled() is False  # explicit kill switch wins
    monkeypatch.delenv("DARKHUB_LINE_DATABASE_URL")
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL")
    monkeypatch.delenv("DARKHUB_CONTROL_DATABASE_URL")
    monkeypatch.setenv(owner_intake.AUTOSUBMIT_ENV, "true")
    assert owner_intake.autosubmit_enabled() is True  # explicit opt-in without URL


# --------------------------------------------------------------------------
# Hub: HubService + endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def hub(tmp_path: Path, store: SQLiteControlStore):
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=store)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        yield service, TestClient(app)
    finally:
        app.dependency_overrides.pop(get_hub_service, None)


def _hub_ticket(service: HubService, ticket_id: str = "USR-62") -> None:
    service.demands_store.save_ticket(
        UserTicket(id=ticket_id, title="Menu vertical esquerdo", problem_statement="Menu vertical no DarkHub")
    )


def test_hub_endpoint_pushes_an_existing_ticket_into_the_line(hub, store) -> None:
    service, client = hub
    _hub_ticket(service)

    resp = client.post("/api/demands/tickets/USR-62/line")

    assert resp.status_code == status.HTTP_202_ACCEPTED
    body = resp.json()
    assert body["ok"] is True and body["run_id"] and body["ticket_id"] == "USR-62"
    assert _run_count(store) == 1
    again = client.post("/api/demands/tickets/USR-62/line")
    assert again.status_code == status.HTTP_202_ACCEPTED and again.json()["run_id"] == body["run_id"]
    assert _run_count(store) == 1


def test_hub_endpoint_error_codes(hub) -> None:
    service, client = hub
    assert client.post("/api/demands/tickets/USR-404/line").status_code == status.HTTP_404_NOT_FOUND
    service.demands_store.save_ticket(
        UserTicket(id="USR-71", project_id="site-ggcampos", title="Outro projeto", problem_statement="x")
    )
    assert client.post("/api/demands/tickets/USR-71/line").status_code == 422


# --------------------------------------------------------------------------
# Telegram: /linha and /demand
# --------------------------------------------------------------------------


def _gateway(tmp_path: Path, service: HubService) -> TelegramGateway:
    config = TelegramConfig(bot_token="123:abc", authorized_user_ids=[42], authorized_chat_ids=[42], role="owner")
    gateway = service._build_telegram_gateway()
    gateway.config = config
    gateway.state_dir = tmp_path / "tg"
    gateway.state_dir.mkdir(parents=True, exist_ok=True)
    gateway.state_file = gateway.state_dir / "state.json"
    gateway.outbox_file = gateway.state_dir / "outbox.json"
    return gateway


def _update(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 42, "type": "private"},
            "date": 1_700_000_000,
            "text": text,
        },
    }


def test_telegram_linha_command_pushes_the_ticket_and_reports_the_run(hub, store, tmp_path) -> None:
    service, _client = hub
    _hub_ticket(service)
    gateway = _gateway(tmp_path, service)

    result = gateway.process_update(_update(1, "/linha usr-62"))

    assert result.authorized and result.target_id == "USR-62" and result.error is None
    assert "enviado a linha" in (result.response_text or "")
    assert _run_count(store) == 1


def test_telegram_linha_reports_unknown_ticket_and_usage(hub, store, tmp_path) -> None:
    service, _client = hub
    gateway = _gateway(tmp_path, service)

    unknown = gateway.process_update(_update(2, "/linha USR-404"))
    assert "nao encontrado" in (unknown.response_text or "") and unknown.error
    usage = gateway.process_update(_update(3, "/linha"))
    assert "Uso: /linha" in (usage.response_text or "")
    assert _run_count(store) == 0


def test_telegram_demand_does_not_touch_the_line_unless_autosubmit_is_on(hub, store, tmp_path) -> None:
    service, _client = hub
    gateway = _gateway(tmp_path, service)

    result = gateway.process_update(_update(4, "/demand Adicionar um menu vertical esquerdo no DarkHub"))

    assert result.target_id and result.target_id.startswith("USR-")
    assert "Linha autonoma" not in (result.response_text or "")
    assert _run_count(store) == 0


def test_telegram_demand_enqueues_a_line_run_when_autosubmit_is_on(hub, store, tmp_path, monkeypatch) -> None:
    service, _client = hub
    monkeypatch.setenv(owner_intake.AUTOSUBMIT_ENV, "true")
    gateway = _gateway(tmp_path, service)

    result = gateway.process_update(_update(5, "/demand Adicionar um menu vertical esquerdo no DarkHub"))

    assert result.target_id and result.target_id.startswith("USR-")
    assert "Linha autonoma" in (result.response_text or "")
    assert _run_count(store) == 1
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT external_id FROM intake_commands")
        assert cur.fetchone()[0] == f"ticket:{result.target_id}"
    finally:
        conn.close()


def test_help_lists_the_linha_command(hub, tmp_path) -> None:
    service, _client = hub
    gateway = _gateway(tmp_path, service)
    assert "/linha" in (gateway.process_update(_update(6, "/help")).response_text or "")
