"""Ticket `implementing`/`validating` sem vida, e a volta a `planned` (USR-498).

Evidencia (15-16/11): tickets ficaram em `validating`/`implementing` por dias sem nenhum
processo, trava, entrega pendente ou job aberto associado -- o despachante parou e nada
devolveu o ticket a `planned`.

`find_stale_active` e `collect_liveness` so DETECTAM (T1). `reconcile_stale_active` (T2)
devolve cada ticket detectado a `planned` por `DemandsStore.requeue_ticket`, acrescenta a
causa em `delivery_evidence` sem apagar o texto anterior e nao mexe em branch, worktree nem
trava de linha. O despachante chama esse sweep no inicio de cada ciclo, antes de
`select_tickets` (T3). `--resume-only` nao chama. `--dry-run` so lista. Ticket em
`in_flight` deste processo entra como vivo, antes das sondas.

Duas pecas deliberadamente separadas:

* `find_stale_active` e pura: dado um lote de tickets e um juiz de vivacidade `is_alive`
  ja resolvido, decide quais tickets `implementing`/`validating` estao ociosos ha mais de
  `idle_limit` sem nenhuma evidencia de vida. Sem ela nenhuma funcao de IO precisa ser
  mockada para testar a logica de filtragem e janela de tempo.
* `collect_liveness` e a agregadora impura: roda, para UM ticket, as quatro sondas que
  realmente têm acesso a processo, trava, fila de entrega e control store, e devolve um
  unico booleano. Fail-closed por sonda (USR-501 ja usa o mesmo principio para os achados do
  sentinela): uma sonda que nao consegue responder (excecao) faz o ticket ser tratado como
  vivo, e a excecao nunca escapa desta funcao -- um defeito de leitura nunca pode causar uma
  reconciliacao indevida.

As quatro sondas reaproveitam modulos ja existentes em vez de reimplementar deteccao de
processo:

1. Processo (`core.line.fleet_dispatcher.running_fleet_runs`): cobre runs da frota
   (`--fleet`) e entregas retomadas pelo chat (`--resume-delivery`), ja detectados por
   varredura de `cmdline`.
2. Trava de linha (`core.line.line_lock.get_lock_info`): cobre a sessao manual
   (`run_ticket.py <TICKET>` sem flags), que NAO aparece em `running_fleet_runs` porque nao
   carrega `--fleet` nem `--resume-delivery`. `get_lock_info` ja encapsula a checagem de PID
   vivo e heartbeat (USR-250); esta sonda so compara o `ticket_id` gravado na trava, sem reler
   PID por conta propria.
3. Entrega pendente (`core.line.pending_delivery.queue_dir`): um registro com
   `status == "running"` e o subprocesso de retomada que a propria fila ja rastreia; `status`
   `waiting`/`blocked` nao conta como vida (e exatamente o caso que a esteira parada produz).
4. Run aberto no control store (`ControlStore.list_active_run_ids` /
   `.get_run_status`): cobre jobs ainda abertos (`RUN_OPEN_JOB_STATUSES`) para o ticket,
   mesmo sem processo, trava ou entrega pendente associados nesta maquina.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from core.line.route_wait import parse_run_created_at

logger = logging.getLogger(__name__)

#: Unicos status de ticket que esta deteccao considera "em execucao" (DeliveryStatus).
ACTIVE_STATUSES = frozenset({"implementing", "validating"})

#: Um ticket "em dias", nao em minutos: a janela e bem mais larga que o estagnamento de
#: job em voo (`core.workflow.line_live.QUEUED_STALL_LIMIT`, 30 min) porque aqui o alvo e um
#: despachante parado, nao uma etapa lenta.
DEFAULT_IDLE_LIMIT: timedelta = timedelta(hours=24)

STALE_ACTIVE_CAUSE = "stale_active_no_liveness"

#: Teto de um sweep: o mesmo teto do varredor de runs orfaos, para um ciclo nao reescrever a fila inteira.
MAX_STALE_ACTIVE_ACTIONS = 25


@dataclass(frozen=True)
class StaleActiveTicket:
    """Um ticket `implementing`/`validating` sem nenhuma evidencia de vida ha mais de `idle_limit`."""

    ticket_id: str
    status: str
    idle: timedelta
    cause: str = field(default=STALE_ACTIVE_CAUSE)


def _ticket_moment(ticket: Mapping[str, Any]) -> Optional[datetime]:
    """`updated_at` e, na falta dele, `created_at` -- o mesmo par usado para idade em `orphan_runs`."""
    for key in ("updated_at", "created_at"):
        moment = parse_run_created_at(ticket.get(key))
        if moment is not None:
            return moment
    return None


def find_stale_active(
    tickets: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    is_alive: Callable[[str], bool],
    idle_limit: timedelta = DEFAULT_IDLE_LIMIT,
) -> list[StaleActiveTicket]:
    """Tickets `implementing`/`validating` ociosos ha mais de `idle_limit` e sem vida.

    Funcao pura: nunca toca disco, processo ou store. `is_alive(ticket_id)` ja encapsula
    qualquer decisao de vivacidade (tipicamente `collect_liveness` parcialmente aplicada);
    so e chamada depois que o filtro de status e idade ja descartou o ticket.
    """
    stale: list[StaleActiveTicket] = []
    for ticket in tickets:
        if not isinstance(ticket, Mapping):
            continue
        status = str(ticket.get("status") or "")
        if status not in ACTIVE_STATUSES:
            continue
        ticket_id = str(ticket.get("id") or "").strip()
        if not ticket_id:
            continue
        moment = _ticket_moment(ticket)
        if moment is None:
            continue
        idle = now - moment
        if idle <= idle_limit:
            continue
        if is_alive(ticket_id):
            continue
        stale.append(StaleActiveTicket(ticket_id=ticket_id, status=status, idle=idle))
    return stale


# --------------------------------------------------------------------------
# Sondas de vida (impuras) agregadas em collect_liveness
# --------------------------------------------------------------------------


def _probe_process(ticket_id: str, running_runs: Optional[Mapping[str, str]]) -> bool:
    if running_runs is None:
        from core.line.fleet_dispatcher import running_fleet_runs

        running_runs = running_fleet_runs()
    return ticket_id in running_runs


def _probe_line_lock(ticket_id: str, root: Optional[Path]) -> bool:
    from core.line.line_lock import get_lock_info

    info = get_lock_info(root)
    return info is not None and info.ticket_id == ticket_id


def _probe_pending_delivery(ticket_id: str, directory: Optional[Path]) -> bool:
    from core.line.pending_delivery import PendingDelivery, queue_dir

    folder = directory if directory is not None else queue_dir()
    path = folder / f"{ticket_id}.json"
    if not path.is_file():
        return False
    row = PendingDelivery.model_validate_json(path.read_text(encoding="utf-8"))
    return row.status == "running"


def _probe_open_run(ticket_id: str, store: Optional[Any]) -> bool:
    from core.workflow.control_store import RUN_OPEN_JOB_STATUSES

    if store is None:
        from core.line.store_selection import default_control_store

        store = default_control_store()
    for run_id in store.list_active_run_ids():
        status = store.get_run_status(run_id)
        if not isinstance(status, Mapping):
            continue
        for job in status.get("jobs") or []:
            if not isinstance(job, Mapping):
                continue
            if str(job.get("ticket_id")) == ticket_id and job.get("status") in RUN_OPEN_JOB_STATUSES:
                return True
    return False


def stale_active_evidence(*, cause: str, previous_status: str) -> str:
    """Uma linha estavel: a causa e o status anterior, sem idade (a idade muda a cada sweep)."""
    safe_cause = " ".join(str(cause).split())
    safe_status = " ".join(str(previous_status).split())
    return f"stale_active cause={safe_cause} previous_status={safe_status}"


@dataclass(frozen=True)
class StaleActiveAction:
    """O que um sweep fez, ou faria em `dry_run`, com um ticket detectado."""

    ticket_id: str
    previous_status: str
    cause: str
    idle_seconds: int
    action: str  # "requeued" | "would_requeue" | "refused" | "error"
    detail: str = ""


def _ticket_row(ticket: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(ticket, Mapping):
        return ticket
    dump = getattr(ticket, "model_dump", None)
    if dump is None:
        return None
    row = dump(mode="json")
    return row if isinstance(row, Mapping) else None


def _log_action(action: StaleActiveAction) -> None:
    payload = {
        "ticket": action.ticket_id,
        "previous_status": action.previous_status,
        "idle_seconds": action.idle_seconds,
        "cause": action.cause,
        "action": action.action,
    }
    logger.info("stale_active_reconciled %s", json.dumps(payload, sort_keys=True, ensure_ascii=False))


def reconcile_stale_active(
    store: Any,
    *,
    now: Optional[datetime] = None,
    idle_limit: timedelta = DEFAULT_IDLE_LIMIT,
    is_alive: Optional[Callable[[str], bool]] = None,
    max_actions: int = MAX_STALE_ACTIVE_ACTIONS,
    dry_run: bool = False,
) -> list[StaleActiveAction]:
    """Volta a `planned` cada ticket detectado, no maximo `max_actions` por chamada.

    Cada acao chama `store.requeue_ticket` com a causa e a linha de evidencia. Ticket ja
    `planned` nao e regravado. `completed` e `cancelled` continuam recusados por esse caminho
    (`ValueError`); a recusa vira acao `refused` e o ledger desse ticket nao muda.
    `dry_run` nao chama `requeue_ticket`. Run vivo (`is_alive` verdadeiro, por omissao
    `collect_liveness`) nao entra na lista. Nao apaga branch, worktree nem trava.

    Nunca propaga: falha ao listar o ledger devolve lista vazia; falha inesperada num ticket
    vira acao `error` e o sweep segue.
    """
    effective_now = now or datetime.now(timezone.utc)
    alive = is_alive if is_alive is not None else (lambda ticket_id: collect_liveness(ticket_id))
    lister = getattr(store, "list_tickets", None)
    if lister is None:
        return []
    try:
        listed = list(lister())
    except Exception as exc:  # noqa: BLE001 - sem lista nao ha o que reconciliar
        logger.warning("stale_active reconcile could not list tickets: %s", exc)
        return []

    rows: list[Mapping[str, Any]] = []
    for ticket in listed:
        row = _ticket_row(ticket)
        if row is not None:
            rows.append(row)
    stale = find_stale_active(rows, now=effective_now, is_alive=alive, idle_limit=idle_limit)

    actions: list[StaleActiveAction] = []
    limit = max(0, max_actions)
    for item in stale:
        if len(actions) >= limit:
            break
        evidence = stale_active_evidence(cause=item.cause, previous_status=item.status)
        idle_seconds = int(item.idle.total_seconds())
        if dry_run:
            action = StaleActiveAction(
                ticket_id=item.ticket_id,
                previous_status=item.status,
                cause=item.cause,
                idle_seconds=idle_seconds,
                action="would_requeue",
            )
            _log_action(action)
            actions.append(action)
            continue
        try:
            _updated, changed = store.requeue_ticket(item.ticket_id, item.cause, evidence=evidence)
        except ValueError as exc:
            action = StaleActiveAction(
                ticket_id=item.ticket_id,
                previous_status=item.status,
                cause=item.cause,
                idle_seconds=idle_seconds,
                action="refused",
                detail=str(exc)[:200],
            )
            _log_action(action)
            actions.append(action)
            continue
        except Exception as exc:  # noqa: BLE001 - um ticket nao derruba o sweep
            logger.warning("stale_active reconcile failed for %s: %s", item.ticket_id, exc)
            action = StaleActiveAction(
                ticket_id=item.ticket_id,
                previous_status=item.status,
                cause=item.cause,
                idle_seconds=idle_seconds,
                action="error",
                detail=str(exc)[:200],
            )
            _log_action(action)
            actions.append(action)
            continue
        if not changed:
            continue
        action = StaleActiveAction(
            ticket_id=item.ticket_id,
            previous_status=item.status,
            cause=item.cause,
            idle_seconds=idle_seconds,
            action="requeued",
        )
        _log_action(action)
        actions.append(action)
    return actions


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m core.line.stale_active [--dry-run] [--max-actions N] [--idle-hours H]`.

    Usa o ledger de `DemandsStore` (`DARKFAC_DEMANDS_PATH` ou o caminho padrao). Imprime um
    JSON com as acoes. Sai 1 se alguma acao for `error`.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Devolve a planned os tickets implementing/validating sem vida (USR-498)."
    )
    parser.add_argument("--dry-run", action="store_true", help="lista as acoes sem gravar o ledger")
    parser.add_argument("--max-actions", type=int, default=MAX_STALE_ACTIVE_ACTIONS)
    parser.add_argument(
        "--idle-hours",
        type=float,
        default=DEFAULT_IDLE_LIMIT.total_seconds() / 3600.0,
        help="idade minima, em horas, alem do status ativo sem vida",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from core.demands.store import DemandsStore

    hours = args.idle_hours if args.idle_hours >= 0 else 0.0
    actions = reconcile_stale_active(
        DemandsStore(),
        idle_limit=timedelta(hours=hours),
        max_actions=max(0, args.max_actions),
        dry_run=args.dry_run,
    )
    print(json.dumps([asdict(action) for action in actions], ensure_ascii=False, indent=1))
    return 1 if any(action.action == "error" for action in actions) else 0


def collect_liveness(
    ticket_id: str,
    *,
    store: Optional[Any] = None,
    running_runs: Optional[Mapping[str, str]] = None,
    lock_root: Optional[Path] = None,
    pending_directory: Optional[Path] = None,
) -> bool:
    """True quando alguma das quatro sondas encontra evidencia de vida para `ticket_id`.

    Fail-closed por sonda: a excecao de qualquer uma delas faz esta funcao devolver `True`
    (vivo) em vez de propagar -- uma leitura que falhou nunca pode, por si so, fazer o
    chamador reconciliar um ticket que ainda pode estar em execucao.
    """
    probes: tuple[Callable[[], bool], ...] = (
        lambda: _probe_process(ticket_id, running_runs),
        lambda: _probe_line_lock(ticket_id, lock_root),
        lambda: _probe_pending_delivery(ticket_id, pending_directory),
        lambda: _probe_open_run(ticket_id, store),
    )
    for probe in probes:
        try:
            if probe():
                return True
        except Exception as exc:  # noqa: BLE001 - sonda sem resposta trava fechado (vivo)
            logger.warning("Sonda de vida falhou para %s; tratando como vivo: %s", ticket_id, exc)
            return True
    return False


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_IDLE_LIMIT",
    "MAX_STALE_ACTIVE_ACTIONS",
    "STALE_ACTIVE_CAUSE",
    "StaleActiveAction",
    "StaleActiveTicket",
    "collect_liveness",
    "find_stale_active",
    "main",
    "reconcile_stale_active",
    "stale_active_evidence",
]


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
