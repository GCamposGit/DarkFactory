"""Portao local espera RAM para 2+ workers e falha explicito (USR-262)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from core.demands.models import DeliveryStatus, UserTicket
from core.demands.store import DemandsStore
from core.line import gate_ram
from core.line.agent_cli import AgentResult, BoundedProcessResult

import run_ticket


def _fake_measure(workers_seq: list[int]):
    calls = {"i": 0}

    def measure() -> gate_ram.RamReading:
        w = workers_seq[min(calls["i"], len(workers_seq) - 1)]
        calls["i"] += 1
        return gate_ram.RamReading(available_gib=3.4 if w < 2 else 8.0, workers=w)

    return measure


def test_enough_ram_proceeds_without_sleeping() -> None:
    sleeps: list[float] = []
    res = gate_ram.wait_for_gate_ram(measure=_fake_measure([3]), sleep=sleeps.append, limit_s=1800)
    assert res.ok and sleeps == [] and res.waited_s == 0


def test_low_ram_that_recovers_proceeds_after_waiting() -> None:
    sleeps: list[float] = []
    msgs: list[str] = []
    res = gate_ram.wait_for_gate_ram(
        measure=_fake_measure([1, 1, 2]), sleep=sleeps.append, limit_s=1800, announce=msgs.append
    )
    assert res.ok and res.reading.workers == 2
    assert sleeps == [60.0, 60.0] and len(msgs) == 2
    assert "[RAM] 3.4 GiB livres, cabem 1 workers; aguardando 2+ (limite 1800 s)" in msgs[0]


def test_low_ram_until_limit_fails() -> None:
    sleeps: list[float] = []
    res = gate_ram.wait_for_gate_ram(
        measure=_fake_measure([1]), sleep=sleeps.append, limit_s=150, announce=lambda m: None
    )
    assert not res.ok and res.reading.workers == 1
    assert sleeps == [60.0, 60.0, 30.0] and res.waited_s == 150


def test_limit_env_override(monkeypatch) -> None:
    monkeypatch.setenv(gate_ram.ENV_RAM_WAIT_S, "42")
    assert gate_ram.ram_wait_limit_s() == 42.0
    monkeypatch.setenv(gate_ram.ENV_RAM_WAIT_S, "lixo")
    assert gate_ram.ram_wait_limit_s() == gate_ram.DEFAULT_RAM_WAIT_S


def test_measure_workers_uses_worker_capacity_math(monkeypatch) -> None:
    from tests import _worker_capacity as wc

    monkeypatch.delenv("DARKFAC_MAX_WORKERS", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr(wc, "get_physical_cpu_count", lambda: 8)
    monkeypatch.setattr(wc, "get_available_memory_bytes", lambda: int(3.4 * wc.BYTES_PER_GIB))
    assert gate_ram.measure_workers().workers == 0
    monkeypatch.setattr(wc, "get_available_memory_bytes", lambda: int(8.6 * wc.BYTES_PER_GIB))
    assert gate_ram.measure_workers().workers == 3


def test_gate_not_run_and_cause_explicit_when_ram_never_recovers(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@d.local"]):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    ledger = repo / ".factory" / "demands"
    ledger.mkdir(parents=True)
    ticket = UserTicket(
        id="USR-262", project_id="darkfac", title="Test Ticket", problem_statement="Test problem", status=DeliveryStatus.PLANNED
    )
    DemandsStore(ledger / "demands.json").save_ticket(ticket)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "ticket/usr-262"], cwd=str(repo), check=True, capture_output=True)
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat"], cwd=str(repo), check=True, capture_output=True)

    gate_calls: list[list[str]] = []
    fix_calls: list[str] = []

    def fake_wait(as_json):
        return gate_ram.wait_for_gate_ram(
            measure=_fake_measure([1]), sleep=lambda s: None, limit_s=120, announce=lambda m: None
        )

    with patch("run_ticket.PROJECT_ROOT", repo), \
         patch("run_ticket._wait_gate_ram_if_local", new=fake_wait), \
         patch("run_ticket._run_bounded", new=lambda argv, **kw: gate_calls.append(argv)
               or BoundedProcessResult(stdout="", stderr="", returncode=0, duration_s=1.0)), \
         patch("run_ticket._invoke_fix_agent", new=lambda *a, **k: fix_calls.append("x")
               or AgentResult(ok=True, text="", harness="claude", duration_s=0.1)):
        result = run_ticket.resume_delivery(
            "USR-262", worktree_path=repo, skip_validation=False, return_result=True
        )

    assert result["ok"] is False and result["cause"] == "gate_insufficient_ram"
    assert fix_calls == []
    assert not any("runner.py" in " ".join(map(str, c)) for c in gate_calls)
    log = result["self_correction_log"]
    assert log[-1]["event"] == "infra_error" and log[-1]["cause"] == "gate_insufficient_ram"
    assert "--resume-delivery USR-262" in log[-1]["error"]
    assert "NAO reinvocado" in capsys.readouterr().err
