"""Sentinela autonomo da linha de producao da Dark Factory (USR-439).

Monitora a saude da esteira de desenvolvimento em qualquer harness (Antigravity, Claude Code,
Codex, Grok e workers headless da frota). Executa com intervalo deterministico de 45 minutos
(2700s via timestamp delta, evitando a assimetria do cron standard 5-campos minute % 45 == 0).

Verifica:
1. Se a fila possui tickets pendentes sem rota viavel (todos os harnesses com cota <= 15%).
2. Se tickets em execucao estao travados/estagnados sem progresso ha mais de 45 minutos.
3. Dispara alerta urgente e amigavel ao Owner via Telegram atraves do NotificationService.
4. Persiste telemetria estruturada em .factory/runs/sentinel_state.json para consulta por todos os harnesses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from core.paths import project_root, state_root

logger = logging.getLogger("darkfac.line.sentinel")

PROJECT_ROOT = project_root()
STATE_PATH = PROJECT_ROOT / ".factory" / "runs" / "sentinel_state.json"
DEFAULT_INTERVAL_SECONDS = 2700.0  # 45 minutos exatos
DEFAULT_STALL_TIMEOUT_SECONDS = 2700.0  # 45 minutos sem progresso em ticket em voo

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


def check_factory_status(
    *,
    max_flight_duration_sec: float = DEFAULT_STALL_TIMEOUT_SECONDS,
    notify_on_stall: bool = True,
    demands_loader: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    notification_dispatcher: Optional[Callable[..., Any]] = None,
    state_path: Optional[Path] = None,
    log_dir: Optional[Path] = None,
) -> SentinelReport:
    """Executa analise completa do estado da linha, rotas e cotas."""
    from core.line.fleet_dispatcher import default_demands_loader, running_fleet_runs
    from core.line.routing import pick

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

    # 4. Avaliacao de rotas
    try:
        dev_pick = pick(
            "development",
            ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        )
        dev_harness = dev_pick[0] if dev_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota de desenvolvimento: %s", exc)
        dev_harness = None

    try:
        fleet_dev_pick = pick(
            "development",
            ["harness:claude", "harness:codex", "harness:grok"],
        )
        fleet_dev_harness = fleet_dev_pick[0] if fleet_dev_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota da frota headless: %s", exc)
        fleet_dev_harness = None

    try:
        plan_pick = pick(
            "planning",
            ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"],
        )
        plan_harness = plan_pick[0] if plan_pick else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Erro ao checar rota de planejamento: %s", exc)
        plan_harness = None

    # 5. Deteccao de estagnacao em voo
    stalled_tickets: List[str] = []
    effective_log_dir = log_dir or (PROJECT_ROOT / ".factory" / "runs" / "fleet_dispatcher_logs")
    now_epoch = time.time()
    for tid, host in in_flight_map.items():
        log_file = effective_log_dir / f"{tid}-{host}.log"
        if log_file.is_file():
            try:
                mtime = log_file.stat().st_mtime
                if now_epoch - mtime > max_flight_duration_sec:
                    stalled_tickets.append(tid)
            except OSError:
                pass

    # 6. Determinacao do status
    is_stalled = False
    alert_sent = False
    reason = "Linha operando normalmente."
    status = SentinelStatus.HEALTHY

    if stalled_tickets:
        is_stalled = True
        status = SentinelStatus.STALLED_IN_FLIGHT
        reason = (
            f"Tickets em voo sem atividade ha mais de {int(max_flight_duration_sec // 60)} min: "
            f"{', '.join(stalled_tickets)}."
        )
    elif pending_tickets and dev_harness is None:
        is_stalled = True
        status = SentinelStatus.STALLED_NO_ROUTE
        quota_str = ", ".join(f"{h}={q}%" for h, q in quotas.items() if q is not None)
        reason = (
            f"Fila contem {len(pending_tickets)} ticket(s) pendente(s) ({', '.join(pending_tickets[:3])}), "
            f"mas nenhum harness possui cota acima do piso de 15% para desenvolvimento. Cotas: {quota_str}."
        )
    elif pending_tickets and fleet_dev_harness is None and dev_harness == "antigravity":
        status = SentinelStatus.DEGRADED
        reason = (
            f"Frota headless bloqueada por cota (Claude, Codex, Grok <= 15%). "
            f"Apenas o harness interativo Antigravity ({quotas.get('antigravity')}%) esta disponivel."
        )
    elif not pending_tickets and not in_flight_tickets:
        status = SentinelStatus.IDLE
        reason = "Fila vazia e nenhum ticket em execucao."

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
        details={
            "max_flight_duration_sec": max_flight_duration_sec,
            "hosts_in_flight": in_flight_map,
        },
    )

    _persist_report(report, state_path)
    return report


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
) -> Optional[SentinelReport]:
    """Executa a checagem somente se o intervalo deterministico (45 min) tiver transcorrido.

    Garante intervalo uniforme de 2700s em qualquer harness ou worker,
    eliminando a assimetria do cron standard 5-campos.
    """
    now = time.time()
    target = path or STATE_PATH
    last = load_last_report(target)
    if not force and last is not None:
        elapsed = now - last.last_run_epoch
        if elapsed < interval_sec:
            logger.debug(
                "Sentinela em janela de silencio: %.1fs decorridos de %.1fs",
                elapsed,
                interval_sec,
            )
            return None

    logger.info("Executando sentinela da linha (intervalo de %.0fs)...", interval_sec)
    return check_factory_status(notify_on_stall=notify_on_stall, state_path=target)


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
        q_str = ", ".join(f"{h}: {q}%" for h, q in report.quotas.items() if q is not None)
        print(f"* Cotas: {q_str}")

    return 2 if report.is_stalled else 0


if __name__ == "__main__":
    sys.exit(main())
