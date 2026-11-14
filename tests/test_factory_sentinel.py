"""Testes unitarios do Sentinela de Linha (USR-439, USR-501)."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import core.line.sentinel as sentinel_mod
from core.integrations.telegram import TelegramConfig, TelegramService
from core.line.sentinel import (
    DEFAULT_IDLE_QUEUE_SECONDS,
    DEFAULT_STATUS_HEARTBEAT_SECONDS,
    STALE_LOCK_FINDING,
    STATUS_WITHOUT_PROCESS_FINDING,
    SentinelReport,
    SentinelStatus,
    check_factory_status,
    load_last_report,
    maybe_run_sentinel,
    read_quotas,
)

_REAL_READ_PENDING = sentinel_mod._read_pending_deliveries
_REAL_READ_RESERVATIONS = sentinel_mod._read_local_reservations
_REAL_READ_LOCKS = sentinel_mod._read_line_locks


@pytest.fixture(autouse=True)
def _isolate_status_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """A amostra real de entrega, reserva e trava nao entra nos testes que nao a pedem."""
    monkeypatch.setattr(sentinel_mod, "_read_pending_deliveries", lambda directory=None: [])
    monkeypatch.setattr(sentinel_mod, "_read_local_reservations", lambda: [])
    monkeypatch.setattr(sentinel_mod, "_read_line_locks", lambda directory=None: [])
from core.notifications.models import NotificationChannel
from core.notifications.service import NotificationService
from core.notifications.store import NotificationStore


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
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
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
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
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
        rep1 = maybe_run_sentinel(
            interval_sec=2700.0,
            path=state_file,
            notify_on_stall=False,
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
        )
        assert rep1 is not None
        assert mock_check.call_count == 1
        # Grava o report retornado no state_file
        from core.line.sentinel import _persist_report
        _persist_report(rep1, state_file)

    # 2. Segunda execucao imediata: silenciada pela janela de 2700s
    with patch("core.line.sentinel.check_factory_status") as mock_check:
        rep2 = maybe_run_sentinel(
            interval_sec=2700.0,
            path=state_file,
            notify_on_stall=False,
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
        )
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
        rep3 = maybe_run_sentinel(
            interval_sec=2700.0,
            force=True,
            path=state_file,
            notify_on_stall=False,
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
        )
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


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _idle_service(tmp_path: Path):
    store = NotificationStore(store_path=tmp_path / "notifications.jsonl", retention_days=30)
    telegram = MagicMock(spec=TelegramService)
    telegram.config = TelegramConfig(bot_token="test:token", authorized_user_ids=[12345678])
    telegram.send_message.return_value = True
    service = NotificationService(store=store, telegram_service=telegram, cooldown_minutes=45)
    observed: list[dict] = []
    real_notify = service.notify

    def spy_notify(**kwargs):
        observed.append(kwargs)
        return real_notify(**kwargs)

    service.notify = spy_notify  # type: ignore[method-assign]
    return service, telegram, observed


def _check_idle(
    tmp_path: Path,
    *,
    demands,
    fleet,
    clock: _Clock,
    running=None,
    pick=("claude", "sonnet"),
    pick_effect=None,
    quotas=None,
    notify=False,
    service_factory=None,
    log_dir=None,
):
    state_file = tmp_path / "sentinel_state.json"
    quotas = quotas or {"claude": 50.0, "codex": 50.0, "grok": 50.0, "antigravity": 50.0}
    running = {} if running is None else running
    pick_patch = (
        patch("core.line.routing.pick", side_effect=pick_effect)
        if pick_effect is not None
        else patch("core.line.routing.pick", return_value=pick)
    )
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value=running), \
         patch("core.line.sentinel.read_quotas", return_value=quotas), \
         pick_patch:
        return check_factory_status(
            demands_loader=lambda: demands,
            notify_on_stall=notify,
            state_path=state_file,
            log_dir=log_dir,
            clock=clock,
            fleet_state_loader=lambda: dict(fleet),
            notification_service_factory=service_factory,
        )


def _assert_no_idle_finding(report) -> None:
    assert "idle_queue" not in report.findings
    assert "idle_queue" not in report.details


def test_idle_queue_alerts_once_after_900_seconds_and_a_new_episode_can_alert(tmp_path: Path):
    """Host livre, rota e ticket de select_tickets: o alerta espera passar de 900s e nao repete a chave."""
    service, telegram, observed = _idle_service(tmp_path)
    clock = _Clock(10_000.0)
    demands = [{"id": "USR-501", "status": "planned"}]
    fleet = {"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": []}
    common = dict(
        demands=demands,
        fleet=fleet,
        clock=clock,
        notify=True,
        service_factory=lambda: service,
    )

    started = _check_idle(tmp_path, **common)
    assert started.status == SentinelStatus.HEALTHY
    assert started.idle_since_epoch == 10_000.0
    assert started.idle_episode_key == "idle_queue:10000"
    _assert_no_idle_finding(started)
    assert observed == []
    assert load_last_report(tmp_path / "sentinel_state.json").idle_since_epoch == 10_000.0

    clock.now = 10_000.0 + DEFAULT_IDLE_QUEUE_SECONDS
    at_boundary = _check_idle(tmp_path, **common)
    assert at_boundary.idle_since_epoch == 10_000.0
    assert at_boundary.status == SentinelStatus.HEALTHY
    _assert_no_idle_finding(at_boundary)
    assert telegram.send_message.call_count == 0

    clock.now = 10_901.0
    alerted = _check_idle(tmp_path, **common)
    assert alerted.status == SentinelStatus.HEALTHY
    assert alerted.findings == ["idle_queue"]
    assert alerted.alert_sent is True
    assert alerted.details["idle_queue"]["episode_key"] == "idle_queue:10000"
    assert alerted.idle_alert_episode == "idle_queue:10000"
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert observed[0]["details"]["finding"] == "idle_queue"
    stored = service.store.list_notifications()
    assert len(stored) == 1
    assert stored[0].details["episode_key"] == "idle_queue:10000"
    assert telegram.send_message.call_count == 1
    assert "Fila Ociosa" in telegram.send_message.call_args.kwargs["text"]

    clock.now = 11_000.0
    repeated = _check_idle(tmp_path, **common)
    assert repeated.findings == ["idle_queue"]
    assert repeated.alert_sent is False
    assert repeated.idle_alert_episode == "idle_queue:10000"
    assert len(observed) == 1
    assert telegram.send_message.call_count == 1
    assert len(service.store.list_notifications()) == 1

    fleet["free_hosts"] = []
    clock.now = 11_100.0
    cleared = _check_idle(tmp_path, **common)
    assert cleared.status == SentinelStatus.HEALTHY
    assert cleared.idle_since_epoch is None
    assert cleared.idle_episode_key is None
    assert cleared.idle_alert_episode is None
    _assert_no_idle_finding(cleared)
    assert len(observed) == 1

    fleet["free_hosts"] = ["notebook"]
    clock.now = 12_000.0
    restarted = _check_idle(tmp_path, **common)
    assert restarted.idle_since_epoch == 12_000.0
    assert restarted.idle_episode_key == "idle_queue:12000"
    _assert_no_idle_finding(restarted)
    assert len(observed) == 1

    clock.now = 12_901.0
    second = _check_idle(tmp_path, **common)
    assert second.findings == ["idle_queue"]
    assert second.alert_sent is True
    assert second.idle_alert_episode == "idle_queue:12000"
    assert len(observed) == 2
    assert observed[1]["channel"] == NotificationChannel.ALL
    assert observed[1]["details"]["episode_key"] == "idle_queue:12000"
    assert len(service.store.list_notifications()) == 2
    assert telegram.send_message.call_count == 2


def test_idle_queue_accepts_queue_remaining_without_select_tickets(tmp_path: Path):
    """queue_remaining sozinho torna o ticket elegivel; o status IDLE da fila vazia no ledger permanece."""
    service, telegram, observed = _idle_service(tmp_path)
    clock = _Clock(2_000.0)
    fleet = {"free_hosts": ["grokbox"], "in_flight": {}, "queue_remaining": ["USR-77"]}
    common = dict(
        demands=[],
        fleet=fleet,
        clock=clock,
        notify=True,
        service_factory=lambda: service,
    )

    started = _check_idle(tmp_path, **common)
    assert started.status == SentinelStatus.IDLE
    assert started.idle_since_epoch == 2_000.0
    _assert_no_idle_finding(started)

    clock.now = 2_901.0
    alerted = _check_idle(tmp_path, **common)
    assert alerted.status == SentinelStatus.IDLE
    assert alerted.findings == ["idle_queue"]
    assert alerted.details["idle_queue"]["eligible_tickets"] == ["USR-77"]
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert len(service.store.list_notifications()) == 1
    assert telegram.send_message.call_count == 1


def test_idle_queue_absent_keeps_current_statuses(tmp_path: Path):
    """Antes de 900s, com execucao viva, sem rota, sem ticket ou sem host, o achado nao aparece."""
    clock = _Clock(8_000.0)
    eligible = [{"id": "USR-501", "status": "planned"}]
    ready = {"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": []}

    early = _check_idle(tmp_path, demands=eligible, fleet=ready, clock=clock)
    assert early.status == SentinelStatus.HEALTHY
    assert early.idle_since_epoch == 8_000.0
    _assert_no_idle_finding(early)

    live_process = _check_idle(
        tmp_path / "live",
        demands=eligible,
        fleet=ready,
        clock=clock,
        running={"USR-501": "notebook"},
    )
    assert live_process.status == SentinelStatus.HEALTHY
    assert live_process.idle_since_epoch is None
    _assert_no_idle_finding(live_process)

    dispatcher_busy = _check_idle(
        tmp_path / "busy",
        demands=eligible,
        fleet={"free_hosts": ["notebook"], "in_flight": {"notebook": "USR-501"}, "queue_remaining": []},
        clock=clock,
    )
    assert dispatcher_busy.status == SentinelStatus.HEALTHY
    assert dispatcher_busy.idle_since_epoch is None
    _assert_no_idle_finding(dispatcher_busy)

    no_route = _check_idle(
        tmp_path / "route",
        demands=eligible,
        fleet=ready,
        clock=clock,
        pick=None,
    )
    assert no_route.status == SentinelStatus.STALLED_NO_ROUTE
    assert no_route.is_stalled
    assert no_route.idle_since_epoch is None
    _assert_no_idle_finding(no_route)

    frozen = [{"id": "USR-9", "status": "planned", "tags": ["congelado-v0"]}]
    no_ticket = _check_idle(
        tmp_path / "ticket",
        demands=frozen,
        fleet={"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": []},
        clock=clock,
    )
    assert no_ticket.status == SentinelStatus.HEALTHY
    assert "USR-9" in no_ticket.pending_tickets
    assert no_ticket.idle_since_epoch is None
    _assert_no_idle_finding(no_ticket)

    no_host = _check_idle(
        tmp_path / "host",
        demands=eligible,
        fleet={"free_hosts": [], "in_flight": {}, "queue_remaining": []},
        clock=clock,
    )
    assert no_host.status == SentinelStatus.HEALTHY
    assert no_host.idle_since_epoch is None
    _assert_no_idle_finding(no_host)

    empty = _check_idle(
        tmp_path / "empty",
        demands=[],
        fleet={"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": []},
        clock=clock,
    )
    assert empty.status == SentinelStatus.IDLE
    assert "vazia" in empty.reason.lower()
    _assert_no_idle_finding(empty)

    def antigravity_only(stage, caps):
        if "harness:antigravity" in caps:
            return ("antigravity", None)
        return None

    degraded = _check_idle(
        tmp_path / "degraded",
        demands=eligible,
        fleet=ready,
        clock=clock,
        pick_effect=antigravity_only,
        quotas={"claude": 5.0, "codex": 0.0, "grok": 10.0, "antigravity": 36.0},
    )
    assert degraded.status == SentinelStatus.DEGRADED
    assert not degraded.is_stalled
    assert degraded.route_development == "antigravity"
    assert degraded.idle_since_epoch == 8_000.0
    _assert_no_idle_finding(degraded)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "USR-103-notebook.log"
    stale.write_text("running", encoding="utf-8")
    old = clock.now - 3_000
    os.utime(stale, (old, old))
    stalled = _check_idle(
        tmp_path / "stalled",
        demands=eligible,
        fleet=ready,
        clock=clock,
        running={"USR-103": "notebook"},
        log_dir=log_dir,
    )
    assert stalled.status == SentinelStatus.STALLED_IN_FLIGHT
    assert stalled.is_stalled
    assert "USR-103" in stalled.stalled_tickets
    assert stalled.idle_since_epoch is None
    _assert_no_idle_finding(stalled)


def _seed_cached_report(
    path: Path,
    *,
    now: float,
    route: str | None = "claude",
    pending: list[str] | None = None,
    idle_since: float | None = None,
    episode: str | None = None,
    quotas: dict | None = None,
) -> None:
    from core.line.sentinel import _persist_report

    report = SentinelReport(
        status=SentinelStatus.HEALTHY,
        is_stalled=False,
        reason="Linha operando normalmente.",
        last_run_epoch=now,
        timestamp="2026-11-10T12:00:00+00:00",
        route_development=route,
        route_planning="claude" if route else None,
        fleet_route_development="claude" if route else None,
        pending_tickets=list(pending or []),
        quotas=quotas if quotas is not None else {
            "claude": 80.0,
            "codex": 70.0,
            "grok": 60.0,
            "antigravity": 50.0,
        },
        idle_since_epoch=idle_since,
        idle_episode_key=episode,
    )
    _persist_report(report, path)


@contextmanager
def _patched_light_sample(running=None):
    """Bloqueia a sonda completa e registra fetch de demandas durante a amostra leve."""
    running = {} if running is None else running
    with patch("core.line.sentinel.check_factory_status") as check, \
         patch("core.line.routing.pick") as pick, \
         patch("core.line.sentinel.read_quotas") as quotas, \
         patch("core.line.fleet_dispatcher.running_fleet_runs", return_value=running) as runs, \
         patch("core.line.fleet_dispatcher.default_demands_loader") as demands, \
         patch("core.line.fleet_dispatcher.subprocess.run") as git:
        yield {
            "check": check,
            "pick": pick,
            "quotas": quotas,
            "runs": runs,
            "demands": demands,
            "git": git,
        }


def _assert_light_sample_skipped_probes(mocks: dict) -> None:
    assert mocks["check"].call_count == 0
    assert mocks["pick"].call_count == 0
    assert mocks["quotas"].call_count == 0
    assert mocks["demands"].call_count == 0
    assert mocks["git"].call_count == 0


def test_light_sample_updates_idle_clock_without_quota_or_route_probe(tmp_path: Path, monkeypatch):
    """Dentro de 2700s a amostra usa despachante, rota em cache e running_fleet_runs."""
    dispatcher = tmp_path / "fleet_dispatcher_state.json"
    dispatcher.write_text(json.dumps({
        "free_hosts": ["notebook"],
        "in_flight": {},
        "queue_remaining": ["USR-501"],
    }), encoding="utf-8")
    monkeypatch.setattr("core.line.fleet_dispatcher.STATE_PATH", dispatcher)
    clock = _Clock(20_000.0)
    state = tmp_path / "sentinel_state.json"
    _seed_cached_report(state, now=20_000.0, route="claude", idle_since=19_500.0, episode="idle_queue:19500")

    with _patched_light_sample() as mocks:
        result = maybe_run_sentinel(interval_sec=2700.0, path=state, clock=clock, notify_on_stall=False)
    assert result is None
    _assert_light_sample_skipped_probes(mocks)
    assert mocks["runs"].called
    loaded = load_last_report(state)
    assert loaded is not None
    assert loaded.idle_since_epoch == 19_500.0
    assert loaded.last_run_epoch == 20_000.0
    assert loaded.route_development == "claude"
    assert loaded.quotas["claude"] == 80.0
    _assert_no_idle_finding(loaded)

    blocked = tmp_path / "blocked.json"
    _seed_cached_report(blocked, now=20_000.0, route="claude")
    with _patched_light_sample(running={"USR-501": "notebook"}) as mocks:
        blocked_result = maybe_run_sentinel(
            interval_sec=2700.0, path=blocked, clock=clock, notify_on_stall=False,
        )
    assert blocked_result is None
    _assert_light_sample_skipped_probes(mocks)
    assert mocks["runs"].called
    assert load_last_report(blocked).idle_since_epoch is None

    no_route = tmp_path / "no_route.json"
    _seed_cached_report(no_route, now=20_000.0, route=None, pending=["USR-501"])
    with _patched_light_sample() as mocks:
        no_route_result = maybe_run_sentinel(
            interval_sec=2700.0, path=no_route, clock=clock, notify_on_stall=False,
        )
    assert no_route_result is None
    _assert_light_sample_skipped_probes(mocks)
    assert load_last_report(no_route).idle_since_epoch is None

    busy = tmp_path / "busy.json"
    dispatcher.write_text(json.dumps({
        "free_hosts": ["notebook"],
        "in_flight": {"notebook": "USR-501"},
        "queue_remaining": ["USR-501"],
    }), encoding="utf-8")
    _seed_cached_report(busy, now=20_000.0, route="claude")
    with _patched_light_sample() as mocks:
        busy_result = maybe_run_sentinel(interval_sec=2700.0, path=busy, clock=clock, notify_on_stall=False)
    assert busy_result is None
    _assert_light_sample_skipped_probes(mocks)
    assert load_last_report(busy).idle_since_epoch is None


def test_light_sample_alerts_once_after_900_seconds(tmp_path: Path):
    """A conjuncao verdadeira por mais de 900s na amostra leve dispara o alerta de T1 uma vez."""
    service, telegram, observed = _idle_service(tmp_path)
    clock = _Clock(30_000.0)
    state = tmp_path / "sentinel_state.json"
    _seed_cached_report(state, now=30_000.0, route="claude")
    fleet = {"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": ["USR-501"]}

    def run():
        with _patched_light_sample() as mocks:
            result = maybe_run_sentinel(
                interval_sec=2700.0,
                path=state,
                clock=clock,
                notify_on_stall=True,
                fleet_state_loader=lambda: dict(fleet),
                notification_service_factory=lambda: service,
            )
        assert result is None
        _assert_light_sample_skipped_probes(mocks)
        assert mocks["runs"].called
        return load_last_report(state)

    started = run()
    assert started.idle_since_epoch == 30_000.0
    assert started.last_run_epoch == 30_000.0
    assert started.route_development == "claude"
    assert started.quotas["grok"] == 60.0
    _assert_no_idle_finding(started)
    assert observed == []

    clock.now = 30_000.0 + DEFAULT_IDLE_QUEUE_SECONDS
    at_boundary = run()
    assert at_boundary.idle_since_epoch == 30_000.0
    _assert_no_idle_finding(at_boundary)
    assert telegram.send_message.call_count == 0

    clock.now = 30_901.0
    alerted = run()
    assert alerted.findings == ["idle_queue"]
    assert alerted.alert_sent is True
    assert alerted.idle_alert_episode == "idle_queue:30000"
    assert alerted.last_run_epoch == 30_000.0
    assert alerted.status == SentinelStatus.HEALTHY
    assert alerted.quotas["claude"] == 80.0
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert observed[0]["details"]["finding"] == "idle_queue"
    assert observed[0]["details"]["quotas"]["claude"] == 80.0
    assert len(service.store.list_notifications()) == 1
    assert telegram.send_message.call_count == 1
    assert "Fila Ociosa" in telegram.send_message.call_args.kwargs["text"]

    clock.now = 31_000.0
    repeated = run()
    assert repeated.findings == ["idle_queue"]
    assert repeated.alert_sent is False
    assert repeated.idle_alert_episode == "idle_queue:30000"
    assert repeated.last_run_epoch == 30_000.0
    assert len(observed) == 1
    assert telegram.send_message.call_count == 1

    fleet["free_hosts"] = []
    clock.now = 31_100.0
    cleared = run()
    assert cleared.idle_since_epoch is None
    assert cleared.idle_episode_key is None
    assert cleared.reason == "Linha operando normalmente."
    _assert_no_idle_finding(cleared)
    assert telegram.send_message.call_count == 1

    fleet["free_hosts"] = ["notebook"]
    clock.now = 31_200.0
    restarted = run()
    assert restarted.idle_since_epoch == 31_200.0
    assert restarted.idle_episode_key == "idle_queue:31200"
    _assert_no_idle_finding(restarted)
    assert telegram.send_message.call_count == 1

    clock.now = 32_101.0
    second = run()
    assert second.findings == ["idle_queue"]
    assert second.alert_sent is True
    assert second.idle_alert_episode == "idle_queue:31200"
    assert second.last_run_epoch == 30_000.0
    assert len(observed) == 2
    assert telegram.send_message.call_count == 2


def test_light_sample_absent_dispatcher_state_does_not_fetch_or_invent_ticket(tmp_path: Path, monkeypatch):
    """Sem estado do despachante nao ha git fetch nem ticket elegivel inventado."""
    missing = tmp_path / "no-dispatcher.json"
    monkeypatch.setattr("core.line.fleet_dispatcher.STATE_PATH", missing)
    clock = _Clock(40_000.0)
    state = tmp_path / "sentinel_state.json"
    _seed_cached_report(
        state,
        now=40_000.0,
        route="claude",
        pending=["USR-501"],
        idle_since=39_000.0,
        episode="idle_queue:39000",
    )
    with _patched_light_sample() as mocks:
        result = maybe_run_sentinel(interval_sec=2700.0, path=state, clock=clock, notify_on_stall=True)
    assert result is None
    _assert_light_sample_skipped_probes(mocks)
    assert mocks["runs"].called
    loaded = load_last_report(state)
    assert loaded.idle_since_epoch is None
    assert loaded.idle_episode_key is None
    assert loaded.idle_alert_episode is None
    assert loaded.pending_tickets == ["USR-501"]
    assert loaded.last_run_epoch == 40_000.0
    _assert_no_idle_finding(loaded)

    present = tmp_path / "fleet_dispatcher_state.json"
    present.write_text(json.dumps({
        "free_hosts": ["notebook"],
        "in_flight": {},
        "queue_remaining": [],
    }), encoding="utf-8")
    monkeypatch.setattr("core.line.fleet_dispatcher.STATE_PATH", present)
    empty_queue = tmp_path / "empty_queue.json"
    _seed_cached_report(empty_queue, now=40_000.0, route="claude", pending=["USR-501"])
    with _patched_light_sample() as mocks:
        empty_result = maybe_run_sentinel(
            interval_sec=2700.0, path=empty_queue, clock=clock, notify_on_stall=True,
        )
    assert empty_result is None
    _assert_light_sample_skipped_probes(mocks)
    empty_loaded = load_last_report(empty_queue)
    assert empty_loaded.idle_since_epoch is None
    assert empty_loaded.pending_tickets == ["USR-501"]
    _assert_no_idle_finding(empty_loaded)


def test_full_probe_and_force_still_report_routes_and_quotas(tmp_path: Path):
    """A sonda de 45 min e --force continuam gravando rota e cotas."""
    clock = _Clock(80_000.0)
    state = tmp_path / "sentinel_state.json"
    _seed_cached_report(state, now=80_000.0 - 2700.0, route=None, quotas={})
    quotas = {"claude": 40.0, "codex": 30.0, "grok": 81.8, "antigravity": 20.0}
    demands = [{"id": "USR-501", "status": "planned"}]
    fleet = {"free_hosts": ["notebook"], "in_flight": {}, "queue_remaining": []}

    def run(*, force: bool = False):
        with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
             patch("core.line.sentinel.read_quotas", return_value=quotas) as read, \
             patch("core.line.routing.pick", return_value=("claude", "sonnet")) as pick:
            report = maybe_run_sentinel(
                interval_sec=2700.0,
                path=state,
                clock=clock,
                force=force,
                notify_on_stall=False,
                demands_loader=lambda: demands,
                fleet_state_loader=lambda: dict(fleet),
            )
        assert report is not None
        assert read.called
        assert pick.called
        assert report.route_development == "claude"
        assert report.route_planning == "claude"
        assert report.fleet_route_development == "claude"
        assert report.quotas == quotas
        return report

    aged = run()
    assert aged.last_run_epoch == 80_000.0
    assert aged.status == SentinelStatus.HEALTHY

    clock.now = 80_100.0
    forced = run(force=True)
    assert forced.last_run_epoch == 80_100.0
    assert forced.quotas["grok"] == 81.8
    assert forced.route_development == "claude"


def _epoch_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _ghost_rows(now: float) -> tuple[dict, list[dict], list[dict]]:
    """Um in_flight, uma entrega running e uma reserva active, todos sem processo e com heartbeat velho."""
    stale = now - DEFAULT_STATUS_HEARTBEAT_SECONDS - 30
    fleet = {
        "free_hosts": [],
        "in_flight": {"notebook": "USR-11"},
        "queue_remaining": [],
        "ts": _epoch_iso(stale),
    }
    pending = [{
        "ticket_id": "USR-12",
        "status": "running",
        "pid": 12012,
        "heartbeat_epoch": stale,
        "started_at": stale,
    }]
    reservations = [{
        "ticket_id": "USR-13",
        "status": "active",
        "host_id": "desktop",
        "heartbeat_at": _epoch_iso(stale),
        "pid": 13013,
    }]
    return fleet, pending, reservations


def _check_ghost(tmp_path: Path, *, clock: _Clock, fleet: dict, pending: list, reservations: list, **extra):
    service = extra.pop("service", None)
    state_file = tmp_path / "sentinel_state.json"
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value=extra.pop("running", {})), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 50.0, "codex": 50.0, "grok": 50.0, "antigravity": 50.0}), \
         patch("core.line.routing.pick", return_value=("claude", "sonnet")):
        return check_factory_status(
            demands_loader=lambda: [{"id": "USR-1", "status": "planned"}],
            notify_on_stall=extra.pop("notify", True),
            state_path=state_file,
            clock=clock,
            fleet_state_loader=lambda: fleet,
            pending_delivery_loader=lambda: pending,
            reservation_loader=lambda: reservations,
            live_pids_loader=extra.pop("live_pids", lambda: set()),
            notification_service_factory=(lambda: service) if service is not None else None,
            **extra,
        )


def test_status_without_process_alerts_once_per_episode(tmp_path: Path):
    """As tres fontes locais alertam juntas, uma vez, e um episodio novo pode alertar de novo."""
    service, telegram, observed = _idle_service(tmp_path)
    clock = _Clock(50_000.0)
    fleet, pending, reservations = _ghost_rows(clock.now)
    fleet_before = json.dumps(fleet)
    pending_before = json.dumps(pending)
    reservation_before = json.dumps(reservations)
    common = dict(
        clock=clock, fleet=fleet, pending=pending, reservations=reservations, service=service,
    )

    alerted = _check_ghost(tmp_path, **common)
    assert alerted.status == SentinelStatus.HEALTHY
    assert alerted.is_stalled is False
    assert alerted.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert alerted.alert_sent is True
    records = alerted.details[STATUS_WITHOUT_PROCESS_FINDING]["records"]
    assert {(row["source"], row["ticket_id"], row["status"]) for row in records} == {
        ("dispatcher_in_flight", "USR-11", "in_flight"),
        ("pending_delivery", "USR-12", "running"),
        ("local_reservation", "USR-13", "active"),
    }
    episode = alerted.status_without_process_episode
    assert episode is not None and episode.startswith("status_without_process:")
    assert alerted.status_without_process_alert_episode == episode
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert observed[0]["details"]["finding"] == STATUS_WITHOUT_PROCESS_FINDING
    assert len(service.store.list_notifications()) == 1
    assert telegram.send_message.call_count == 1
    assert "Status Ativo sem Processo" in telegram.send_message.call_args.kwargs["text"]
    assert json.dumps(fleet) == fleet_before
    assert json.dumps(pending) == pending_before
    assert json.dumps(reservations) == reservation_before
    assert reservations[0]["status"] == "active"
    assert pending[0]["status"] == "running"

    clock.now = 50_200.0
    repeated = _check_ghost(tmp_path, **common)
    assert repeated.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert repeated.alert_sent is False
    assert repeated.status_without_process_alert_episode == episode
    assert repeated.status == SentinelStatus.HEALTHY
    assert telegram.send_message.call_count == 1
    assert len(service.store.list_notifications()) == 1

    cleared = _check_ghost(
        tmp_path, clock=clock, fleet={"free_hosts": [], "in_flight": {}, "queue_remaining": []},
        pending=[], reservations=[], service=service,
    )
    assert cleared.status_without_process_episode is None
    assert cleared.status_without_process_alert_episode is None
    assert STATUS_WITHOUT_PROCESS_FINDING not in cleared.findings
    assert telegram.send_message.call_count == 1

    clock.now = 51_000.0
    new_fleet, _, _ = _ghost_rows(clock.now)
    new_fleet["in_flight"] = {"grokbox": "USR-19"}
    restarted = _check_ghost(
        tmp_path, clock=clock, fleet=new_fleet, pending=[], reservations=[], service=service,
    )
    assert restarted.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert restarted.alert_sent is True
    assert restarted.status_without_process_alert_episode != episode
    assert restarted.details[STATUS_WITHOUT_PROCESS_FINDING]["records"][0]["ticket_id"] == "USR-19"
    assert telegram.send_message.call_count == 2
    assert len(service.store.list_notifications()) == 2


def test_status_without_process_skips_live_fresh_boundary_and_cloud(tmp_path: Path):
    """Processo vivo, heartbeat ate 120s, entrega waiting, reserva draining e run de nuvem nao alertam."""
    now = 60_000.0
    clock = _Clock(now)
    fresh = now - DEFAULT_STATUS_HEARTBEAT_SECONDS
    stale = now - DEFAULT_STATUS_HEARTBEAT_SECONDS - 1
    base = {"free_hosts": [], "queue_remaining": []}

    def run(name: str, **kwargs):
        return _check_ghost(tmp_path / name, clock=clock, notify=False, **kwargs)

    live_ticket = run(
        "live-ticket",
        fleet={**base, "in_flight": {"notebook": "USR-21"}, "ts": _epoch_iso(stale)},
        pending=[],
        reservations=[],
        running={"USR-21": "notebook"},
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in live_ticket.findings
    assert live_ticket.status == SentinelStatus.HEALTHY

    live_pid = run(
        "live-pid",
        fleet={**base, "in_flight": {}},
        pending=[{"ticket_id": "USR-22", "status": "running", "pid": 222, "heartbeat_epoch": stale, "started_at": stale}],
        reservations=[],
        live_pids=lambda: {222},
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in live_pid.findings

    at_boundary = run(
        "boundary",
        fleet={**base, "in_flight": {"notebook": "USR-23"}, "ts": _epoch_iso(fresh)},
        pending=[],
        reservations=[],
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in at_boundary.findings

    just_over = run(
        "just-over",
        fleet={**base, "in_flight": {"notebook": "USR-24"}, "ts": _epoch_iso(stale)},
        pending=[],
        reservations=[],
    )
    assert just_over.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert just_over.alert_sent is False
    assert just_over.status == SentinelStatus.HEALTHY

    fresh_pending = run(
        "fresh-pending",
        fleet={**base, "in_flight": {}},
        pending=[{"ticket_id": "USR-25", "status": "running", "pid": 2525, "heartbeat_epoch": fresh}],
        reservations=[],
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in fresh_pending.findings

    waiting = run(
        "waiting",
        fleet={**base, "in_flight": {}},
        pending=[{"ticket_id": "USR-26", "status": "waiting", "pid": 2626, "heartbeat_epoch": stale}],
        reservations=[{"ticket_id": "USR-27", "status": "draining", "host_id": "notebook", "heartbeat_at": _epoch_iso(stale)}],
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in waiting.findings

    cloud = run(
        "cloud",
        fleet={**base, "in_flight": {"cloud-worker": "USR-28"}, "ts": _epoch_iso(stale)},
        pending=[{"ticket_id": "USR-29", "status": "running", "host_id": "vps", "pid": 2929, "heartbeat_epoch": stale}],
        reservations=[{"ticket_id": "USR-30", "status": "active", "host_id": "cloud", "heartbeat_at": _epoch_iso(stale)}],
    )
    assert STATUS_WITHOUT_PROCESS_FINDING not in cloud.findings
    assert STATUS_WITHOUT_PROCESS_FINDING not in cloud.details
    assert cloud.status == SentinelStatus.HEALTHY


def test_status_sources_are_read_without_being_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ler entrega, reserva e estado do despachante nao grava esses arquivos nem a trava."""
    folder = tmp_path / "pending_delivery"
    folder.mkdir()
    pending_path = folder / "USR-31.json"
    pending_path.write_text(json.dumps({
        "ticket_id": "USR-31",
        "worktree": str(tmp_path / "wt"),
        "stage": "development",
        "status": "running",
        "pid": 3131,
        "started_at": 10.0,
    }), encoding="utf-8")
    os.utime(pending_path, (4_000.0, 4_000.0))
    explicit = folder / "USR-32.json"
    explicit.write_text(json.dumps({
        "ticket_id": "USR-32",
        "worktree": str(tmp_path / "wt"),
        "stage": "development",
        "status": "running",
        "pid": 3232,
        "started_at": 10.0,
        "heartbeat_at": _epoch_iso(8_000.0),
    }), encoding="utf-8")
    waiting = folder / "USR-33.json"
    waiting.write_text(json.dumps({
        "ticket_id": "USR-33",
        "worktree": str(tmp_path / "wt"),
        "stage": "development",
        "status": "waiting",
    }), encoding="utf-8")
    before = {path.name: path.read_bytes() for path in folder.glob("*.json")}
    rows = _REAL_READ_PENDING(folder)
    assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == before
    by_ticket = {row["ticket_id"]: row for row in rows}
    assert set(by_ticket) == {"USR-31", "USR-32"}
    assert by_ticket["USR-31"]["heartbeat_epoch"] == pytest.approx(4_000.0, abs=1)
    assert by_ticket["USR-32"]["heartbeat_epoch"] == pytest.approx(8_000.0, abs=1)
    assert by_ticket["USR-31"]["status"] == "running"

    db = tmp_path / "control.db"
    db.write_bytes(b"sentinel-does-not-open-this")
    lock = tmp_path / "line.lock"
    lock.write_text("pid=9 ticket=USR-31\n", encoding="utf-8")
    fleet_path = tmp_path / "fleet_dispatcher_state.json"
    fleet_path.write_text(json.dumps({"in_flight": {"notebook": "USR-31"}, "ts": _epoch_iso(1_000.0)}), encoding="utf-8")
    monkeypatch.setattr("core.paths.state_root", lambda: tmp_path)
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    writes: list[str] = []

    class Store:
        def list_active_reservations(self):
            return [{
                "ticket_id": "USR-34",
                "status": "active",
                "host_id": "notebook",
                "heartbeat_at": _epoch_iso(1_000.0),
            }]

        def heartbeat_reservation(self, *args, **kwargs):
            writes.append("heartbeat")

        def reserve_cycle(self, *args, **kwargs):
            writes.append("reserve")

        def release_reservation(self, *args, **kwargs):
            writes.append("release")

    monkeypatch.setattr("core.demands.queue_store.control_store", lambda: Store())
    listed = _REAL_READ_RESERVATIONS()
    assert listed[0]["status"] == "active"
    assert writes == []
    assert db.read_bytes() == b"sentinel-does-not-open-this"
    assert lock.read_text(encoding="utf-8") == "pid=9 ticket=USR-31\n"

    clock = _Clock(20_000.0)
    fleet_before = fleet_path.read_bytes()
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 40.0}), \
         patch("core.line.routing.pick", return_value=("claude", "sonnet")):
        report = check_factory_status(
            demands_loader=lambda: [],
            notify_on_stall=False,
            state_path=tmp_path / "sentinel_state.json",
            clock=clock,
            fleet_state_loader=lambda: json.loads(fleet_path.read_text(encoding="utf-8")),
            pending_delivery_loader=lambda: _REAL_READ_PENDING(folder),
            reservation_loader=_REAL_READ_RESERVATIONS,
            live_pids_loader=lambda: set(),
        )
    assert report.status == SentinelStatus.IDLE
    assert report.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert {row["ticket_id"] for row in report.details[STATUS_WITHOUT_PROCESS_FINDING]["records"]} == {
        "USR-31", "USR-32", "USR-34",
    }
    assert fleet_path.read_bytes() == fleet_before
    assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == before
    assert listed[0]["status"] == "active"
    assert writes == []
    assert lock.read_text(encoding="utf-8") == "pid=9 ticket=USR-31\n"


