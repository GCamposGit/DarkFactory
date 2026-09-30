"""Line intake on the real (Postgres) store, one Telegram bot per job, and waking waiting grills.

No network and no real Postgres: the "line store" is a temp-file `SQLiteControlStore` distinct
from the Hub's own local store, Telegram sends are recorded, and webhook registration uses a fake
HTTP callable.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.demands.models import UserTicket
from core.integrations import telegram_webhooks
from core.integrations.telegram import TelegramGateway
from core.line import canary, human, owner_intake, stage_grill
from core.line.agent_cli import AgentResult
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import IntakeCommand
from core.workflow.control_store import SQLiteControlStore
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.service import HubService
from tests.line.conftest import copy_bare_origin

_ENV_NAMES = (
    *owner_intake.DATABASE_URL_ENVS,
    owner_intake.AUTOSUBMIT_ENV,
    "TELEGRAM_OPS_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_OWNER_BOT_TOKEN",
    "TELEGRAM_AUTHORIZED_USERS",
    "TELEGRAM_AUTHORIZED_CHATS",
    "TELEGRAM_WEBHOOK_SECRET",
    "DARKHUB_PUBLIC_URL",
    "DARKHUB_ENV",
    "DARKHUB_TELEGRAM_AUTO_WEBHOOK",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def local_store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "hub_local_control.db")


@pytest.fixture
def line_store(tmp_path: Path) -> SQLiteControlStore:
    return SQLiteControlStore(db_path=tmp_path / "line_control.db")


def _run_count(store: SQLiteControlStore) -> int:
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM runs")
        return cur.fetchone()[0]
    finally:
        conn.close()


def _job_status(store: SQLiteControlStore, run_id: str, stage: str) -> str:
    conn = store._connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM jobs WHERE run_id = ? AND stage = ?", (run_id, stage))
        return cur.fetchone()["status"]
    finally:
        conn.close()


def _set_job_status(store: SQLiteControlStore, run_id: str, stage: str, status: str) -> None:
    conn = store._connect()
    try:
        conn.execute("UPDATE jobs SET status = ? WHERE run_id = ? AND stage = ?", (status, run_id, stage))
        conn.commit()
    finally:
        conn.close()


def _accept(store: SQLiteControlStore, project_id: str = "acme", external_id: str = "x-1") -> str:
    receipt = store.accept(
        IntakeCommand(
            project_id=project_id, channel="test", external_id=external_id, mode="autonomous",
            policy_ref="darkfac://line/v1",
            payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
        ),
        datetime(2026, 9, 30, 8, tzinfo=UTC),
    )
    assert receipt.run_id
    return receipt.run_id


# --------------------------------------------------------------------------
# 1. Hub line store: Postgres whenever a URL is configured
# --------------------------------------------------------------------------


def test_hub_uses_the_postgres_line_store_even_with_its_own_local_store(
    monkeypatch, tmp_path, local_store, line_store
) -> None:
    monkeypatch.setenv("DARKHUB_CONTROL_DATABASE_URL", "postgresql://darkfac_worker@db/darkfac_hf02_prod")
    opened: list[str | None] = []

    def _fake_open(fallback=None, *, url=None):
        opened.append(url)
        return line_store

    monkeypatch.setattr(owner_intake, "open_line_store", _fake_open)
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store)

    assert service._line_control_store() is line_store
    assert service._line_control_store() is line_store
    assert opened == ["postgresql://darkfac_worker@db/darkfac_hf02_prod"]  # opened once, then cached

    service.demands_store.save_ticket(UserTicket(id="USR-62", title="Menu vertical", problem_statement="Menu no DarkHub"))
    submission = service.submit_ticket_to_line("USR-62")
    assert submission.ok and submission.run_id
    assert _run_count(line_store) == 1
    assert _run_count(local_store) == 0  # nothing leaked into the Hub-local SQLite no worker reads


def test_only_an_explicit_line_store_overrides_the_configured_url(monkeypatch, tmp_path, local_store, line_store) -> None:
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(owner_intake, "open_line_store", lambda *a, **k: pytest.fail("must not open Postgres"))
    service = HubService(
        project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store, line_control_store=line_store
    )
    assert service._line_control_store() is line_store


def test_without_a_line_url_the_hub_keeps_its_local_fallback(tmp_path, local_store) -> None:
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store)
    assert service._line_control_store() is local_store


def test_unreachable_line_store_is_reported_not_swallowed(monkeypatch, tmp_path, local_store) -> None:
    monkeypatch.setenv("DARKHUB_CONTROL_DATABASE_URL", "postgresql://x")

    def _boom(fallback=None, *, url=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(owner_intake, "open_line_store", _boom)
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store)
    service.demands_store.save_ticket(UserTicket(id="USR-62", title="Menu vertical", problem_statement="Menu no DarkHub"))
    submission = service.submit_ticket_to_line("USR-62")
    assert submission.ok is False and "db down" in submission.message
    assert _run_count(local_store) == 0  # never silently falls back to the local store


def test_demand_autosubmit_reaches_the_line_with_only_the_control_database_url(
    monkeypatch, tmp_path, local_store, line_store
) -> None:
    monkeypatch.setenv("DARKHUB_CONTROL_DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "42")
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "1:ops")
    monkeypatch.setattr(owner_intake, "open_line_store", lambda fallback=None, *, url=None: line_store)
    assert owner_intake.autosubmit_enabled() is True
    service = HubService(project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store)
    gateway = service._build_telegram_gateway(role="ops")

    result = gateway.process_update(_message_update(1, "/demand Adicionar menu vertical esquerdo no DarkHub"))

    assert "Linha autonoma" in result.response_text
    assert _run_count(line_store) == 1 and _run_count(local_store) == 0


# --------------------------------------------------------------------------
# 2. Bot separation: routes, gateways, worker sender
# --------------------------------------------------------------------------


def _message_update(update_id: int, text: str) -> dict:
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


@pytest.fixture
def hub(monkeypatch, tmp_path, local_store, line_store):
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "111:ops-token")
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "222:owner-token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "42")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "42")
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        TelegramGateway,
        "send_message",
        lambda self, chat_id, text, buttons=None: sent.append((self.config.role, self.config.bot_token, text)) or True,
    )
    service = HubService(
        project_root=tmp_path, data_dir=tmp_path / "hub_data", control_store=local_store, line_control_store=line_store
    )
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        yield service, TestClient(app), sent
    finally:
        app.dependency_overrides.pop(get_hub_service, None)


def test_owner_route_never_handles_demands_and_answers_with_the_owner_bot(hub, line_store) -> None:
    service, client, sent = hub

    resp = client.post("/api/webhooks/telegram/owner", json=_message_update(1, "/demand Adicionar menu vertical"))

    assert resp.status_code == 200
    assert service.demands_store.list_tickets() == []  # no ticket, no line run
    assert _run_count(line_store) == 0
    assert len(sent) == 1
    role, token, text = sent[0]
    assert (role, token) == ("owner", "222:owner-token") and "@darkfac_ops_bot" in text

    # Plain chatter is ignored silently.
    client.post("/api/webhooks/telegram/owner", json=_message_update(2, "bom dia"))
    assert len(sent) == 1


@pytest.mark.parametrize("path", ["/api/webhooks/telegram/ops", "/api/webhooks/telegram"])
def test_ops_and_legacy_routes_handle_demands_and_reply_with_the_ops_bot(hub, line_store, path) -> None:
    service, client, sent = hub

    resp = client.post(path, json=_message_update(10, "/demand Adicionar menu vertical esquerdo"))

    assert resp.status_code == 200
    assert len(service.demands_store.list_tickets()) == 1
    assert sent and sent[0][0] == "ops" and sent[0][1] == "111:ops-token"


def test_update_ids_of_the_two_bots_are_deduplicated_independently(hub) -> None:
    _service, client, sent = hub
    client.post("/api/webhooks/telegram/owner", json=_message_update(7, "/help"))
    client.post("/api/webhooks/telegram/ops", json=_message_update(7, "/help"))  # same update_id, other bot
    assert [entry[0] for entry in sent] == ["owner", "ops"]


def test_grill_button_on_the_ops_route_records_the_answer_and_wakes_the_job_on_the_line_store(
    hub, line_store, local_store
) -> None:
    service, client, sent = hub
    run_id = _accept(line_store, project_id="darkfac", external_id="grill-btn-1")
    _set_job_status(line_store, run_id, "grill", "waiting_human")

    resp = client.post(
        "/api/webhooks/telegram/ops",
        json={
            "update_id": 50,
            "callback_query": {
                "id": "cb-1",
                "from": {"id": 42},
                "message": {"message_id": 5, "chat": {"id": 42, "type": "private"}, "date": 1},
                "data": f"cb:grill:{run_id}#q1:1",
            },
        },
    )

    body = resp.json()
    assert resp.status_code == 200 and body["resumed"] is True
    assert "@darkfac_bot" not in body["response_text"]
    assert line_store.get_grill_answers(run_id) == {"q1": "1"}
    assert _job_status(line_store, run_id, "grill") == "pending"  # woken immediately, not at the deadline
    assert _run_count(local_store) == 0
    assert sent and sent[-1][0] == "ops"

    # Idempotent: a repeated tap changes nothing and does not raise.
    again = client.post(
        "/api/webhooks/telegram/ops",
        json={"update_id": 51, "callback_query": {"id": "cb-2", "from": {"id": 42}, "data": f"cb:grill:{run_id}#q1:1"}},
    )
    assert again.status_code == 200 and _job_status(line_store, run_id, "grill") == "pending"


def test_grill_button_on_the_owner_route_is_not_handled(hub, line_store) -> None:
    _service, client, sent = hub
    run_id = _accept(line_store, project_id="darkfac", external_id="grill-btn-2")
    _set_job_status(line_store, run_id, "grill", "waiting_human")

    client.post(
        "/api/webhooks/telegram/owner",
        json={
            "update_id": 60,
            "callback_query": {
                "id": "cb-9", "from": {"id": 42},
                "message": {"message_id": 5, "chat": {"id": 42, "type": "private"}, "date": 1},
                "data": f"cb:grill:{run_id}#q1:0",
            },
        },
    )

    assert line_store.get_grill_answers(run_id) == {}
    assert _job_status(line_store, run_id, "grill") == "waiting_human"
    assert sent and sent[-1][0] == "owner" and "@darkfac_ops_bot" in sent[-1][2]


def test_worker_grill_notifications_use_the_ops_bot_when_its_token_is_set(monkeypatch) -> None:
    import core.integrations.telegram as tg

    roles: list[str] = []

    def _fake_config(config_file=None, role="ops"):
        roles.append(role)
        return tg.TelegramConfig(bot_token="1:tok", role=role, authorized_chat_ids=[42])

    monkeypatch.setattr(tg, "load_telegram_config", _fake_config)
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "2:owner")

    assert stage_grill.default_telegram_sender() is not None
    assert roles == ["owner"]  # no ops token: owner is the fallback

    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "1:ops")
    assert stage_grill.default_telegram_sender() is not None
    assert roles == ["owner", "ops"]


def test_alerts_and_human_requests_stay_on_the_owner_bot(monkeypatch, tmp_path) -> None:
    from core.notifications.service import NotificationService
    from core.notifications.store import NotificationStore

    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", "1111:owner_token")
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", "2222:ops_token")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_USERS", "999")
    monkeypatch.setenv("TELEGRAM_AUTHORIZED_CHATS", "999")
    service = NotificationService(store=NotificationStore(store_path=tmp_path / "n.jsonl"), cooldown_minutes=1)
    tg_service = service._get_telegram_service()
    assert tg_service.config.role == "owner" and tg_service.config.bot_token == "1111:owner_token"


# --------------------------------------------------------------------------
# 3. Webhook registration (per-bot routes, idempotent, no token in logs)
# --------------------------------------------------------------------------


class FakeTelegramApi:
    def __init__(self, current: dict[str, str] | None = None, fail: bool = False) -> None:
        self.current = dict(current or {})  # bot token -> registered url
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def __call__(self, method: str, url: str, payload):
        self.calls.append((method, url, payload))
        token = url.split("/bot", 1)[1].split("/", 1)[0]
        if self.fail:
            raise OSError(f"boom talking to {url}")
        if url.endswith("/getWebhookInfo"):
            return {"ok": True, "result": {"url": self.current.get(token, "")}}
        self.current[token] = payload["url"]
        return {"ok": True, "result": True}


_PROD_ENV = {
    "DARKHUB_ENV": "production",
    "TELEGRAM_OPS_BOT_TOKEN": "111:ops-token",
    "TELEGRAM_BOT_TOKEN": "111:ops-token",
    "TELEGRAM_OWNER_BOT_TOKEN": "222:owner-token",
    "TELEGRAM_WEBHOOK_SECRET": "s3cret_value",
}


def test_each_bot_is_registered_on_its_own_route_with_the_existing_secret() -> None:
    api = FakeTelegramApi(current={"111:ops-token": "https://darkhub.ggcampos.com/api/webhooks/telegram", "222:owner-token": "https://darkhub.ggcampos.com/api/webhooks/telegram"})

    result = telegram_webhooks.register_telegram_webhooks(_PROD_ENV, http=api)

    assert result == {"ops": "updated", "owner": "updated"}
    sets = [(url, payload) for method, url, payload in api.calls if method == "POST"]
    assert {u.split("/bot")[1].split("/")[0]: p["url"] for u, p in sets} == {
        "111:ops-token": "https://darkhub.ggcampos.com/api/webhooks/telegram/ops",
        "222:owner-token": "https://darkhub.ggcampos.com/api/webhooks/telegram/owner",
    }
    for _url, payload in sets:
        assert payload["secret_token"] == "s3cret_value" and payload["allowed_updates"] == ["message", "callback_query"]


def test_registration_is_idempotent() -> None:
    api = FakeTelegramApi()
    assert telegram_webhooks.register_telegram_webhooks(_PROD_ENV, http=api) == {"ops": "updated", "owner": "updated"}
    posts_before = sum(1 for c in api.calls if c[0] == "POST")
    assert telegram_webhooks.register_telegram_webhooks(_PROD_ENV, http=api) == {"ops": "unchanged", "owner": "unchanged"}
    assert sum(1 for c in api.calls if c[0] == "POST") == posts_before


def test_registration_uses_the_public_url_env_and_skips_a_shared_token() -> None:
    env = {**_PROD_ENV, "DARKHUB_PUBLIC_URL": "https://hub.example.test/", "TELEGRAM_OWNER_BOT_TOKEN": "111:ops-token"}
    api = FakeTelegramApi()
    result = telegram_webhooks.register_telegram_webhooks(env, http=api)
    assert result == {"ops": "updated"}  # one bot cannot have two webhooks
    assert api.current["111:ops-token"] == "https://hub.example.test/api/webhooks/telegram/ops"


def test_registration_is_disabled_outside_production_unless_forced() -> None:
    env = {k: v for k, v in _PROD_ENV.items() if k != "DARKHUB_ENV"}
    api = FakeTelegramApi()
    assert telegram_webhooks.register_telegram_webhooks(env, http=api) == {} and api.calls == []
    assert telegram_webhooks.register_telegram_webhooks({**env, "DARKHUB_TELEGRAM_AUTO_WEBHOOK": "true"}, http=api)
    api2 = FakeTelegramApi()
    assert telegram_webhooks.register_telegram_webhooks({**_PROD_ENV, "DARKHUB_TELEGRAM_AUTO_WEBHOOK": "false"}, http=api2) == {}


def test_registration_failure_never_raises_and_never_logs_a_token(caplog) -> None:
    api = FakeTelegramApi(fail=True)
    with caplog.at_level(logging.INFO):
        result = telegram_webhooks.register_telegram_webhooks(_PROD_ENV, http=api)
    assert result == {"ops": "failed", "owner": "failed"}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in ("111:ops-token", "222:owner-token", "s3cret_value"):
        assert secret not in logged
    assert "role=ops" in logged


def test_no_tokens_means_nothing_to_register() -> None:
    assert telegram_webhooks.register_telegram_webhooks({"DARKHUB_ENV": "production"}, http=FakeTelegramApi()) == {}


# --------------------------------------------------------------------------
# 4. Grill answers reach the pending grill through the control store
# --------------------------------------------------------------------------


@pytest.fixture
def acme(tmp_path, monkeypatch) -> ProjectDescriptor:
    origin = copy_bare_origin(tmp_path / "origin.git")
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    monkeypatch.setattr(stage_grill, "default_telegram_sender", lambda: None)
    return ProjectDescriptor(id="acme", name="Acme", repo_url=str(origin))


_REPLY = json.dumps(
    {
        "questions": [
            {"id": "q1", "text": "Cobrar assinatura?", "kind": "business", "options": ["sim", "nao"], "recommended": "nao"}
        ],
        "assumptions": [],
        "is_product_scale": False,
    }
)


def test_store_records_grill_answers_last_one_wins(local_store) -> None:
    now = datetime(2026, 9, 30, tzinfo=UTC)
    local_store.record_grill_answer("run-1", "q1", "0", now)
    local_store.record_grill_answer("run-1", "q2", "nao", now)
    local_store.record_grill_answer("run-1", "q1", "1", now + timedelta(seconds=1))
    assert local_store.get_grill_answers("run-1") == {"q1": "1", "q2": "nao"}
    assert local_store.get_grill_answers("run-other") == {}


def test_answer_recorded_without_git_wakes_the_job_and_finalizes_the_pending_grill(acme, monkeypatch, line_store) -> None:
    run_id = _accept(line_store, project_id="acme", external_id="grill-e2e")
    monkeypatch.setattr(
        stage_grill, "run_read_agent", lambda *a, **k: AgentResult(ok=True, text=_REPLY, harness="claude", duration_s=0.01)
    )
    now = datetime(2026, 9, 30, 8, tzinfo=UTC)
    first = stage_grill.run_grill(acme, run_id, "demanda", now=now)
    assert first.outcome == "waiting_human"
    _set_job_status(line_store, run_id, "grill", "waiting_human")

    # The Hub has no git: the handler must not touch the workspace at all.
    monkeypatch.setattr(
        stage_grill, "submit_grill_answers", lambda *a, **k: pytest.fail("the git-based answer path must not run")
    )
    handler = human.build_telegram_line_grill_handler(line_store)
    assert handler(run_id, "q1", "0", 42) == {"resumed": True}
    assert _job_status(line_store, run_id, "grill") == "pending"

    # A partial/duplicate reconcile with no new info would still wait; the recorded answer finalizes it.
    result = stage_grill.run_grill(
        acme, run_id, "demanda", now=now + timedelta(minutes=1), answers=line_store.get_grill_answers(run_id)
    )
    assert result.outcome == "success" and not result.evidence_refs
    from core.line import workspace as ws_mod

    md = (ws_mod.context_dir(ws_mod.checkout(acme, run_id)) / "GRILL.md").read_text(encoding="utf-8")
    assert "-> **sim** (respondida pelo owner)" in md and "Aguardando decisao" not in md


def test_handler_for_an_unknown_run_records_nothing(line_store) -> None:
    handler = human.build_telegram_line_grill_handler(line_store)
    assert handler("run-does-not-exist", "q1", "0", 42) == {"resumed": False}
    assert line_store.get_grill_answers("run-does-not-exist") == {}


def test_grill_stage_handler_hands_the_recorded_answers_to_run_grill(monkeypatch, acme, line_store) -> None:
    from core.line.bindings import GrillStageHandler

    run_id = _accept(line_store, project_id="acme", external_id="grill-handler")
    line_store.record_grill_answer(run_id, "q1", "1", datetime(2026, 9, 30, tzinfo=UTC))
    seen: dict = {}
    monkeypatch.setattr(stage_grill, "run_grill", lambda *a, **k: seen.update(k) or None)

    class _Ctx:
        class claim:
            class job_key:
                ticket_id = "acme"

            job_key.run_id = run_id

    handler = GrillStageHandler(
        store=line_store, host_caps=[], routing_config=None, project_resolver=lambda pid: acme
    )
    handler.handle(_Ctx())
    assert seen["answers"] == {"q1": "1"}


# --------------------------------------------------------------------------
# 5. The canary wakes its own waiting grill
# --------------------------------------------------------------------------


class _Observer:
    def __init__(self, grill_status: str) -> None:
        self.grill_status = grill_status
        self.calls = 0

    def observe(self, run_id: str):
        self.calls += 1
        return [canary.CanaryStageObservation(stage="grill", status=self.grill_status)]


class _Clock:
    def __init__(self, day: date) -> None:
        self._day = day

    def today(self) -> date:
        return self._day

    def now(self) -> datetime:
        return datetime(self._day.year, self._day.month, self._day.day, 9, tzinfo=UTC)


def _canary_iteration(store, tmp_path, observer):
    day = date(2026, 9, 30)
    return canary.run_daily(
        day=day, store=store, observer=observer, clock=_Clock(day), reports_dir=tmp_path / "reports",
        failure_sender=lambda text: True, send_weekly=False, max_attempts_per_day=4,
    )


def test_canary_wakes_a_waiting_grill_and_is_idempotent(tmp_path, line_store) -> None:
    observer = _Observer("waiting_human")
    first = _canary_iteration(line_store, tmp_path, observer)
    assert first.outcome == "in_progress" and first.run_id
    # Attempt 1's grill is still `pending` (never claimed): nothing to wake, nothing breaks.
    assert _job_status(line_store, first.run_id, "grill") == "pending"

    _set_job_status(line_store, first.run_id, "grill", "waiting_human")
    woken = _canary_iteration(line_store, tmp_path, observer)
    assert woken.run_id == first.run_id and "woken" in woken.notes
    assert _job_status(line_store, first.run_id, "grill") == "pending"

    again = _canary_iteration(line_store, tmp_path, observer)  # already pending: a fenced no-op
    assert again.run_id == first.run_id and again.notes == ""
    assert _job_status(line_store, first.run_id, "grill") == "pending"


def test_canary_does_not_wake_a_grill_that_is_not_waiting(tmp_path, line_store) -> None:
    observer = _Observer("running")
    report = _canary_iteration(line_store, tmp_path, observer)
    _set_job_status(line_store, report.run_id, "grill", "running")
    again = _canary_iteration(line_store, tmp_path, observer)
    assert again.notes == "" and _job_status(line_store, report.run_id, "grill") == "running"


def test_canary_wake_failure_never_breaks_the_iteration(monkeypatch, tmp_path, line_store) -> None:
    def _boom(*a, **k):
        raise RuntimeError("store hiccup")

    monkeypatch.setattr(human, "resume_blocked_job", _boom)
    report = _canary_iteration(line_store, tmp_path, _Observer("waiting_human"))
    assert report.outcome == "in_progress" and report.notes == ""
