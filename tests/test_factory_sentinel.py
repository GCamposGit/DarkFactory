"""Testes unitarios do Sentinela de Linha (USR-439)."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.line.sentinel import (
    DEFAULT_INTERVAL_SECONDS,
    SentinelReport,
    SentinelStatus,
    check_factory_status,
    load_last_report,
    maybe_run_sentinel,
    read_quotas,
)


def test_sentinel_idle_when_no_tickets(tmp_path: Path):
    """Fila vazia e sem execucoes em voo resulta em IDLE."""
    state_file = tmp_path / "sentinel_state.json"

    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 50.0, "codex": 50.0, "grok": 50.0, "antigravity": 50.0}), \
         patch("core.line.routing.pick", return_value=("claude", "sonnet")):
        report = check_factory_status(
            demands_loader=lambda: [],
            notify_on_stall=False,
            state_path=state_file,
        )
        assert report.status == SentinelStatus.IDLE
        assert not report.is_stalled
        assert "vazia" in report.reason.lower()
        assert state_file.is_file()


def test_sentinel_healthy_when_route_available(tmp_path: Path):
    """Demanda pendente com rota de desenvolvimento viavel resulta em HEALTHY."""
    state_file = tmp_path / "sentinel_state.json"

    demands = [{"id": "USR-100", "status": "planned"}]
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 50.0, "codex": 0.0, "grok": 0.0, "antigravity": 50.0}), \
         patch("core.line.routing.pick", side_effect=lambda stage, caps: ("claude", "sonnet")):
        report = check_factory_status(
            demands_loader=lambda: demands,
            notify_on_stall=False,
            state_path=state_file,
        )
        assert report.status == SentinelStatus.HEALTHY
        assert not report.is_stalled
        assert report.route_development == "claude"
        assert "USR-100" in report.pending_tickets


def test_sentinel_stalled_no_route_triggers_notification(tmp_path: Path):
    """Demanda pendente sem rota viavel (todas as cotas <= 15%) marca STALLED_NO_ROUTE e notifica."""
    state_file = tmp_path / "sentinel_state.json"
    mock_notifier = MagicMock()

    demands = [{"id": "USR-101", "status": "planned"}]
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 5.0, "codex": 0.0, "grok": 10.0, "antigravity": 5.0}), \
         patch("core.line.routing.pick", return_value=None):
        report = check_factory_status(
            demands_loader=lambda: demands,
            notify_on_stall=True,
            notification_dispatcher=mock_notifier,
            state_path=state_file,
        )
        assert report.status == SentinelStatus.STALLED_NO_ROUTE
        assert report.is_stalled
        assert "USR-101" in report.pending_tickets
        assert mock_notifier.called
        args, _ = mock_notifier.call_args
        assert args[0] == SentinelStatus.STALLED_NO_ROUTE
        assert "USR-101" in args[2]


def test_sentinel_degraded_when_interactive_only(tmp_path: Path):
    """Demanda pendente com frota headless travada, mas Antigravity interativo viavel marca DEGRADED."""
    state_file = tmp_path / "sentinel_state.json"

    demands = [{"id": "USR-102", "status": "planned"}]

    def mock_pick(stage: str, caps: list[str]):
        if "harness:antigravity" in caps:
            return ("antigravity", None)
        return None

    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 5.0, "codex": 0.0, "grok": 10.0, "antigravity": 36.0}), \
         patch("core.line.routing.pick", side_effect=mock_pick):
        report = check_factory_status(
            demands_loader=lambda: demands,
            notify_on_stall=False,
            state_path=state_file,
        )
        assert report.status == SentinelStatus.DEGRADED
        assert not report.is_stalled
        assert report.route_development == "antigravity"
        assert report.fleet_route_development is None


def test_sentinel_stalled_in_flight(tmp_path: Path):
    """Ticket em voo com log inativo ha mais de 45m e marcado como STALLED_IN_FLIGHT."""
    state_file = tmp_path / "sentinel_state.json"
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    stale_log = log_dir / "USR-103-notebook.log"
    stale_log.write_text("running...", encoding="utf-8")

    # Muta mtime para 3000 segundos no passado (> 2700s)
    old_time = time.time() - 3000
    os.utime(stale_log, (old_time, old_time))

    mock_notifier = MagicMock()
    with patch("core.line.sentinel.read_quotas", return_value={"claude": 20.0}), \
         patch("core.line.routing.pick", return_value=("claude", "sonnet")), \
         patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={"USR-103": "notebook"}):
        report = check_factory_status(
            max_flight_duration_sec=2700.0,
            notify_on_stall=True,
            notification_dispatcher=mock_notifier,
            state_path=state_file,
            log_dir=log_dir,
        )
        assert report.status == SentinelStatus.STALLED_IN_FLIGHT
        assert report.is_stalled
        assert "USR-103" in report.stalled_tickets
        assert mock_notifier.called


def test_maybe_run_sentinel_silence_window(tmp_path: Path):
    """maybe_run_sentinel respeita a janela de 45 minutos (2700s) entre execucoes."""
    state_file = tmp_path / "sentinel_state.json"

    # 1. Primeira execucao: roda e grava estado
    with patch("core.line.sentinel.check_factory_status") as mock_check:
        mock_check.return_value = SentinelReport(
            status=SentinelStatus.IDLE,
            is_stalled=False,
            reason="idle",
            last_run_epoch=time.time(),
        )
        rep1 = maybe_run_sentinel(interval_sec=2700.0, path=state_file)
        assert rep1 is not None
        assert mock_check.call_count == 1
        # Grava o report retornado no state_file
        from core.line.sentinel import _persist_report
        _persist_report(rep1, state_file)

    # 2. Segunda execucao imediata: silenciada pela janela de 2700s
    with patch("core.line.sentinel.check_factory_status") as mock_check:
        rep2 = maybe_run_sentinel(interval_sec=2700.0, path=state_file)
        assert rep2 is None
        assert mock_check.call_count == 0

    # 3. Terceira execucao com --force: roda ignorando a janela
    with patch("core.line.sentinel.check_factory_status") as mock_check:
        mock_check.return_value = SentinelReport(
            status=SentinelStatus.IDLE,
            is_stalled=False,
            reason="idle",
            last_run_epoch=time.time(),
        )
        rep3 = maybe_run_sentinel(interval_sec=2700.0, force=True, path=state_file)
        assert rep3 is not None
        assert mock_check.call_count == 1


def test_read_quotas_isolated():
    """read_quotas mapeia provedores e formata headroom corretamente."""
    with patch("core.line.routing._quota_headroom_diagnosis", return_value=(42.567, "healthy")):
        quotas = read_quotas()
        assert quotas["claude"] == 42.57
        assert quotas["codex"] == 42.57
        assert quotas["grok"] == 42.57
        assert quotas["antigravity"] == 42.57


def test_fleet_dispatcher_sentinel_hook_called():
    """FleetDispatcher invoca sentinel_hook de forma segura no run_forever."""
    from core.line.fleet_dispatcher import DispatcherConfig, FleetDispatcher

    mock_sentinel = MagicMock()
    mock_digest = MagicMock()

    calls = 0

    def mock_cycle():
        nonlocal calls
        calls += 1
        return {}

    d = FleetDispatcher(
        DispatcherConfig(cycle_sec=0.01),
        dry_run=False,
        digest_hook=mock_digest,
        sentinel_hook=mock_sentinel,
    )
    d.run_cycle = mock_cycle

    # Roda 1 iteracao do loop
    loop_count = 0

    def stop_condition():
        nonlocal loop_count
        loop_count += 1
        return loop_count > 1

    d.run_forever(stop=stop_condition)

    assert mock_sentinel.called
    assert mock_digest.called