def test_reused_pid_does_not_count_as_the_delivery_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pid vivo com outro create_time nao segura o alerta; o processo original sim."""
    started = 70_000.0
    clock = _Clock(started + 500)

    class Proc:
        def __init__(self, create: float, status: str = "running") -> None:
            self._create = create
            self._status = status

        def is_running(self) -> bool:
            return True

        def status(self) -> str:
            return self._status

        def create_time(self) -> float:
            return self._create

    class Psutil:
        NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        AccessDenied = type("AccessDenied", (Exception,), {})
        STATUS_ZOMBIE = "zombie"

        def __init__(self, proc: Proc) -> None:
            self._proc = proc

        def Process(self, pid: int) -> Proc:
            return self._proc

    def evaluate(proc: Proc):
        monkeypatch.setitem(sys.modules, "psutil", Psutil(proc))
        return _check_ghost(
            tmp_path / str(proc.create_time()),
            clock=clock,
            fleet={"free_hosts": [], "in_flight": {}, "queue_remaining": []},
            pending=[{
                "ticket_id": "USR-40",
                "status": "running",
                "pid": 4040,
                "heartbeat_epoch": started,
                "started_at": started,
            }],
            reservations=[],
            notify=False,
            live_pids=None,
        )

    reused = evaluate(Proc(1_000.0))
    assert reused.findings == [STATUS_WITHOUT_PROCESS_FINDING]

    original = evaluate(Proc(started + 1))
    assert STATUS_WITHOUT_PROCESS_FINDING not in original.findings

    zombie = evaluate(Proc(started, status="zombie"))
    assert zombie.findings == [STATUS_WITHOUT_PROCESS_FINDING]


def test_light_sample_alerts_status_without_process_once(tmp_path: Path):
    """Dentro da janela de 45 min o achado sai sem sonda de cota ou rota e nao repete o episodio."""
    service, telegram, observed = _idle_service(tmp_path)
    clock = _Clock(90_000.0)
    state = tmp_path / "sentinel_state.json"
    _seed_cached_report(state, now=90_000.0, route="claude")
    stale = _epoch_iso(clock.now - 400)
    fleet = {"free_hosts": [], "in_flight": {"notebook": {"ticket_id": "USR-41", "pid": 4141}}, "queue_remaining": [], "ts": stale}

    def run():
        with _patched_light_sample() as mocks:
            result = maybe_run_sentinel(
                interval_sec=2700.0,
                path=state,
                clock=clock,
                notify_on_stall=True,
                fleet_state_loader=lambda: dict(fleet),
                pending_delivery_loader=lambda: [],
                reservation_loader=lambda: [],
                live_pids_loader=lambda: set(),
                notification_service_factory=lambda: service,
            )
        assert result is None
        _assert_light_sample_skipped_probes(mocks)
        return load_last_report(state)

    alerted = run()
    assert alerted is not None
    assert alerted.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert alerted.alert_sent is True
    assert alerted.status == SentinelStatus.HEALTHY
    assert alerted.last_run_epoch == 90_000.0
    assert alerted.route_development == "claude"
    assert alerted.quotas["claude"] == 80.0
    assert alerted.reason == "Linha operando normalmente."
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert telegram.send_message.call_count == 1

    clock.now = 90_100.0
    repeated = run()
    assert repeated.findings == [STATUS_WITHOUT_PROCESS_FINDING]
    assert repeated.alert_sent is False
    assert repeated.status_without_process_alert_episode == alerted.status_without_process_episode
    assert telegram.send_message.call_count == 1
    assert repeated.last_run_epoch == 90_000.0


_COMMITTER_DATE = "1700000000 +0000"


def _git_commit(repo: Path, when: str = _COMMITTER_DATE) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_AUTHOR_DATE"] = when
    env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git", "-c", "user.name=sentinel", "-c", "user.email=sentinel@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "merge",
        ],
        cwd=repo, check=True, capture_output=True, text=True, env=env,
    )
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


def _write_lock(folder: Path, name: str, ticket: str, started: float, pid: int = 4242) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps({
        "pid": pid,
        "ticket_id": ticket,
        "hostname": "notebook",
        "started_at": started,
        "heartbeat_at": started,
    }), encoding="utf-8")
    return path


def _write_merge(root: Path, ticket: str, sha: str) -> Path:
    path = root / ticket / ".darkfac" / "runs" / ticket / "merge.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pr_url": "https://example.invalid/pull/1",
        "merge_sha": sha,
    }), encoding="utf-8")
    return path


def _use_real_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sentinel_mod, "_read_line_locks", _REAL_READ_LOCKS)


def _check_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    clock: _Clock,
    locks: Path,
    artifacts: Path,
    repo: Path,
    service=None,
    notify: bool = True,
):
    _use_real_locks(monkeypatch)
    with patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
         patch("core.line.sentinel.read_quotas", return_value={"claude": 50.0, "codex": 50.0, "grok": 50.0, "antigravity": 50.0}), \
         patch("core.line.routing.pick", return_value=("claude", "sonnet")):
        return check_factory_status(
            demands_loader=lambda: [],
            notify_on_stall=notify,
            state_path=tmp_path / "sentinel_state.json",
            clock=clock,
            fleet_state_loader=lambda: {},
            pending_delivery_loader=lambda: [],
            reservation_loader=lambda: [],
            live_pids_loader=lambda: set(),
            notification_service_factory=(lambda: service) if service is not None else None,
            lock_directory=locks,
            ticket_artifacts_root=artifacts,
            git_repo=repo,
        )


def test_stale_lock_alerts_once_when_started_before_the_same_ticket_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """line.lock e line.fleet-*.lock anteriores ao merge do mesmo ticket alertam uma vez e ficam no disco."""
    service, telegram, observed = _idle_service(tmp_path)
    repo = tmp_path / "repo"
    sha = _git_commit(repo)
    committer = sentinel_mod._git_committer_epoch(sha, repo)
    assert committer == 1_700_000_000
    started = float(committer) - 50
    locks = tmp_path / "line_lock"
    artifacts = tmp_path / "artifacts"
    lock_paths = [
        _write_lock(locks, "line.lock", "USR-501", started, pid=501),
        _write_lock(locks, "line.fleet-grokbox.lock", "USR-501", started - 5, pid=502),
    ]
    merge = _write_merge(artifacts, "USR-501", sha)
    old = started - 5_000
    for path in (*lock_paths, merge):
        os.utime(path, (old, old))
    before = {path.name: path.read_bytes() for path in lock_paths}
    clock = _Clock(1_800_000_000.0)
    common = dict(clock=clock, locks=locks, artifacts=artifacts, repo=repo, service=service)

    alerted = _check_stale(tmp_path, monkeypatch, **common)
    assert alerted.status == SentinelStatus.IDLE
    assert alerted.is_stalled is False
    assert alerted.findings == [STALE_LOCK_FINDING]
    assert alerted.alert_sent is True
    records = alerted.details[STALE_LOCK_FINDING]["records"]
    assert {row["lock_name"] for row in records} == {"line.lock", "line.fleet-grokbox.lock"}
    assert {row["ticket_id"] for row in records} == {"USR-501"}
    assert {row["merge_sha"] for row in records} == {sha}
    episode = alerted.stale_lock_episode
    assert episode is not None and episode.startswith("stale_lock:")
    assert alerted.stale_lock_alert_episode == episode
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert observed[0]["details"]["finding"] == STALE_LOCK_FINDING
    assert telegram.send_message.call_count == 1
    assert "Trava anterior ao merge" in telegram.send_message.call_args.kwargs["text"]
    assert {path.name: path.read_bytes() for path in locks.iterdir() if path.is_file()} == before
    assert not any(".stale-" in path.name for path in locks.iterdir())

    clock.now = 1_800_000_100.0
    repeated = _check_stale(tmp_path, monkeypatch, **common)
    assert repeated.findings == [STALE_LOCK_FINDING]
    assert repeated.alert_sent is False
    assert repeated.stale_lock_alert_episode == episode
    assert telegram.send_message.call_count == 1
    assert {path.name: path.read_bytes() for path in locks.iterdir() if path.is_file()} == before

    for path in lock_paths:
        path.unlink()
    _write_lock(locks, "line.lock", "USR-501", float(committer) + 10, pid=503)
    cleared = _check_stale(tmp_path, monkeypatch, **common)
    assert STALE_LOCK_FINDING not in cleared.findings
    assert cleared.stale_lock_episode is None
    assert cleared.stale_lock_alert_episode is None
    assert telegram.send_message.call_count == 1
    assert (locks / "line.lock").is_file()

    fresh = _write_lock(locks, "line.fleet-notebook.lock", "USR-501", float(committer) - 80, pid=504)
    os.utime(fresh, (old, old))
    restarted = _check_stale(tmp_path, monkeypatch, **common)
    assert restarted.findings == [STALE_LOCK_FINDING]
    assert restarted.alert_sent is True
    assert restarted.stale_lock_alert_episode != episode
    assert {row["lock_name"] for row in restarted.details[STALE_LOCK_FINDING]["records"]} == {
        "line.fleet-notebook.lock",
    }
    assert telegram.send_message.call_count == 2
    assert fresh.read_bytes() == (locks / "line.fleet-notebook.lock").read_bytes()


def test_stale_lock_ignores_later_lock_other_ticket_and_unresolvable_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Trava posterior ao merge, merge de outro ticket e merge ilegivel nao alertam."""
    repo = tmp_path / "repo"
    sha = _git_commit(repo)
    committer = float(sentinel_mod._git_committer_epoch(sha, repo) or 0)
    clock = _Clock(1_800_000_000.0)

    later = tmp_path / "later"
    later_lock = _write_lock(later / "locks", "line.lock", "USR-10", committer + 10)
    _write_merge(later / "artifacts", "USR-10", sha)
    _write_lock(later / "locks", "line.fleet.lock", "USR-10", committer - 100)
    _write_lock(later / "locks", "notes.lock", "USR-10", committer - 100)
    later_before = later_lock.read_bytes()
    later_report = _check_stale(
        later, monkeypatch, clock=clock, locks=later / "locks", artifacts=later / "artifacts", repo=repo,
    )
    assert STALE_LOCK_FINDING not in later_report.findings
    assert later_report.stale_lock_episode is None
    assert later_lock.read_bytes() == later_before

    equal = tmp_path / "equal"
    _write_lock(equal / "locks", "line.fleet-desktop.lock", "USR-11", committer)
    _write_merge(equal / "artifacts", "USR-11", sha)
    equal_report = _check_stale(
        equal, monkeypatch, clock=clock, locks=equal / "locks", artifacts=equal / "artifacts", repo=repo,
    )
    assert STALE_LOCK_FINDING not in equal_report.findings

    other = tmp_path / "other"
    _write_lock(other / "locks", "line.lock", "USR-12", committer - 100)
    _write_merge(other / "artifacts", "USR-99", sha)
    other_report = _check_stale(
        other, monkeypatch, clock=clock, locks=other / "locks", artifacts=other / "artifacts", repo=repo,
    )
    assert STALE_LOCK_FINDING not in other_report.findings
    assert STALE_LOCK_FINDING not in other_report.details

    missing = tmp_path / "missing"
    _write_lock(missing / "locks", "line.lock", "USR-13", committer - 100)
    missing_report = _check_stale(
        missing, monkeypatch, clock=clock, locks=missing / "locks", artifacts=missing / "artifacts", repo=repo,
    )
    assert STALE_LOCK_FINDING not in missing_report.findings

    invalid = tmp_path / "invalid"
    bad = _write_merge(invalid / "artifacts", "USR-14", "nao-e-sha")
    _write_lock(invalid / "locks", "line.lock", "USR-14", 1_000.0)
    invalid_report = _check_stale(
        invalid, monkeypatch, clock=clock, locks=invalid / "locks", artifacts=invalid / "artifacts", repo=repo,
    )
    assert STALE_LOCK_FINDING not in invalid_report.findings
    assert bad.is_file()


