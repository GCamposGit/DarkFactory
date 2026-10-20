from __future__ import annotations

import json
from pathlib import Path

from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.projects import registry as project_registry
from core.projects.models import ProjectDescriptor
from core.projects.registry import ProjectRegistry
from hub.backend.service import HubService


def _message(update_id: int, text: str, *, chat_id: int = 42) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "from": {"id": 7},
            "chat": {"id": chat_id},
            "date": 1_700_000_000,
            "text": text,
        },
    }


def _registry(tmp_path: Path, monkeypatch) -> None:
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps([
            ProjectDescriptor(id="pilot-hello", name="pilot-hello", path=str(tmp_path / "pilot"), prefix="PH").model_dump()
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(project_registry, "_GLOBAL_REGISTRY", ProjectRegistry(projects_file))


def test_gateway_routes_explicit_project_to_handler_and_keeps_two_arg_compatibility(tmp_path: Path) -> None:
    calls: list[tuple] = []

    def handler(text: str, user_id: int, project_id: str, *, external_id: str) -> dict:
        calls.append((text, user_id, project_id, external_id))
        return {"ticket_id": "PH-001", "title": "Hello", "status": "planned"}

    gateway = TelegramGateway(
        config=TelegramConfig(role="ops", authorized_user_ids=[7]),
        state_dir=tmp_path,
        demand_handler=handler,
    )

    result = gateway.process_update(_message(1, "/demand pilot-hello: pagina inicial"))

    assert "PH-001" in result.response_text
    assert "nada foi registrado" not in result.response_text
    assert calls == [("pagina inicial", 7, "pilot-hello", "tg-msg-42-101")]

    legacy_calls: list[tuple[str, int]] = []
    legacy = TelegramGateway(
        config=TelegramConfig(role="ops", authorized_user_ids=[7]),
        state_dir=tmp_path / "legacy",
        demand_handler=lambda text, user_id: legacy_calls.append((text, user_id)) or {
            "ticket_id": "USR-001", "title": "Old", "status": "planned"
        },
    )
    legacy.process_update(_message(2, "/demand legacy demand"))
    assert legacy_calls == [("legacy demand", 7)]


def test_hub_handler_creates_ph_ticket_with_message_external_id_and_route_regressions(
    tmp_path: Path, monkeypatch
) -> None:
    _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "7")
    service = HubService(data_dir=tmp_path / "hub", project_root=tmp_path)
    gateway = service._build_telegram_gateway()

    result = gateway.process_update(_message(1, "/demand pilot-hello: landing page"))
    ticket_id = result.target_id
    assert ticket_id and ticket_id.startswith("PH-")
    ticket = service.demands_service.get_ticket(ticket_id)
    assert ticket is not None
    assert ticket.project_id == "pilot-hello"
    assert "tg-msg-42-101" in ticket.tags or any("tg-msg-42-101" in step for step in ticket.core_journey)
    assert not any(t.project_id == "darkfac" and "tg-msg-42-101" in " ".join(t.tags + t.core_journey)
                   for t in service.demands_service.list_tickets())

    default = gateway.process_update(_message(2, "/demand core demand"))
    assert default.target_id and default.target_id.startswith("USR-")

    unknown = gateway.process_update(_message(3, "/demand project-does-not-exist: new product"))
    assert "onboarding" in unknown.response_text.lower() or "solicitacao" in unknown.response_text.lower()
    assert not service.demands_service.list_tickets(project_id="project-does-not-exist")
