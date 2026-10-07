"""Bridge from owner demands (Telegram /demand, /linha, DarkHub action) into the production line.

No network, no real Postgres, no real Telegram: the control store is a temp-file
`SQLiteControlStore`, and Telegram updates are fed straight to `TelegramGateway`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.line import owner_intake
from core.roadmap.models import DeliveryStatus
from core.workflow.control_contracts import IntakeCommand, StoreUnavailableError
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

    _ticket(demands, "USR-70", status=DeliveryStatus.COMPLETED, delivery_evidence="legacy:ledger:test")
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
    from core.orchestrator.adapters import control_postgres

    def _unreachable(self):
        raise StoreUnavailableError("connection refused")

    monkeypatch.setattr(control_postgres.PostgresControlStore, "_init_db", _unreachable)  # no network in tests
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://u:p@db.invalid:5432/none")
    with pytest.raises(StoreUnavailableError):
        owner_intake.open_line_store()
    with pytest.raises(StoreUnavailableError):
        owner_intake.open_line_store(url="postgresql://u:p@db.invalid:5432/none")


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
    config = TelegramConfig(bot_token="123:abc", authorized_user_ids=[42], authorized_chat_ids=[42], role="ops")
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


# --------------------------------------------------------------------------
# Ticket retries: a dead run must not be replayed forever
# --------------------------------------------------------------------------


def _line_stages() -> list[str]:
    from core.line.bindings import LINE_STAGES

    return [stage for stage in LINE_STAGES if stage != "retrospective"]


def _set_run_jobs(store: SQLiteControlStore, run_id: str, statuses: dict[str, str]) -> None:
    """Replace the run's jobs with one job per stage in the given status."""
    now = datetime(2026, 9, 30, 9, tzinfo=UTC).isoformat()
    conn = store._connect()
    try:
        conn.execute("DELETE FROM jobs WHERE run_id = ?", (run_id,))
        for stage, status in statuses.items():
            conn.execute(
                "INSERT INTO jobs (run_id,ticket_id,plan_version,stage,iteration,status,role,required_capabilities,"
                "fencing_token,timeout_seconds,retry_count,max_retries,actual_cost,output_refs,evidence_refs,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,0,1800,0,3,0.0,'[]','[]',?,?)",
                (run_id, "darkfac", "1.0", stage, 0, status, "role", "[]", now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _kill(store: SQLiteControlStore, run_id: str) -> None:
    _set_run_jobs(store, run_id, {"grill": "succeeded", "planning": "succeeded", "development": "failed"})


def _deliver(store: SQLiteControlStore, run_id: str) -> None:
    _set_run_jobs(store, run_id, {stage: "succeeded" for stage in _line_stages()})


def _external_ids(store: SQLiteControlStore) -> list[str]:
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT external_id FROM intake_commands ORDER BY committed_at, external_id")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def test_attempt_one_command_is_byte_identical_to_the_legacy_one(demands) -> None:
    ticket = _ticket(demands)
    legacy = owner_intake.command_for_ticket(ticket)
    assert owner_intake.command_for_ticket(ticket, 1).payload_digest == legacy.payload_digest
    assert legacy.external_id == "ticket:USR-62" and "attempt" not in legacy.payload
    second = owner_intake.command_for_ticket(ticket, 2)
    assert second.external_id == "ticket:USR-62:a2" and second.payload["attempt"] == 2
    assert second.payload_digest != legacy.payload_digest


def test_in_flight_run_is_replayed_never_duplicated(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert first.attempt == 1 and "tentativa 1" in first.message
    again = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert again.ok and again.replayed and again.run_id == first.run_id and again.state == "in_flight"
    assert "tentativa 1" in again.message and "ja estava na linha" in again.message
    assert _external_ids(store) == ["ticket:USR-62"]


def test_failed_run_is_retried_as_a_new_attempt(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _kill(store, first.run_id)

    second = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert second.ok and not second.replayed and second.attempt == 2 and second.state == "submitted"
    assert second.run_id != first.run_id and "tentativa 2" in second.message
    assert _external_ids(store) == ["ticket:USR-62", "ticket:USR-62:a2"]
    assert store.get_run_payload(second.run_id)["attempt"] == 2

    # Attempt 2 is now the in-flight one: replayed, not duplicated.
    third = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert third.run_id == second.run_id and third.attempt == 2 and third.replayed
    assert len(_external_ids(store)) == 2

    # When attempt 2 also dies, attempt 3 starts.
    _kill(store, second.run_id)
    fourth = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert fourth.attempt == 3 and _external_ids(store)[-1] == "ticket:USR-62:a3"


def test_failed_run_with_leftover_retry_rows_is_retried_not_replayed(store, demands) -> None:
    # Production run-f0c98a90cf0f: old independent_review iterations kept status `retry` after
    # development failed; the run must count as failed, not in flight.
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _set_run_jobs(
        store,
        first.run_id,
        {"grill": "succeeded", "independent_review": "retry", "planning": "replan", "development": "failed"},
    )

    second = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert second.ok and not second.replayed and second.attempt == 2
    assert _external_ids(store) == ["ticket:USR-62", "ticket:USR-62:a2"]


def test_run_parked_on_the_owner_is_still_in_flight(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _set_run_jobs(store, first.run_id, {"grill": "waiting_human"})

    again = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert again.replayed and again.run_id == first.run_id and again.state == "in_flight"


def test_delivered_ticket_is_reported_as_delivered_not_resubmitted(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _deliver(store, first.run_id)

    result = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert result.ok and result.state == "delivered" and result.run_id == first.run_id
    assert "ja foi entregue" in result.message and "tentativa 1" in result.message
    assert _external_ids(store) == ["ticket:USR-62"]


def test_attempts_are_capped_by_the_environment(store, demands, monkeypatch) -> None:
    monkeypatch.setenv(owner_intake.MAX_ATTEMPTS_ENV, "2")
    _ticket(demands)
    run = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store).run_id
    _kill(store, run)
    run = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store).run_id
    _kill(store, run)

    capped = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert capped.ok is False and "tentativas permitidas" in capped.message and capped.attempt == 2
    assert len(_external_ids(store)) == 2


def test_attempt_cap_default_and_invalid_values(monkeypatch) -> None:
    monkeypatch.delenv(owner_intake.MAX_ATTEMPTS_ENV, raising=False)
    assert owner_intake.DEFAULT_MAX_TICKET_ATTEMPTS == 10
    assert owner_intake.max_ticket_attempts_from_env() == 10
    for bad in ("0", "-1", "abc"):
        monkeypatch.setenv(owner_intake.MAX_ATTEMPTS_ENV, bad)
        assert owner_intake.max_ticket_attempts_from_env() == 10
    monkeypatch.setenv(owner_intake.MAX_ATTEMPTS_ENV, "7")
    assert owner_intake.max_ticket_attempts_from_env() == 7


def test_a_run_accepted_before_retries_existed_is_found_and_retried(store, demands) -> None:
    ticket = _ticket(demands)
    from core.demands.autonomous_intake import AutonomousIntakeService

    legacy = AutonomousIntakeService(store=store).accept(
        owner_intake.command_for_ticket(ticket), datetime(2026, 9, 29, 14, tzinfo=UTC)
    )
    _kill(store, legacy.run_id)  # the real-world run-9d292b5ac27c: development validate_exhausted

    result = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert result.attempt == 2 and result.run_id != legacy.run_id
    assert _external_ids(store) == ["ticket:USR-62", "ticket:USR-62:a2"]


def test_run_state_reads_the_jobs(store) -> None:
    run_id = store.accept(
        IntakeCommand(
            project_id="darkfac", channel="test", external_id="state-1", mode="autonomous", policy_ref="p",
            payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
        ),
        datetime(2026, 9, 30, 9, tzinfo=UTC),
    ).run_id
    assert owner_intake.run_state(store, run_id) == "in_flight"  # initial grill job pending
    _set_run_jobs(store, run_id, {"grill": "succeeded", "development": "running"})
    assert owner_intake.run_state(store, run_id) == "in_flight"
    _set_run_jobs(store, run_id, {"grill": "waiting_human"})
    assert owner_intake.run_state(store, run_id) == "in_flight"
    _kill(store, run_id)
    assert owner_intake.run_state(store, run_id) == "failed"
    _deliver(store, run_id)
    assert owner_intake.run_state(store, run_id) == "succeeded"
    assert owner_intake.run_state(store, None) == "in_flight"  # unknown: never start a duplicate
    assert owner_intake.run_state(object(), "run-x") == "in_flight"


def test_find_intake_runs_matches_only_the_ticket_and_its_attempts(store) -> None:
    now = datetime(2026, 9, 30, 9, tzinfo=UTC)
    for external_id in ("ticket:USR-6", "ticket:USR-62", "ticket:USR-62:a2", "ticket:USR-62:a10", "ticket:USR_62:a2", "ticket:USR-62:b2"):
        store.accept(
            IntakeCommand(
                project_id="darkfac", channel="owner", external_id=external_id, mode="autonomous",
                policy_ref="p", payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
            ),
            now,
        )
    store.accept(
        IntakeCommand(
            project_id="darkfac", channel="other", external_id="ticket:USR-62:a3", mode="autonomous",
            policy_ref="p", payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
        ),
        now,
    )
    found = [external_id for external_id, _run in store.find_intake_runs("owner", "ticket:USR-62")]
    assert sorted(found) == ["ticket:USR-62", "ticket:USR-62:a10", "ticket:USR-62:a2"]


def test_hub_endpoint_maps_the_attempt_limit_to_409(hub, store, monkeypatch) -> None:
    service, client = hub
    monkeypatch.setenv(owner_intake.MAX_ATTEMPTS_ENV, "1")
    _hub_ticket(service)
    first = client.post("/api/demands/tickets/USR-62/line").json()
    _kill(store, first["run_id"])
    resp = client.post("/api/demands/tickets/USR-62/line")
    assert resp.status_code == 409 and "tentativas permitidas" in resp.json()["detail"]


def test_telegram_linha_reports_the_attempt_and_the_delivered_state(hub, store, tmp_path) -> None:
    service, _client = hub
    _hub_ticket(service)
    gateway = _gateway(tmp_path, service)
    first = gateway.process_update(_update(30, "/linha USR-62"))
    assert "tentativa 1" in first.response_text
    run_id = store.find_intake_runs("owner", "ticket:USR-62")[0][1]
    _kill(store, run_id)
    second = gateway.process_update(_update(31, "/linha USR-62"))
    assert "tentativa 2" in second.response_text and "enviado a linha" in second.response_text
    run2 = store.find_intake_runs("owner", "ticket:USR-62")[-1][1]
    _deliver(store, run2)
    third = gateway.process_update(_update(32, "/linha USR-62"))
    assert "ja foi entregue" in third.response_text and third.error is None


# --------------------------------------------------------------------------
# Grill reuse across attempts: which earlier runs may a retry adopt from?
# --------------------------------------------------------------------------


def test_previous_attempt_run_ids_lists_earlier_identical_attempts_newest_first(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _kill(store, first.run_id)
    second = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _kill(store, second.run_id)
    third = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert owner_intake.previous_attempt_run_ids(store, store.get_run_payload(third.run_id)) == [
        second.run_id,
        first.run_id,
    ]
    assert owner_intake.previous_attempt_run_ids(store, store.get_run_payload(second.run_id)) == [first.run_id]
    # attempt 1 has nothing before it
    assert owner_intake.previous_attempt_run_ids(store, store.get_run_payload(first.run_id)) == []


def test_previous_attempt_run_ids_ignores_attempts_of_an_edited_ticket(store, demands) -> None:
    _ticket(demands)
    first = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    _kill(store, first.run_id)
    # Same ticket content (so the retry is accepted) is the normal case; simulate an edit by
    # rewriting the stored payload of the first attempt.
    conn = store._connect()
    try:
        row = conn.execute("SELECT payload FROM intake_commands WHERE run_id = ?", (first.run_id,)).fetchone()
        import json

        edited = json.loads(row["payload"])
        edited["problem"] = "Outro problema, editado"
        conn.execute("UPDATE intake_commands SET payload = ? WHERE run_id = ?", (json.dumps(edited), first.run_id))
        conn.commit()
    finally:
        conn.close()
    second = owner_intake.submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    assert second.attempt == 2
    assert owner_intake.previous_attempt_run_ids(store, store.get_run_payload(second.run_id)) == []


def test_previous_attempt_run_ids_is_fail_safe(store) -> None:
    assert owner_intake.previous_attempt_run_ids(store, None) == []
    assert owner_intake.previous_attempt_run_ids(store, {"ticket_id": "USR-1"}) == []  # no attempt key
    assert owner_intake.previous_attempt_run_ids(store, {"ticket_id": "USR-1", "attempt": "2"}) == []
    assert owner_intake.previous_attempt_run_ids(object(), {"ticket_id": "USR-1", "attempt": 2}) == []

    class Broken:
        def find_intake_runs(self, channel, external_id):
            raise RuntimeError("db down")

        def get_run_payload(self, run_id):
            return {}

    assert owner_intake.previous_attempt_run_ids(Broken(), {"ticket_id": "USR-1", "attempt": 2}) == []