def test_git_failure_does_not_use_file_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Git sem committer time nao promove o mtime do merge.json nem o da trava."""
    repo = tmp_path / "repo"
    sha = _git_commit(repo)
    clock = _Clock(1_800_000_000.0)
    case = tmp_path / "broken"
    locks = case / "locks"
    artifacts = case / "artifacts"
    absent = "ab" * 20
    lock = _write_lock(locks, "line.lock", "USR-15", 1_000.0)
    merge = _write_merge(artifacts, "USR-15", absent)
    assert sentinel_mod._git_committer_epoch(absent, repo) is None
    assert lock.stat().st_mtime > 1_000
    assert merge.stat().st_mtime > 1_000
    before = lock.read_bytes()
    missed = _check_stale(
        case, monkeypatch, clock=clock, locks=locks, artifacts=artifacts, repo=repo, notify=True,
    )
    assert STALE_LOCK_FINDING not in missed.findings
    assert missed.alert_sent is False
    assert lock.read_bytes() == before

    _write_merge(artifacts, "USR-16", sha)
    _write_lock(locks, "line.fleet-notebook.lock", "USR-16", 1_000.0)

    def explode(*_args, **_kwargs):
        raise OSError("git quebrado")

    monkeypatch.setattr(sentinel_mod.subprocess, "run", explode)
    raised = _check_stale(
        case / "raised", monkeypatch, clock=clock, locks=locks, artifacts=artifacts, repo=repo,
    )
    assert STALE_LOCK_FINDING not in raised.findings
    assert (locks / "line.fleet-notebook.lock").is_file()
    assert merge.is_file()


def test_light_sample_alerts_stale_lock_without_quota_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A amostra de 15 minutos alerta a trava sem pick, read_quotas ou apagar o arquivo."""
    service, telegram, observed = _idle_service(tmp_path)
    repo = tmp_path / "repo"
    sha = _git_commit(repo)
    committer = float(sentinel_mod._git_committer_epoch(sha, repo) or 0)
    locks = tmp_path / "line_lock"
    artifacts = tmp_path / "artifacts"
    lock = _write_lock(locks, "line.lock", "USR-501", committer - 30)
    _write_merge(artifacts, "USR-501", sha)
    before = lock.read_bytes()
    state = tmp_path / "sentinel_state.json"
    clock = _Clock(1_800_000_000.0)
    _seed_cached_report(state, now=clock.now, route="claude")
    _use_real_locks(monkeypatch)

    def run():
        with patch("core.line.sentinel.check_factory_status") as check, \
             patch("core.line.routing.pick") as pick, \
             patch("core.line.sentinel.read_quotas") as quotas, \
             patch("core.line.fleet_dispatcher.running_fleet_runs", return_value={}), \
             patch("core.line.fleet_dispatcher.default_demands_loader") as demands:
            result = maybe_run_sentinel(
                interval_sec=2700.0,
                path=state,
                clock=clock,
                notify_on_stall=True,
                fleet_state_loader=lambda: {},
                pending_delivery_loader=lambda: [],
                reservation_loader=lambda: [],
                live_pids_loader=lambda: set(),
                notification_service_factory=lambda: service,
                lock_directory=locks,
                ticket_artifacts_root=artifacts,
                git_repo=repo,
            )
        assert result is None
        assert check.call_count == 0
        assert pick.call_count == 0
        assert quotas.call_count == 0
        assert demands.call_count == 0
        return load_last_report(state)

    alerted = run()
    assert alerted is not None
    assert alerted.findings == [STALE_LOCK_FINDING]
    assert alerted.alert_sent is True
    assert alerted.status == SentinelStatus.HEALTHY
    assert alerted.last_run_epoch == clock.now
    assert alerted.quotas["claude"] == 80.0
    assert alerted.route_development == "claude"
    assert len(observed) == 1
    assert observed[0]["channel"] == NotificationChannel.ALL
    assert telegram.send_message.call_count == 1
    assert lock.read_bytes() == before

    clock.now = 1_800_000_050.0
    repeated = run()
    assert repeated is not None
    assert repeated.findings == [STALE_LOCK_FINDING]
    assert repeated.alert_sent is False
    assert repeated.stale_lock_alert_episode == alerted.stale_lock_episode
    assert telegram.send_message.call_count == 1
    assert lock.read_bytes() == before


def test_briefing_lists_the_three_findings_when_present(monkeypatch: pytest.MonkeyPatch):
    """O briefing mostra idle_queue, active_without_process e stale_lock so quando existem."""
    from core.ops import briefing

    def install(findings: list[str]) -> None:
        report = SentinelReport(
            status=SentinelStatus.HEALTHY,
            is_stalled=False,
            reason="Linha operando normalmente.",
            findings=findings,
            details={name: {"episode_key": name} for name in findings},
        )
        monkeypatch.setattr(sentinel_mod, "load_last_report", lambda path=None: report)

    install(["idle_queue", "status_without_process", "stale_lock"])
    listed = "\n".join(briefing._sentinel())
    assert "Achados: idle_queue, active_without_process, stale_lock" in listed

    install(["idle_queue"])
    only_idle = "\n".join(briefing._sentinel())
    assert "idle_queue" in only_idle
    assert "active_without_process" not in only_idle
    assert "stale_lock" not in only_idle

    install([])
    assert "Achados:" not in "\n".join(briefing._sentinel())
