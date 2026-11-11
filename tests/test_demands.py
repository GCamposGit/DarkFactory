"""Comprehensive automated tests for the User Demands module and Backlog integration.

Covers:
- Pydantic v2 models and tag enforcements (user-demand distinction).
- Zero-credit guidance ($0.00) via heuristic script and local model fallback.
- Durable atomic persistence in DemandsStore.
- Headless business logic in DemandsService.
- Integration into the Operational Roadmap (UserDemandsRoadmapSource).
- DarkHub REST API endpoints (/api/demands/*).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core.demands.models import (
    DemandInput,
    DemandOrigin,
    DemandSpecificationGuidance,
    UserTicket,
    TAG_USER_DEMAND,
    TAG_CODE_REVIEW,
    TAG_AGENT_FEATURE,
)
from core.demands.specifier import DemandSpecifier, HeuristicDemandSpecifier
from core.demands.store import DemandsStore
from core.demands.service import DemandsService
from core.roadmap.compiler import RoadmapCompiler
from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType
from core.roadmap.sources import UserDemandsRoadmapSource
from core.workflow.control_contracts import StoreUnavailableError
from hub.backend.main import app


@pytest.fixture
def temp_demands_env():
    """Provides an isolated DemandsStore and DemandsService in a temporary directory."""
    temp_dir = Path(tempfile.mkdtemp(prefix="darkfac_demands_test_"))
    demands_path = temp_dir / "demands.json"
    store = DemandsStore(demands_path)
    specifier = DemandSpecifier(heuristic_specifier=HeuristicDemandSpecifier())
    service = DemandsService(store=store, specifier=specifier)
    yield {
        "dir": temp_dir,
        "path": demands_path,
        "store": store,
        "specifier": specifier,
        "service": service,
    }
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_shared_checkout_ticket_writes_leave_git_clean(tmp_path, monkeypatch):
    """USR-415: create/update from a shared checkout go to the control store, never to tracked files."""
    import subprocess

    repo = tmp_path / "repo"
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    monkeypatch.delenv("DARKFAC_DEMANDS_PATH", raising=False)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo))
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.chdir(repo)

    store = DemandsStore(ledger)
    store.save_ticket(UserTicket(id="USR-9001", project_id="darkfac", title="Controle", origin=DemandOrigin.USER))
    store.update_status("USR-9001", DeliveryStatus.WAITING_HUMAN)

    assert store.get_ticket("USR-9001").status == DeliveryStatus.WAITING_HUMAN
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""
    ledger.unlink()
    DemandsStore()
    assert not ledger.exists()


def test_user_demand_tag_enforcement():
    """Verify that UserTicket always enforces the user-demand tag and preserves classification."""
    # Even if tags are initially empty, user-demand must be injected
    ticket = UserTicket(
        id="USR-01",
        project_id="darkfac",
        title="Implementar exportação CSV",
        origin=DemandOrigin.USER,
        tags=["feature", "dashboard"],
    )
    assert TAG_USER_DEMAND in ticket.tags
    assert ticket.tags[0] == TAG_USER_DEMAND
    assert TAG_CODE_REVIEW not in ticket.tags
    assert TAG_AGENT_FEATURE not in ticket.tags
    assert ticket.origin == DemandOrigin.USER


def test_heuristic_specifier_offline_zero_cost():
    """Verify heuristic specifier runs deterministically with 0 tokens and $0 cost."""
    specifier = HeuristicDemandSpecifier()
    inp = DemandInput(
        project_id="darkfac",
        title="Adicionar suporte a exportação de métricas",
        problem_statement="Os clientes não conseguem consolidar métricas fora do painel",
        core_journey="1. Usuário acessa aba de métricas\n2. Clica em exportar\n3. Recebe JSON/CSV",
        non_goals=["Não suportar formatos legados como XML"],
        acceptance_criteria=["Endpoint /api/export retorna 200"],
    )

    guidance = specifier.analyze(inp, ticket_id="USR-01")
    assert isinstance(guidance, DemandSpecificationGuidance)
    assert guidance.cost_usd == 0.0
    assert guidance.engine_used == "heuristic_script"
    assert guidance.readiness_score >= 70
    assert guidance.is_ready is True
    assert guidance.suggested_ticket is not None
    assert guidance.suggested_ticket.id == "USR-01"
    assert TAG_USER_DEMAND in guidance.suggested_ticket.tags
    assert guidance.suggested_ticket.reachability_contract != ""


def test_heuristic_specifier_detects_missing_elements():
    """Verify that vague inputs are penalized and actionable feedback is returned."""
    specifier = HeuristicDemandSpecifier()
    vague_input = DemandInput(
        title="fix",
        problem_statement="",
        core_journey="",
    )

    guidance = specifier.analyze(vague_input, ticket_id="USR-01")
    assert guidance.is_ready is False
    assert guidance.readiness_score < 60
    assert any("título" in m.lower() for m in guidance.missing_elements)
    assert any("problema" in m.lower() for m in guidance.missing_elements)
    assert any("non-goal" in m.lower() for m in guidance.missing_elements)


def test_demands_store_persistence(temp_demands_env):
    """Verify atomic storage, ID generation, retrieval, and status updates."""
    store: DemandsStore = temp_demands_env["store"]

    assert store.next_ticket_id() == "USR-01"

    t1 = UserTicket(
        id="USR-01",
        project_id="darkfac",
        title="Demanda de teste 1",
        problem_statement="Problema 1",
        acceptance_criteria=["Critério 1"],
    )
    store.save_ticket(t1)

    assert store.next_ticket_id() == "USR-02"
    all_tickets = store.list_tickets()
    assert len(all_tickets) == 1
    assert all_tickets[0].id == "USR-01"
    assert all_tickets[0].title == "Demanda de teste 1"

    # Status update
    updated = store.update_status("USR-01", DeliveryStatus.IMPLEMENTING)
    assert updated.status == DeliveryStatus.IMPLEMENTING
    reloaded = store.get_ticket("USR-01")
    assert reloaded is not None
    assert reloaded.status == DeliveryStatus.IMPLEMENTING


def test_demands_service_headless(temp_demands_env):
    """Verify high-level DemandsService operations."""
    service: DemandsService = temp_demands_env["service"]

    inp = DemandInput(
        project_id="darkfac",
        title="Criar endpoint de status operacional",
        problem_statement="Monitoramento externo necessita checar saúde dos módulos",
    )
    guidance = service.guide_demand(inp, force_heuristic=True)
    assert guidance.suggested_ticket is not None

    created = service.create_ticket(guidance.suggested_ticket)
    assert created.id == "USR-01"
    assert TAG_USER_DEMAND in created.tags

    listed = service.list_tickets(project_id="darkfac")
    assert len(listed) == 1
    assert listed[0].id == "USR-01"

    updated = service.update_ticket_status("USR-01", DeliveryStatus.VALIDATING)
    assert updated.status == DeliveryStatus.VALIDATING


def test_roadmap_integration_with_user_demands(temp_demands_env):
    """Verify that UserDemandsRoadmapSource compiles user tickets into the roadmap snapshot."""
    store: DemandsStore = temp_demands_env["store"]
    path: Path = temp_demands_env["path"]

    # Add a user demand ticket
    store.save_ticket(
        UserTicket(
            id="USR-01",
            project_id="darkfac",
            title="Funcionalidade solicitada pelo usuário",
            problem_statement="Permitir download de logs",
            core_journey=["Acessar", "Baixar"],
            non_goals=["Não incluir credenciais"],
            status=DeliveryStatus.PLANNED,
            horizon=PlanningHorizon.NOW,
            acceptance_criteria=["Logs exportados em UTF-8"],
            tags=["user-demand", "logs"],
        )
    )

    source = UserDemandsRoadmapSource(path)
    result = source.read("darkfac")
    assert result.state.status == "available"
    assert len(result.records) == 1
    candidate = result.records[0]
    assert candidate.id == "USR-01"
    assert "user-demand" in candidate.tags
    assert candidate.source_id == "user-demands"

    # Compile with RoadmapCompiler
    compiler = RoadmapCompiler([source])
    snapshot = compiler.compile("darkfac")
    assert len(snapshot.items) == 1
    item = snapshot.items[0]
    assert item.id == "USR-01"
    assert "user-demand" in item.tags
    assert any(ref.produced_by == "user" for ref in item.source_refs)


from hub.backend.api import get_hub_service
from hub.backend.service import HubService


def test_hub_api_demands_endpoints(temp_demands_env):
    """Verify REST API routes /api/demands/* via FastAPI TestClient in isolated environment."""
    test_hub_service = HubService(data_dir=temp_demands_env["dir"])
    app.dependency_overrides[get_hub_service] = lambda: test_hub_service

    try:
        client = TestClient(app)

        # 0. Next ID endpoint
        next_resp = client.get("/api/demands/next-id?project_id=darkfac")
        assert next_resp.status_code == 200
        assert next_resp.json()["next_id"] == "USR-01"

        # 1. Guide demand endpoint
        guide_payload = {
            "project_id": "darkfac",
            "title": "Implementar webhook de notificações",
            "problem_statement": "Usuário precisa receber alertas no Discord",
            "core_journey": "Evento ocorre -> webhook é disparado",
            "non_goals": ["Não suportar SMS neste ciclo"],
            "acceptance_criteria": ["Webhook entrega payload JSON válido"],
            "horizon": "now",
            "item_type": "feature",
            "extra_tags": ["discord", "alerts"],
            "suggested_files": ["core/webhook/service.py"],
            "reachability_contract": "python -m pytest tests/test_webhook.py -v",
        }
        resp = client.post("/api/demands/guide?force_heuristic=true", json=guide_payload)
        assert resp.status_code == 200
        guidance = resp.json()
        assert guidance["cost_usd"] == 0.0
        assert guidance["suggested_ticket"] is not None
        assert TAG_USER_DEMAND in guidance["suggested_ticket"]["tags"]

        suggested = guidance["suggested_ticket"]

        # 2. Create demand ticket endpoint
        create_resp = client.post("/api/demands/tickets", json=suggested)
        assert create_resp.status_code == 201
        created_ticket = create_resp.json()
        ticket_id = created_ticket["id"]
        assert TAG_USER_DEMAND in created_ticket["tags"]

        # 3. List demand tickets endpoint
        list_resp = client.get("/api/demands/tickets?project_id=darkfac")
        assert list_resp.status_code == 200
        tickets = list_resp.json()
        assert any(t["id"] == ticket_id for t in tickets)

        # 4. Get demand ticket endpoint
        get_resp = client.get(f"/api/demands/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == ticket_id

        # 5. Update ticket status endpoint
        patch_resp = client.patch(f"/api/demands/tickets/{ticket_id}/status?status=implementing")
        assert patch_resp.status_code == 200
        assert patch_resp.json()["status"] == "implementing"

        # 6. Completion needs delivery evidence: a clean 422, not a server error (USR-94)
        refused = client.patch(f"/api/demands/tickets/{ticket_id}/status?status=completed")
        assert refused.status_code == 422
        assert "delivery_evidence" in refused.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def queue_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Control store isolated from the shared checkout and from Postgres."""
    state = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKFAC_DEMANDS_PATH", raising=False)
    from core.demands import queue_store

    queue_store._store.cache_clear()
    yield {"state": state, "queue": queue_store, "root": tmp_path}
    queue_store._store.cache_clear()


def _queued_ticket(
    ticket_id: str,
    *,
    title: str | None = None,
    status: DeliveryStatus = DeliveryStatus.PLANNED,
    delivery_evidence: str | None = None,
) -> UserTicket:
    return UserTicket(
        id=ticket_id,
        project_id="darkfac",
        title=title or f"Ticket {ticket_id}",
        problem_statement="registrado so na fila duravel",
        status=status,
        delivery_evidence=delivery_evidence,
    )


def _ledger_ids(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [row["id"] for row in raw]


def _forbid_next_ticket_id(*_args: object, **_kwargs: object) -> str:
    raise AssertionError("next_ticket_id must not run while resolving a queued ticket")


def _linked_worktree_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Worktree ledger: default demands.json path, but ``.git`` is a file (not control-only)."""
    repo = tmp_path / "wt"
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    (repo / ".git").write_text("gitdir: /unused\n", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo))
    return ledger


def test_queue_get_ticket_reads_control_store_or_none(queue_env) -> None:
    """USR-424: get_ticket returns the queued UserTicket, or None without creating the store."""
    queue = queue_env["queue"]
    state = queue_env["state"]

    assert queue.get_ticket("USR-404") is None
    assert queue.get_ticket("") is None
    assert not (state / "control.db").exists()

    queued = _queued_ticket("USR-413", title="Veio da fila", status=DeliveryStatus.IMPLEMENTING)
    queue.register_ticket(queued)

    found = queue.get_ticket("USR-413")
    assert found is not None
    assert found.id == "USR-413"
    assert found.title == "Veio da fila"
    assert found.status == DeliveryStatus.IMPLEMENTING
    assert queue.get_ticket("USR-404") is None
    assert [ticket.id for ticket in queue.list_queue_tickets()] == ["USR-413"]


def test_worktree_resolve_writes_the_same_id_once(queue_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worktree ledger gains one row with the queue id; a second call does not allocate."""
    queue = queue_env["queue"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    queued = _queued_ticket("USR-413", title="Veio da fila", status=DeliveryStatus.IMPLEMENTING)
    queue.register_ticket(queued)

    store = DemandsStore(ledger)
    assert store._control_only() is False
    monkeypatch.setattr(store, "next_ticket_id", _forbid_next_ticket_id)

    materialized = store.materialize_ticket("USR-413")
    assert materialized is not None
    assert materialized.id == "USR-413"
    assert materialized.title == "Veio da fila"
    assert _ledger_ids(ledger) == ["USR-413"]
    written = ledger.read_bytes()

    resolved = store.resolve_for_delivery("USR-413")
    assert resolved is not None
    assert resolved.id == "USR-413"
    assert ledger.read_bytes() == written
    assert _ledger_ids(ledger) == ["USR-413"]
    assert [ticket.id for ticket in queue.list_queue_tickets()] == ["USR-413"]

    assert store.materialize_ticket("USR-404") is None
    assert _ledger_ids(ledger) == ["USR-413"]
    assert [ticket.id for ticket in queue.list_queue_tickets()] == ["USR-413"]


def test_open_local_row_is_updated_in_place_from_the_queue(queue_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-terminal file row with the same id is replaced by the queue ticket, still one line."""
    queue = queue_env["queue"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    store = DemandsStore(ledger)
    store.save_ticket(_queued_ticket("USR-413", title="Rascunho", status=DeliveryStatus.PLANNED))
    queue.register_ticket(_queued_ticket("USR-413", title="Veio da fila", status=DeliveryStatus.IMPLEMENTING))
    monkeypatch.setattr(store, "next_ticket_id", _forbid_next_ticket_id)

    resolved = store.resolve_for_delivery("USR-413")
    assert resolved is not None
    assert resolved.id == "USR-413"
    assert resolved.title == "Veio da fila"
    assert resolved.status == DeliveryStatus.IMPLEMENTING
    assert _ledger_ids(ledger) == ["USR-413"]

    again = store.materialize_ticket("USR-413")
    assert again is not None and again.id == "USR-413"
    assert _ledger_ids(ledger) == ["USR-413"]


@pytest.mark.parametrize(
    ("status", "title", "evidence"),
    [
        (DeliveryStatus.COMPLETED, "Ja entregue", "sha256:abc"),
        (DeliveryStatus.CANCELLED, "Cancelado", None),
    ],
)
def test_terminal_local_row_is_not_replaced_by_open_queue(
    queue_env, monkeypatch: pytest.MonkeyPatch, status: DeliveryStatus, title: str, evidence: str | None
) -> None:
    """A completed or cancelled ledger row stays when the durable queue is still open."""
    queue = queue_env["queue"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    store = DemandsStore(ledger)
    store.save_ticket(
        _queued_ticket("USR-413", title=title, status=status, delivery_evidence=evidence)
    )
    before = ledger.read_bytes()
    queue.register_ticket(_queued_ticket("USR-413", title="Ainda na fila", status=DeliveryStatus.PLANNED))
    monkeypatch.setattr(store, "next_ticket_id", _forbid_next_ticket_id)

    resolved = store.resolve_for_delivery("USR-413")
    assert resolved is not None
    assert resolved.status == status
    assert resolved.title == title
    assert resolved.delivery_evidence == evidence
    assert ledger.read_bytes() == before
    assert json.loads(ledger.read_text(encoding="utf-8"))[0]["status"] == status.value
    assert queue.get_ticket("USR-413") is not None
    assert queue.get_ticket("USR-413").status == DeliveryStatus.PLANNED


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_terminal_row_without_timestamps_is_not_rewritten(
    queue_env, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """A second parse must not refill created_at and then close the open queue row."""
    from datetime import datetime, timezone

    import core.common.time as time_mod

    class _Clock:
        n = 0

        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            cls.n += 1
            return datetime(2020, 1, 1, 0, 0, cls.n, tzinfo=timezone.utc)

    monkeypatch.setattr(time_mod, "datetime", _Clock)
    queue = queue_env["queue"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    row: dict[str, object] = {
        "id": "USR-413",
        "title": "Ja fechado",
        "status": status,
        "problem_statement": "linha local terminal sem datas",
    }
    if status == "completed":
        row["delivery_evidence"] = "sha256:abc"
    payload = [
        {"id": "USR-1", "title": "Vizinho", "status": "planned", "problem_statement": "outra linha"},
        row,
    ]
    ledger.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = ledger.read_bytes()
    queue.register_ticket(_queued_ticket("USR-413", title="Ainda na fila", status=DeliveryStatus.PLANNED))

    store = DemandsStore(ledger)
    monkeypatch.setattr(store, "next_ticket_id", _forbid_next_ticket_id)
    ticks_before = _Clock.n
    resolved = store.materialize_ticket("USR-413")
    assert _Clock.n > ticks_before

    assert resolved is not None
    assert resolved.status.value == status
    assert resolved.title == "Ja fechado"
    assert ledger.read_bytes() == before
    assert _ledger_ids(ledger) == ["USR-1", "USR-413"]
    queued = queue.get_ticket("USR-413")
    assert queued is not None and queued.status == DeliveryStatus.PLANNED
    assert queued.title == "Ainda na fila"


def test_shared_checkout_materialize_does_not_rewrite_demands_json(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-415: materializing on the shared checkout leaves the tracked ledger untouched."""
    import subprocess

    queue = queue_env["queue"]
    repo = queue_env["root"] / "repo"
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo))
    monkeypatch.chdir(repo)

    store = DemandsStore(ledger)
    assert store._control_only() is True
    before = ledger.read_bytes()
    assert store.resolve_for_delivery("USR-NOPE") is None
    assert ledger.read_bytes() == before
    assert not (queue_env["state"] / "control.db").exists()

    queued = _queued_ticket("USR-9001", title="So na fila")
    queue.register_ticket(queued)
    monkeypatch.setattr(store, "next_ticket_id", _forbid_next_ticket_id)

    found = store.materialize_ticket("USR-9001")
    again = store.resolve_for_delivery("USR-9001")
    assert found is not None and found.id == "USR-9001"
    assert again is not None and again.id == "USR-9001"
    assert again.title == "So na fila"
    assert ledger.read_bytes() == before
    assert [ticket.id for ticket in queue.list_queue_tickets()] == ["USR-9001"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True)
    assert status.stdout.strip() == ""


def _queue_pairs(state: Path) -> list[tuple[str, str, str]]:
    import sqlite3

    db = state / "control.db"
    if not db.is_file():
        return []
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(
            "SELECT project_id, ticket_id, status FROM tickets ORDER BY project_id, ticket_id"
        ))
    finally:
        conn.close()


def test_worktree_save_updates_existing_queue_pair_only(queue_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree save updates (project_id, ticket_id) and does not insert a file-only ticket."""
    queue = queue_env["queue"]
    state = queue_env["state"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    store = DemandsStore(ledger)
    assert store._inserts_missing_queue_rows() is False

    store.save_ticket(_queued_ticket("USR-500", title="So no arquivo", status=DeliveryStatus.PLANNED))
    assert not (state / "control.db").exists()
    assert queue.get_ticket("USR-500") is None
    assert _ledger_ids(ledger) == ["USR-500"]

    queue.register_ticket(_queued_ticket("USR-413", title="Na fila", status=DeliveryStatus.PLANNED))
    store.save_ticket(_queued_ticket("USR-413", title="Atualizado na worktree", status=DeliveryStatus.IMPLEMENTING))

    found = queue.get_ticket("USR-413")
    assert found is not None
    assert found.id == "USR-413"
    assert found.project_id == "darkfac"
    assert found.title == "Atualizado na worktree"
    assert found.status == DeliveryStatus.IMPLEMENTING
    assert _queue_pairs(state) == [("darkfac", "USR-413", "implementing")]
    assert _ledger_ids(ledger) == ["USR-500", "USR-413"]

    store.save_ticket(_queued_ticket("USR-413", title="Outro projeto", status=DeliveryStatus.VALIDATING).model_copy(
        update={"project_id": "other"}
    ))
    assert _queue_pairs(state) == [("darkfac", "USR-413", "implementing")]
    assert queue.get_ticket("USR-413") is not None
    assert queue.get_ticket("USR-413").title == "Atualizado na worktree"
    assert queue.get_ticket("USR-500") is None


def test_demands_path_override_stays_file_only(queue_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """DARKFAC_DEMANDS_PATH writes the file and does not create or update the queue."""
    queue = queue_env["queue"]
    state = queue_env["state"]
    ledger = _linked_worktree_ledger(queue_env["root"], monkeypatch)
    monkeypatch.setenv("DARKFAC_DEMANDS_PATH", str(queue_env["root"] / "override.json"))
    store = DemandsStore(ledger)

    store.save_ticket(_queued_ticket("USR-500", title="So no arquivo"))
    assert not (state / "control.db").exists()
    assert _ledger_ids(ledger) == ["USR-500"]

    queue.register_ticket(_queued_ticket("USR-413", title="Na fila", status=DeliveryStatus.PLANNED))
    store.save_ticket(_queued_ticket("USR-413", title="Nao pode espelhar", status=DeliveryStatus.IMPLEMENTING))
    store.save_ticket(_queued_ticket("USR-501", title="Outro so no arquivo"))

    found = queue.get_ticket("USR-413")
    assert found is not None
    assert found.title == "Na fila"
    assert found.status == DeliveryStatus.PLANNED
    assert queue.get_ticket("USR-500") is None
    assert queue.get_ticket("USR-501") is None
    assert _queue_pairs(state) == [("darkfac", "USR-413", "planned")]
    assert _ledger_ids(ledger) == ["USR-500", "USR-413", "USR-501"]


def _default_ledger_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Shared-checkout-shaped repo (real ``.git`` dir): matches the Hub/CLI default ledger path
    so reads merge the control store, the same condition the Hub route depends on."""
    import subprocess

    repo = tmp_path / "repo"
    ledger = repo / ".factory" / "demands" / "demands.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("[]", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo))
    return ledger


class _DemandsOnlyHubService:
    """Minimal stand-in exposing just what the two demand-ticket routes call on HubService.

    Failure tests must not stop here: ``HubService`` can grow its own try/except. Use
    ``_real_hub_for_ledger`` for store-down cases (USR-449).
    """

    def __init__(self, demands_service: DemandsService) -> None:
        self._demands_service = demands_service

    def get_demand_ticket(self, ticket_id: str):
        return self._demands_service.get_ticket(ticket_id)

    def list_demand_tickets(self, project_id=None, status=None):
        stat_enum = DeliveryStatus(status) if status else None
        return self._demands_service.list_tickets(project_id=project_id, status=stat_enum)


def _real_hub_for_ledger(ledger: Path) -> HubService:
    """Production HubService whose demands file is the default ledger, so reads merge the queue."""
    repo = ledger.parents[2]
    service = HubService(data_dir=repo / ".factory", project_root=repo)
    assert service.demands_store.path.resolve() == ledger.resolve()
    return service


def _refuse_psycopg(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Install a ``psycopg`` whose ``connect`` fails immediately.

    ``PostgresControlStore`` imports psycopg inside the constructor. Replacing the module
    exercises that dial on machines where the optional package is absent, and never opens a
    socket or waits on a connect timeout.
    """
    import sys
    import types

    seen: dict[str, object] = {}

    def _refuse(url: str, **kwargs: object) -> None:
        seen["url"] = url
        seen["connect_timeout"] = kwargs.get("connect_timeout")
        raise RuntimeError("connection refused (simulado)")

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=_refuse))
    return seen


def test_demand_lookup_reads_control_store_without_file_snapshot(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-449 T1: a ticket registered only in the control store (never written to demands.json,
    no snapshot wait) is readable through DemandsService.get_ticket in the same round -- the exact
    read path GET /api/demands/tickets/<id> uses."""
    queue = queue_env["queue"]
    ledger = _default_ledger_repo(queue_env["root"], monkeypatch)

    queue.register_ticket(_queued_ticket("USR-449", title="So na fila duravel", status=DeliveryStatus.PLANNED))

    service = DemandsService(store=DemandsStore(ledger))
    found = service.get_ticket("USR-449")
    assert found is not None
    assert found.title == "So na fila duravel"
    assert _ledger_ids(ledger) == []  # never materialized into the file snapshot by a plain read

    assert service.get_ticket("USR-000-NUNCA-EXISTIU") is None


def test_hub_api_get_demand_ticket_200_for_queue_only_404_only_for_absent(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-449 T1: GET /api/demands/tickets/<id> is 200 for a control-store-only ticket and 404
    only for an id genuinely absent from both the file and the store."""
    queue = queue_env["queue"]
    ledger = _default_ledger_repo(queue_env["root"], monkeypatch)
    queue.register_ticket(_queued_ticket("USR-449", title="Na fila, nao no arquivo"))

    stub_service = _DemandsOnlyHubService(DemandsService(store=DemandsStore(ledger)))
    app.dependency_overrides[get_hub_service] = lambda: stub_service
    try:
        client = TestClient(app)
        ok = client.get("/api/demands/tickets/USR-449")
        assert ok.status_code == 200
        assert ok.json()["id"] == "USR-449"

        missing = client.get("/api/demands/tickets/USR-000-NUNCA-EXISTIU")
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_hub_api_get_demand_ticket_store_failure_is_503_not_404(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control store that is configured but errors on read must surface as 503, never a false
    404 and never a quiet fallback that looks like a normal (empty) answer (USR-449).

    The route is served by the real ``HubService``, not a stand-in that only forwards the call.
    """
    queue = queue_env["queue"]
    ledger = _default_ledger_repo(queue_env["root"], monkeypatch)
    queue.register_ticket(_queued_ticket("USR-449", title="Ticket real"))
    service = _real_hub_for_ledger(ledger)

    def _boom():
        raise StoreUnavailableError("Postgres unreachable (simulado)")

    monkeypatch.setattr(queue, "control_store", _boom)
    with pytest.raises(StoreUnavailableError):
        service.get_demand_ticket("USR-449")

    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        resp = client.get("/api/demands/tickets/USR-449")
        assert resp.status_code == 503
        assert "store" in resp.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_list_queue_tickets_raises_on_store_failure_but_empty_when_never_configured(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that genuinely does not exist yet reads as ``[]``; once it exists, a read failure
    raises StoreUnavailableError instead of being swallowed into that same empty result (USR-449)."""
    queue = queue_env["queue"]
    state = queue_env["state"]

    assert queue.list_queue_tickets() == []
    assert not (state / "control.db").exists()

    queue.register_ticket(_queued_ticket("USR-9100"))
    assert (state / "control.db").exists()

    def _boom():
        raise RuntimeError("disco cheio (simulado)")

    monkeypatch.setattr(queue, "control_store", _boom)
    with pytest.raises(StoreUnavailableError):
        queue.list_queue_tickets()


def test_queue_store_control_url_honors_hub_env_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """USR-449: queue_store resolves the control-store URL through the same precedence
    core.line.owner_intake.line_database_url uses (DARKHUB_LINE_DATABASE_URL /
    DARKFAC_HF02_DATABASE_URL / DARKHUB_CONTROL_DATABASE_URL), not only DARKFAC_HF02_DATABASE_URL --
    that mismatch is what pointed the Hub's demand lookups at an empty local SQLite file instead
    of the coordinator/worker's Postgres, even though DARKHUB_CONTROL_DATABASE_URL was set."""
    from core.demands import queue_store
    import core.line.owner_intake as owner_intake

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.setenv("DARKHUB_CONTROL_DATABASE_URL", "postgresql://hub-only-role@host/db")
    assert owner_intake.line_database_url() == "postgresql://hub-only-role@host/db"

    captured: dict[str, object] = {}

    def _fake_default_control_store(*, sqlite_db_path=None, database_url=None):
        captured["database_url"] = database_url
        return object()

    monkeypatch.setattr("core.line.store_selection.default_control_store", _fake_default_control_store)
    queue_store._store.cache_clear()
    try:
        queue_store.control_store()
        assert captured["database_url"] == "postgresql://hub-only-role@host/db"
    finally:
        queue_store._store.cache_clear()


def test_default_control_store_rejects_silent_mock_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """USR-449: a configured (non-mock) Postgres URL the adapter cannot reach must raise, never
    hand back its in-memory mock as if it were the real, persistent store."""
    import core.orchestrator.adapters.control_postgres as control_postgres
    from core.line import store_selection

    class _AlwaysMock:
        def __init__(self, **_kwargs: object) -> None:
            self.mock_mode = True

    monkeypatch.setattr(control_postgres, "PostgresControlStore", _AlwaysMock)

    with pytest.raises(StoreUnavailableError):
        store_selection.default_control_store(database_url="postgresql://user:pass@unreachable-host/db")

    # The documented escape hatch (an explicit "mock:" URL) is still honored, not rejected.
    store = store_selection.default_control_store(database_url="mock://anything")
    assert store.mock_mode is True

    # A URL that only shares the "mock" prefix is not that hatch.
    with pytest.raises(StoreUnavailableError):
        store_selection.default_control_store(database_url="mockery://not-an-escape-hatch")


class _GenericListFailure:
    """Stand-in whose ``list_tickets`` fails with a plain exception, not StoreUnavailableError."""

    def list_tickets(self) -> list[object]:
        raise RuntimeError("disco cheio (simulado)")


def test_generic_list_tickets_failure_is_store_unavailable_and_http_503(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-449: ``control_store().list_tickets()`` raising a generic exception (not an already
    wrapped StoreUnavailableError) must be normalized. ``list_queue_tickets`` and ``get_ticket``
    propagate StoreUnavailableError, and both Hub reads answer 503 — never 200 or 404.

    Patching ``control_store`` to raise StoreUnavailableError itself does not cover the
    ``except Exception`` rewrap. A regression that swallowed the generic error into ``[]``
    would turn the known id into a false 404 and the list into an empty 200. The HTTP
    client is the real ``HubService``, so a try/except on that class cannot hide the 503.
    """
    queue = queue_env["queue"]
    ledger = _default_ledger_repo(queue_env["root"], monkeypatch)
    queue.register_ticket(_queued_ticket("USR-449", title="Ticket real"))
    service = _real_hub_for_ledger(ledger)
    monkeypatch.setattr(queue, "control_store", lambda: _GenericListFailure())

    with pytest.raises(StoreUnavailableError) as listed:
        queue.list_queue_tickets()
    assert isinstance(listed.value.__cause__, RuntimeError)

    with pytest.raises(StoreUnavailableError) as known:
        queue.get_ticket("USR-449")
    assert isinstance(known.value.__cause__, RuntimeError)

    with pytest.raises(StoreUnavailableError):
        queue.get_ticket("USR-000-NUNCA-EXISTIU")

    with pytest.raises(StoreUnavailableError) as via_hub:
        service.get_demand_ticket("USR-449")
    assert isinstance(via_hub.value.__cause__, RuntimeError)
    with pytest.raises(StoreUnavailableError):
        service.list_demand_tickets()

    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        one = client.get("/api/demands/tickets/USR-449")
        assert one.status_code == 503
        assert "store" in one.json()["detail"].lower()
        assert "disco cheio" in one.json()["detail"].lower()

        missing = client.get("/api/demands/tickets/USR-000-NUNCA-EXISTIU")
        assert missing.status_code == 503

        listing = client.get("/api/demands/tickets")
        assert listing.status_code == 503
        assert "store" in listing.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()


def test_default_control_store_mock_url_reaches_real_adapter() -> None:
    """USR-449: the resolved URL is what the real PostgresControlStore stores. A mock URL stays
    the in-memory escape hatch and does not open a network connection."""
    from core.line import store_selection
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = store_selection.default_control_store(database_url="mock://hub-resolved")
    assert type(store) is PostgresControlStore
    assert store.raw_url == "mock://hub-resolved"
    assert store.mock_mode is True


def test_real_postgres_invalid_url_is_store_unavailable_not_attribute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USR-449: the real PostgresControlStore exposes ``mock_mode``. A refused dial degrades to
    the in-memory mock inside the adapter; ``default_control_store`` must raise
    StoreUnavailableError, never AttributeError.

    ``psycopg.connect`` is replaced and raises at once. The test does not open a socket and
    does not wait on a connect timeout (port 99999 is not a CI oracle).
    """
    from core.line import store_selection
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    url = "postgresql://darkfac:pw@db.invalid/darkfac"
    seen = _refuse_psycopg(monkeypatch)
    raw = PostgresControlStore(
        database_url=url,
        runtime_owner="cloud_dbos_postgres",
        lease_duration_sec=300,
    )
    assert type(raw) is PostgresControlStore
    assert raw.mock_mode is True
    assert seen["url"] == url
    assert seen["connect_timeout"] == 5

    seen.clear()
    with pytest.raises(StoreUnavailableError) as caught:
        store_selection.default_control_store(database_url=url)
    assert type(caught.value) is StoreUnavailableError
    assert "mock" in str(caught.value).lower()
    assert seen["connect_timeout"] == 5


def test_default_control_store_missing_mock_mode_is_not_attribute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that does not publish ``mock_mode`` is returned, not crashed with AttributeError.
    The explicit getattr default is what keeps that miss from breaking the structured contract."""
    import core.orchestrator.adapters.control_postgres as control_postgres
    from core.line import store_selection

    class _NoMockMode:
        def __init__(self, **kwargs: object) -> None:
            self.raw_url = kwargs.get("database_url")

    monkeypatch.setattr(control_postgres, "PostgresControlStore", _NoMockMode)
    url = "postgresql://darkfac:pw@db.invalid/darkfac"
    store = store_selection.default_control_store(database_url=url)
    assert isinstance(store, _NoMockMode)
    assert store.raw_url == url


def test_postgres_schema_init_bounds_connect_then_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema bootstrap dials with connect_timeout=5. A refused connect still sets ``mock_mode``
    on the real adapter instead of hanging or raising AttributeError.

    The adapter imports psycopg inside the constructor. The test injects that module, so a
    missing optional install cannot skip the dial and pass on the ImportError fallback.
    """
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    seen = _refuse_psycopg(monkeypatch)
    url = "postgresql://darkfac:pw@db.invalid/darkfac"
    store = PostgresControlStore(database_url=url)
    assert type(store) is PostgresControlStore
    assert store.mock_mode is True
    assert seen["url"] == url
    assert seen["connect_timeout"] == 5


def test_mockery_prefix_is_not_a_mock_escape_hatch() -> None:
    """``mockery://`` starts with the letters mock, so the real adapter does not dial, but it is
    not the ``mock:`` escape. ``default_control_store`` raises instead of returning that mock."""
    from core.line import store_selection
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    url = "mockery://not-an-escape-hatch"
    raw = PostgresControlStore(database_url=url)
    assert type(raw) is PostgresControlStore
    assert raw.mock_mode is True
    assert raw.raw_url == url
    with pytest.raises(StoreUnavailableError):
        store_selection.default_control_store(database_url=url)


def test_real_hub_service_unreachable_configured_store_is_503_not_404(
    queue_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """USR-449 baseline: a line URL is set and the dial fails. ``list_queue_tickets`` surfaces
    ``StoreUnavailableError`` (the adapter degrades to mock; ``default_control_store`` refuses it).
    The real ``HubService`` does not swallow that, and both Hub reads answer 503 — never 200 or 404.

    The dial is injected. No socket is opened and nothing waits on a connect timeout.
    """
    ledger = _default_ledger_repo(queue_env["root"], monkeypatch)
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKHUB_CONTROL_DATABASE_URL", raising=False)
    url = "postgresql://darkfac:pw@db.invalid/darkfac"
    monkeypatch.setenv("DARKHUB_LINE_DATABASE_URL", url)
    queue_env["queue"]._store.cache_clear()
    seen = _refuse_psycopg(monkeypatch)
    service = _real_hub_for_ledger(ledger)

    with pytest.raises(StoreUnavailableError):
        service.get_demand_ticket("USR-449")
    with pytest.raises(StoreUnavailableError):
        service.list_demand_tickets()
    assert seen["url"] == url
    assert seen["connect_timeout"] == 5

    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        client = TestClient(app)
        one = client.get("/api/demands/tickets/USR-449")
        assert one.status_code == 503
        assert "store" in one.json()["detail"].lower()

        missing = client.get("/api/demands/tickets/USR-000-NUNCA-EXISTIU")
        assert missing.status_code == 503

        listing = client.get("/api/demands/tickets")
        assert listing.status_code == 503
        assert "store" in listing.json()["detail"].lower()
    finally:
        app.dependency_overrides.clear()
        queue_env["queue"]._store.cache_clear()
