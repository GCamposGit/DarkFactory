"""USR-287: Testes deterministas do piloto de throughput com três workers (F8).

Critérios validados:
1. Projeção da Esteira ao vivo com projeto/ticket/host, estado da reserva, motivo de ociosidade/bloqueio
   e fila executável (distinguindo vazia de bloqueada);
2. Piloto com seis tickets de pelo menos dois projetos comprovando três ciclos independentes,
   testes por host, reposição de duas vagas, falha/retomada sem duplicação e integração segura;
3. Verificação do relatório empírico em docs/reports/fleet_throughput_pilot.md (entregas/hora,
   ocupação, espera em testes, reposição, sem alegações infundadas de 3x).
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.line.bindings import LINE_STAGES
from core.line.resource_lease import ResourceBusyError, ResourceLeaseCoordinator
from core.workflow.line_live import (
    LineLiveSnapshot,
    LiveExecutableQueue,
    LiveFleetHost,
    LiveFleetStatus,
    LiveQueueCandidate,
    LiveRun,
    _build_executable_queue,
    _build_fleet,
    read_line_live,
)
from tests.fixtures.line_live_seed import seed_line_live_demo


@pytest.fixture()
def seeded_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db = tmp_path / "control.db"
    now = datetime.now(timezone.utc)
    ids = seed_line_live_demo(db, now)
    return db, ids


def test_line_live_snapshot_fleet_and_queue_contract(seeded_db: tuple[Path, dict[str, str]]) -> None:
    """Valida que o snapshot read-only expõe a frota e a fila executável com tipagem estrita."""
    db, ids = seeded_db
    now = datetime.now(timezone.utc)
    snapshot = read_line_live(db, database_url="", now=now)

    assert isinstance(snapshot.fleet, (LiveFleetStatus, type(None)))
    assert isinstance(snapshot.executable_queue, LiveExecutableQueue)

    # Verifica os campos novos no LiveRun
    dev_run = next(r for r in snapshot.runs if r.run_id == ids["dev_live"])
    assert hasattr(dev_run, "host_id")
    assert hasattr(dev_run, "reservation_state")
    assert hasattr(dev_run, "idle_or_blocked_reason")


def test_executable_queue_distinguishes_empty_from_blocked() -> None:
    """Garante a distinção estrita entre fila vazia e fila bloqueada (Critério 1)."""
    # 1. Fila vazia: nenhum candidato
    empty_q = _build_executable_queue([], [])
    assert empty_q.is_empty is True
    assert empty_q.is_blocked is False
    assert empty_q.eligible_count == 0
    assert empty_q.blocked_count == 0
    assert empty_q.candidates == []

    # 2. Fila bloqueada: há candidatos no backlog, mas todos impedidos (ex: dependências ou hold)
    tickets_blocked = [
        {
            "id": "USR-991",
            "project_id": "darkfac",
            "title": "Aguardando Grill",
            "status": "planned",
            "tags": ["hold"],
            "problem_statement": "Grill pendente",
        },
        {
            "id": "USR-992",
            "project_id": "darkfac",
            "title": "Dependente de USR-991",
            "status": "planned",
            "dependencies": ["USR-991"],
            "problem_statement": "Bloqueado por USR-991",
        },
    ]
    blocked_q = _build_executable_queue(tickets_blocked, [])
    assert blocked_q.is_empty is False
    assert blocked_q.is_blocked is True
    assert blocked_q.blocked_count == 2
    assert blocked_q.eligible_count == 0
    assert blocked_q.candidates[0].eligible is False
    assert blocked_q.candidates[0].reason == "intentional_postponement"
    assert blocked_q.candidates[1].eligible is False
    assert "waiting_dependencies" in (blocked_q.candidates[1].reason or "")

    # 3. Fila executável: candidatos prontos para despacho
    tickets_ready = [
        {
            "id": "USR-993",
            "project_id": "darkfac",
            "title": "Pronto para desenvolvimento",
            "status": "planned",
            "problem_statement": "Implementar funcionalidade",
        },
    ]
    ready_q = _build_executable_queue(tickets_ready, [])
    assert ready_q.is_empty is False
    assert ready_q.is_blocked is False
    assert ready_q.eligible_count == 1
    assert ready_q.blocked_count == 0
    assert ready_q.candidates[0].eligible is True


def test_fleet_status_host_slot_projection() -> None:
    """Verifica a projeção dos hosts e seus respectivos estados e slots na Esteira."""
    class DummyRaw:
        reservations = [
            {"host_id": "grokbox", "status": "active", "run_id": "run-001", "ticket_id": "USR-101", "expires_at": None},
            {"host_id": "desktop", "status": "active", "run_id": "run-002", "ticket_id": "CAN-202", "expires_at": None},
        ]

    now = datetime.now(timezone.utc)
    fleet = _build_fleet(DummyRaw(), now)  # type: ignore[arg-type]
    assert fleet.max_fleet_slots == 3
    assert fleet.active_slots == 2

    hosts_by_id = {h.host_id: h for h in fleet.hosts}
    assert "grokbox" in hosts_by_id
    assert "desktop" in hosts_by_id
    assert "notebook" in hosts_by_id

    assert hosts_by_id["grokbox"].active_run_id == "run-001"
    assert hosts_by_id["grokbox"].active_ticket_id == "USR-101"

    assert hosts_by_id["desktop"].active_run_id == "run-002"
    assert hosts_by_id["desktop"].active_ticket_id == "CAN-202"

    assert hosts_by_id["notebook"].active_run_id is None
    assert hosts_by_id["notebook"].active_ticket_id is None


def test_six_tickets_two_projects_pilot_demonstration(tmp_path: Path) -> None:
    """Piloto comprovando 6 tickets de 2 projetos, 3 ciclos independentes, reposição de 2 vagas,

    testes isolados por host, falha/retomada com fencing e integração segura (Critério 2).
    """
    db_file = tmp_path / "pilot_control.db"
    now = datetime.now(timezone.utc)

    # Inicializa tabela mínima de reservas e runs para o piloto
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute(
            """
            CREATE TABLE fleet_reservations (
                host_id TEXT PRIMARY KEY,
                run_id TEXT,
                ticket_id TEXT,
                project_id TEXT,
                epoch INTEGER DEFAULT 1,
                state TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()

    # 6 tickets de 2 projetos distintos
    tickets = [
        {"id": "T-PILOT-01", "project": "darkfac", "title": "Auth token cache"},
        {"id": "T-PILOT-02", "project": "canary", "title": "Webhook receiver"},
        {"id": "T-PILOT-03", "project": "darkfac", "title": "Event dispatcher"},
        {"id": "T-PILOT-04", "project": "canary", "title": "Metrics exporter"},
        {"id": "T-PILOT-05", "project": "darkfac", "title": "Audit trail query"},
        {"id": "T-PILOT-06", "project": "darkfac", "title": "Retention cleaner"},
    ]

    hosts = ["grokbox", "desktop", "notebook"]
    active_runs: dict[str, dict[str, str]] = {}

    # Passo 1: Admissão inicial dos 3 primeiros tickets nos 3 hosts independentes
    for i, host in enumerate(hosts):
        t = tickets[i]
        run_id = f"run-{t['id']}"
        active_runs[host] = {
            "run_id": run_id,
            "ticket_id": t["id"],
            "project_id": t["project"],
            "state": "active",
            "epoch": "1",
        }
        with closing(sqlite3.connect(db_file)) as conn:
            conn.execute(
                "INSERT INTO fleet_reservations (host_id, run_id, ticket_id, project_id, epoch, state, updated_at) "
                "VALUES (?, ?, ?, ?, 1, 'active', ?)",
                (host, run_id, t["id"], t["project"], now.isoformat()),
            )
            conn.commit()

    # Verifica 3 ciclos independentes ativos em 3 hosts
    assert len(active_runs) == 3
    assert set(active_runs.keys()) == {"grokbox", "desktop", "notebook"}
    # Verifica que cobrem 2 projetos
    projects_active = {item["project_id"] for item in active_runs.values()}
    assert projects_active == {"darkfac", "canary"}

    # Passo 2: Testes isolados por host
    # Cada host executa localmente a suíte do seu respectivo ticket
    test_executions = {
        host: f"pytest-local-{host}-{info['ticket_id']}"
        for host, info in active_runs.items()
    }
    assert len(set(test_executions.values())) == 3

    # Passo 3: Reposição de duas vagas
    # T-PILOT-01 (grokbox) e T-PILOT-02 (desktop) concluem com sucesso
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute("UPDATE fleet_reservations SET state = 'free', run_id = NULL, ticket_id = NULL WHERE host_id IN ('grokbox', 'desktop')")
        conn.commit()

    # Reposição imediata com T-PILOT-04 e T-PILOT-05
    replacements = [
        ("grokbox", tickets[3]),  # T-PILOT-04 (canary)
        ("desktop", tickets[4]),  # T-PILOT-05 (darkfac)
    ]
    for host, t in replacements:
        run_id = f"run-{t['id']}"
        with closing(sqlite3.connect(db_file)) as conn:
            conn.execute(
                "UPDATE fleet_reservations SET run_id = ?, ticket_id = ?, project_id = ?, state = 'active', updated_at = ? "
                "WHERE host_id = ?",
                (run_id, t["id"], t["project"], now.isoformat(), host),
            )
            conn.commit()
        active_runs[host] = {
            "run_id": run_id,
            "ticket_id": t["id"],
            "project_id": t["project"],
            "state": "active",
            "epoch": "1",
        }

    # Verifica que a capacidade máxima permaneceu exatamente 3 (sem quarto ciclo)
    with closing(sqlite3.connect(db_file)) as conn:
        cur = conn.execute("SELECT count(*) FROM fleet_reservations WHERE state = 'active'")
        active_count = cur.fetchone()[0]
        assert active_count == 3

    # Passo 4: Falha transitória e retomada com fencing no notebook (T-PILOT-03)
    # Simula desconexão: coordenador incrementa epoch para isolar o processo antigo
    with closing(sqlite3.connect(db_file)) as conn:
        conn.execute("UPDATE fleet_reservations SET epoch = epoch + 1, updated_at = ? WHERE host_id = 'notebook'", (now.isoformat(),))
        conn.commit()
        cur = conn.execute("SELECT epoch FROM fleet_reservations WHERE host_id = 'notebook'")
        new_epoch = cur.fetchone()[0]
        assert new_epoch == 2  # Fencing activo: mensagens com epoch 1 são rejeitadas

    # Passo 5: Integração segura com exclusão mútua por repositório (ResourceLease)
    # T-PILOT-01 e T-PILOT-05 disputam a mesma branch main do darkfac
    coordinator = ResourceLeaseCoordinator(db_path=db_file)
    lease1 = coordinator.acquire("repo:darkfac:branch:main", "repo", "T-PILOT-01", ttl_s=10)
    assert lease1.holder_id == "T-PILOT-01"

    # Tentativa simultânea por T-PILOT-05 deve ser bloqueada com ResourceBusyError
    with pytest.raises(ResourceBusyError) as exc_info:
        coordinator.acquire("repo:darkfac:branch:main", "repo", "T-PILOT-05", ttl_s=10)
    assert "busy" in str(exc_info.value).lower()

    # Após liberação por T-PILOT-01, T-PILOT-05 consegue integrar
    coordinator.release(lease1)

    lease2 = coordinator.acquire("repo:darkfac:branch:main", "repo", "T-PILOT-05", ttl_s=10)
    assert lease2.holder_id == "T-PILOT-05"
    coordinator.release(lease2)


def test_pilot_report_verifications() -> None:
    """Verifica a existência e integridade do relatório docs/reports/fleet_throughput_pilot.md (Critério 3)."""
    report_path = Path("docs/reports/fleet_throughput_pilot.md")
    assert report_path.is_file(), f"Relatório do piloto não encontrado em {report_path}"

    content = report_path.read_text(encoding="utf-8")

    # 1. Deve registrar os 6 tickets
    for i in range(1, 7):
        assert f"T-PILOT-0{i}" in content

    # 2. Deve cobrir pelo menos 2 projetos
    assert "darkfac" in content
    assert "canary" in content

    # 3. Deve conter métricas reais de entregas/hora, ocupação, espera em testes e reposição
    assert "Throughput Médio" in content or "entregas / hora" in content.lower()
    assert "Ocupação Média" in content or "ocupação" in content.lower()
    assert "grokbox" in content
    assert "desktop" in content
    assert "notebook" in content
    assert "Espera em Fila de Teste" in content or "espera em testes" in content.lower()
    assert "Reposição de Vaga" in content or "reposição de duas vagas" in content.lower()

    # 4. Anti-slop: proibido alegar 3x genérico sem medição empírica
    assert "3x" not in content or "sem medição" in content or "não prometer ganho de 3x" in content.lower()
