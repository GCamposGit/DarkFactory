"""USR-340: /demand <projeto>: ... vai ao intake autonomo; darkfac/sem projeto segue o demand_handler."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.projects.models import ProjectDescriptor
from core.projects.registry import ProjectRegistry


def _msg(update_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "from": {"id": 10, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 20, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = ProjectRegistry(projects_file=tmp_path / "projects.json")
    registry.register_project(ProjectDescriptor(id="pilot-hello", name="Pilot Hello", prefix="PH"))
    monkeypatch.setattr("core.projects.registry.get_project_registry", lambda: registry)
    store = DemandsStore(tmp_path / "demands.json")
    handler_calls: list[str] = []

    def handler(text: str, user_id: int) -> dict:
        handler_calls.append(text)
        return {"ticket_id": "USR-900", "title": text[:20], "status": "planned"}

    def project_handler(project_id: str, text: str, user_id: int) -> dict:
        ticket = store.save_ticket(
            UserTicket(id=store.next_ticket_id(project_id), project_id=project_id, title=text[:40], problem_statement=text)
        )
        return {"ticket_id": ticket.id}

    def build(project_handler=project_handler):
        return TelegramGateway(
            config=TelegramConfig(bot_token="tok", role="ops", authorized_user_ids=[10]),
            state_dir=tmp_path / "tg",
            demand_handler=handler,
            project_demand_handler=project_handler,
        )

    return build, handler_calls


def test_project_demand_creates_ph_ticket(env) -> None:
    build, calls = env
    result = build().process_update(_msg(1, "/demand pilot-hello: criar pagina hello"))
    assert result.target_id and result.target_id.startswith("PH-")
    assert calls == []


@pytest.mark.parametrize("text", ["/demand corrigir fila interna", "/demand darkfac: corrigir fila interna"])
def test_darkfac_and_projectless_keep_demand_handler(env, text: str) -> None:
    build, calls = env

    def boom(project_id: str, text: str, user_id: int) -> dict:
        raise AssertionError("project handler must not run")

    result = build(boom).process_update(_msg(2, text))
    assert calls == ["corrigir fila interna"]
    assert result.target_id == "USR-900"


def test_intake_unavailable_is_explicit_and_registers_nothing(env) -> None:
    build, calls = env

    def broken(project_id: str, text: str, user_id: int) -> dict:
        raise RuntimeError("store down")

    result = build(broken).process_update(_msg(3, "/demand pilot-hello: criar pagina"))
    assert "nada foi registrado" in result.response_text
    assert result.error and "store down" in result.error
    assert calls == []
