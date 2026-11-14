"""Sentinela autonomo da linha de producao da Dark Factory (USR-439, USR-501).

Monitora a saude da esteira de desenvolvimento em qualquer harness (Antigravity, Claude Code,
Codex, Grok e workers headless da frota). Executa com intervalo deterministico de 45 minutos
(2700s via timestamp delta, evitando a assimetria do cron standard 5-campos minute % 45 == 0).

Verifica:
1. Se a fila possui tickets pendentes sem rota viavel (todos os harnesses com cota <= 15%).
2. Se tickets em execucao estao travados/estagnados sem progresso ha mais de 45 minutos.
3. Dispara alerta urgente e amigavel ao Owner via Telegram atraves do NotificationService.
4. Persiste telemetria estruturada em .factory/runs/sentinel_state.json para consulta por todos os harnesses.
5. Fila ociosa (USR-501): host livre, rota de desenvolvimento e ticket elegivel ficam sem
   execucao. O alerta (Hub e Telegram, uma vez por episodio) sai depois de 15 minutos.
   Dentro da janela de 45 minutos o gancho nao sonda cota nem rota: a amostra leve avanca
   o relogio com o estado do despachante, a rota em cache e running_fleet_runs.
6. Status ativo sem processo (USR-501): in_flight local, pending_delivery running e reserva
   local active alertam uma vez por episodio quando o ticket e o pid nao estao em
   running_fleet_runs e o heartbeat passou de 120s. Processo vivo ou heartbeat ate 120s
   nao alerta. Run de nuvem fora dos hosts locais fica de fora. Nada e gravado na reserva,
   na entrega pendente, na trava nem no estado do despachante.
7. Trava anterior ao merge (USR-501): line.lock e line.fleet-*.lock alertam uma vez por
   episodio quando LineLockInfo.started_at e anterior ao committer time do merge_sha em
   merge.json do mesmo ticket. Trava posterior a esse merge, merge ilegivel e merge de
   outro ticket ficam em silencio. Git sem committer time nao usa o mtime do arquivo.
   O alerta nao apaga a trava. A amostra de 15 minutos, dentro da janela de 45 minutos,
   avalia os tres achados sem pick nem read_quotas.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional

from pydantic import BaseModel, Field

from core.paths import project_root, state_root

logger = logging.getLogger("darkfac.line.sentinel")

PROJECT_ROOT = project_root()
STATE_PATH = PROJECT_ROOT / ".factory" / "runs" / "sentinel_state.json"
DEFAULT_INTERVAL_SECONDS = 2700.0  # 45 minutos exatos
DEFAULT_STALL_TIMEOUT_SECONDS = 2700.0  # 45 minutos sem progresso em ticket em voo
DEFAULT_IDLE_QUEUE_SECONDS = 900.0  # 15 minutos com host, rota e ticket sem execucao
DEFAULT_STATUS_HEARTBEAT_SECONDS = 120.0  # heartbeat ate este valor ainda segura o alerta
IDLE_QUEUE_FINDING = "idle_queue"
STATUS_WITHOUT_PROCESS_FINDING = "status_without_process"
STALE_LOCK_FINDING = "stale_lock"
_LOCAL_HOST_FALLBACK = frozenset({"grokbox", "notebook", "desktop"})
_MANAGED_FINDINGS = frozenset({
    IDLE_QUEUE_FINDING,
    STATUS_WITHOUT_PROCESS_FINDING,
    STALE_LOCK_FINDING,
})
_LOCK_FILE_RE = re.compile(r"^(?:line\.lock|line\.fleet-.+\.lock)$")
_TICKET_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_GIT_SHOW_TIMEOUT_SEC = 10.0

HARNESS_PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
    "grok": "xai",
    "antigravity": "google",
}


class SentinelStatus(str, Enum):
    HEALTHY = "healthy"
    IDLE = "idle"
    STALLED_NO_ROUTE = "stalled_no_route"
    STALLED_IN_FLIGHT = "stalled_in_flight"
    DEGRADED = "degraded"


class SentinelReport(BaseModel):
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_run_epoch: float = Field(default_factory=time.time)
    status: SentinelStatus
    is_stalled: bool
    reason: str
    route_development: Optional[str] = None
    route_planning: Optional[str] = None
    fleet_route_development: Optional[str] = None
    pending_tickets: List[str] = Field(default_factory=list)
    in_flight_tickets: List[str] = Field(default_factory=list)
    stalled_tickets: List[str] = Field(default_factory=list)
    quotas: Dict[str, Optional[float]] = Field(default_factory=dict)
    alert_sent: bool = False
    idle_since_epoch: Optional[float] = None
    idle_episode_key: Optional[str] = None
    idle_alert_episode: Optional[str] = None
    status_without_process_episode: Optional[str] = None
    status_without_process_alert_episode: Optional[str] = None
    stale_lock_episode: Optional[str] = None
    stale_lock_alert_episode: Optional[str] = None
    findings: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


def read_quotas() -> Dict[str, Optional[float]]:
    """Le as cotas restantes de cada harness de forma rapida e segura."""
    from core.line.routing import _quota_headroom_diagnosis

    quotas: Dict[str, Optional[float]] = {}
    for harness, provider in HARNESS_PROVIDERS.items():
        try:
            headroom, _ = _quota_headroom_diagnosis(provider)
            quotas[harness] = round(headroom, 2) if headroom is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Falha ao ler cota do provedor %s: %s", provider, exc)
            quotas[harness] = None
    return quotas


def _probe_sentinel_routes() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Avalia rotas ativas de desenvolvimento, frota e planejamento."""
    from core.line.routing import pick

    try:
        dev_pick = pick("development", ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"])
        dev_harness = dev_pick[0] if dev_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota de desenvolvimento: %s", exc)
        dev_harness = None

    try:
        fleet_dev_pick = pick("development", ["harness:claude", "harness:codex", "harness:grok"])
        fleet_dev_harness = fleet_dev_pick[0] if fleet_dev_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota da frota headless: %s", exc)
        fleet_dev_harness = None

    try:
        plan_pick = pick("planning", ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"])
        plan_harness = plan_pick[0] if plan_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota de planejamento: %s", exc)
        plan_harness = None

    return dev_harness, fleet_dev_harness, plan_harness


def _find_stalled_in_flight_tickets(
    in_flight_map: Mapping[str, str],
    log_dir: Optional[Path],
    max_flight_duration_sec: float,
    now_epoch: float,
) -> list[str]:
    stalled_tickets: list[str] = []
    effective_log_dir = log_dir or (PROJECT_ROOT / ".factory" / "runs" / "fleet_dispatcher_logs")
    for tid, host in in_flight_map.items():
        log_file = effective_log_dir / f"{tid}-{host}.log"
        if log_file.is_file():
            try:
                if now_epoch - log_file.stat().st_mtime > max_flight_duration_sec:
                    stalled_tickets.append(tid)
            except OSError:
                pass
    return stalled_tickets


@dataclass
class _IdleDecision:
    idle_since_epoch: Optional[float]
    episode_key: Optional[str]
    alerted_episode: Optional[str]
    finding: bool
    alert_sent: bool
    detail: Optional[Dict[str, Any]]
    reason: str


def _string_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if not isinstance(raw, (list, tuple)):
        return []
    ordered: List[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def _read_fleet_state(loader: Optional[Callable[[], Dict[str, Any]]]) -> Dict[str, Any]:
    """Estado do ultimo ciclo do despachante (free_hosts, in_flight, queue_remaining)."""
    if loader is not None:
        try:
            loaded = loader()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Falha ao ler estado da frota para fila ociosa: %s", exc)
            return {}
        return loaded if isinstance(loaded, dict) else {}
    from core.line.fleet_dispatcher import STATE_PATH as fleet_state_path

    try:
        if not fleet_state_path.is_file():
            return {}
        data = json.loads(fleet_state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Falha ao ler estado da frota para fila ociosa: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _eligible_ticket_ids(demands: Any, fleet_state: Mapping[str, Any]) -> List[str]:
    """Ticket elegivel por select_tickets ou, na falta dele, por queue_remaining."""
    selected: List[str] = []
    rows = demands if isinstance(demands, list) else []
    try:
        from core.line.fleet_dispatcher import select_tickets

        picked, _skipped = select_tickets(rows, busy_ids=set())
        selected = [str(ticket_id) for ticket_id in picked if str(ticket_id).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao avaliar tickets elegiveis da fila: %s", exc)
    ordered: List[str] = []
    seen: set[str] = set()
    for ticket_id in [*selected, *_string_list(fleet_state.get("queue_remaining"))]:
        if ticket_id not in seen:
            seen.add(ticket_id)
            ordered.append(ticket_id)
    return ordered


def _has_dispatcher_in_flight(fleet_state: Mapping[str, Any]) -> bool:
    raw = fleet_state.get("in_flight")
    if isinstance(raw, dict):
        return any(str(key).strip() or str(value).strip() for key, value in raw.items())
    if isinstance(raw, (list, tuple, set)):
        return any(str(item).strip() for item in raw)
    return bool(raw)


def _idle_queue_message(route: str, free_hosts: List[str], eligible: List[str], elapsed_sec: float) -> str:
    shown = ", ".join(eligible[:5])
    if len(eligible) > 5:
        shown += f" (+{len(eligible) - 5} outros)"
    minutes = max(1, int(elapsed_sec // 60))
    return (
        "A esteira automatica da Dark Factory esta parada com capacidade ociosa.\n\n"
        "<b>Detalhes da Situacao:</b>\n"
        f"• <b>Situacao:</b> fila ociosa ha mais de {minutes} min\n"
        f"• <b>Hosts livres:</b> {', '.join(free_hosts)}\n"
        f"• <b>Rota de desenvolvimento:</b> {route}\n"
        f"• <b>Tickets elegiveis sem execucao:</b> {shown}\n\n"
        "<b>O que fazer:</b>\n"
        "Nenhuma acao sua e obrigatoria. O despachante segue tentando a cada ciclo. "
        "Se a fila continuar parada, abra o DarkHub e confira o estado da frota."
    )


def _send_idle_queue_alert(
    *,
    route: str,
    free_hosts: List[str],
    eligible: List[str],
    elapsed_sec: float,
    episode_key: str,
    idle_since_epoch: float,
    quotas: Dict[str, Optional[float]],
    notification_service_factory: Optional[Callable[[], Any]],
) -> bool:
    """Um evento no store do Hub e um envio Telegram, canal ALL."""
    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    service = (
        notification_service_factory()
        if notification_service_factory is not None
        else NotificationService(cooldown_minutes=45)
    )
    event = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Linha da Fabrica Parada: Fila Ociosa",
        message=_idle_queue_message(route, free_hosts, eligible, elapsed_sec),
        channel=NotificationChannel.ALL,
        details={
            "finding": IDLE_QUEUE_FINDING,
            "episode_key": episode_key,
            "idle_since_epoch": idle_since_epoch,
            "elapsed_sec": elapsed_sec,
            "free_hosts": free_hosts,
            "eligible_tickets": eligible,
            "route_development": route,
            "quotas": quotas,
        },
    )
    return event is not None


def _apply_idle_queue(
    *,
    previous: Optional[SentinelReport],
    now_epoch: float,
    threshold_sec: float,
    fleet_state: Mapping[str, Any],
    demands: Any,
    route: Optional[str],
    running_runs: Mapping[str, str],
    notify: bool,
    quotas: Dict[str, Optional[float]],
    notification_service_factory: Optional[Callable[[], Any]],
) -> _IdleDecision:
    """Persiste o inicio da ociosidade e alerta so depois que o episodio passa de 15 min.

    A comparacao e estrita (> 900s): no segundo exato o achado ainda nao existe.
    A mesma chave de episodio nao reenvia; a condicao zerada apaga a chave.
    """
    free_hosts = _string_list(fleet_state.get("free_hosts"))
    eligible = _eligible_ticket_ids(demands, fleet_state)
    route_name = str(route).strip() if route else ""
    active = (
        bool(free_hosts)
        and bool(route_name)
        and bool(eligible)
        and not running_runs
        and not _has_dispatcher_in_flight(fleet_state)
    )
    if not active:
        return _IdleDecision(None, None, None, False, False, None, "")

    if previous is not None and previous.idle_since_epoch is not None and previous.idle_since_epoch <= now_epoch:
        idle_since = float(previous.idle_since_epoch)
        episode = previous.idle_episode_key or f"idle_queue:{int(idle_since)}"
    else:
        idle_since = float(now_epoch)
        episode = f"idle_queue:{int(idle_since)}"
    alerted = previous.idle_alert_episode if previous is not None and previous.idle_alert_episode == episode else None
    elapsed = float(now_epoch) - idle_since
    finding = elapsed > threshold_sec
    if not finding:
        return _IdleDecision(idle_since, episode, alerted, False, False, None, "")

    detail = {
        "episode_key": episode,
        "idle_since_epoch": idle_since,
        "elapsed_sec": elapsed,
        "free_hosts": free_hosts,
        "eligible_tickets": eligible,
        "route_development": route_name,
    }
    shown = ", ".join(eligible[:3])
    reason = (
        f"Fila ociosa ha mais de {max(1, int(elapsed // 60))} min com host livre "
        f"({', '.join(free_hosts)}), rota {route_name} e ticket elegivel ({shown}) sem execucao."
    )
    sent = False
    if notify and alerted != episode:
        try:
            if _send_idle_queue_alert(
                route=route_name,
                free_hosts=free_hosts,
                eligible=eligible,
                elapsed_sec=elapsed,
                episode_key=episode,
                idle_since_epoch=idle_since,
                quotas=quotas,
                notification_service_factory=notification_service_factory,
            ):
                alerted = episode
                sent = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao enviar alerta de fila ociosa: %s", exc)
    return _IdleDecision(idle_since, episode, alerted, True, sent, detail, reason)


@dataclass(frozen=True)
class _ActiveStatus:
    source: str
    ticket_id: str
    pid: Optional[int]
    host_id: str
    heartbeat_epoch: Optional[float]
    started_at: Optional[float]
    raw_status: str


@dataclass
class _GhostDecision:
    finding: bool
    alert_sent: bool
    episode_key: Optional[str]
    alerted_episode: Optional[str]
    detail: Optional[Dict[str, Any]]


def _parse_epoch(value: Any) -> Optional[float]:
    """Epoch em segundos, ISO-8601, ou None quando o valor nao e um instante."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return number if number > 0 else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        epoch = parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return epoch if epoch > 0 else None


def _parse_pid(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _local_host_ids() -> frozenset[str]:
    """Hosts da frota configurada. Um id de fora dessa lista e run de nuvem."""
    try:
        from core.line.fleet_policy import load_fleet_config

        hosts = {host.id.strip().lower() for host in load_fleet_config().hosts if str(host.id).strip()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Falha ao ler hosts locais da frota: %s", exc)
        hosts = set()
    return frozenset(hosts or _LOCAL_HOST_FALLBACK)


def _is_local_host(host_id: str, local_hosts: frozenset[str]) -> bool:
    return bool(host_id) and host_id in local_hosts


def _mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "model_dump"):
        try:
            dumped = row.model_dump()
        except Exception:  # noqa: BLE001
            dumped = None
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _pid_is_alive(pid: int, started_at: Optional[float]) -> bool:
    """Processo vivo. Com started_at, pid reaproveitado pelo SO nao conta."""
    try:
        import psutil
    except Exception as exc:  # noqa: BLE001
        logger.debug("psutil indisponivel para o pid %s: %s", pid, exc)
        return False
    try:
        proc = psutil.Process(int(pid))
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if started_at and started_at > 0:
            return abs(float(proc.create_time()) - float(started_at)) < 5
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("Falha ao consultar o pid %s: %s", pid, exc)
        return False


def _read_pending_deliveries(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Le JSON de entrega pendente. Nao grava, nao chama tick e nao muda status."""
    try:
        from core.line.pending_delivery import queue_dir
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fila de entrega pendente indisponivel para o sentinela: %s", exc)
        return []
    folder = directory or queue_dir()
    if not folder.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Entrega pendente ilegivel em %s: %s", path.name, exc)
            continue
        if not isinstance(raw, dict) or str(raw.get("status") or "") != "running":
            continue
        heartbeat = _parse_epoch(raw.get("heartbeat_epoch"))
        if heartbeat is None:
            heartbeat = _parse_epoch(raw.get("heartbeat_at"))
        if heartbeat is None:
            try:
                heartbeat = float(path.stat().st_mtime)
            except OSError:
                heartbeat = _parse_epoch(raw.get("started_at"))
        rows.append({
            "ticket_id": raw.get("ticket_id") or raw.get("ticket"),
            "pid": raw.get("pid"),
            "host_id": raw.get("host_id") or raw.get("host") or "",
            "heartbeat_epoch": heartbeat,
            "started_at": raw.get("started_at"),
            "status": "running",
        })
    return rows


def _read_local_reservations() -> List[Any]:
    """Lista reservas sem renovar, expirar ou gravar."""
    try:
        from core.paths import state_root
    except Exception as exc:  # noqa: BLE001
        logger.warning("Raiz de estado indisponivel para reservas locais: %s", exc)
        return []
    if not os.environ.get("DARKFAC_HF02_DATABASE_URL") and not (state_root() / "control.db").is_file():
        return []
    try:
        from core.demands.queue_store import control_store

        return list(control_store().list_active_reservations())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao ler reservas locais para o sentinela: %s", exc)
        return []


def _status_from_dispatcher(fleet_state: Mapping[str, Any], local_hosts: frozenset[str]) -> List[_ActiveStatus]:
    raw = fleet_state.get("in_flight")
    if not isinstance(raw, dict):
        return []
    state_beat = _parse_epoch(fleet_state.get("heartbeat_epoch"))
    if state_beat is None:
        state_beat = _parse_epoch(fleet_state.get("heartbeat_at"))
    if state_beat is None:
        state_beat = _parse_epoch(fleet_state.get("ts"))
    found: List[_ActiveStatus] = []
    for host, value in raw.items():
        host_id = str(host).strip().lower()
        if not _is_local_host(host_id, local_hosts):
            continue
        if isinstance(value, Mapping):
            ticket = str(value.get("ticket_id") or value.get("ticket") or "").strip()
            pid = _parse_pid(value.get("pid"))
            beat = _parse_epoch(value.get("heartbeat_epoch"))
            if beat is None:
                beat = _parse_epoch(value.get("heartbeat_at"))
            started = _parse_epoch(value.get("started_at"))
        else:
            ticket = str(value).strip()
            pid = None
            beat = None
            started = None
        if beat is None:
            beat = state_beat
        if not ticket and pid is None:
            continue
        found.append(_ActiveStatus(
            "dispatcher_in_flight", ticket, pid, host_id, beat, started, "in_flight",
        ))
    return found


def _status_from_pending(rows: Any, local_hosts: frozenset[str]) -> List[_ActiveStatus]:
    found: List[_ActiveStatus] = []
    for row in rows if isinstance(rows, list) else []:
        data = _mapping(row)
        if str(data.get("status") or "") != "running":
            continue
        host_id = str(data.get("host_id") or data.get("host") or "").strip().lower()
        if host_id and not _is_local_host(host_id, local_hosts):
            continue
        ticket = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        pid = _parse_pid(data.get("pid"))
        if not ticket and pid is None:
            continue
        beat = _parse_epoch(data.get("heartbeat_epoch"))
        if beat is None:
            beat = _parse_epoch(data.get("heartbeat_at"))
        found.append(_ActiveStatus(
            "pending_delivery",
            ticket,
            pid,
            host_id,
            beat,
            _parse_epoch(data.get("started_at")),
            "running",
        ))
    return found


def _status_from_reservations(rows: Any, local_hosts: frozenset[str]) -> List[_ActiveStatus]:
    found: List[_ActiveStatus] = []
    for row in rows if isinstance(rows, list) else []:
        data = _mapping(row)
        if str(data.get("status") or "").strip().lower() != "active":
            continue
        host_id = str(data.get("host_id") or data.get("host") or "").strip().lower()
        if not _is_local_host(host_id, local_hosts):
            continue
        ticket = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        meta = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
        pid = _parse_pid(data.get("pid"))
        if pid is None:
            pid = _parse_pid(meta.get("pid"))
        if not ticket and pid is None:
            continue
        beat = _parse_epoch(data.get("heartbeat_epoch"))
        if beat is None:
            beat = _parse_epoch(data.get("heartbeat_at"))
        found.append(_ActiveStatus(
            "local_reservation", ticket, pid, host_id, beat, _parse_epoch(data.get("started_at")), "active",
        ))
    return found


def _load_rows(loader: Optional[Callable[[], Any]], fallback: Callable[[], Any], label: str) -> Any:
    source = loader if loader is not None else fallback
    try:
        loaded = source()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao ler %s para status sem processo: %s", label, exc)
        return []
    if isinstance(loaded, (list, tuple)):
        return list(loaded)
    return []


def _collect_active_statuses(
    fleet_state: Mapping[str, Any],
    *,
    pending_loader: Optional[Callable[[], Any]],
    reservation_loader: Optional[Callable[[], Any]],
) -> List[_ActiveStatus]:
    local_hosts = _local_host_ids()
    pending_rows = _load_rows(pending_loader, _read_pending_deliveries, "entregas pendentes")
    reservation_rows = _load_rows(reservation_loader, _read_local_reservations, "reservas locais")
    return [
        *_status_from_dispatcher(fleet_state, local_hosts),
        *_status_from_pending(pending_rows, local_hosts),
        *_status_from_reservations(reservation_rows, local_hosts),
    ]


def _alive_pids(samples: List[_ActiveStatus], loader: Optional[Callable[[], Collection[int]]]) -> set[int]:
    if loader is not None:
        try:
            return {int(pid) for pid in loader() if _parse_pid(pid) is not None}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Falha ao ler pids vivos para status sem processo: %s", exc)
            return set()
    return {
        sample.pid
        for sample in samples
        if sample.pid is not None and _pid_is_alive(sample.pid, sample.started_at)
    }


def _process_present(sample: _ActiveStatus, running: Mapping[str, str], live_pids: set[int]) -> bool:
    if sample.ticket_id and sample.ticket_id in running:
        return True
    if sample.pid is None:
        return False
    if sample.pid in live_pids or str(sample.pid) in running:
        return True
    return False


def _ghost_episode(samples: List[_ActiveStatus]) -> str:
    parts: List[str] = []
    ordered = sorted(samples, key=lambda item: (item.source, item.ticket_id, item.pid or -1, item.host_id))
    for item in ordered:
        beat = "none" if item.heartbeat_epoch is None else str(int(item.heartbeat_epoch))
        pid = "none" if item.pid is None else str(item.pid)
        parts.append(f"{item.source}:{item.ticket_id}:{pid}:{item.host_id}:{beat}")
    return "status_without_process:" + "|".join(parts)


def _source_label(source: str) -> str:
    return {
        "dispatcher_in_flight": "fila do despachante",
        "pending_delivery": "entrega pendente",
        "local_reservation": "reserva local",
    }.get(source, source)


def _status_without_process_message(records: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for record in records[:5]:
        ticket = record["ticket_id"] or "sem ticket"
        host = record["host_id"] or "sem host"
        pid = record["pid"] if record["pid"] is not None else "nenhum"
        lines.append(
            f"• <b>{ticket}</b> em {_source_label(str(record['source']))} ({host}, pid {pid})"
        )
    extra = f"\n• mais {len(records) - 5}" if len(records) > 5 else ""
    return (
        "A esteira automatica da Dark Factory encontrou status ativo sem processo correspondente.\n\n"
        "<b>Detalhes da Situacao:</b>\n"
        + "\n".join(lines)
        + extra
        + "\n\n<b>O que fazer:</b>\n"
        "Nenhuma acao sua e obrigatoria. O sentinela nao altera reserva, entrega pendente, "
        "trava nem o estado do despachante. Se um episodio novo avisar de novo, abra o DarkHub "
        "e confira se o processo ainda existe."
    )


def _send_status_without_process_alert(
    *,
    records: List[Dict[str, Any]],
    episode_key: str,
    notification_service_factory: Optional[Callable[[], Any]],
) -> bool:
    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    service = (
        notification_service_factory()
        if notification_service_factory is not None
        else NotificationService(cooldown_minutes=45)
    )
    event = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Linha da Fabrica: Status Ativo sem Processo",
        message=_status_without_process_message(records),
        channel=NotificationChannel.ALL,
        details={
            "finding": STATUS_WITHOUT_PROCESS_FINDING,
            "episode_key": episode_key,
            "records": records,
        },
    )
    return event is not None


def _record_ghost(sample: _ActiveStatus, now_epoch: float) -> Dict[str, Any]:
    age = None if sample.heartbeat_epoch is None else float(now_epoch) - float(sample.heartbeat_epoch)
    return {
        "source": sample.source,
        "ticket_id": sample.ticket_id,
        "pid": sample.pid,
        "host_id": sample.host_id,
        "status": sample.raw_status,
        "heartbeat_epoch": sample.heartbeat_epoch,
        "heartbeat_age_sec": age,
    }


def _apply_status_without_process(
    *,
    previous: Optional[SentinelReport],
    now_epoch: float,
    threshold_sec: float,
    fleet_state: Mapping[str, Any],
    running_runs: Mapping[str, str],
    notify: bool,
    notification_service_factory: Optional[Callable[[], Any]],
    pending_loader: Optional[Callable[[], Any]],
    reservation_loader: Optional[Callable[[], Any]],
    live_pids_loader: Optional[Callable[[], Collection[int]]],
) -> _GhostDecision:
    """Alerta so o status local cujo processo sumiu e o heartbeat ja passou de 120s.

    A comparacao e estrita (> 120s). Processo vivo ou heartbeat dentro do prazo
    zera o episodio. A mesma chave nao reenvia. Esta funcao nao grava as fontes.
    """
    samples = _collect_active_statuses(
        fleet_state, pending_loader=pending_loader, reservation_loader=reservation_loader,
    )
    live = _alive_pids(samples, live_pids_loader)
    ghosts = [
        sample
        for sample in samples
        if not _process_present(sample, running_runs, live)
        and (
            sample.heartbeat_epoch is None
            or (float(now_epoch) - float(sample.heartbeat_epoch)) > threshold_sec
        )
    ]
    if not ghosts:
        return _GhostDecision(False, False, None, None, None)

    episode = _ghost_episode(ghosts)
    already = (
        previous.status_without_process_alert_episode
        if previous is not None and previous.status_without_process_alert_episode == episode
        else None
    )
    records = [_record_ghost(sample, now_epoch) for sample in ghosts]
    sent = False
    alerted = already
    if notify and already != episode:
        try:
            if _send_status_without_process_alert(
                records=records,
                episode_key=episode,
                notification_service_factory=notification_service_factory,
            ):
                alerted = episode
                sent = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao enviar alerta de status sem processo: %s", exc)
    detail = {"episode_key": episode, "records": records}
    return _GhostDecision(True, sent, episode, alerted, detail)


def _read_line_locks(directory: Optional[Path] = None) -> List[tuple[str, Any]]:
    """Le ``line.lock`` e ``line.fleet-*.lock`` sem gravar nem apagar a trava."""
    from core.line.line_lock import LineLockInfo, lock_dir

    folder = directory if directory is not None else lock_dir()
    if not folder.is_dir():
        return []
    try:
        names = sorted(path.name for path in folder.iterdir() if path.is_file())
    except OSError as exc:
        logger.warning("Falha ao listar travas de linha: %s", type(exc).__name__)
        return []
    found: List[tuple[str, Any]] = []
    for name in names:
        if _LOCK_FILE_RE.fullmatch(name) is None:
            continue
        path = folder / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        info = LineLockInfo.from_dict(payload)
        if info is None:
            continue
        found.append((name, info))
    return found


def _ticket_artifacts_root(explicit: Optional[Path]) -> Path:
    if explicit is not None:
        return explicit
    from core.line.ticket_engine import artifacts_root

    return artifacts_root()


def _merge_sha_for_ticket(ticket_id: str, root: Optional[Path] = None) -> Optional[str]:
    """``merge_sha`` do ``merge.json`` deste ticket, ou None se o merge nao resolve."""
    ticket = str(ticket_id or "").strip()
    if _TICKET_TOKEN_RE.fullmatch(ticket) is None:
        return None
    base = _ticket_artifacts_root(root)
    path = (base / ticket / ".darkfac" / "runs" / ticket / "merge.json")
    try:
        if not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    sha = payload.get("merge_sha")
    if not isinstance(sha, str) or _COMMIT_SHA_RE.fullmatch(sha.strip()) is None:
        return None
    return sha.strip()


def _git_committer_epoch(sha: str, repo: Optional[Path] = None) -> Optional[float]:
    """Committer time do SHA. Falha de git devolve None e nao consulta mtime."""
    token = sha.strip()
    if _COMMIT_SHA_RE.fullmatch(token) is None:
        return None
    root = repo if repo is not None else PROJECT_ROOT
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%ct", token],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_SHOW_TIMEOUT_SEC,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Leitura do committer time falhou para %s: %s", token, type(exc).__name__)
        return None
    if completed.returncode != 0:
        logger.warning("git show do committer time falhou para %s", token)
        return None
    lines = (completed.stdout or "").strip().splitlines()
    if not lines:
        return None
    try:
        return float(lines[0].strip())
    except ValueError:
        return None


def _resolve_committer_epoch(
    sha: str,
    *,
    repo: Optional[Path],
    loader: Optional[Callable[[str], Optional[float]]],
) -> Optional[float]:
    if loader is None:
        return _git_committer_epoch(sha, repo)
    try:
        value = loader(sha)
    except Exception as exc:  # noqa: BLE001 - falha de leitura nao vira mtime
        logger.warning("Leitura do committer time falhou para %s: %s", sha, type(exc).__name__)
        return None
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stale_lock_episode(records: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    ordered = sorted(
        records,
        key=lambda row: (str(row["lock_name"]), str(row["ticket_id"]), str(row["merge_sha"]), float(row["started_at"])),
    )
    for row in ordered:
        parts.append(
            f"{row['lock_name']}:{row['ticket_id']}:{format(float(row['started_at']), '.3f')}:{row['merge_sha']}"
        )
    return "stale_lock:" + "|".join(parts)


def _stale_lock_message(records: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for record in records[:5]:
        sha = str(record["merge_sha"])
        lines.append(
            f"• <b>{record['ticket_id']}</b> em {record['lock_name']} "
            f"(pid {record['pid']}, merge {sha[:12]})"
        )
    extra = f"\n• mais {len(records) - 5}" if len(records) > 5 else ""
    return (
        "A esteira automatica da Dark Factory encontrou uma trava de linha "
        "mais antiga que o merge do proprio ticket.\n\n"
        "<b>Detalhes da Situacao:</b>\n"
        + "\n".join(lines)
        + extra
        + "\n\n<b>O que fazer:</b>\n"
        "Nenhuma acao sua e obrigatoria. O sentinela nao apaga a trava. "
        "Se um episodio novo avisar de novo, abra o DarkHub e confira quem ainda segura a linha."
    )


def _send_stale_lock_alert(
    *,
    records: List[Dict[str, Any]],
    episode_key: str,
    notification_service_factory: Optional[Callable[[], Any]],
) -> bool:
    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    service = (
        notification_service_factory()
        if notification_service_factory is not None
        else NotificationService(cooldown_minutes=45)
    )
    event = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Linha da Fabrica: Trava anterior ao merge",
        message=_stale_lock_message(records),
        channel=NotificationChannel.ALL,
        details={
            "finding": STALE_LOCK_FINDING,
            "episode_key": episode_key,
            "records": records,
        },
    )
    return event is not None


def _apply_stale_lock(
    *,
    previous: Optional[SentinelReport],
    notify: bool,
    notification_service_factory: Optional[Callable[[], Any]],
    lock_directory: Optional[Path] = None,
    ticket_artifacts_root: Optional[Path] = None,
    git_repo: Optional[Path] = None,
    committer_time_loader: Optional[Callable[[str], Optional[float]]] = None,
) -> _GhostDecision:
    """Alerta a trava cujo inicio e anterior ao merge do mesmo ticket.

    A comparacao e estrita (``started_at < committer``). Sem merge resolvivel,
    com git sem committer time, ou com merge de outro ticket, nao ha achado.
    Esta funcao so le a trava e o recibo: nao apaga nem reescreve ``line.lock``.
    """
    records: List[Dict[str, Any]] = []
    for name, info in _read_line_locks(lock_directory):
        sha = _merge_sha_for_ticket(str(info.ticket_id), ticket_artifacts_root)
        if sha is None:
            continue
        committer = _resolve_committer_epoch(sha, repo=git_repo, loader=committer_time_loader)
        if committer is None:
            continue
        started = float(info.started_at)
        if not started < committer:
            continue
        records.append({
            "lock_name": name,
            "ticket_id": str(info.ticket_id),
            "pid": int(info.pid),
            "hostname": str(info.hostname),
            "started_at": started,
            "merge_sha": sha,
            "committer_epoch": committer,
        })
    if not records:
        return _GhostDecision(False, False, None, None, None)

    episode = _stale_lock_episode(records)
    already = (
        previous.stale_lock_alert_episode
        if previous is not None and previous.stale_lock_alert_episode == episode
        else None
    )
    sent = False
    alerted = already
    if notify and already != episode:
        try:
            if _send_stale_lock_alert(
                records=records,
                episode_key=episode,
                notification_service_factory=notification_service_factory,
            ):
                alerted = episode
                sent = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao enviar alerta de trava anterior ao merge: %s", exc)
    detail = {"episode_key": episode, "records": records}
    return _GhostDecision(True, sent, episode, alerted, detail)


def _classify_line(
    *,
    stalled_tickets: List[str],
    pending_tickets: List[str],
    in_flight_tickets: List[str],
    dev_harness: Optional[str],
    fleet_dev_harness: Optional[str],
    quotas: Dict[str, Optional[float]],
    max_flight_duration_sec: float,
) -> tuple[SentinelStatus, bool, str]:
    if stalled_tickets:
        reason = (
            f"Tickets em voo sem atividade ha mais de {int(max_flight_duration_sec // 60)} min: "
            f"{', '.join(stalled_tickets)}."
        )
        return SentinelStatus.STALLED_IN_FLIGHT, True, reason
    if pending_tickets and dev_harness is None:
        quota_str = ", ".join(f"{h}={q}%" for h, q in quotas.items() if q is not None)
        reason = (
            f"Fila contem {len(pending_tickets)} ticket(s) pendente(s) ({', '.join(pending_tickets[:3])}), "
            f"mas nenhum harness possui cota acima do piso de 15% para desenvolvimento. Cotas: {quota_str}."
        )
        return SentinelStatus.STALLED_NO_ROUTE, True, reason
    if pending_tickets and fleet_dev_harness == "openrouter":
        reason = (
            "Frota headless operando em contingencia via OpenRouter (fallback ativo). "
            "Cotas de assinatura no piso critico (Claude, Codex, Grok <= 15%)."
        )
        return SentinelStatus.DEGRADED, False, reason
    if pending_tickets and fleet_dev_harness is None and dev_harness == "antigravity":
        reason = (
            f"Frota headless bloqueada por cota (Claude, Codex, Grok <= 15%). "
            f"Apenas o harness interativo Antigravity ({quotas.get('antigravity')}%) esta disponivel."
        )
        return SentinelStatus.DEGRADED, False, reason
    if not pending_tickets and not in_flight_tickets:
        return SentinelStatus.IDLE, False, "Fila vazia e nenhum ticket em execucao."
    return SentinelStatus.HEALTHY, False, "Linha operando normalmente."


def _compose_findings(previous: List[str], *, idle: bool, ghost: bool, stale: bool = False) -> List[str]:
    findings = [item for item in previous if item not in _MANAGED_FINDINGS]
    if idle:
        findings.append(IDLE_QUEUE_FINDING)
    if ghost:
        findings.append(STATUS_WITHOUT_PROCESS_FINDING)
    if stale:
        findings.append(STALE_LOCK_FINDING)
    return findings


def _record_probe(
    *,
    now_epoch: float,
    status: SentinelStatus,
    is_stalled: bool,
    reason: str,
    alert_sent: bool,
    dev_harness: Optional[str],
    plan_harness: Optional[str],
    fleet_dev_harness: Optional[str],
    pending_tickets: List[str],
    in_flight_tickets: List[str],
    stalled_tickets: List[str],
    quotas: Dict[str, Optional[float]],
    in_flight_map: Mapping[str, str],
    demands: Any,
    max_flight_duration_sec: float,
    notify: bool,
    state_path: Optional[Path],
    fleet_state_loader: Optional[Callable[[], Dict[str, Any]]],
    notification_service_factory: Optional[Callable[[], Any]],
    idle_queue_sec: float,
    pending_delivery_loader: Optional[Callable[[], Any]],
    reservation_loader: Optional[Callable[[], Any]],
    live_pids_loader: Optional[Callable[[], Collection[int]]],
    status_heartbeat_sec: float,
    lock_directory: Optional[Path],
    ticket_artifacts_root: Optional[Path],
    git_repo: Optional[Path],
    committer_time_loader: Optional[Callable[[str], Optional[float]]],
) -> SentinelReport:
    """Grava fila ociosa, status sem processo e trava anterior ao merge no mesmo relatorio."""
    target_state = state_path or STATE_PATH
    previous = load_last_report(target_state)
    fleet_state = _read_fleet_state(fleet_state_loader)
    idle = _apply_idle_queue(
        previous=previous,
        now_epoch=now_epoch,
        threshold_sec=idle_queue_sec,
        fleet_state=fleet_state,
        demands=demands,
        route=dev_harness,
        running_runs=in_flight_map,
        notify=notify,
        quotas=quotas,
        notification_service_factory=notification_service_factory,
    )
    ghost = _apply_status_without_process(
        previous=previous,
        now_epoch=now_epoch,
        threshold_sec=status_heartbeat_sec,
        fleet_state=fleet_state,
        running_runs=in_flight_map,
        notify=notify,
        notification_service_factory=notification_service_factory,
        pending_loader=pending_delivery_loader,
        reservation_loader=reservation_loader,
        live_pids_loader=live_pids_loader,
    )
    stale = _apply_stale_lock(
        previous=previous,
        notify=notify,
        notification_service_factory=notification_service_factory,
        lock_directory=lock_directory,
        ticket_artifacts_root=ticket_artifacts_root,
        git_repo=git_repo,
        committer_time_loader=committer_time_loader,
    )
    if idle.reason:
        reason = idle.reason
    alert_sent = alert_sent or idle.alert_sent or ghost.alert_sent or stale.alert_sent
    details: Dict[str, Any] = {
        "max_flight_duration_sec": max_flight_duration_sec,
        "hosts_in_flight": dict(in_flight_map),
    }
    if idle.finding and idle.detail is not None:
        details[IDLE_QUEUE_FINDING] = idle.detail
    if ghost.finding and ghost.detail is not None:
        details[STATUS_WITHOUT_PROCESS_FINDING] = ghost.detail
    if stale.finding and stale.detail is not None:
        details[STALE_LOCK_FINDING] = stale.detail
    report = SentinelReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        last_run_epoch=now_epoch,
        status=status,
        is_stalled=is_stalled,
        reason=reason,
        route_development=dev_harness,
        route_planning=plan_harness,
        fleet_route_development=fleet_dev_harness,
        pending_tickets=pending_tickets,
        in_flight_tickets=in_flight_tickets,
        stalled_tickets=stalled_tickets,
        quotas=quotas,
        alert_sent=alert_sent,
        idle_since_epoch=idle.idle_since_epoch,
        idle_episode_key=idle.episode_key,
        idle_alert_episode=idle.alerted_episode,
        status_without_process_episode=ghost.episode_key,
        status_without_process_alert_episode=ghost.alerted_episode,
        stale_lock_episode=stale.episode_key,
        stale_lock_alert_episode=stale.alerted_episode,
        findings=_compose_findings([], idle=idle.finding, ghost=ghost.finding, stale=stale.finding),
        details=details,
    )
    _persist_report(report, state_path)
    return report


def check_factory_status(
    *,
    max_flight_duration_sec: float = DEFAULT_STALL_TIMEOUT_SECONDS,
    notify_on_stall: bool = True,
    demands_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    notification_dispatcher: Optional[Callable[..., Any]] = None,
    state_path: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
    fleet_state_loader: Optional[Callable[[], Dict[str, Any]]] = None,
    notification_service_factory: Optional[Callable[[], Any]] = None,
    idle_queue_sec: float = DEFAULT_IDLE_QUEUE_SECONDS,
    pending_delivery_loader: Optional[Callable[[], Any]] = None,
    reservation_loader: Optional[Callable[[], Any]] = None,
    live_pids_loader: Optional[Callable[[], Collection[int]]] = None,
    status_heartbeat_sec: float = DEFAULT_STATUS_HEARTBEAT_SECONDS,
    lock_directory: Optional[Path] = None,
    ticket_artifacts_root: Optional[Path] = None,
    git_repo: Optional[Path] = None,
    committer_time_loader: Optional[Callable[[str], Optional[float]]] = None,
) -> SentinelReport:
    """Executa analise completa do estado da linha, rotas, cotas e achados USR-501."""
    from core.line.fleet_dispatcher import default_demands_loader, running_fleet_runs

    # 1. Carrega demandas
    loader = demands_loader or default_demands_loader
    try:
        demands = loader()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falha ao carregar demandas para o sentinela: %s", exc)
        demands = []

    pending_tickets = [
        str(d.get("id"))
        for d in demands
        if d.get("status") == "planned" and str(d.get("id", "")).startswith("USR-")
    ]

    # 2. Tickets em voo
    try:
        in_flight_map = running_fleet_runs()
        in_flight_tickets = list(in_flight_map.keys())
    except Exception as exc:  # noqa: BLE001
        logger.debug("Falha ao ler tickets em voo: %s", exc)
        in_flight_map = {}
        in_flight_tickets = []

    # 3. Cotas dos modelos
    quotas = read_quotas()

    # 4. Avaliacao de rotas e estagnacao
    dev_harness, fleet_dev_harness, plan_harness = _probe_sentinel_routes()
    now_epoch = time.time() if clock is None else float(clock())
    stalled_tickets = _find_stalled_in_flight_tickets(in_flight_map, log_dir, max_flight_duration_sec, now_epoch)

    status, is_stalled, reason = _classify_line(
        stalled_tickets=stalled_tickets,
        pending_tickets=pending_tickets,
        in_flight_tickets=in_flight_tickets,
        dev_harness=dev_harness,
        fleet_dev_harness=fleet_dev_harness,
        quotas=quotas,
        max_flight_duration_sec=max_flight_duration_sec,
    )
    alert_sent = False

    # 7. Disparo de notificacao se travado
    if is_stalled and notify_on_stall:
        try:
            if notification_dispatcher:
                notification_dispatcher(status, reason, pending_tickets, quotas)
                alert_sent = True
            else:
                alert_sent = _send_telegram_alert(status, reason, pending_tickets, quotas)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Falha ao enviar alerta do sentinela: %s", exc)

    return _record_probe(
        now_epoch=now_epoch,
        status=status,
        is_stalled=is_stalled,
        reason=reason,
        alert_sent=alert_sent,
        dev_harness=dev_harness,
        plan_harness=plan_harness,
        fleet_dev_harness=fleet_dev_harness,
        pending_tickets=pending_tickets,
        in_flight_tickets=in_flight_tickets,
        stalled_tickets=stalled_tickets,
        quotas=quotas,
        in_flight_map=in_flight_map,
        demands=demands,
        max_flight_duration_sec=max_flight_duration_sec,
        notify=notify_on_stall,
        state_path=state_path,
        fleet_state_loader=fleet_state_loader,
        notification_service_factory=notification_service_factory,
        idle_queue_sec=idle_queue_sec,
        pending_delivery_loader=pending_delivery_loader,
        reservation_loader=reservation_loader,
        live_pids_loader=live_pids_loader,
        status_heartbeat_sec=status_heartbeat_sec,
        lock_directory=lock_directory,
        ticket_artifacts_root=ticket_artifacts_root,
        git_repo=git_repo,
        committer_time_loader=committer_time_loader,
    )


def _send_telegram_alert(
    status: SentinelStatus,
    reason: str,
    pending_tickets: List[str],
    quotas: Dict[str, Optional[float]],
) -> bool:
    """Envia notificacao clara e amigavel (padrao leigo USR-438) ao Owner."""
    from core.notifications.models import AlertCategory, AlertSeverity
    from core.notifications.service import NotificationService

    service = NotificationService(cooldown_minutes=45)
    title = (
        "Linha da Fabrica Parada: Sem Rota Viavel"
        if status == SentinelStatus.STALLED_NO_ROUTE
        else "Linha da Fabrica Parada: Tarefa Estagnada"
    )

    quota_lines = []
    for h, val in quotas.items():
        v_str = f"{val}%" if val is not None else "indisponivel"
        quota_lines.append(f"• <b>{h.capitalize()}:</b> {v_str}")
    quota_block = "\n".join(quota_lines)

    tickets_str = ", ".join(pending_tickets[:5]) if pending_tickets else "Nenhum"
    if len(pending_tickets) > 5:
        tickets_str += f" (+{len(pending_tickets) - 5} outros)"

    message = (
        f"A esteira automatica da Dark Factory identificou uma pausa na operacao.\n\n"
        f"<b>Detalhes da Situacao:</b>\n"
        f"• <b>Status:</b> {status.value}\n"
        f"• <b>Motivo:</b> {reason}\n"
        f"• <b>Demandas pendentes:</b> {tickets_str}\n\n"
        f"<b>Saldo das Cotas:</b>\n{quota_block}\n\n"
        f"<b>O que fazer:</b>\n"
        f"O sistema aguarda automaticamente o reset de cotas dos provedores ou "
        f"voce pode autorizar um modelo alternativo pelo DarkHub."
    )

    res = service.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title=title,
        message=message,
        details={"status": status.value, "pending_tickets": pending_tickets},
    )
    return res is not None


def _persist_report(report: SentinelReport, path: Optional[Path] = None) -> None:
    target = path or STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(report.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        logger.warning("Falha ao gravar estado do sentinela: %s", exc)


def _read_running_runs(loader: Optional[Callable[[], Mapping[str, str]]]) -> Dict[str, str]:
    """``running_fleet_runs`` (ou o loader injetado) sem derrubar a amostra leve."""
    if loader is not None:
        source: Callable[[], Any] = loader
    else:
        from core.line.fleet_dispatcher import running_fleet_runs

        source = running_fleet_runs
    try:
        loaded = source()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Falha ao ler execucoes da frota na amostra leve: %s", exc)
        return {}
    if not isinstance(loaded, Mapping):
        return {}
    runs: Dict[str, str] = {}
    for key, value in loaded.items():
        ticket = str(key).strip()
        if ticket:
            runs[ticket] = str(value).strip()
    return runs


def _restore_idle_reason(previous: SentinelReport, details: Dict[str, Any]) -> str:
    """Devolve o motivo anterior ao achado de fila ociosa, se a amostra o tiver substituido."""
    saved = details.pop("baseline_reason", None)
    if isinstance(saved, str) and saved.strip():
        return saved
    if previous.reason.startswith("Fila ociosa"):
        if previous.status == SentinelStatus.IDLE:
            return "Fila vazia e nenhum ticket em execucao."
        if previous.status == SentinelStatus.HEALTHY:
            return "Linha operando normalmente."
    return previous.reason


def _light_idle_sample(
    previous: SentinelReport,
    *,
    now_epoch: float,
    threshold_sec: float,
    fleet_state_loader: Optional[Callable[[], Dict[str, Any]]],
    running_loader: Optional[Callable[[], Mapping[str, str]]],
    notify: bool,
    notification_service_factory: Optional[Callable[[], Any]],
    state_path: Path,
    pending_delivery_loader: Optional[Callable[[], Any]] = None,
    reservation_loader: Optional[Callable[[], Any]] = None,
    live_pids_loader: Optional[Callable[[], Collection[int]]] = None,
    status_heartbeat_sec: float = DEFAULT_STATUS_HEARTBEAT_SECONDS,
    lock_directory: Optional[Path] = None,
    ticket_artifacts_root: Optional[Path] = None,
    git_repo: Optional[Path] = None,
    committer_time_loader: Optional[Callable[[str], Optional[float]]] = None,
) -> None:
    """Avanca o relogio de 15 min sem pick, read_quotas ou carga de demandas.

    A rota e a do ultimo relatorio. O ticket elegivel sai so de ``queue_remaining``.
    Estado ausente nao faz git fetch e nao promove ``pending_tickets`` a elegivel.
    ``last_run_epoch`` continua sendo o da sonda completa. O status sem processo
    e a trava anterior ao merge entram na mesma amostra. A trava nao e apagada.
    Git sem committer time nao usa mtime.
    """
    fleet_state = _read_fleet_state(fleet_state_loader)
    running = _read_running_runs(running_loader)
    idle = _apply_idle_queue(
        previous=previous,
        now_epoch=now_epoch,
        threshold_sec=threshold_sec,
        fleet_state=fleet_state,
        demands=[],
        route=previous.route_development,
        running_runs=running,
        notify=notify,
        quotas=dict(previous.quotas),
        notification_service_factory=notification_service_factory,
    )
    ghost = _apply_status_without_process(
        previous=previous,
        now_epoch=now_epoch,
        threshold_sec=status_heartbeat_sec,
        fleet_state=fleet_state,
        running_runs=running,
        notify=notify,
        notification_service_factory=notification_service_factory,
        pending_loader=pending_delivery_loader,
        reservation_loader=reservation_loader,
        live_pids_loader=live_pids_loader,
    )
    stale = _apply_stale_lock(
        previous=previous,
        notify=notify,
        notification_service_factory=notification_service_factory,
        lock_directory=lock_directory,
        ticket_artifacts_root=ticket_artifacts_root,
        git_repo=git_repo,
        committer_time_loader=committer_time_loader,
    )
    details = dict(previous.details)
    if idle.finding:
        if "baseline_reason" not in details and not previous.reason.startswith("Fila ociosa"):
            details["baseline_reason"] = previous.reason
        if idle.detail is not None:
            details[IDLE_QUEUE_FINDING] = idle.detail
        reason = idle.reason or previous.reason
    elif idle.idle_since_epoch is None:
        details.pop(IDLE_QUEUE_FINDING, None)
        reason = _restore_idle_reason(previous, details)
    else:
        details.pop(IDLE_QUEUE_FINDING, None)
        reason = previous.reason
    if ghost.finding and ghost.detail is not None:
        details[STATUS_WITHOUT_PROCESS_FINDING] = ghost.detail
    else:
        details.pop(STATUS_WITHOUT_PROCESS_FINDING, None)
    if stale.finding and stale.detail is not None:
        details[STALE_LOCK_FINDING] = stale.detail
    else:
        details.pop(STALE_LOCK_FINDING, None)
    updated = previous.model_copy(update={
        "reason": reason,
        "alert_sent": idle.alert_sent or ghost.alert_sent or stale.alert_sent,
        "idle_since_epoch": idle.idle_since_epoch,
        "idle_episode_key": idle.episode_key,
        "idle_alert_episode": idle.alerted_episode,
        "status_without_process_episode": ghost.episode_key,
        "status_without_process_alert_episode": ghost.alerted_episode,
        "stale_lock_episode": stale.episode_key,
        "stale_lock_alert_episode": stale.alerted_episode,
        "findings": _compose_findings(
            list(previous.findings), idle=idle.finding, ghost=ghost.finding, stale=stale.finding,
        ),
        "details": details,
    })
    _persist_report(updated, state_path)


def load_last_report(path: Optional[Path] = None) -> Optional[SentinelReport]:
    target = path or STATE_PATH
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return SentinelReport.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Falha ao carregar ultimo relatorio do sentinela: %s", exc)
        return None


def maybe_run_sentinel(
    interval_sec: float = DEFAULT_INTERVAL_SECONDS,
    *,
    force: bool = False,
    notify_on_stall: bool = True,
    path: Optional[Path] = None,
    clock: Optional[Callable[[], float]] = None,
    demands_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    fleet_state_loader: Optional[Callable[[], Dict[str, Any]]] = None,
    running_loader: Optional[Callable[[], Mapping[str, str]]] = None,
    notification_service_factory: Optional[Callable[[], Any]] = None,
    idle_queue_sec: float = DEFAULT_IDLE_QUEUE_SECONDS,
    pending_delivery_loader: Optional[Callable[[], Any]] = None,
    reservation_loader: Optional[Callable[[], Any]] = None,
    live_pids_loader: Optional[Callable[[], Collection[int]]] = None,
    status_heartbeat_sec: float = DEFAULT_STATUS_HEARTBEAT_SECONDS,
    lock_directory: Optional[Path] = None,
    ticket_artifacts_root: Optional[Path] = None,
    git_repo: Optional[Path] = None,
    committer_time_loader: Optional[Callable[[str], Optional[float]]] = None,
) -> Optional[SentinelReport]:
    """Sonda completa a cada 45 min; no intervalo, amostra leve sem sonda de cota.

    Dentro da janela nao chama ``check_factory_status``, ``pick`` nem ``read_quotas``.
    A amostra de 15 minutos atualiza ``idle_since_epoch`` com o estado do despachante,
    a rota em cache e ``running_fleet_runs``, avalia status local sem processo e a
    trava anterior ao merge. ``--force`` e a janela vencida seguem na sonda completa.
    """
    now = time.time() if clock is None else float(clock())
    target = path or STATE_PATH
    last = load_last_report(target)
    if not force and last is not None and (now - last.last_run_epoch) < interval_sec:
        logger.debug(
            "Sentinela em janela de silencio (%.1fs de %.1fs); amostra leve da fila, sem sonda de cota ou rota",
            now - last.last_run_epoch,
            interval_sec,
        )
        _light_idle_sample(
            last,
            now_epoch=now,
            threshold_sec=idle_queue_sec,
            fleet_state_loader=fleet_state_loader,
            running_loader=running_loader,
            notify=notify_on_stall,
            notification_service_factory=notification_service_factory,
            state_path=target,
            pending_delivery_loader=pending_delivery_loader,
            reservation_loader=reservation_loader,
            live_pids_loader=live_pids_loader,
            status_heartbeat_sec=status_heartbeat_sec,
            lock_directory=lock_directory,
            ticket_artifacts_root=ticket_artifacts_root,
            git_repo=git_repo,
            committer_time_loader=committer_time_loader,
        )
        return None

    logger.info("Executando sentinela da linha (intervalo de %.0fs)...", interval_sec)
    probe_clock = clock if clock is not None else (lambda: now)
    return check_factory_status(
        notify_on_stall=notify_on_stall,
        state_path=target,
        demands_loader=demands_loader,
        clock=probe_clock,
        fleet_state_loader=fleet_state_loader,
        notification_service_factory=notification_service_factory,
        idle_queue_sec=idle_queue_sec,
        pending_delivery_loader=pending_delivery_loader,
        reservation_loader=reservation_loader,
        live_pids_loader=live_pids_loader,
        status_heartbeat_sec=status_heartbeat_sec,
        lock_directory=lock_directory,
        ticket_artifacts_root=ticket_artifacts_root,
        git_repo=git_repo,
        committer_time_loader=committer_time_loader,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinela da linha Dark Factory (USR-439)")
    parser.add_argument("--force", action="store_true", help="Forcar execucao ignorando intervalo de 45m")
    parser.add_argument("--interval-sec", type=float, default=DEFAULT_INTERVAL_SECONDS, help="Intervalo em segundos (padrao 2700 = 45m)")
    parser.add_argument("--json", action="store_true", help="Saida em JSON estruturado")
    parser.add_argument("--quiet", action="store_true", help="Modo silencioso (sem saida stdout se saudavel)")
    parser.add_argument("--no-notify", action="store_true", help="Nao enviar alertas ao Telegram")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if not args.quiet else logging.WARNING)

    report = maybe_run_sentinel(
        interval_sec=args.interval_sec,
        force=args.force,
        notify_on_stall=not args.no_notify,
    )

    if report is None and not args.force:
        report = load_last_report()

    if args.json:
        if report:
            print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
        else:
            print("{}")
        return 0

    if report is None:
        if not args.quiet:
            print("Sentinela: nenhum relatorio disponivel e intervalo nao expirou.")
        return 0

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not args.quiet or report.is_stalled:
        icon = "[ALERTA]" if report.is_stalled else ("[AVISO]" if report.status == SentinelStatus.DEGRADED else "[OK]")
        print(f"{icon} Sentinela da Linha | Status: {report.status.value.upper()}")
        print(f"* Motivo: {report.reason}")
        print(f"* Rota Dev: {report.route_development or 'NENHUMA'} (Frota: {report.fleet_route_development or 'NENHUMA'})")
        print(f"* Rota Plan: {report.route_planning or 'NENHUMA'}")
        print(f"* Demandas pendentes: {len(report.pending_tickets)} | Em voo: {len(report.in_flight_tickets)}")
        if report.findings:
            print(f"* Achados: {', '.join(report.findings)}")
        q_str = ", ".join(f"{h}: {q}%" for h, q in report.quotas.items() if q is not None)
        print(f"* Cotas: {q_str}")

    return 2 if report.is_stalled else 0


if __name__ == "__main__":
    sys.exit(main())
