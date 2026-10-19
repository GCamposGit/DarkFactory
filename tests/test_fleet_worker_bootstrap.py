"""Tests for F7 (USR-286): Ativação da frota com um ciclo por host e drain.

Acceptance criteria:
1. Inventário verifica Grokbox Linux, Notebook e Desktop Windows, acesso ao controle
   e projetos e CLIs autenticados; serviços usam prioridade 1/2/3 e capacidade 1/1/1,
   sem fixar harness/modelo.
2. Bootstrap idempotente, início automático, drain e atualização segura preservam
   ciclos dos outros hosts; rollback suspende admissão e preserva worktrees e evidências.
3. python -m pytest tests/test_fleet_worker_bootstrap.py -q passa; registrar probes
   reais dos três hosts. Acesso exclusivo do owner vira OA completa; não delegar
   Git/deploy/backup ao owner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.start_fleet_worker import (
    FLEET_HOST_PRIORITIES,
    FLEET_PRIORITY_LABELS,
    HostInventory,
    WorkerLifecycleStatus,
    bootstrap_fleet_worker,
    discover_local_host_id,
    drain_fleet_worker,
    get_drain_file,
    get_pid_file,
    get_worker_status,
    is_pid_alive,
    probe_host_inventory,
    rollback_fleet_worker,
    stop_fleet_worker,
)


# ---------------------------------------------------------------------------
# Criterion 1: Three-host inventory, priority 1/2/3, capacity 1/1/1, unpinned
# ---------------------------------------------------------------------------


def test_inventory_verifies_three_hosts_topology(tmp_path: Path) -> None:
    """Inventário valida Grokbox Linux, Notebook Windows e Desktop Windows,
    com prioridade 1/2/3, capacidade 1/1/1 e sem fixação de harness/modelo."""

    # 1. Grokbox (Linux, Priority 1, Cap 1)
    grok_inv = probe_host_inventory(host_id="grokbox", repo_root=tmp_path)
    assert grok_inv.host_id == "grokbox"
    assert grok_inv.platform == "linux"
    assert grok_inv.priority == 1
    assert grok_inv.max_slots == 1
    assert "remote_linux" in grok_inv.tags

    # 2. Notebook (Windows, Priority 2, Cap 1)
    note_inv = probe_host_inventory(host_id="notebook", repo_root=tmp_path)
    assert note_inv.host_id == "notebook"
    assert note_inv.platform == "windows"
    assert note_inv.priority == 2
    assert note_inv.max_slots == 1

    # 3. Desktop (Windows, Priority 3, Cap 1)
    desk_inv = probe_host_inventory(host_id="desktop", repo_root=tmp_path)
    assert desk_inv.host_id == "desktop"
    assert desk_inv.platform == "windows"
    assert desk_inv.priority == 3
    assert desk_inv.max_slots == 1
    assert "test_worker" in desk_inv.tags

    # Tooling and harnesses must be dictionaries of booleans, not hardcoded strings
    assert isinstance(note_inv.tooling, dict)
    assert set(note_inv.tooling.keys()) == {"git", "gh", "node", "python"}
    assert isinstance(note_inv.harnesses, dict)
    assert set(note_inv.harnesses.keys()) == {"claude", "codex", "grok", "antigravity"}

    # No fixed model or harness pinned in the inventory
    inv_data = note_inv.model_dump()
    assert "pinned_harness" not in inv_data
    assert "fixed_model" not in inv_data


def test_priority_labels_match_fleet_design() -> None:
    """Verifica mapeamento de prioridade para a fila de execução."""
    assert FLEET_PRIORITY_LABELS["grokbox"] == "primary"
    assert FLEET_PRIORITY_LABELS["notebook"] == "secondary"
    assert FLEET_PRIORITY_LABELS["desktop"] == "fallback"

    assert FLEET_HOST_PRIORITIES["grokbox"] == 1
    assert FLEET_HOST_PRIORITIES["notebook"] == 2
    assert FLEET_HOST_PRIORITIES["desktop"] == 3


# ---------------------------------------------------------------------------
# Criterion 2: Idempotent bootstrap, drain, rolling update, rollback
# ---------------------------------------------------------------------------


def test_bootstrap_idempotency_prevents_duplicate_workers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bootstrap de um worker já em execução é idempotente e não inicia duplicatas."""
    monkeypatch.setattr("scripts.start_fleet_worker._PIDS_DIR", tmp_path / "pids")
    (tmp_path / "pids").mkdir(parents=True, exist_ok=True)

    # Simulate already running worker with alive PID 99999
    pid_file = tmp_path / "pids" / "fleet_worker_notebook.pid"
    pid_file.write_text("99999", encoding="utf-8")

    monkeypatch.setattr("scripts.start_fleet_worker.is_pid_alive", lambda pid: pid == 99999)

    status = get_worker_status("notebook")
    assert status.running is True
    assert status.pid == 99999

    # Re-running bootstrap returns already_running
    res = bootstrap_fleet_worker("notebook")
    assert res["status"] == "already_running"
    assert res["pid"] == 99999
    assert res["max_slots"] == 1
    assert res["priority"] == 2


