"""Tests for line run cancellation and wall-clock expiration retry (USR-123)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.line import owner_intake
from core.line.owner_intake import cancel_line_run, run_state, submit_ticket_to_line
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService


@pytest.fixture
def store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "control.db")


@pytest.fixture
def demands(tmp_path: Path) -> DemandsStore:
    return DemandsStore(path=tmp_path / "demands.json")


def _ticket(demands: DemandsStore, ticket_id: str = "USR-62") -> UserTicket:
    return demands.save_ticket(
        UserTicket(
            id=ticket_id,
            project_id="darkfac",
            title="Menu vertical esquerdo",
            problem_statement="Menu vertical esquerdo no DarkHub",
            core_journey=["Owner usa menu"],
            acceptance_criteria=["Menu funcional"],
        )
    )


def test_sqlite_control_store_cancel_run(store: SQLiteControlStore, demands: DemandsStore) -> None:
    _ticket(demands)
    sub = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert sub.ok and sub.run_id

    # Run is initially active with grill job pending
    status = store.get_run_status(sub.run_id)
    assert status is not None
    assert status["status"] == "active"
    open_jobs = [j for j in status["jobs"] if j["status"] in ("pending", "running", "waiting_human")]
    assert len(open_jobs) >= 1

    # Cancel the run
    res = store.cancel_run(sub.run_id, reason="owner_cancelled", actor="owner")
    assert res["ok"] is True
    assert res["run_id"] == sub.run_id
    assert res["jobs_cancelled"] >= 1

    # Verify run and job statuses
    updated_status = store.get_run_status(sub.run_id)
    assert updated_status is not None
    assert updated_status["status"] == "cancelled"
    for j in updated_status["jobs"]:
        assert j["status"] == "cancelled"
        assert j["cause_code"] == "owner_cancelled"

    # Verify audit event in outbox
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM outbox WHERE aggregate_id = ? AND event_type = 'run_cancelled'", (sub.run_id,))
        row = cur.fetchone()
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload["reason"] == "owner_cancelled"
        assert payload["actor"] == "owner"
    finally:
        conn.close()


def test_sqlite_control_store_cancel_nonexistent_run(store: SQLiteControlStore) -> None:
    res = store.cancel_run("run-nonexistent")
    assert res["ok"] is False
    assert "not found" in res["error"]


def test_cancel_line_run_by_ticket_id_enables_retry(store: SQLiteControlStore, demands: DemandsStore) -> None:
    _ticket(demands)
    first = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert first.ok and first.attempt == 1

    # Attempting to submit again without cancel replays attempt 1
    replayed = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert replayed.ok and replayed.attempt == 1 and replayed.replayed is True

    # Cancel via ticket_id
    cancel_res = cancel_line_run("USR-62", demands_store=demands, store=store)
    assert cancel_res.ok is True
    assert cancel_res.ticket_id == "USR-62"
    assert cancel_res.run_id == first.run_id
    assert cancel_res.jobs_cancelled >= 1
    assert "cancelado com sucesso" in cancel_res.message

    # run_state for first run is now 'failed' (logically terminal)
    assert run_state(store, first.run_id) == "failed"

    # Submitting ticket again opens attempt 2!
    second = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert second.ok is True
    assert second.attempt == 2
    assert second.run_id != first.run_id
    assert second.replayed is False


def test_cancel_line_run_by_run_id(store: SQLiteControlStore, demands: DemandsStore) -> None:
    _ticket(demands)
    first = submit_ticket_to_line("USR-62", demands_store=demands, store=store)

    cancel_res = cancel_line_run(first.run_id, demands_store=demands, store=store)
    assert cancel_res.ok is True
    assert cancel_res.run_id == first.run_id

    # Unknown ticket
    unknown = cancel_line_run("USR-999", demands_store=demands, store=store)
    assert unknown.ok is False
    assert "Nenhum run encontrado" in unknown.message


@pytest.fixture
def hub(tmp_path: Path, store: SQLiteControlStore):
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=store)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        yield service, TestClient(app)
    finally:
        app.dependency_overrides.pop(get_hub_service, None)


def _gateway(tmp_path: Path, service: HubService) -> TelegramGateway:
    config = TelegramConfig(bot_token="123:abc", authorized_user_ids=[42], authorized_chat_ids=[42], role="ops")
    gateway = service._build_telegram_gateway()
    gateway.config = config
    gateway.state_dir = tmp_path / "tg"
    gateway.state_dir.mkdir(parents=True, exist_ok=True)
    gateway.state_file = gateway.state_dir / "state.json"
    gateway.outbox_file = gateway.state_dir / "outbox.json"
    return gateway


def _update(uid: int, text: str) -> dict[str, Any]:
    return {
        "update_id": uid,
        "message": {
            "message_id": uid,
            "date": 1700000000,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
            "text": text,
        },
    }


def test_telegram_cancelar_command(hub, store, tmp_path) -> None:
    service, _client = hub
    _ticket(service.demands_store)
    first = submit_ticket_to_line("USR-62", demands_store=service.demands_store, store=store)
    assert first.ok and first.run_id

    gateway = _gateway(tmp_path, service)

    # Usage when no target provided
    usage = gateway.process_update(_update(1, "/cancelar"))
    assert "Uso: /cancelar" in (usage.response_text or "")

    # Cancel USR-62
    cancel_op = gateway.process_update(_update(2, "/cancelar USR-62"))
    assert "cancelado com sucesso" in (cancel_op.response_text or "")

    # Now /linha USR-62 opens attempt 2!
    retry_op = gateway.process_update(_update(3, "/linha USR-62"))
    assert "tentativa 2" in (retry_op.response_text or "")


def test_hub_api_cancel_endpoints(hub, store) -> None:
    service, client = hub
    _ticket(service.demands_store)

    # Submit ticket to line
    submit_resp = client.post("/api/demands/tickets/USR-62/line")
    assert submit_resp.status_code == 202
    run_id = submit_resp.json()["run_id"]

    # Cancel via /demands/tickets/{id}/line/cancel
    cancel_resp = client.post("/api/demands/tickets/USR-62/line/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["ok"] is True
    assert cancel_resp.json()["run_id"] == run_id

    # 404 for unknown ticket
    unknown_resp = client.post("/api/demands/tickets/USR-404/line/cancel")
    assert unknown_resp.status_code == 404

    # Submit attempt 2
    submit_resp2 = client.post("/api/demands/tickets/USR-62/line")
    assert submit_resp2.status_code == 202
    run_id2 = submit_resp2.json()["run_id"]
    assert run_id2 != run_id

    # Cancel via /line/runs/{run_id}/cancel
    cancel_run_resp = client.post(f"/api/line/runs/{run_id2}/cancel")
    assert cancel_run_resp.status_code == 200
    assert cancel_run_resp.json()["ok"] is True
    assert cancel_run_resp.json()["run_id"] == run_id2


def test_wall_clock_expired_no_route_wait_allows_retry(store: SQLiteControlStore, demands: DemandsStore) -> None:
    """Acceptance criterion 2: run whose wall clock expired with waiting_human(no_route_available) treated as ended."""
    _ticket(demands, "USR-62")
    sub = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert sub.ok and sub.run_id

    # Set run created_at 7 hours ago and lone job waiting_human(no_route_available)
    past = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
    conn = store._connect()
    try:
        conn.execute("UPDATE runs SET created_at = ? WHERE run_id = ?", (past, sub.run_id))
        conn.execute(
            "UPDATE jobs SET status = 'waiting_human', cause_code = 'no_route_available' WHERE run_id = ?",
            (sub.run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    # run_state returns 'failed'
    assert run_state(store, sub.run_id, wall_clock_hours=6.0) == "failed"

    # submit_ticket_to_line opens attempt 2
    sub2 = submit_ticket_to_line("USR-62", demands_store=demands, store=store)
    assert sub2.ok is True
    assert sub2.attempt == 2
    assert sub2.run_id != sub.run_id
