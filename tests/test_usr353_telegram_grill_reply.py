"""USR-353: a free-text Grill reply on Telegram (either bot) is recorded, visible and synced."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.owner_actions.answer_sync import DEMANDS_RELATIVE, apply_answers, pending_answers
from core.owner_actions.models import OwnerAction
from hub.backend.api import get_hub_service, router
from hub.backend.service import HubService

KEY = "test-telemetry-key"
GRILL_PROMPT = "Intervenção Prioritária (Grill Pendente)\nDemanda: Teto de gasto [USR-341]"
HEADERS = {"X-DarkFac-Telemetry-Key": KEY}


def _update(update_id: int, text: str, reply_to: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "chat": {"id": 42, "type": "private"},
        "date": 1760000000,
        "text": text,
    }
    if reply_to:
        message["reply_to_message"] = {
            "message_id": 1,
            "chat": {"id": 42, "type": "private"},
            "date": 1759990000,
            "text": reply_to,
        }
    return {"update_id": update_id, "message": message}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", KEY)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "42")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr("core.integrations.telegram.TelegramGateway.deliver_reply", lambda *a, **k: None)
    service = HubService(data_dir=tmp_path / "data", project_root=tmp_path)
    service.demands_service.store.save_ticket(
        UserTicket(id="USR-341", title="Teto de gasto", problem_statement="Contexto original")
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_hub_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client, service


def _answers(test_client: TestClient) -> list[dict[str, Any]]:
    return test_client.get("/api/owner-actions/answers", headers=HEADERS).json()["answers"]


@pytest.mark.parametrize("bot", ["owner", "ops"])
def test_free_text_reply_is_recorded_and_exposed(client, bot: str) -> None:
    test_client, service = client
    payload = _update(100, "Opcao B, teto de 50 dolares", GRILL_PROMPT)
    response = test_client.post(f"/api/webhooks/telegram/{bot}", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "grill" and body["target_id"] == "USR-341" and not body.get("error")
    assert "registrada" in body["response_text"]

    answers = _answers(test_client)
    assert len(answers) == 1
    assert answers[0]["answer"]["note"] == "Opcao B, teto de 50 dolares"
    assert answers[0]["blocks"] == ["USR-341"]

    ticket = service.demands_service.store.get_ticket("USR-341")
    assert any(t.startswith("owner-decision:OA-GRILL-USR-341-100=") for t in ticket.tags)
    assert "teto de 50 dolares" in ticket.problem_statement

    test_client.post(f"/api/webhooks/telegram/{bot}", json=payload)
    assert len(_answers(test_client)) == 1


def test_owner_bot_unrelated_chatter_stays_ignored_and_records_nothing(client) -> None:
    test_client, _service = client
    body = test_client.post("/api/webhooks/telegram/owner", json=_update(101, "oi")).json()
    assert body["action"] == "unknown" and not body.get("response_text")
    assert _answers(test_client) == []


def test_answer_sync_imports_grill_reply_into_repo_ledger(client, tmp_path: Path) -> None:
    test_client, _service = client
    test_client.post("/api/webhooks/telegram/ops", json=_update(102, "Aprovado, teto 40", GRILL_PROMPT))
    answers = [OwnerAction.model_validate(r) for r in _answers(test_client)]

    root = tmp_path / "repo"
    (root / ".factory" / "owner_actions").mkdir(parents=True)
    (root / ".factory" / "owner_actions" / "owner_actions.json").write_text('{"actions": []}', encoding="utf-8")
    DemandsStore(root / DEMANDS_RELATIVE).save_ticket(UserTicket(id="USR-341", title="Teto", problem_statement="x"))

    assert [a.id for a in pending_answers(root, answers)] == [answers[0].id]
    result = apply_answers(root, answers)
    assert result.annotated == {answers[0].id: ["USR-341"]}
    assert pending_answers(root, answers) == []