def test_drain_worker_creates_signal_and_preserves_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Drain sinaliza interrupção de novas admissões permitindo término do ciclo atual."""
    monkeypatch.setattr("scripts.start_fleet_worker._PIDS_DIR", tmp_path / "pids")
    (tmp_path / "pids").mkdir(parents=True, exist_ok=True)

    # Simulate active worker
    pid_file = tmp_path / "pids" / "fleet_worker_grokbox.pid"
    pid_file.write_text("88888", encoding="utf-8")
    monkeypatch.setattr("scripts.start_fleet_worker.is_pid_alive", lambda pid: pid == 88888)

    drain_res = drain_fleet_worker("grokbox", reason="test_maintenance")
    assert drain_res["status"] == "draining"
    assert drain_res["host_id"] == "grokbox"
    assert drain_res["active_pid"] == 88888

    # Drain file exists and contains metadata
    drain_file = tmp_path / "pids" / "fleet_worker_grokbox.drain"
    assert drain_file.exists()
    content = json.loads(drain_file.read_text(encoding="utf-8"))
    assert content["reason"] == "test_maintenance"
    assert content["host_id"] == "grokbox"

    # Status reflects draining state while process remains running
    status = get_worker_status("grokbox")
    assert status.running is True
    assert status.draining is True


def test_safe_rolling_update_preserves_peer_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Atualização/drain de um host preserva os ciclos em execução nos demais hosts."""
    pids_dir = tmp_path / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("scripts.start_fleet_worker._PIDS_DIR", pids_dir)

    # 3 active hosts
    (pids_dir / "fleet_worker_grokbox.pid").write_text("101", encoding="utf-8")
    (pids_dir / "fleet_worker_notebook.pid").write_text("102", encoding="utf-8")
    (pids_dir / "fleet_worker_desktop.pid").write_text("103", encoding="utf-8")

    monkeypatch.setattr("scripts.start_fleet_worker.is_pid_alive", lambda pid: pid in (101, 102, 103))
    # The pids are fake: never signal whatever owns pid 101 on this host
    # (os.kill on POSIX, taskkill on Windows).
    sent: list[int] = []
    monkeypatch.setattr("scripts.start_fleet_worker.os.kill", lambda pid, sig: sent.append(pid))
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "taskkill":
            sent.append(int(cmd[cmd.index("/PID") + 1]))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("scripts.start_fleet_worker.subprocess.run", fake_run)

    # Drain and stop Grokbox for rolling update
    drain_fleet_worker("grokbox", reason="rolling_update_step_1")
    stop_fleet_worker("grokbox")
    assert sent == [101]

    # Grokbox is now stopped
    st_grok = get_worker_status("grokbox")
    assert st_grok.running is False

    # Peer hosts (Notebook and Desktop) remain active and undisturbed
    st_note = get_worker_status("notebook")
    assert st_note.running is True
    assert st_note.pid == 102
    assert st_note.draining is False

    st_desk = get_worker_status("desktop")
    assert st_desk.running is True
    assert st_desk.pid == 103
    assert st_desk.draining is False


