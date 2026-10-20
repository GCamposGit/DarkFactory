"""USR-278: Telegram /demand routes to the right project; unknown => onboarding, never darkfac."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.demands.autonomous_intake import AutonomousIntakeService
from core.integrations.telegram import TelegramConfig, TelegramGateway
from core.intake import project_routing
from core.projects.models import ProjectDescriptor
from core.projects.registry import ProjectRegistry
from core.workflow.control_store import SQLiteControlStore


def _msg(update_id: int, text: str, message_id: int | None = None) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id if message_id is not None else update_id,
            "date": 1700000000,
            "from": {"id": 10, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 20, "type": "private"},
            "text": text,
        },
    }


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    registry = ProjectRegistry(projects_file=tmp_path / "projects.json")
    registry.register_project(ProjectDescriptor(id="alpha-app", name="Alpha App", prefix="ALP"))
    registry.register_project(ProjectDescriptor(id="beta-app", name="Beta App", prefix="BET"))
    monkeypatch.setattr("core.projects.registry.get_project_registry", lambda: registry)
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    gateway = TelegramGateway(
        config=TelegramConfig(bot_token="tok", role="ops", authorized_user_ids=[10]),
        state_dir=tmp_path / "tg",
        intake_service=AutonomousIntakeService(store=store),
    )
    return gateway, store, tmp_path / "tg"


def _runs(store: SQLiteControlStore) -> list[tuple[str, str]]:
    conn = store._connect()
    try:
        return [(r[0], r[1]) for r in conn.execute("SELECT project_id, demand_id FROM runs ORDER BY project_id")]
    finally:
        conn.close()


def test_two_projects_are_isolated_and_internal_default_is_darkfac(env) -> None:
    gateway, store, _ = env
    gateway.process_update(_msg(1, "/demand alpha-app: criar tela de login"))
    gateway.process_update(_msg(2, "/demand --project beta-app emitir relatorio mensal"))
    gateway.process_update(_msg(3, "/demand corrigir fila interna"))
    gateway.process_update(_msg(4, "/demand fix: typo no hub"))
    projects = sorted(p for p, _ in _runs(store))
    assert projects == ["alpha-app", "beta-app", "darkfac", "darkfac"]


def test_replay_is_idempotent(env) -> None:
    gateway, store, _ = env
    first = gateway.process_update(_msg(1, "/demand alpha-app: criar tela de login", message_id=77))
    again = gateway.process_update(_msg(1, "/demand alpha-app: criar tela de login", message_id=77))
    # Same message re-delivered under a new update_id still maps to one demand.
    redelivered = gateway.process_update(_msg(2, "/demand alpha-app: criar tela de login", message_id=77))
    assert again.duplicate
    assert redelivered.duplicate and first.target_id
    assert len(_runs(store)) == 1


def test_unknown_project_goes_to_onboarding_not_darkfac(env) -> None:
    gateway, store, state_dir = env
    res = gateway.process_update(_msg(1, "/demand gamma-shop: loja online"))
    assert "onboarding" in res.response_text.lower()
    assert _runs(store) == []
    rows = json.loads((state_dir / project_routing.ONBOARDING_FILE).read_text(encoding="utf-8"))
    assert [r["project_id"] for r in rows] == ["gamma-shop"]
    assert rows[0]["creates_repository"] is False
    # replay of the same message does not duplicate the request
    gateway.process_update(_msg(2, "/demand gamma-shop: loja online", message_id=1))
    assert len(json.loads((state_dir / project_routing.ONBOARDING_FILE).read_text(encoding="utf-8"))) == 1


def test_newproject_records_request_and_known_project_is_refused(env) -> None:
    gateway, store, state_dir = env
    res = gateway.process_update(_msg(1, "/newproject Loja Delta: marketplace de artesanato"))
    assert res.target_id == "ONB-0001"
    rows = json.loads((state_dir / project_routing.ONBOARDING_FILE).read_text(encoding="utf-8"))
    assert rows[0]["project_id"] == "loja-delta"
    known = gateway.process_update(_msg(2, "/newproject alpha-app: algo"))
    assert "ja esta registrado" in known.response_text
    assert _runs(store) == []