def test_rollback_suspends_admission_and_preserves_worktrees_and_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rollback suspende novas admissões mas preserva worktrees e evidências intactas."""
    pids_dir = tmp_path / "pids"
    worktrees_dir = tmp_path / ".worktrees"
    evidence_dir = tmp_path / ".factory" / "test_logs"

    pids_dir.mkdir(parents=True, exist_ok=True)
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Seed mock worktree and mock evidence file
    mock_worktree = worktrees_dir / "usr-44"
    mock_worktree.mkdir()
    (mock_worktree / "code.py").write_text("print(1)", encoding="utf-8")

    mock_log = evidence_dir / "run_evidence.log"
    mock_log.write_text("test passed", encoding="utf-8")

    monkeypatch.setattr("scripts.start_fleet_worker._PIDS_DIR", pids_dir)
    monkeypatch.setattr("scripts.start_fleet_worker._REPO_ROOT", tmp_path)

    res = rollback_fleet_worker("desktop")
    assert res["status"] == "admission_suspended"
    assert res["host_id"] == "desktop"
    assert res["worktrees_preserved"] is True
    assert res["evidence_preserved"] is True

    # Check files are untouched
    assert (mock_worktree / "code.py").exists()
    assert mock_log.exists()

    # Drain signal is set
    drain_file = pids_dir / "fleet_worker_desktop.drain"
    assert drain_file.exists()


# ---------------------------------------------------------------------------
# Criterion 3: Probes recording & Owner Action generation (no manual Git/deploy/backup)
# ---------------------------------------------------------------------------


def test_structured_inventory_probes_recording(tmp_path: Path) -> None:
    """Probes estruturados dos três hosts produzem modelos válidos com timestamps e serialização."""
    for host_id in ("grokbox", "notebook", "desktop"):
        inv = probe_host_inventory(host_id=host_id, repo_root=tmp_path)
        assert isinstance(inv, HostInventory)
        assert inv.host_id == host_id
        assert inv.max_slots == 1
        assert inv.priority in (1, 2, 3)
        assert isinstance(inv.probed_at, datetime)
        assert inv.probed_at.tzinfo is not None

        # JSON round-trip
        data = json.loads(inv.model_dump_json())
        assert data["host_id"] == host_id
        assert data["max_slots"] == 1


def test_owner_action_generation_when_credentials_missing() -> None:
    """Acesso exclusivo do owner deve virar Owner Action completa; nunca delegar Git/deploy/backup."""
    from core.owner_actions.models import ActionStep, OwnerAction, OwnerActionDraft

    draft = OwnerActionDraft(
        title="Reautenticar harness Codex no Desktop",
        why="Acesso exclusivo do owner exigido para fluxo interativo de autenticacao OAuth",
        blocks=["USR-286"],
        steps=[
            ActionStep(text="No Desktop, abrir terminal PowerShell como Administrador", command="powershell"),
            ActionStep(text="Executar 'codex auth login'", command="codex auth login"),
            ActionStep(text="Concluir o fluxo OAuth no navegador aberto"),
        ],
        verify="codex auth status",
        created_by="antigravity",
    )
    action = draft.build("OA-099")

    assert isinstance(action, OwnerAction)
    assert action.status.value == "open"
    assert action.id == "OA-099"
    assert "codex auth login" in " ".join(s.text for s in action.steps)
    assert action.verify == "codex auth status"
    assert "USR-286" in action.blocks

    # Ensure no manual git, deploy or backup delegation is present in instructions
    forbidden_terms = ["git commit", "git push", "git merge", "dokploy_redeploy.py", "backup_cron"]
    all_text = " ".join(s.text + " " + (s.command or "") for s in action.steps)
    for term in forbidden_terms:
        assert term not in all_text


def test_fleet_worker_probe_database_status_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """database_connected é True quando DatabaseProbeResult.status == 'ready'."""
    from core.orchestrator.cloud_db import DatabaseProbeResult

    monkeypatch.setattr(
        "core.orchestrator.cloud_db.probe_cloud_database",
        lambda url=None: DatabaseProbeResult(
            status="ready",
            database_name="darkfac_hf02_prod",
            current_user="darkfac_worker",
            is_unprivileged=True,
        ),
    )

    inv = probe_host_inventory(
        host_id="notebook",
        database_url="postgresql://darkfac_worker:secret@localhost:5432/darkfac_hf02_prod",
        repo_root=tmp_path,
    )
    assert inv.database_connected is True
    assert inv.database_status == "ready"
    assert inv.database_diagnostic is None


def test_fleet_worker_probe_database_status_blocked_and_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """database_connected é False para status blocked, error ou exceção, com diagnóstico."""
    from core.orchestrator.cloud_db import DatabaseProbeResult

    # 1. status == blocked
    monkeypatch.setattr(
        "core.orchestrator.cloud_db.probe_cloud_database",
        lambda url=None: DatabaseProbeResult(
            status="blocked",
            error_message="superuser forbidden (waiting_access)",
        ),
    )
    inv_blocked = probe_host_inventory(host_id="notebook", repo_root=tmp_path)
    assert inv_blocked.database_connected is False
    assert inv_blocked.database_status == "blocked"
    assert inv_blocked.database_diagnostic == "superuser forbidden (waiting_access)"

    # 2. status == error
    monkeypatch.setattr(
        "core.orchestrator.cloud_db.probe_cloud_database",
        lambda url=None: DatabaseProbeResult(
            status="error",
            error_message="connection to 100.83.176.60:5432 failed: connection refused",
        ),
    )
    inv_error = probe_host_inventory(host_id="notebook", repo_root=tmp_path)
    assert inv_error.database_connected is False
    assert inv_error.database_status == "error"
    assert "connection refused" in (inv_error.database_diagnostic or "")

    # 3. exception raised by probe
    def _raise(url=None):
        raise ConnectionResetError("network unreachable")

    monkeypatch.setattr("core.orchestrator.cloud_db.probe_cloud_database", _raise)
    inv_exc = probe_host_inventory(host_id="notebook", repo_root=tmp_path)
    assert inv_exc.database_connected is False
    assert inv_exc.database_status == "error"
    assert "network unreachable" in (inv_exc.database_diagnostic or "")


def test_fleet_worker_probe_database_sanitizes_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DSN ou senha secreta nunca aparecem no diagnóstico ou serialização do inventário."""
    from core.orchestrator.cloud_db import DatabaseProbeResult

    secret_pw = "super_secret_pw99_unleaked"
    raw_url = f"postgresql://darkfac_worker:{secret_pw}@100.83.176.60:5432/darkfac_hf02_prod"

    def _probe_leaker(url=None):
        return DatabaseProbeResult(
            status="error",
            error_message=f"failed connecting to {url}",
        )

    monkeypatch.setattr("core.orchestrator.cloud_db.probe_cloud_database", _probe_leaker)

    inv = probe_host_inventory(host_id="desktop", database_url=raw_url, repo_root=tmp_path)
    assert inv.database_connected is False
    assert inv.database_status == "error"

    # Credential must NOT be present anywhere in the inventory serialization
    dumped = inv.model_dump_json()
    assert secret_pw not in dumped
    assert secret_pw not in (inv.database_diagnostic or "")
    assert ":***@" in (inv.database_diagnostic or "")



def test_probe_cli_runs_from_repo_root_without_module_not_found() -> None:
    """O CLI `probe` executado como script não pode falhar com ModuleNotFoundError de `core`."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "start_fleet_worker.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    result = subprocess.run(
        [sys.executable, str(script), "probe", "--host", "notebook", "--json"],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )

    combined = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in combined
    payload = json.loads(result.stdout)
    assert payload["host_id"] == "notebook"
