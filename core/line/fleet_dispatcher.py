"""Despachante continuo da frota (USR-366).

Roda sem sessao de chat (tarefa agendada) e mantem Notebook, Grokbox e Desktop ocupados: a cada
ciclo le ``demands.json`` de ``origin/main``, escolhe tickets ``planned`` executaveis e despacha UM
por host livre, na ordem de ``line_fleet.json`` (notebook > grokbox > desktop). Cada despacho e um
``run_ticket.py --fleet --host-id <host>`` (que escolhe o harness por cota e usa a reserva por host
de ``line_lock``); hosts remotos fixam o worker do portao em ``DARKFAC_TEST_WORKERS``
(só a URL daquele host) com ``DARKFAC_FLEET_GATE_REMOTE=1`` (``--remote-required``).
Esse pin de execução não é a lista da admissão: o filho recebe
``DARKFAC_ADMISSION_WORKERS`` com a lista que o despachante já tinha, ou o default
da frota se ela estava vazia, mais a URL do pin, sem repetir.
Grokbox inacessivel e pulado sem alerta e sem espera de wake. Queda do Grokbox (exit 75 ou
``[REMOTE_JOB_LOST]``) libera a reserva, mantem o ticket ``planned`` e nao aplica backoff
global: o ciclo seguinte lanca no proximo host livre. Sonda sem health nao alerta. Outra
infra entra em backoff. Deploy pos-merge e deduplicado por janela de silencio.

A tag ``line-block`` e a unica marca de via expressa. Um ticket elegivel com ela fura a fila regular
e a lista de recuperacao e sai no primeiro host livre. Tag excluida, dependencia aberta e cooldown
continuam de fora. Com recuperacao global ativa ha um unico slot: o ``line-block`` o ocupa quando
ninguem esta em voo e nao abre segunda entrega quando ja ha uma. A segunda falha classificada
``self_modification`` do mesmo ticket alerta o owner uma vez, com a causa e o comando unico.
O alerta nao liga a recuperacao global.

Entre ciclos, o loop compara o HEAD completo do checkout em execucao (``PROJECT_ROOT``,
via ``current_git_sha`` com ``use_env`` falso) com o SHA lido na largada. Se os dois sao
completos e diferentes, o processo se substitui com ``sys.executable -m
core.line.fleet_dispatcher`` e os argumentos originais, sem encerrar runs em voo.
``DARKFAC_GIT_SHA`` e ``origin/main`` nao entram na comparacao. SHA igual, SHA ilegivel,
``--once`` e ``--dry-run`` nao substituem o processo por essa comparacao.

Se ``pending_delivery.tick`` devolve a causa ``stale_dispatcher_import`` (``ImportError``
ou ``ModuleNotFoundError`` na retomada), ``run_forever`` substitui o processo depois do
ciclo e antes do resumo e do sentinela, uma vez por SHA, mesmo com o HEAD inalterado.
Grava ``DARKFAC_DISPATCHER_IMPORT_RESTART_SHA`` com o HEAD completo desta geracao antes
do exec. Com a variavel igual ao HEAD atual, a ocorrencia seguinte registra a causa e o
loop dorme, sem segundo exec. A troca de SHA do checkout nao define essa variavel.
``--dry-run``, ``--once`` e um ``launch`` presente tambem nao disparam esse reinicio.

Antes de montar a fila, cada ciclo reconcilia ticket ``implementing`` ou ``validating``
sem vida ha mais de 24 horas (``reconcile_stale_active``). O ticket devolvido a
``planned`` pode sair no mesmo ciclo. ``--resume-only`` nao reconcilia. ``--dry-run``
lista sem gravar. Ticket em ``in_flight`` deste processo nao entra. Falha do sweep fica
em ``reconciled`` e o ciclo segue. O estado, inclusive ``reconciled``, vai para
``fleet_dispatcher_state.json``.

Uso::

    python -m core.line.fleet_dispatcher --loop
    python -m core.line.fleet_dispatcher --once --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from core.demands.models import TAG_LINE_BLOCK
from core.line import run_outcome
from core.line.failure_taxonomy import canonical_cause, classify
from core.infra.git_sha import current_git_sha, normalize_sha
from core.paths import state_root

logger = logging.getLogger("darkfac.line.fleet_dispatcher")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = state_root() / "runs" / "fleet_dispatcher_state.json"


def _default_checkout_sha() -> str | None:
    """HEAD completo do checkout em execucao. Ignora ``DARKFAC_GIT_SHA`` e ``origin/main``."""
    return current_git_sha(PROJECT_ROOT, use_env=False)


def _checkout_sha_changed(startup: str | None, current: str | None) -> bool:
    """True somente quando os dois SHAs ja normalizados sao completos e diferentes."""
    return bool(startup and current and startup != current)


IMPORT_RESTART_SHA_ENV = "DARKFAC_DISPATCHER_IMPORT_RESTART_SHA"
LOG_DIR = state_root() / "runs" / "fleet_dispatcher_logs"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

USAGE_PUBLISH_SEC = 300.0
EXCLUDED_TAGS = frozenset({"congelado-v0", "not-authorized-for-execution", "planning-only", "waiting_human"})
MIN_NOTEBOOK_WORKERS = 1
INFRA_EXIT_CODES = frozenset({75})
OPPORTUNISTIC_HOST_IDS = frozenset({"grokbox"})
ROUTE_WAIT_MARKERS = ("harness_infra_error: route_wait", '"cause": "route_wait"', "causa: route_wait")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
FAILURE_HISTORY_LIMIT = 20
DEPLOY_PENDING_CONVERGENCE_EXIT_CODE = 3
DEPLOY_PENDING_CONVERGENCE_REASON = "canonical_deploy_pending_convergence"
DEPLOY_DEFERRAL_FILE = "deploy_deferrals.json"
DEPLOY_DEFERRAL_TEXT_LIMIT = 500
CONVERGENCE_RECOVERY_MAX_ATTEMPTS = 6
CONVERGENCE_RECOVERY_BACKOFF_BASE_SEC = 300.0
CONVERGENCE_RECOVERY_BACKOFF_MAX_SEC = 3600.0
CONVERGENCE_RECOVERY_TIMEOUT_SEC = 900.0
DEFAULT_PROGRESS_WINDOW_SEC = 120.0


def configure_logging(log_dir: Path, *, stream: Any = None) -> RotatingFileHandler:
    """Configure UTF-8 file rotation and retain a console/journal copy."""
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "fleet-dispatcher.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handlers: list[logging.Handler] = [file_handler, logging.StreamHandler(stream)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return file_handler


def scheduled_cleanup_hook(log_dir: Path) -> Callable[[], None]:
    """Start or poll maintenance without holding up a fleet cycle or a reload."""
    from core.git.orphan_worktree_cleanup import schedule_due

    process: subprocess.Popen[bytes] | None = None

    def tick() -> None:
        nonlocal process
        if process is not None:
            result = process.poll()
            if result is None:
                return
            if result != 0:
                logger.warning("varredura de worktrees encerrou com codigo %s; consulte o log de manutencao", result)
            process = None
        if not schedule_due(PROJECT_ROOT, datetime.now(timezone.utc)):
            return
        with (log_dir / "orphan-worktree-cleanup.log").open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "cleanup_orphan_worktrees.py"), "--scheduled"],
                cwd=PROJECT_ROOT,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )

    return tick


@dataclass
class HostSpec:
    id: str
    priority: int
    worker_url: Optional[str] = None  # None = executa localmente (Notebook)
    enabled: bool = True
    config: Optional[dict[str, Any]] = None


def default_hosts() -> list[HostSpec]:
    """Ordem e URLs: prioridade de ``line_fleet.json``; URLs sobrescrevem por ``DARKFAC_FLEET_URL_<ID>``.

    ``DARKFAC_FLEET_DISABLED_HOSTS`` (ids separados por virgula) tira hosts da frota sem editar o
    config versionado, ex.: Desktop ocupado com outra carga.
    """
    from core.line import fleet_policy

    config = fleet_policy.load_fleet_config()
    hosts: list[HostSpec] = []
    for h in config.sorted_hosts():
        hosts.append(HostSpec(h.id, h.priority, h.endpoint, h.enabled, h.model_dump(mode="json")))
    return hosts


@dataclass
class DispatcherConfig:
    resume_only: bool = False
    recovery_tickets: tuple[str, ...] = ()
    hosts: list[HostSpec] = field(default_factory=default_hosts)
    cycle_sec: float = 30.0
    stable_probes: int = 3
    deploy_window_sec: float = 180.0
    recovery_list_ttl_sec: float = 43200.0
    progress_window_sec: float = DEFAULT_PROGRESS_WINDOW_SEC
    state_path: Path = STATE_PATH
    policy_path: Optional[Path] = None


@dataclass
class InFlight:
    ticket_id: str
    host_id: str
    handle: Any  # tem .poll() -> int | None
    log_path: Optional[Path] = None
    started_at: float = 0.0
    project_id: str = "darkfac"
    candidate_digest: str = "unknown"
    context_digest: str = "unknown"
    resource_version: str = "unknown"
    admission_token: Optional[str] = None
    run_id: Optional[str] = None
    recovery_token: Optional[str] = None


@dataclass(frozen=True)
class HostProbe:
    free: bool
    reason: str = ""
    resource_version: str = ""
    workers: Optional[int] = None
    available_gib: Optional[float] = None
    observed_at: Optional[str] = None


# --- selecao pura ---------------------------------------------------------------------------------


def select_tickets(
    demands: Sequence[Mapping[str, Any]],
    *,
    busy_ids: set[str],
    admission_store: Any = None,
    main_sha: str = "",
    now: float | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Tickets ``planned`` executaveis, em ordem estavel (id), mais o motivo de cada exclusao."""
    done = {d["id"] for d in demands if d.get("status") == "completed"}
    eligible: list[tuple[int, str, str]] = []
    skipped: dict[str, str] = {}
    for d in demands:
        tid = str(d.get("id", ""))
        if d.get("status") != "planned":
            continue
        tags = set(d.get("tags") or [])
        if tags & EXCLUDED_TAGS:
            skipped[tid] = "tag:" + ",".join(sorted(tags & EXCLUDED_TAGS))
        elif tid in busy_ids:
            skipped[tid] = "em_voo"
        elif [x for x in (d.get("dependencies") or []) if x not in done]:
            skipped[tid] = "dependencia_aberta"
        else:
            try:
                reason = admission_store.retry_reason(
                    str(d.get("project_id") or d.get("project") or "darkfac"), tid,
                    main_sha=main_sha, now=now,
                ) if admission_store is not None else ""
            except Exception:
                logger.exception("Budget unavailable for %s", tid)
                reason = "admission_store_unavailable"
            if reason:
                skipped[tid] = reason
            else:
                try:
                    priority = int(d.get("priority", 0) or 0)
                except (TypeError, ValueError):
                    priority = 0
                eligible.append((-priority, str(d.get("created_at", "")), tid))
    return [tid for _, _, tid in sorted(eligible)], skipped


def demand_has_line_block(demand: Mapping[str, Any]) -> bool:
    """True somente quando a tag canonica ``line-block`` esta na lista de tags.

    Titulo, status e qualquer outro campo nao marcam a via expressa. Uma string
    solta nao e lista de tags.
    """
    tags = demand.get("tags")
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        return False
    return TAG_LINE_BLOCK in tags


def order_fleet_queue(
    queue: Sequence[str],
    demands: Sequence[Mapping[str, Any]],
    *,
    recovery_tickets: Sequence[str] = (),
    recovery_list_live: bool = False,
    recovery_active: bool = False,
    delivery_in_flight: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Ordena a fila do ciclo. A preempcao nao reabre quem ``select_tickets`` tirou.

    Faixas, quando a lista de recuperacao esta viva: ``line-block``, depois a
    lista, depois o restante. Sem a lista, ``line-block`` ainda fura a fila
    regular. Com recuperacao global e entrega ou retomada ja em voo, a lista
    viva nao admite ninguem; sem a lista, so o ``line-block`` fica de fora.
    Dentro de cada faixa vale a ordem recebida; a lista de recuperacao mantem
    a ordem configurada.
    """
    queued = [str(tid) for tid in queue]
    if recovery_list_live and recovery_active and delivery_in_flight:
        allowed = {str(tid) for tid in recovery_tickets if str(tid) in queued}
        return [], {tid: "recuperacao_prioritaria" for tid in queued if tid not in allowed}

    by_id: dict[str, Mapping[str, Any]] = {}
    for row in demands:
        tid = str(row.get("id", ""))
        if tid and tid not in by_id:
            by_id[tid] = row
    blocks = [tid for tid in queued if tid in by_id and demand_has_line_block(by_id[tid])]
    block_ids = set(blocks)
    ordinary = [tid for tid in queued if tid not in block_ids]
    if recovery_active and delivery_in_flight:
        # Sem lista viva a reserva global pode nao estar presa na entrega que
        # ja voa. O line-block ainda assim nao abre a segunda entrega.
        return ordinary, {tid: "recuperacao_prioritaria" for tid in blocks}
    if not recovery_list_live:
        return blocks + ordinary, {}

    allowed_order = [str(tid) for tid in recovery_tickets if str(tid) in queued]
    allowed_ids = set(allowed_order)
    recovery_rest = [tid for tid in allowed_order if tid not in block_ids]
    regular = [tid for tid in queued if tid not in block_ids and tid not in allowed_ids]
    return blocks + recovery_rest + regular, {}


def default_demands_loader() -> list[dict[str, Any]]:
    subprocess.run(["git", "fetch", "origin", "main", "-q"], cwd=PROJECT_ROOT, check=False,
                   creationflags=CREATE_NO_WINDOW, timeout=120)
    out = subprocess.run(["git", "show", "origin/main:.factory/demands/demands.json"], cwd=PROJECT_ROOT,
                         capture_output=True, text=True, encoding="utf-8", check=True,
                         creationflags=CREATE_NO_WINDOW, timeout=60).stdout
    data = json.loads(out)
    rows = data if isinstance(data, list) else list(data.get("demands", []))
    return merge_control_queue(rows)


_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "canceled"})


def merge_control_queue(
    rows: list[dict[str, Any]], queue_loader: Optional[Callable[[], list[Any]]] = None
) -> list[dict[str, Any]]:
    """Ledger rows plus the control-store queue (USR-343 S4a), where `--create --queue-only` and the
    Telegram intake register new tickets. A terminal status in the ledger wins; an unreachable store
    leaves the ledger as is."""
    try:
        if queue_loader is None:
            from core.demands import queue_store

            queue_loader = queue_store.list_queue_tickets
        control = [t.model_dump(mode="json") if hasattr(t, "model_dump") else dict(t) for t in queue_loader()]
    except Exception as exc:  # noqa: BLE001 - the ledger alone still drives the fleet
        logger.warning("fila do control store indisponivel (%s); usando so demands.json", exc)
        return rows
    by_id = {str(r.get("id")): r for r in rows}
    for row in control:
        current = by_id.get(str(row.get("id")))
        if current is not None and str(current.get("status")) in _TERMINAL_STATUSES:
            continue
        by_id[str(row.get("id"))] = row
    return list(by_id.values())


# --- probes e lancamento --------------------------------------------------------------------------


def default_probe(host: HostSpec, stable_probes: int = 3) -> HostProbe:
    if host.worker_url is None:
        try:
            from core.line import gate_ram, line_lock

            if line_lock.is_locked():
                return HostProbe(False, "line_lock_ativo", f"{host.id}:line_lock")
            reading = gate_ram.measure_workers()
            workers = reading.workers
        except Exception as exc:  # noqa: BLE001
            return HostProbe(False, f"probe_local_falhou:{type(exc).__name__}", f"{host.id}:probe_error:{type(exc).__name__}")
        return HostProbe(workers >= MIN_NOTEBOOK_WORKERS, f"workers={workers}", f"{host.id}:workers={workers}",
                         workers=workers, available_gib=reading.available_gib,
                         observed_at=datetime.now(timezone.utc).isoformat())
    from core.harness import remote_dispatch as rd

    for _ in range(max(1, stable_probes)):
        health = rd.probe_health(host.worker_url)
        if health is None:
            return HostProbe(False, "health_indisponivel", f"{host.id}:health_unavailable")
        workers = health.get("workers")
        if health.get("busy"):
            return HostProbe(False, "worker_ocupado", f"{host.id}:busy")
        if not isinstance(workers, int) or workers <= 0:
            return HostProbe(False, "worker_capacity_missing_or_zero", f"{host.id}:workers_missing")
        observed_at = health.get("observed_at") or health.get("resource_observed_at")
        if observed_at:
            try:
                stamp = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
                if stamp.tzinfo is None or (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() >= 90:
                    return HostProbe(False, "resource_reading_stale", f"{host.id}:stale")
            except ValueError:
                return HostProbe(False, "resource_reading_invalid", f"{host.id}:invalid")
    # Exclude observation timestamps: polling a healthy worker must not create a new resource version.
    identity = {
        key: health.get(key) for key in ("worker_id", "workers", "busy", "status", "version", "machine_id")
        if key in health
    }
    from core.line.admission_store import safe_digest
    return HostProbe(True, "saudavel", f"{host.id}:{safe_digest(identity)}", workers=workers,
                     available_gib=health.get("available_gib"),
                     observed_at=str(observed_at or datetime.now(timezone.utc).isoformat()))


def _admission_workers_for_pin(previous: str | None, pin_url: str) -> str:
    """Lista que a admissão lê: workers já no despachante, ou o default da frota, mais o pin.

    URLs repetidas saem uma vez, na primeira ocorrência. O pin entra no fim quando ainda
    não estava na lista. ``DARKFAC_TEST_WORKERS`` do filho continua só com a URL do host.
    """
    from core.harness.remote_dispatch import DEFAULT_WORKERS

    def _urls(raw: str) -> list[str]:
        found: list[str] = []
        for part in raw.split(","):
            url = part.strip()
            if url and url not in found:
                found.append(url)
        return found

    urls = _urls(previous or "")
    if not urls:
        urls = _urls(DEFAULT_WORKERS)
    pin = pin_url.strip()
    if pin and pin not in urls:
        urls.append(pin)
    return ",".join(urls)


def resumable_delivery_worktree(ticket_id: str, project_id: str = "darkfac") -> Optional[Path]:
    """Worktree preservada com commits a frente de origin/main e plano valido, ou None (USR-447).

    Nesse caso o ticket nao precisa replanejar nem reconstruir: o launcher retoma a entrega.
    Qualquer falha na busca cai no relancamento completo.
    """
    from core.git.ticket_workspace import find_preserved_workspaces

    try:
        cwd, base = PROJECT_ROOT, "main"
        if project_id != "darkfac":  # EC-02: external products resume in their own checkout
            from core.projects.registry import get_project_registry
            from core.projects.ticket_execution import project_checkout

            project = get_project_registry().get_project(project_id)
            if project is None:
                return None
            cwd, base = project_checkout(project), project.default_branch
        chosen = find_preserved_workspaces(
            ticket_id, cwd=cwd, base=base, ignore_paths=(".factory/demands/demands.json",),
        ).chosen
    except Exception as exc:  # noqa: BLE001 - sem busca, relancamento completo
        logger.warning("busca de worktree preservada de %s falhou: %s", ticket_id, exc)
        return None
    if chosen is None or chosen.commits_ahead < 1 or chosen.changed_paths:
        return None
    candidate_roots = (
        chosen.workspace.path,
        state_root() / "artifacts" / "ticket_runs" / ticket_id,
    )
    if not any(
        all(((root / ".darkfac" / "runs" / ticket_id) / name).is_file() for name in ("SPEC.md", "tickets.json"))
        for root in candidate_roots
    ):
        return None
    return chosen.workspace.path


def default_launch(ticket_id: str, host: HostSpec, demand: Mapping[str, Any], *,
                   run_id: Optional[str] = None, recovery: bool = False,
                   candidate_digest: str = "unknown") -> InFlight:
    # EC-02: run_ticket.py starts straight from PROJECT_ROOT (no ephemeral fleet-dispatch-* clone);
    # the shared state root is passed explicitly so the child reads the same control store and usage.
    from core.line.operating_harness import headless_executor_env
    from core.paths import state_root

    shared_state = state_root().resolve()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{ticket_id}-{host.id}.log"
    updates = {
        "DARKFAC_CANDIDATE_DIGEST": candidate_digest,
        "DARKFAC_RUN_LOG": str(log_path.resolve()),
    }
    if recovery:
        updates["DARKFAC_RECOVERY_MODE"] = "1"
    # The ticket version the dispatcher selected (origin/main); the child must not re-read a stale local ledger.
    dispatch_ticket = shared_state / "runs" / f"{ticket_id}.dispatch_ticket.json"
    dispatch_ticket.parent.mkdir(parents=True, exist_ok=True)
    dispatch_ticket.write_text(json.dumps(dict(demand), ensure_ascii=False, default=str), encoding="utf-8")
    updates["DARKFAC_DISPATCH_TICKET"] = str(dispatch_ticket)
    if host.worker_url:
        # Gate pin stays on DARKFAC_TEST_WORKERS unless ordered workers are explicitly configured;
        # admission reads the fleet list via DARKFAC_ADMISSION_WORKERS (USR-510 / USR-471).
        updates["DARKFAC_ADMISSION_WORKERS"] = _admission_workers_for_pin(
            os.environ.get("DARKFAC_TEST_WORKERS"), host.worker_url,
        )
        updates["DARKFAC_TEST_WORKERS"] = host.worker_url
        updates["DARKFAC_REMOTE_HARNESS"] = "required"
        updates["DARKFAC_FLEET_GATE_REMOTE"] = "1"
    else:
        updates["DARKFAC_REMOTE_HARNESS"] = "off"
    env = headless_executor_env(
        os.environ,
        project_root=PROJECT_ROOT,
        state_root=shared_state,
        host_id=host.id,
        run_id=run_id,
        updates=updates,
    )
    # run_ticket binds project_root() to the candidate worktree for each gate invocation (USR-516).
    env.pop("DARKFAC_PROJECT_ROOT", None)
    env.pop("PROJECT_ROOT", None)
    cmd = [sys.executable, str(PROJECT_ROOT / "run_ticket.py"), ticket_id, "--fleet", "--host-id", host.id]
    resume_worktree = resumable_delivery_worktree(ticket_id, str(demand.get("project_id") or "darkfac"))
    if resume_worktree is not None:
        cmd.extend(["--resume-delivery", "--worktree", str(resume_worktree)])
    if run_id:
        cmd.extend(["--run-id", run_id])
    if recovery:
        cmd.append("--recovery")
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW)
    return InFlight(ticket_id, host.id, proc, log_path, time.time())


def running_fleet_runs() -> dict[str, str]:
    """``{ticket: host}`` dos ``run_ticket.py`` vivos (sobrevivem a reinicio).

    ``--fleet --host-id <H>`` ocupa o host; ``--resume-delivery <ID>`` (entrega retomada pelo chat)
    so tira o ticket da fila, sem host.
    """
    import psutil

    runs: dict[str, str] = {}
    for proc in psutil.process_iter(["cmdline"]):
        cmd = proc.info.get("cmdline") or []
        if not any(a.endswith("run_ticket.py") for a in cmd):
            continue
        ticket = next((a for a in cmd if re.fullmatch(r"[A-Z]+-\d+", a)), None)
        if ticket is None or not ({"--fleet", "--resume-delivery"} & set(cmd)):
            continue
        host = cmd[cmd.index("--host-id") + 1] if "--fleet" in cmd and "--host-id" in cmd[:-1] else ""
        runs[ticket] = host
    return runs


def running_fleet_tickets() -> set[str]:
    return set(running_fleet_runs())


def running_fleet_hosts() -> set[str]:
    return {h for h in running_fleet_runs().values() if h}


#: USR-475 T5: `default_deploy` previously waited with no bound at all. A stuck Dokploy
#: poll (or a hung child) held the deploy slot forever and left anything it dispatched
#: remotely (e.g. a validation gate job) running with nobody left to confirm it is done.
#: 3600s is generous against `dokploy_redeploy.py`'s own documented per-service wait
#: (``DEFAULT_TIMEOUT_SECONDS`` = 900s) across a handful of services plus backup/node-sync.
ENV_DEPLOY_TIMEOUT_SEC = "DARKFAC_DEPLOY_TIMEOUT_SEC"
DEFAULT_DEPLOY_TIMEOUT_SEC = 3600.0
#: Conventional "timed out" exit code (bash/coreutils `timeout(1)`); never a real
#: `dokploy_redeploy.py` exit code, so a caller can tell a bound-exceeded deploy apart
#: from an ordinary failure.
DEPLOY_TIMEOUT_EXIT_CODE = 124


def deploy_timeout_sec() -> float:
    raw = os.environ.get(ENV_DEPLOY_TIMEOUT_SEC, "").strip()
    if not raw:
        return DEFAULT_DEPLOY_TIMEOUT_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DEPLOY_TIMEOUT_SEC
    return value if value > 0 else DEFAULT_DEPLOY_TIMEOUT_SEC


def default_deploy(timeout: Optional[float] = None) -> int:
    """Runs ``scripts/dokploy_redeploy.py`` and waits, bounded by ``timeout`` seconds.

    On timeout the whole process tree is killed (not just the direct child, so a
    grandchild ``git``/``gh`` call is never left orphaned) and the wait ends with
    :data:`DEPLOY_TIMEOUT_EXIT_CODE` instead of hanging the dispatcher's deploy slot.
    """
    bound = deploy_timeout_sec() if timeout is None else timeout
    proc = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "dokploy_redeploy.py")],
        cwd=PROJECT_ROOT, creationflags=CREATE_NO_WINDOW,
    )
    try:
        return proc.wait(timeout=bound)
    except subprocess.TimeoutExpired:
        logger.error("default_deploy excedeu %.0fs; encerrando a subarvore do processo", bound)
        from core.execution.sandbox import kill_process_tree

        kill_process_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        return DEPLOY_TIMEOUT_EXIT_CODE


def default_converge(timeout: Optional[float] = None, expected_sha: str | None = None) -> int:
    """Recuperacao delimitada: ``node_sync retry`` sobre o candidato imutavel pendente, depois o backup pos-deploy."""
    from core.infra import node_sync

    pending = node_sync.load_pending_convergence()
    candidate = normalize_sha(expected_sha) or normalize_sha((pending or {}).get("expected_sha"))
    if candidate is None:
        logger.error("Recuperacao sem SHA alvo valido; mantendo deploy pendente")
        return 1
    bound = CONVERGENCE_RECOVERY_TIMEOUT_SEC if timeout is None else timeout
    deadline = time.monotonic() + bound
    # retry pode sair 0 sem pendencia; a verificacao explicita do candidato fecha essa corrida.
    for module, args in (
        ("core.infra.node_sync", ["retry"]),
        ("core.infra.node_sync", ["verify", "--expected-sha", candidate]),
        ("core.infra.backup_cron", ["--once"]),
    ):
        proc = subprocess.Popen(
            [sys.executable, "-m", module, *args], cwd=PROJECT_ROOT, creationflags=CREATE_NO_WINDOW,
        )
        try:
            rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            logger.error("default_converge excedeu %.0fs em %s; encerrando a subarvore do processo", bound, module)
            from core.execution.sandbox import kill_process_tree

            kill_process_tree(proc.pid)
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
            return DEPLOY_TIMEOUT_EXIT_CODE
        if rc != 0:
            return rc
    return 0


def is_lock_refusal(log_tail: str) -> bool:
    return canonical_cause(log_tail) == "lock"


def is_infra_failure(rc: int, log_tail: str) -> bool:
    return rc == 75 or classify(classify_log_cause(log_tail) or log_tail) == "infra"


def is_route_wait_log(log_tail: str) -> bool:
    """Espera de rota nao e queda de host: continua no cooldown global."""
    text = log_tail or ""
    return any(marker in text for marker in ROUTE_WAIT_MARKERS)


def is_opportunistic_host_loss(host_id: str, rc: int, log_tail: str) -> bool:
    """Queda esperada do Grokbox: exit 75 ou ``[REMOTE_JOB_LOST]``, sem espera de rota.

    O ticket volta a fila sem cooldown global. O mesmo recurso do Grokbox segue no disjuntor
    ate a versao mudar; os outros hosts continuam elegiveis no ciclo seguinte.
    """
    if str(host_id or "").lower() not in OPPORTUNISTIC_HOST_IDS:
        return False
    if is_route_wait_log(log_tail):
        return False
    text = log_tail or ""
    return rc in INFRA_EXIT_CODES or "[REMOTE_JOB_LOST]" in text


def note_unavailable_host(host: HostSpec, probe: HostProbe) -> None:
    """Registra host indisponivel. Grokbox sem health nao chama notify nem alerta o owner.

    Nao grava offline: o ciclo seguinte sonda o host de novo.
    """
    if str(host.id).lower() in OPPORTUNISTIC_HOST_IDS and probe.reason == "health_indisponivel":
        logger.info("grokbox sem health; sem alerta; sonda de novo no proximo ciclo")
        return
    logger.info("host %s indisponivel: %s", host.id, probe.reason or "sem motivo")


def failure_reason(rc: int, log_tail: str, lines: int = 8, chars: int = 800) -> str:
    """Select the actual error line, excluding quota banners and secrets."""
    return run_outcome.error_detail(log_tail, f"sem saida no log; exit_code={rc}")[:chars]


_CAUSE_HUMAN_RE = re.compile(r"\[ERRO NA ENTREGA\][^\n]*?causa:\s*([a-z0-9_]+)")


def classify_log_cause(log_tail: str) -> str:
    """The official delivery line takes precedence over nested gate output.

    ``self_modification`` counts only on that line (USR-494). A mention in the
    tail, including a JSON envelope, is not this cause.
    """
    official = list(_CAUSE_HUMAN_RE.finditer(log_tail or ""))
    if official:
        return official[0].group(1)
    # Only a complete launcher JSON envelope is a fallback, never an embedded cause.
    for line in (log_tail or "").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("ok") is False and payload.get("cause"):
            cause = str(payload["cause"])
            if cause == "self_modification":
                continue
            return cause
    return ""


def ticket_candidate_digest(tid: str) -> str:
    """Read candidate commits from registered ticket worktrees, independently of the ledger schema."""
    from core.git.ticket_workspace import list_worktrees
    from core.line.admission_store import safe_digest

    slug = tid.lower()
    candidates = [
        (row.get("worktree"), row.get("HEAD"))
        for row in list_worktrees(PROJECT_ROOT)
        if row.get("branch", "").removeprefix("refs/heads/") == f"ticket/{slug}"
        or row.get("branch", "").removeprefix("refs/heads/").startswith(f"ticket/{slug}-")
    ]
    return safe_digest(sorted(candidates))



def _tail(path: Optional[Path], n: int = 6000) -> str:
    try:
        if path and path.exists():
            return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        pass
    return ""


def _delivery_evidence(log_tail: str) -> bool:
    """Legacy merge marker used to schedule the ordinary post-merge deploy."""
    return "merged_awaiting_deploy" in log_tail or "[DELIVERY_MERGED]" in log_tail


def _external_delivery_evidence(log_tail: str) -> bool:
    """Delivery marker emitted by external projects that do not schedule DarkFac redeploy."""
    return "[DELIVERY_EXTERNAL]" in log_tail


def _reconcile_failure(exc: BaseException) -> dict[str, Any]:
    """Acao estavel quando o sweep inteiro levanta. O ciclo grava isto e segue."""
    detail = f"{type(exc).__name__}: {exc}"
    return {
        "ticket_id": "",
        "previous_status": "",
        "cause": "stale_active_reconcile_failed",
        "idle_seconds": 0,
        "action": "error",
        "detail": detail[:200],
    }


def _default_control_store_factory() -> Any:
    """Create the same canonical reservation store used by fleet workers."""
    from core.line.store_selection import default_control_store

    return default_control_store()


def _reservation_is_live(reservation: Any, now: datetime) -> bool:
    """Treat active and draining reservations as occupied until their lease expires."""
    if str(getattr(reservation, "status", "")) not in {"active", "draining"}:
        return False
    try:
        expires_at = datetime.fromisoformat(str(reservation.expires_at))
    except (AttributeError, TypeError, ValueError):
        # A malformed active lease must not make a host appear free.
        logger.warning("canonical reservation has an unreadable expiry; preserving its host")
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at.astimezone(timezone.utc) > now.astimezone(timezone.utc)


class _UnwiredDemands:
    """Ledger vazio quando o processo nao e o daemon.

    ``FleetDispatcher`` construido pelos testes nao abre o ``demands.json`` do
    checkout. O daemon passa ``daemon_demands_store`` em ``main``.
    """

    def list_tickets(self) -> list[Any]:
        return []


def daemon_demands_store() -> Any:
    """Ledger que o despachante continuo reconcilia. Import tardio, sem efeito na importacao."""
    from core.demands.store import DemandsStore

    return DemandsStore()


def _ticket_failure_isolates(reason: str) -> bool:
    """Hold or technical failure of one ticket leaves the rest of the queue eligible."""
    return reason == "backoff_infra" or reason.startswith(
        ("ticket_hold:", "pending_delivery:", "lancamento_falhou:"))


def _progress_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else None
    if isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return stamp.timestamp()
        except ValueError:
            return None
    return None


def progress_activity(
    cycle: Mapping[str, Any],
    previous_stages: Mapping[str, Any] | None,
    *,
    previous_progress_epoch: float | None = None,
    now: float | None = None,
    window_sec: float = DEFAULT_PROGRESS_WINDOW_SEC,
) -> tuple[str, dict[str, str], float | None]:
    """Report ``running`` only while real work evidence is inside the progress window.

    A process heartbeat by itself is not progress. Starts, useful worker/log heartbeats,
    dispatches and stage transitions are persisted so a restart uses the same window.
    """
    observed_now = float(now if now is not None else time.time())
    resumptions = cycle.get("resumptions") or []
    stages: dict[str, str] = {}
    blocked = False
    evidence: list[float] = []
    for row in resumptions:
        if not isinstance(row, Mapping):
            continue
        ticket = str(row.get("ticket") or "")
        stage = str(row.get("stage") or "")
        if ticket and stage:
            stages[ticket] = stage
        if row.get("status") == "running" and row.get("pid"):
            for key in ("heartbeat_epoch", "progress_epoch", "started_at"):
                stamp = _progress_epoch(row.get(key))
                if stamp is not None:
                    evidence.append(stamp)
        if row.get("status") in {"blocked", "dispatcher_busy"}:
            blocked = True
    previous = previous_stages if isinstance(previous_stages, Mapping) else {}
    advanced = any(ticket in previous and str(previous[ticket]) != stage for ticket, stage in stages.items())
    if advanced or cycle.get("dispatched"):
        evidence.append(observed_now)
    for item in cycle.get("progress_evidence", ()):
        stamp = _progress_epoch(item.get("at") if isinstance(item, Mapping) else item)
        if stamp is not None:
            evidence.append(stamp)
    if previous_progress_epoch is not None:
        previous_stamp = _progress_epoch(previous_progress_epoch)
        if previous_stamp is not None:
            evidence.append(previous_stamp)
    latest_progress = max(evidence) if evidence else None
    window = max(0.0, float(window_sec))
    recent = (latest_progress is not None and latest_progress <= observed_now + 60.0
              and observed_now - latest_progress <= window)
    if recent:
        return "running", stages, latest_progress
    if blocked or cycle.get("blocked_causes"):
        return "blocked", stages, latest_progress
    return "idle", stages, latest_progress


# --- despachante ----------------------------------------------------------------------------------


class FleetDispatcher:
    def __init__(
        self,
        config: Optional[DispatcherConfig] = None,
        *,
        demands_loader: Callable[[], list[dict[str, Any]]] = default_demands_loader,
        probe: Optional[Callable[[HostSpec], HostProbe]] = None,
        launch: Optional[Callable[[str, HostSpec], InFlight]] = None,
        deploy: Callable[[], int] = default_deploy,
        converge: Callable[[], int] = default_converge,
        clock: Callable[[], float] = time.time,
        busy_ids_loader: Callable[[], set[str]] = lambda: set(),
        busy_hosts_loader: Callable[[], set[str]] = lambda: set(),
        dry_run: bool = False,
        digest_hook: Optional[Callable[[], Any]] = None,
        sentinel_hook: Optional[Callable[[], Any]] = None,
        orphan_worktree_cleanup_hook: Optional[Callable[[], Any]] = None,
        usage_hook: Optional[Callable[[], Any]] = None,
        admission_store: Any = None,
        express_alert: Optional[Callable[[Any], bool]] = None,
        code_sha_reader: Optional[Callable[[], object]] = None,
        exec_fn: Optional[Callable[[str, Sequence[str]], Any]] = None,
        launch_argv: Optional[Sequence[str]] = None,
        demands_store_factory: Optional[Callable[[], Any]] = None,
        control_store_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config or DispatcherConfig()
        self.demands_loader = demands_loader
        self.probe = probe or (lambda h: default_probe(h, self.config.stable_probes))
        self.launch = launch
        self.deploy = deploy
        self.converge = converge
        self._deferred_sha: str | None = None  # SHA com deploy ja feito e convergencia pendente (exit 3)
        self.clock = clock
        self.busy_hosts_loader = busy_hosts_loader  # hosts com run --fleet vivo fora deste processo
        self.busy_ids_loader = busy_ids_loader  # tickets com run/worktree de entrega fora deste processo
        self.dry_run = dry_run
        self.digest_hook = digest_hook  # resumo diario ao owner (USR-376); idempotente por dia
        self.sentinel_hook = sentinel_hook  # sentinela da linha a cada 45m (USR-439); idempotente
        self.orphan_worktree_cleanup_hook = orphan_worktree_cleanup_hook  # sweep diario de worktrees (USR-500)
        self.usage_hook = usage_hook  # publica cotas no Hub; so o processo real injeta
        self._usage_published_at = 0.0
        if admission_store is None:
            from core.line.admission_store import AdmissionStore
            from core.paths import state_root

            try:
                admission_store = AdmissionStore(state_root() / "admission.sqlite3")
            except Exception as exc:  # noqa: BLE001 - run_cycle reports a structured fail-closed cause
                logger.error("admission store indisponivel na inicializacao (%s)", type(exc).__name__)
                admission_store = None
        self.admission_store = admission_store
        self._main_sha = run_outcome.main_sha(PROJECT_ROOT)
        self.express_alert = express_alert  # None usa o alerta ALL da via expressa (USR-494 T4)
        self.in_flight: dict[str, InFlight] = {}  # trava por host: no maximo um ticket por host
        self.lock_busy: dict[str, str] = {}  # host -> motivo (trava de linha recusada), vai a idle_reasons
        self.last_merge_at: Optional[float] = None
        self.pending_deploy = False
        self._restore_deploy_deferral()
        self.pending_release_path = self.config.state_path.with_name("pending_recovery_releases.json")
        self.pending_releases = json.loads(self.pending_release_path.read_text(encoding="utf-8")) if self.pending_release_path.exists() else []
        # A dispatcher can disappear after a child has merged but before canonical
        # deploy. Keep that boundary durable so a fresh process does not lose it.
        self.delivery_journal_path = self.config.state_path.with_name("pending_deliveries.json")
        self.delivery_journal = self._read_delivery_journal()
        if any(row.get("status") == "awaiting_deploy" for row in self.delivery_journal):
            self.pending_deploy = True
            latest = max(float(row.get("merged_at_epoch", 0)) for row in self.delivery_journal
                          if row.get("status") == "awaiting_deploy")
            self.last_merge_at = latest or self.clock()
        if self.pending_releases:
            self.pending_deploy = True
            self.last_merge_at = self.clock() - self.config.deploy_window_sec
        self.deploys = 0
        self.last_cycle: dict[str, Any] = self._previous_cycle()
        self.failures: list[dict[str, Any]] = []  # ultimas falhas: ticket, host, exit_code, motivo, classe
        self.resource_readings: dict[str, dict[str, Any]] = {}
        self.recovery = self.admission_store.recovery_state() if self.admission_store is not None and hasattr(self.admission_store, "recovery_state") else {
            "active": True, "cause": "admission_store_unavailable", "fingerprint": ""}
        self._code_sha_reader = code_sha_reader or _default_checkout_sha
        self._exec_fn = exec_fn or os.execv
        self._launch_argv = tuple(launch_argv) if launch_argv is not None else tuple(sys.argv[1:])
        self._startup_sha = self._read_checkout_sha()
        self.demands_store_factory = demands_store_factory
        self.control_store_factory = control_store_factory or _default_control_store_factory
        self._control_store: Any = None

    def _read_canonical_reservations(self, now: float) -> dict[str, Any]:
        """Reconcile expired leases and return currently occupied hosts.

        The dispatcher only observes canonical reservations. The owner of a
        reservation remains responsible for renewing or releasing it.
        """
        if self._control_store is None:
            self._control_store = self.control_store_factory()
        instant = datetime.fromtimestamp(now, timezone.utc)
        self._control_store.reconcile_fleet_reservations(instant)
        reservations = self._control_store.list_active_reservations()
        return {
            str(reservation.host_id): reservation
            for reservation in reservations
            if _reservation_is_live(reservation, instant)
        }

    def _read_checkout_sha(self) -> str | None:
        try:
            raw = self._code_sha_reader()
        except Exception:  # noqa: BLE001 - SHA ilegivel nao substitui o processo
            logger.exception("falha ao ler o SHA do checkout em execucao")
            return None
        return normalize_sha(raw)

    def _exec_if_checkout_sha_changed(self) -> bool:
        """Substitui este processo quando o HEAD do checkout mudou desde a largada.

        Nao chama kill, terminate nem wait em handles em voo ou filhos de
        pending_delivery, e nao remove checkout operacional. Retorna True so se
        o exec voltou; se ele levanta, o erro fica no log e o ciclo segue.
        """
        if self.dry_run:
            return False
        current = self._read_checkout_sha()
        if not _checkout_sha_changed(self._startup_sha, current):
            return False
        command = [sys.executable, "-m", "core.line.fleet_dispatcher", *self._launch_argv]
        logger.info(
            "SHA do checkout mudou de %s para %s; reiniciando o despachante sem encerrar runs em voo",
            self._startup_sha, current,
        )
        try:
            self._exec_fn(sys.executable, command)
        except Exception:  # noqa: BLE001 - falha de exec nao pode derrubar o daemon
            logger.exception(
                "reinicio do despachante falhou apos mudanca de SHA %s -> %s",
                self._startup_sha, current,
            )
            return False
        return True

    def _exec_if_stale_dispatcher_import(self, cycle: dict[str, Any] | None) -> bool:
        """Substitui o processo uma vez por SHA apos import obsoleto na retomada.

        So age com launch ausente e fora de dry_run, depois do ciclo. Nao define
        ``IMPORT_RESTART_SHA_ENV`` no caminho de mudanca de checkout. Nao chama
        kill, terminate nem wait. Retorna True so se o exec voltou.
        """
        if self.dry_run or self.launch is not None or not cycle:
            return False
        from core.line.pending_delivery import STALE_DISPATCHER_IMPORT

        resumptions = cycle.get("resumptions")
        if not isinstance(resumptions, list) or not any(
            isinstance(row, Mapping) and row.get("cause") == STALE_DISPATCHER_IMPORT for row in resumptions
        ):
            return False
        current = self._read_checkout_sha()
        if current is None:
            logger.error("import obsoleto na retomada sem SHA completo; sem reinicio")
            return False
        if os.environ.get(IMPORT_RESTART_SHA_ENV) == current:
            logger.info(
                "import obsoleto na retomada ja reiniciou o despachante na geracao %s",
                current,
            )
            return False
        command = [sys.executable, "-m", "core.line.fleet_dispatcher", *self._launch_argv]
        logger.info(
            "import obsoleto na retomada com SHA %s; reiniciando o despachante uma vez nesta geracao",
            current,
        )
        os.environ[IMPORT_RESTART_SHA_ENV] = current
        try:
            self._exec_fn(sys.executable, command)
        except Exception:  # noqa: BLE001 - falha de exec nao pode derrubar o daemon
            logger.exception(
                "reinicio do despachante falhou apos import obsoleto na geracao %s",
                current,
            )
            return False
        return True

    def _record_failure(self, job: InFlight, rc: int, kind: str, tail: str) -> None:
        outcome = run_outcome.read_outcome(job.ticket_id, job.run_id)
        if outcome and not outcome.exit_code:
            outcome = None
        cause = outcome.cause if outcome else classify_log_cause(tail) or run_outcome.diagnostic_cause(tail, rc)
        if not outcome and cause == "unknown":
            cause = "infra" if rc == 75 else canonical_cause(kind)
        log_failure = run_outcome.actionable_failure(tail)
        detail_source = log_failure or (outcome.detail if outcome else tail)
        reason = failure_reason(rc, detail_source)
        self.failures = (self.failures + [{
            "ts": datetime.now(timezone.utc).isoformat(), "ticket": job.ticket_id, "host": job.host_id,
            "exit_code": outcome.exit_code if outcome else rc, "kind": kind, "reason": reason,
            "cause": cause, "cause_class": classify(cause),
            "log": str(job.log_path) if job.log_path else None,
        }])[-FAILURE_HISTORY_LIMIT:]
        logger.error("%s falhou em %s: causa=%s motivo=%s", job.ticket_id, job.host_id, cause, reason)
        try:
            from core.line.admission_store import make_fingerprint

            fingerprint = make_fingerprint(
                project_id=job.project_id, ticket_id=job.ticket_id,
                candidate_digest=job.candidate_digest, context_digest=job.context_digest,
                error=f"{kind}:{tail}" if kind == "recovery_receipt" else cause,
                resource_version=job.resource_version, main_sha=run_outcome.main_sha(PROJECT_ROOT),
            )
            self.admission_store.record_failure(fingerprint)
            phase = str(outcome.stage) if outcome and outcome.stage else {
                "launch": "launch", "lock": "dispatch", "infra": "dispatch",
                "no_delivery_evidence": "delivery", "recovery_receipt": "deploy",
            }.get(kind, kind)
            if not (kind == "infra" and is_opportunistic_host_loss(job.host_id, rc, tail)):
                self.admission_store.record_attempt(
                    job.project_id, job.ticket_id, cause, run_id=job.run_id or uuid.uuid4().hex,
                    main_sha=fingerprint.main_sha, detail=reason, log_path=str(job.log_path or ""),
                    phase=phase,
                )
        except Exception:
            logger.exception("falha ao persistir orcamento de %s", job.ticket_id)
        self._note_express_failure(job.ticket_id, "" if cause == "unknown" else cause)

    def _express_episode_store(self) -> Any:
        from core.line.express_lane import ExpressEpisodeStore

        return ExpressEpisodeStore(self.config.state_path.with_name("express_episodes.json"))

    def _note_express_failure(self, ticket_id: str, cause: str) -> None:
        """Registra o episodio da causa classificada. Falha de aviso nao apaga o hold."""

        try:
            from core.line.express_lane import observe_express_failure

            observe_express_failure(
                ticket_id,
                cause,
                store=self._express_episode_store(),
                notify=self.express_alert,
            )
        except Exception:  # noqa: BLE001 - o alerta nao desfaz o hold nem a falha ja registrada
            logger.exception("episodio da via expressa falhou para %s", ticket_id)

    def _close_express_episode(self, ticket_id: str) -> None:
        """Entrega concluida encerra o episodio self_modification deste ticket."""

        try:
            from core.line.express_lane import observe_express_success

            observe_express_success(ticket_id, store=self._express_episode_store())
        except Exception:  # noqa: BLE001 - o encerramento do episodio nao desfaz a entrega
            logger.exception("falha ao encerrar episodio da via expressa de %s", ticket_id)

    def _release_reservation(self, job: InFlight) -> None:
        if not job.admission_token:
            return
        try:
            from core.line.admission_store import safe_digest

            self.admission_store.release(
                project_id=job.project_id, ticket_id=job.ticket_id,
                candidate_digest=safe_digest(job.candidate_digest),
                context_digest=safe_digest(job.context_digest),
                resource_version=safe_digest(job.resource_version), token=job.admission_token,
            )
        except Exception:  # noqa: BLE001
            logger.exception("falha ao liberar reserva de admissao de %s", job.ticket_id)
        if job.recovery_token:
            try:
                self.admission_store.release_recovery(job.recovery_token)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao liberar reserva global de recovery de %s", job.ticket_id)

    def _reap(self) -> None:
        for host_id, job in list(self.in_flight.items()):
            rc = job.handle.poll()
            if rc is None:
                continue
            del self.in_flight[host_id]
            outcome = run_outcome.read_outcome(job.ticket_id, job.run_id)
            if outcome is not None:
                rc = outcome.exit_code
            tail = _tail(job.log_path) if rc != 0 else ""
            if rc == 0:
                tail = _tail(job.log_path)
                in_recovery = bool(self.admission_store and self.admission_store.recovery_state().get("active"))
                if job.project_id != "darkfac":
                    if _external_delivery_evidence(tail):
                        self.admission_store.reset_budget(job.project_id, job.ticket_id)
                        self._close_express_episode(job.ticket_id)
                        self._release_reservation(job)
                    else:
                        self._record_failure(job, rc, "no_delivery_evidence", tail)
                        if in_recovery:
                            self.recovery = self.admission_store.record_recovery_receipt_cause("no_delivery_evidence")
                        self._release_reservation(job)
                elif _delivery_evidence(tail):
                    self.admission_store.reset_budget(job.project_id, job.ticket_id)
                    self._close_express_episode(job.ticket_id)
                    self._release_reservation(job)
                    self._record_pending_delivery(job)
                    if in_recovery:
                        pending = {"ticket_id": job.ticket_id, "run_id": job.run_id}
                        if pending not in self.pending_releases:
                            self.pending_releases.append(pending)
                            self._save_pending_releases()
                        self.recovery = self.admission_store.record_recovery_receipt_cause("awaiting_deploy_proof")
                    self.last_merge_at = self.clock()
                    self.pending_deploy = True
                    self._deferred_sha = None
                elif not in_recovery:
                    self._record_failure(job, rc, "no_delivery_evidence", tail)
                    self._release_reservation(job)
                else:
                    self._record_failure(job, rc, "no_delivery_evidence", tail)
                    self.recovery = self.admission_store.record_recovery_receipt_cause("no_delivery_evidence")
                    self._release_reservation(job)
            elif (outcome.cause == "lock" if outcome else is_lock_refusal(tail)):
                self._record_failure(job, rc, "lock", tail)
                self._release_reservation(job)
                self.lock_busy[host_id] = f"host_ocupado_trava:{job.ticket_id}"
                logger.warning("trava de linha em %s; %s reenfileirado", host_id, job.ticket_id)
            elif (outcome.cause_class == "infra" if outcome else is_infra_failure(rc, tail)):
                self._record_failure(job, rc, "infra", tail)
                self._release_reservation(job)
                if is_opportunistic_host_loss(host_id, rc, tail):
                    logger.warning(
                        "infra oportunista em %s (%s); %s volta a fila sem backoff global",
                        host_id, rc, job.ticket_id,
                    )
                else:
                    logger.warning("infra em %s (%s); %s reenfileirado", host_id, rc, job.ticket_id)
            else:
                self._record_failure(job, rc, "failure", tail)
                self._release_reservation(job)

    @staticmethod
    def _deferral_path() -> Path:
        return state_root() / "runs" / DEPLOY_DEFERRAL_FILE

    def _load_deferrals(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self._deferral_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _restore_deploy_deferral(self) -> None:
        """Restaura somente adiamentos cujo registro e pendencia node_sync confirmam o mesmo SHA."""
        from core.infra import node_sync

        try:
            pending = node_sync.load_pending_convergence()
            sha = normalize_sha((pending or {}).get("expected_sha"))
            record = self._load_deferrals().get(sha) if sha else None
            if not sha or not isinstance(record, dict) or normalize_sha(record.get("target_sha")) != sha:
                return
            self._deferred_sha = sha
            self.pending_deploy = True
            # recovery_next_at continua governando o backoff; o ciclo pode retomar logo.
            self.last_merge_at = self.clock() - self.config.deploy_window_sec
        except Exception:  # estado corrompido/inacessivel nao pode derrubar o dispatcher
            logger.exception("Falha ao restaurar adiamento de deploy")

    def _save_deferrals(self, records: dict[str, Any]) -> None:
        if len(records) > FAILURE_HISTORY_LIMIT:
            newest = sorted(records.items(), key=lambda kv: str(kv[1].get("timestamp", "")))
            records = dict(newest[-FAILURE_HISTORY_LIMIT:])
        path = self._deferral_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)

    def _record_deploy_deferral(self, exit_code: int) -> dict[str, Any] | None:
        """Persiste o adiamento por SHA (idempotente): repeticoes incrementam ``attempts``.

        Garante um registro pendente com candidato imutavel para o ``node_sync retry``.
        """
        from core.infra import node_sync

        try:
            pending = node_sync.load_pending_convergence() or {}
            target_sha = normalize_sha(pending.get("expected_sha")) or _default_checkout_sha() or "unknown"
            node = node_sync.redact_worker_output(pending.get("node") or "unknown", limit=80)
            diagnostic = node_sync.redact_worker_output(
                pending.get("reason") or "convergence pending; no diagnostic recorded", limit=DEPLOY_DEFERRAL_TEXT_LIMIT
            )
            if normalize_sha(pending.get("expected_sha")) is None and normalize_sha(target_sha):
                node_sync.record_pending_convergence(node, target_sha, diagnostic)
            records = self._load_deferrals()
            previous = records.get(target_sha)
            previous = previous if isinstance(previous, dict) else {}
            record = {
                **previous,
                "target_sha": target_sha, "pending_node": node, "reason": diagnostic,
                "exit_code": exit_code, "attempts": int(previous.get("attempts", 0)) + 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            records[target_sha] = record
            self._save_deferrals(records)
            return record
        except Exception:  # diagnostico nunca derruba o ciclo do despachante
            logger.exception("Falha ao persistir o adiamento do deploy canonico")
            return None

    def _clear_deploy_deferrals(self) -> None:
        from core.infra import node_sync

        self._deferred_sha = None
        if self._load_deferrals():
            self._save_deferrals({})
        node_sync.clear_pending_convergence()

    def deploy_deferral_snapshot(self) -> dict[str, Any] | None:
        """Adiamento por convergencia pendente para o Hub distinguir 'aguardando no' de 'deploy falhou'."""
        sha = self._deferred_sha
        record = self._load_deferrals().get(sha) if sha else None
        if not isinstance(record, dict):
            return None
        keys = ("pending_node", "reason", "exit_code", "attempts", "recovery_attempts", "recovery_next_at",
                "recovery_last_exit_code", "recovery_exhausted", "timestamp")
        return {
            "state": "awaiting_node_convergence", "target_sha": sha,
            "recovery_max_attempts": CONVERGENCE_RECOVERY_MAX_ATTEMPTS,
            **{key: record.get(key) for key in keys if key in record},
        }

    def _note_failure(self, rc: int, reason: str) -> None:
        self.failures = (self.failures + [{
            "ts": datetime.now(timezone.utc).isoformat(), "ticket": "deploy", "host": "vps",
            "exit_code": rc, "kind": "deploy", "reason": reason, "log": None,
        }])[-FAILURE_HISTORY_LIMIT:]

    def _recover_convergence(self) -> bool | None:
        """Recuperacao delimitada de um deploy com convergencia pendente.

        Retorna True quando a convergencia foi comprovada (deploy concluido), False quando o
        ciclo deve apenas esperar (backoff, teto esgotado ou falha) e None quando o SHA alvo
        mudou ou a pendencia sumiu sem prova e o caminho normal de deploy completo deve ser reaberto.
        Verdadeiro somente com evidencia explicita de todos os nos no SHA alvo e backup concluido.
        """
        from core.infra import node_sync

        sha = self._deferred_sha
        pending = node_sync.load_pending_convergence()
        records = self._load_deferrals()
        record = records.get(sha) if sha is not None else None
        if sha is None or not isinstance(record, dict) or (
            pending is not None and normalize_sha(pending.get("expected_sha")) != sha
        ):
            self._deferred_sha = None
            return None
        if pending is None:
            # Pendencia sumiu sem prova de convergencia da VPS nem backup: o deploy completo reabre.
            records.pop(sha, None)
            self._save_deferrals(records)
            self._deferred_sha = None
            return None
        attempts = int(record.get("recovery_attempts", 0))
        if attempts >= CONVERGENCE_RECOVERY_MAX_ATTEMPTS:
            if not record.get("recovery_exhausted"):
                record["recovery_exhausted"] = True
                records[sha] = record
                self._save_deferrals(records)
                self._note_failure(DEPLOY_PENDING_CONVERGENCE_EXIT_CODE, "convergence_recovery_exhausted")
                logger.error(
                    "Recuperacao de convergencia esgotada para %s apos %s tentativas; pendencia preservada",
                    sha[:12], attempts,
                )
                self._write_state()
            return False
        if self.clock() < float(record.get("recovery_next_at", 0.0)):
            return False
        try:
            rc = self.converge(expected_sha=sha) if self.converge is default_converge else self.converge()
        except Exception:  # noqa: BLE001 - falha da recuperacao conta como tentativa
            logger.exception("Recuperacao de convergencia levantou excecao")
            rc = 1
        verified = False
        if rc == 0:
            try:
                report = node_sync.verify(expected_sha=sha)
                included = [node for node in report.nodes if node.state != "excluded"]
                verified = (
                    normalize_sha(report.expected_sha) == sha
                    and bool(included)
                    and any(node.name == "VPS" for node in included)
                    and all(node.state == "converged" and normalize_sha(node.git_sha) == sha for node in included)
                )
            except Exception:  # noqa: BLE001 - sem prova explicita, preservar a pendencia
                logger.exception("Verificacao explicita de convergencia falhou para %s", sha[:12])
        if rc == 0 and verified:
            node_sync.clear_pending_convergence()
            records.pop(sha, None)
            self._save_deferrals(records)
            self._deferred_sha = None
            return True
        recovery_rc = rc if rc != 0 else DEPLOY_PENDING_CONVERGENCE_EXIT_CODE
        attempts += 1
        backoff = min(CONVERGENCE_RECOVERY_BACKOFF_BASE_SEC * 2 ** (attempts - 1), CONVERGENCE_RECOVERY_BACKOFF_MAX_SEC)
        record.update(
            recovery_attempts=attempts, recovery_next_at=self.clock() + backoff,
            recovery_last_exit_code=recovery_rc, recovery_last_at=datetime.now(timezone.utc).isoformat(),
        )
        records[sha] = record
        self._save_deferrals(records)
        self._note_failure(recovery_rc, f"convergence_recovery_exit:{recovery_rc}")
        logger.error(
            "Recuperacao de convergencia falhou ou ficou sem prova: exit_code=%s tentativa=%s/%s",
            recovery_rc, attempts, CONVERGENCE_RECOVERY_MAX_ATTEMPTS,
        )
        self.last_merge_at = self.clock()
        self._write_state()
        return False

    def _maybe_deploy(self) -> bool:
        """Um deploy por lote: so apos a janela de silencio desde o ultimo merge."""
        if not self.pending_deploy or self.last_merge_at is None:
            return False
        if self.clock() - self.last_merge_at < self.config.deploy_window_sec:
            return False
        if self.dry_run:
            return False
        already_deployed = bool(self.pending_releases) and any(
            row.get("status") == "deployed"
            and float(row.get("deployed_at_epoch") or 0) >= float(self.last_merge_at)
            for row in self.delivery_journal
        )
        if already_deployed:
            if not self._complete_pending_releases():
                self.last_merge_at = self.clock()
                return False
            self.pending_deploy = False
            return True
        from core.demands.store import _PathMutex, DemandsLockTimeout
        recovered = self._recover_convergence() if self._deferred_sha is not None else None
        if recovered is False:
            return False
        if recovered is None:
            try:
                with _PathMutex(state_root() / "runs" / "deploy", timeout=0.1):
                    rc = self.deploy()
            except DemandsLockTimeout:
                rc = 75
            if rc != 0:
                reason = f"canonical_deploy_exit:{rc}"
                if rc == DEPLOY_PENDING_CONVERGENCE_EXIT_CODE:
                    reason = DEPLOY_PENDING_CONVERGENCE_REASON
                    record = self._record_deploy_deferral(rc)
                    self._deferred_sha = normalize_sha(record.get("target_sha")) if record is not None else None
                self._note_failure(rc, reason)
                logger.error("Canonical deployment deferred: exit_code=%s reason=%s", rc, reason)
                self.last_merge_at = self.clock()
                self._write_state()
                return False
        if recovered is None:
            self._clear_deploy_deferrals()  # deploy completo saiu 0: convergencia e backup comprovados
        # Persist the successful deploy boundary before recovery receipt checks:
        # those checks may need another pass, but must not redeploy a proven run.
        if any(row.get("status") == "awaiting_deploy" for row in self.delivery_journal):
            self._complete_delivery_journal()
        if self.pending_releases and not self._complete_pending_releases():
            self.last_merge_at = self.clock()
            return False
        self.pending_deploy = False
        self.deploys += 1
        return True

    def _read_delivery_journal(self) -> list[dict[str, Any]]:
        try:
            rows = json.loads(self.delivery_journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [row for row in rows if isinstance(row, dict) and row.get("ticket_id") and row.get("run_id")]

    def _save_delivery_journal(self) -> None:
        self.delivery_journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.delivery_journal_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.delivery_journal, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.delivery_journal_path)

    def _record_pending_delivery(self, job: InFlight) -> None:
        """Persist candidate-bound delivery identity before the process can restart."""
        run_id = job.run_id or ""
        if not run_id or any(row.get("run_id") == run_id for row in self.delivery_journal):
            return
        outcome = run_outcome.read_outcome(job.ticket_id, run_id)
        cost_usd: float | None = None
        try:
            from core.usage.ledger import ModelUsageLedger

            usage_events = ModelUsageLedger()._store.read().get("events", [])
            cost_usd = round(sum(
                float(event.get("cost_usd") or 0.0)
                for event in usage_events if event.get("ticket_id") == job.ticket_id
            ), 8)
        except (AttributeError, OSError, TypeError, ValueError):
            logger.info("cost evidence unavailable for run %s", run_id)
        row = {
            "ticket_id": job.ticket_id,
            "project_id": job.project_id,
            "run_id": run_id,
            "candidate_digest": job.candidate_digest,
            "stage": "deploy",
            "status": "awaiting_deploy",
            "merged_at": datetime.now(timezone.utc).isoformat(),
            "merged_at_epoch": float(self.clock()),
            "gate_sha": getattr(outcome, "candidate_sha", None) if outcome else None,
            "evidence_refs": [str(job.log_path)] if job.log_path else [],
            "interventions": self._owner_interventions(job),
            "cost_usd": cost_usd,
        }
        self.delivery_journal.append(row)
        self._save_delivery_journal()

    @staticmethod
    def _owner_interventions(job: InFlight) -> list[dict[str, Any]]:
        """Snapshot owner actions and decisions linked to this ticket into its delivery proof."""
        try:
            from core.owner_actions.store import OwnerActionStore

            actions = OwnerActionStore().list_actions()
            result: list[dict[str, Any]] = []
            for action in actions:
                if job.ticket_id not in action.blocks:
                    continue
                result.append({
                    "id": action.id, "kind": action.kind.value,
                    "status": action.status.value,
                    "resolved_at": action.resolved_at.isoformat() if action.resolved_at else None,
                    "answer": action.answer.model_dump(mode="json") if action.answer else None,
                })
            return result
        except Exception as exc:  # noqa: BLE001 - delivery persists with explicit evidence degradation
            logger.warning("owner intervention evidence unavailable for %s (%s)", job.ticket_id, type(exc).__name__)
            return [{"evidence": "unavailable"}]

    def _complete_delivery_journal(self) -> None:
        """Mark journal entries deployed only after the canonical deploy returned success."""
        completed_at = datetime.now(timezone.utc).isoformat()
        for row in self.delivery_journal:
            if row.get("status") != "awaiting_deploy":
                continue
            row["status"] = "deployed"
            row["deployed_at"] = completed_at
            row["deployed_at_epoch"] = float(self.clock())
            row["deploy_sha"] = run_outcome.main_sha(PROJECT_ROOT)
            try:
                from core.line.recovery_receipt import build_receipt

                receipt = build_receipt(str(row["ticket_id"]), str(row["run_id"]), PROJECT_ROOT)
                if receipt.get("candidate_digest") == row.get("candidate_digest"):
                    row["receipt"] = receipt
                    row["evidence_refs"] = sorted({
                        str(evidence["evidence_ref"])
                        for evidence in receipt.values()
                        if isinstance(evidence, dict) and evidence.get("evidence_ref")
                    })
            except Exception as exc:  # delivery already succeeded; keep its journal and expose missing proof
                row["receipt_error"] = type(exc).__name__
                logger.warning("candidate-bound delivery receipt unavailable for %s (%s)",
                               row.get("ticket_id"), type(exc).__name__)
        self._save_delivery_journal()

    def _save_pending_releases(self) -> None:
        self.pending_release_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.pending_release_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.pending_releases), encoding="utf-8")
        os.replace(temp, self.pending_release_path)

    def _complete_pending_releases(self) -> bool:
        from core.line.recovery_receipt import build_receipt
        from core.line.admission import verify_recovery_receipt, RECOVERY_RECEIPT_MARKER
        from core.line.admission_store import safe_digest

        for pending in list(self.pending_releases):
            try:
                receipt = build_receipt(pending["ticket_id"], pending["run_id"], PROJECT_ROOT)
                valid, cause = verify_recovery_receipt(
                    receipt, candidate_digest=receipt["candidate_digest"], run_id=pending["run_id"],
                    now=datetime.now(timezone.utc),
                )
                if not valid:
                    raise ValueError(cause)
            except Exception as exc:  # external evidence failure must retain the durable wait
                self.recovery = self.admission_store.record_recovery_receipt_cause(str(exc)[:300])
                logger.warning("release proof pending for %s: %s", pending["ticket_id"], exc)
                return False
            self.recovery = self.admission_store.complete_recovery(
                receipt_digest=safe_digest(receipt), candidate_digest=receipt["candidate_digest"])
            logger.info("%s%s", RECOVERY_RECEIPT_MARKER, json.dumps(receipt))
            self.pending_releases.remove(pending)
            self._save_pending_releases()
        return True

    def _dispatch_ticket(
        self,
        host: HostSpec,
        probe: HostProbe,
        tid: str,
        demand: dict[str, Any],
        queue: list[str],
        idle: dict[str, str],
        skipped: dict[str, str],
        dispatched: list[dict[str, str]],
        now: float,
        reservation_state: dict[str, Any],
    ) -> None:
        project_id = str(demand.get("project_id") or demand.get("project") or "darkfac")
        candidate = str(demand.get("candidate_digest") or demand.get("baseline_sha") or ticket_candidate_digest(tid))
        context = demand.get("context_digest")
        if not context:
            from core.line.admission_store import safe_digest

            context = safe_digest({
                key: demand.get(key)
                for key in ("title", "problem_statement", "acceptance_criteria", "criteria", "context", "constraints")
                if demand.get(key) is not None
            })
        context = str(context)
        resource_version = probe.resource_version or f"{host.id}:{probe.reason}"
        receipt = demand.get("resource_version") or demand.get("quota_receipt") or demand.get("quota_window")
        if receipt is not None:
            from core.line.admission_store import safe_digest

            resource_version = f"{resource_version}:receipt:{safe_digest(receipt)}"
        if self.dry_run:
            dispatched.append({"ticket": tid, "host": host.id, "dry_run": "1"})
            return
        recovery_token = None
        token = None
        try:
            from core.line.admission_store import normalize_error, safe_digest

            candidate_key = safe_digest(candidate)
            context_key = safe_digest(context)
            resource_key = safe_digest(resource_version)
            reason = self.admission_store.retry_reason(project_id, tid, main_sha=self._main_sha, now=self.clock())
            if not reason and self._opportunistic_failure_open(host.id, project_id, tid, resource_key):
                reason = "identical_failure_open"
            if reason:
                idle[host.id] = skipped[tid] = reason
                return
            if self.recovery.get("active"):
                recovery_token = self.admission_store.reserve_recovery()
                if recovery_token is None:
                    idle[host.id] = "recovery_delivery_reserved"
                    skipped[tid] = "recovery_delivery_reserved"
                    queue.insert(0, tid)
                    return
            token = self.admission_store.reserve(
                project_id=project_id, ticket_id=tid, candidate_digest=candidate_key,
                context_digest=context_key, resource_version=resource_key, host_id=host.id,
            )
            if token is None:
                if recovery_token:
                    self.admission_store.release_recovery(recovery_token)
                idle[host.id] = "admission_reserved"
                skipped[tid] = "admission_reserved"
                queue.insert(0, tid)
                return
        except Exception as exc:  # noqa: BLE001 - no agent or checkout when state is uncertain
            if recovery_token:
                try:
                    self.admission_store.release_recovery(recovery_token)
                except Exception:  # noqa: BLE001
                    logger.exception("falha ao liberar reserva global apos recusa de admissao")
            if token:
                try:
                    self.admission_store.release(
                        project_id=project_id, ticket_id=tid, candidate_digest=candidate_key,
                        context_digest=context_key, resource_version=resource_key, token=token,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("falha ao liberar reserva apos recusa de admissao")
            idle[host.id] = "admission_store_unavailable"
            skipped[tid] = "admission_store_unavailable"
            queue.insert(0, tid)
            logger.error("admission store recusou %s (%s)", tid, type(exc).__name__)
            return
        try:
            run_id = f"fleet-{tid.lower()}-{uuid.uuid4().hex}"
            try:
                current_reservations = self._read_canonical_reservations(self.clock())
            except Exception as exc:  # noqa: BLE001 - never launch with unknown canonical capacity
                reservation_state["reservations"] = {}
                reservation_state["error"] = type(exc).__name__
                for configured_host in self.config.hosts:
                    idle[configured_host.id] = "canonical_reservations_unavailable"
                logger.error("canonical reservation revalidation failed for %s (%s)", host.id, type(exc).__name__)
                self._release_reservation(InFlight(
                    tid, host.id, None, project_id=project_id, candidate_digest=candidate,
                    context_digest=context, resource_version=resource_version,
                    admission_token=token, recovery_token=recovery_token,
                ))
                skipped[tid] = "canonical_reservations_unavailable"
                queue.insert(0, tid)
                return
            reservation_state["reservations"] = current_reservations
            reservation = current_reservations.get(host.id)
            if reservation is not None:
                idle[host.id] = f"host_reserved:{reservation.ticket_id}"
                self._release_reservation(InFlight(
                    tid, host.id, None, project_id=project_id, candidate_digest=candidate,
                    context_digest=context, resource_version=resource_version,
                    admission_token=token, recovery_token=recovery_token,
                ))
                skipped[tid] = f"host_reserved:{reservation.ticket_id}"
                queue.insert(0, tid)
                return
            if self.launch is None:
                job = default_launch(tid, host, demand, run_id=run_id,
                                     recovery=bool(self.recovery.get("active")),
                                     candidate_digest=candidate)
            else:
                job = self._launch_with_contract(tid, host, run_id, candidate)
            self._register_flight(host.id, job, project_id, candidate, context, resource_version, token,
                                  run_id, recovery_token)
            dispatched.append({"ticket": tid, "host": host.id})
            skipped.pop(tid, None)
        except Exception as exc:  # noqa: BLE001 - falha de infra: ticket volta a fila
            self._record_launch_failure(
                host, tid, project_id, candidate, context, resource_version,
                candidate_key, context_key, resource_key, token, recovery_token, exc, idle, skipped, now,
            )

    def _launch_with_contract(self, tid: str, host: HostSpec, run_id: str, candidate: str) -> InFlight:
        kwargs = {"run_id": run_id, "recovery": bool(self.recovery.get("active")),
                  "candidate_digest": candidate}
        try:
            signature = inspect.signature(self.launch)
        except (TypeError, ValueError):
            # Opaque callables must obey the current launcher contract. Never
            # retry a launch after a TypeError from inside its implementation.
            return self.launch(tid, host, **kwargs)
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        supported = kwargs if accepts_kwargs else {
            key: value for key, value in kwargs.items() if key in signature.parameters
        }
        return self.launch(tid, host, **supported)

    def _register_flight(
        self, host_id: str, job: InFlight, project_id: str, candidate: str, context: str,
        resource_version: str, token: str, run_id: str, recovery_token: str | None,
    ) -> None:
        job.project_id = project_id
        job.candidate_digest = candidate
        job.context_digest = context
        job.resource_version = resource_version
        job.admission_token = token
        job.run_id = run_id
        job.recovery_token = recovery_token
        if job.started_at <= 0:
            job.started_at = float(self.clock())
        self.in_flight[host_id] = job

    def _record_launch_failure(
        self, host: HostSpec, tid: str, project_id: str, candidate: str, context: str,
        resource_version: str, candidate_key: str, context_key: str, resource_key: str,
        token: str, recovery_token: str | None, exc: Exception, idle: dict[str, str],
        skipped: dict[str, str], now: float,
    ) -> None:
        from core.line.agent_cli import redact_secrets

        safe_error = " | ".join(redact_secrets(str(exc)).splitlines())[-800:]
        idle[host.id] = f"lancamento_falhou:{safe_error}"
        skipped[tid] = f"lancamento_falhou:{type(exc).__name__}"
        failed_job = InFlight(
            tid, host.id, None, project_id=project_id, candidate_digest=candidate,
            context_digest=context, resource_version=resource_version,
        )
        self._record_failure(failed_job, 1, "launch", f"{type(exc).__name__}: {exc}")
        try:
            self.admission_store.release(
                project_id=project_id, ticket_id=tid, candidate_digest=candidate_key,
                context_digest=context_key, resource_version=resource_key, token=token,
            )
        except Exception:  # noqa: BLE001
            logger.exception("falha ao liberar reserva apos erro de launcher %s", tid)
        if self.recovery.get("active"):
            try:
                if recovery_token:
                    self.admission_store.release_recovery(recovery_token)
            except Exception:  # noqa: BLE001
                logger.exception("falha ao liberar reserva global apos erro de launcher %s", tid)

    def _opportunistic_failure_open(self, host_id: str, project_id: str, tid: str, resource_key: str) -> bool:
        if str(host_id).lower() not in OPPORTUNISTIC_HOST_IDS:
            return False
        return any(
            row.get("open")
            and row.get("project_id") == project_id
            and row.get("ticket_id") == tid
            and row.get("resource_version") == resource_key
            for row in self.admission_store.snapshot()
        )

    def _dispatch_hosts(
        self,
        queue: list[str],
        demands: list[dict[str, Any]],
        idle: dict[str, str],
        skipped: dict[str, str],
        dispatched: list[dict[str, str]],
        orphan_hosts: set[str],
        now: float,
        reservation_state: dict[str, Any],
    ) -> None:
        configured_disabled = {
            host_id.strip().casefold()
            for host_id in self._fleet_config.disabled_hosts
            if host_id.strip()
        }
        disabled_env = {
            host_id.strip().casefold()
            for host_id in os.environ.get("DARKFAC_FLEET_DISABLED_HOSTS", "").split(",")
            if host_id.strip()
        }
        for host in sorted(self.config.hosts, key=lambda h: h.priority):
            if not host.enabled:
                idle[host.id] = "disabled_by_config"
                continue
            if host.id.casefold() in configured_disabled:
                idle[host.id] = "excluded_by_config"
                continue
            if host.id.casefold() in disabled_env:
                idle[host.id] = "excluded_by_environment"
                continue
            if self.pending_releases:
                idle[host.id] = "recovery_awaiting_deploy_proof"
                continue
            if host.id in self.in_flight:
                continue
            if host.id in orphan_hosts:
                idle[host.id] = "run_orfao_em_execucao"
                continue
            if reservation_state["error"]:
                idle[host.id] = "canonical_reservations_unavailable"
                continue
            reserved_hosts = reservation_state["reservations"]
            reservation = reserved_hosts.get(host.id)
            if reservation is not None:
                idle[host.id] = f"host_reserved:{reservation.ticket_id}"
                continue
            if not queue and not any(d.get("status") == "planned" for d in demands):
                idle[host.id] = "sem_ticket_executavel"
                continue
            pr = self.probe(host)
            host_config = host.config or {}
            reading = {
                "workers": pr.workers if pr.workers is not None else int(pr.free),
                "available_gib": pr.available_gib if pr.available_gib is not None else host_config.get("min_free_memory_gib", 0),
                "observed_at": pr.observed_at or datetime.now(timezone.utc).isoformat(),
                "probe_reason": pr.reason,
            }
            self.resource_readings[host.id] = reading
            from core.line import fleet_policy
            decision = next((row for row in fleet_policy.effective_host_policy(
                self._fleet_config, disabled_from_environment=os.environ.get("DARKFAC_FLEET_DISABLED_HOSTS", "").split(","),
                readings={host.id: reading}, now=datetime.now(timezone.utc),
            ) if row["host_id"] == host.id), None)
            if not pr.free:
                note_unavailable_host(host, pr)
                idle[host.id] = f"host_indisponivel:{pr.reason}"
                continue
            if decision is None or not decision["eligible"]:
                idle[host.id] = f"host_indisponivel:{decision['reason'] if decision else 'policy_unavailable'}"
                continue
            if not queue:
                idle[host.id] = "sem_ticket_executavel"
                continue
            if self.recovery.get("active") and dispatched:
                idle[host.id] = "recovery_single_delivery_reserved"
                continue
            carried: list[str] = []
            while queue:
                tid = queue.pop(0)
                demand = next((row for row in demands if str(row.get("id", "")) == tid), None)
                if demand is None:
                    skipped[tid] = "demand_disappeared"
                    idle[host.id] = "demand_disappeared"
                    break
                self._dispatch_ticket(host, pr, tid, demand, queue, idle, skipped, dispatched, now,
                                      reservation_state)
                reason = skipped.get(tid, "")
                if reason == "identical_failure_open":
                    carried.append(tid)
                    continue
                if _ticket_failure_isolates(reason):
                    continue
                break
            if carried:
                queue[:0] = carried
            if any(row.get("host") == host.id for row in dispatched):
                idle.pop(host.id, None)

    def _recovery_list_status(self, terminal: set[str], busy: set[str], queue: list[str], now: float) -> dict[str, Any]:
        """Estado da lista de recuperacao (--recovery-tickets), persistido para sobreviver a reinicio.

        A lista so prioriza; encerra com ``recovery_list_completed`` (toda terminal) ou
        ``recovery_list_expired`` (apos o TTL), e ``recovery_list_no_admissible`` e publicado
        enquanto nenhum item esta na fila nem em voo (a fila nao e travada por isso).
        """
        tickets = list(self.config.recovery_tickets)
        if not tickets:
            return {}
        path = self.config.state_path.with_name("recovery_list_state.json")
        state: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("tickets") == tickets:
                state = loaded
        except (OSError, ValueError):
            state = {}
        changed = False
        if not state:
            state = {"tickets": tickets, "started_at": now, "cause": ""}
            changed = True
        pending = [t for t in tickets if t not in terminal]
        if not state.get("cause"):
            if not pending:
                state["cause"], changed = "recovery_list_completed", True
            elif now - float(state.get("started_at", now)) > self.config.recovery_list_ttl_sec:
                state["cause"], changed = "recovery_list_expired", True
        if changed:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                logger.exception("falha ao gravar estado da lista de recuperacao")
        cause = str(state.get("cause", ""))
        live = not cause
        if live and not any(t in queue or t in busy for t in tickets):
            cause = "recovery_list_no_admissible"
        return {"live": live, "tickets": tickets, "pending": pending, "cause": cause}

    def _demands_store(self) -> Any:
        if self.demands_store_factory is None:
            return _UnwiredDemands()
        return self.demands_store_factory()

    def _reconcile_stale_active(self) -> list[dict[str, Any]]:
        """Devolve status ativo sem vida a planned antes da fila deste ciclo.

        ``resume_only`` nao le o ledger. ``dry_run`` so lista. Ticket em
        ``in_flight`` deste processo e vivo antes de qualquer sonda. Excecao
        vira acao ``error`` e nao escapa.
        """
        if self.config.resume_only:
            return []
        in_flight = {job.ticket_id for job in self.in_flight.values()}
        try:
            from dataclasses import asdict

            from core.line.stale_active import collect_liveness, reconcile_stale_active

            def is_alive(ticket_id: str) -> bool:
                if ticket_id in in_flight:
                    return True
                return collect_liveness(ticket_id)

            actions = reconcile_stale_active(
                self._demands_store(),
                is_alive=is_alive,
                dry_run=self.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 - reconciliacao nunca derruba o ciclo
            logger.exception("reconciliacao de status ativo sem processo falhou")
            return [_reconcile_failure(exc)]
        return [asdict(action) for action in actions]

    def _apply_reconciled_queue(self, demands: list[Any], reconciled: Sequence[Mapping[str, Any]]) -> None:
        """O snapshot da fila pode ainda dizer implementing; o sweep deste ciclo ja devolveu a planned.

        Ticket em ``in_flight`` nao muda de status aqui, mesmo se uma acao o citar.
        """
        protected = {job.ticket_id for job in self.in_flight.values()}
        requeued = {
            str(row.get("ticket_id") or "")
            for row in reconciled
            if isinstance(row, Mapping) and row.get("action") == "requeued"
        }
        requeued.discard("")
        requeued.difference_update(protected)
        if not requeued:
            return
        for demand in demands:
            if not isinstance(demand, dict):
                continue
            ticket_id = str(demand.get("id") or "")
            if ticket_id not in requeued:
                continue
            if str(demand.get("status") or "") not in {"implementing", "validating"}:
                continue
            demand["status"] = "planned"

    def _resumptions(self) -> list[dict[str, Any]]:
        """Due delivery waits. A test double or a real launch skips this probe."""
        if self.launch is not None or self.dry_run:
            return []
        from core.line.pending_delivery import tick

        return tick(PROJECT_ROOT)

    def _load_cycle_fleet_config(self) -> None:
        """Reload `.factory/config/line_fleet.json` (or synthesize config from `self.config.hosts`)."""
        from core.line import fleet_policy

        if self.config.policy_path is not None:
            fleet_config = fleet_policy.load_fleet_config(self.config.policy_path)
            self.config.hosts = [
                HostSpec(h.id, h.priority, h.endpoint, h.enabled, h.model_dump(mode="json"))
                for h in fleet_config.sorted_hosts()
            ]
            self._fleet_config = fleet_config
            return
        self._fleet_config = fleet_policy.FleetConfig(hosts=[
            fleet_policy.HostConfig(
                id=host.id, name=host.id, priority=host.priority, endpoint=host.worker_url,
                enabled=host.enabled,
                max_workers=int((host.config or {}).get("max_workers", 1)),
                min_free_memory_gib=float((host.config or {}).get("min_free_memory_gib", 0)),
            ) for host in self.config.hosts
        ])

    def _publish_cycle_policy(self, orphan_hosts: set[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Evaluate effective host policy against live readings/reservations and persist its snapshot."""
        from core.line import fleet_policy

        occupied = {host_id: job.ticket_id for host_id, job in self.in_flight.items()}
        occupied.update({host_id: "orphan_run" for host_id in orphan_hosts if host_id not in occupied})
        active_reservations = {
            host_id: {"active": True, "ticket_id": job.ticket_id, "run_id": job.run_id}
            for host_id, job in self.in_flight.items() if job.admission_token
        }
        policy_reservation_state = "available"
        try:
            for reservation in self.admission_store.active_host_reservations():
                active_reservations.setdefault(reservation["host_id"], {
                    "active": True, "expires_at": reservation["expires_at"], "source": "admission_store",
                })
        except Exception as exc:  # an unavailable canonical reservation ledger must fail closed
            policy_reservation_state = "unavailable"
            for host in self.config.hosts:
                active_reservations.setdefault(host.id, {
                    "active": True, "source": "admission_store_unavailable", "cause": type(exc).__name__,
                })
        disabled_env = os.environ.get("DARKFAC_FLEET_DISABLED_HOSTS", "").split(",")
        host_policy = fleet_policy.effective_host_policy(
            self._fleet_config,
            disabled_from_environment=disabled_env,
            readings=self.resource_readings,
            occupied=occupied,
            reservations=active_reservations,
            now=datetime.now(timezone.utc),
        )
        policy_report = fleet_policy.policy_snapshot_report(
            self._fleet_config,
            host_policy,
            now=datetime.now(timezone.utc),
            config_source=self.config.policy_path or "dispatcher_config",
            reservation_state=policy_reservation_state,
            disabled_from_environment=disabled_env,
        )
        try:
            snapshot = fleet_policy.write_effective_policy_snapshot(policy_report)
            policy_snapshot = {"path": str(snapshot), "updated_at": policy_report["updated_at"]}
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("could not persist effective fleet policy snapshot: %s", exc)
            policy_snapshot = {"error": type(exc).__name__, "updated_at": policy_report["updated_at"]}
        return host_policy, policy_snapshot

    def run_cycle(self) -> dict[str, Any]:
        self._load_cycle_fleet_config()
        self.resource_readings = {}
        self._reap()
        from core.line.pending_delivery import acknowledge_deploy
        resumptions = self._resumptions()
        if any(r.get("status") == "dispatcher_busy" for r in resumptions):
            return self._finish_cycle({"ts": datetime.now(timezone.utc).isoformat(), "resumptions": resumptions,
                                       "dispatched": [], "deployed": False})
        awaiting_deploy = any(r["status"] == "merged_awaiting_deploy" for r in resumptions)
        if awaiting_deploy and not self.pending_deploy:
            self.pending_deploy, self.last_merge_at = True, self.clock()
        # A new dispatcher must close a previously journaled delivery before it
        # admits regular work. This also keeps --resume-only from becoming a build
        # mode: it may complete this continuation, then returns below.
        durable_delivery_wait = any(row.get("status") == "awaiting_deploy" for row in self.delivery_journal)
        deployed_resume = self._maybe_deploy() if self.pending_deploy else False
        if deployed_resume and awaiting_deploy:
            acknowledge_deploy(PROJECT_ROOT, resumptions)
        if durable_delivery_wait and not deployed_resume:
            return self._finish_cycle({"ts": datetime.now(timezone.utc).isoformat(),
                                       "resumptions": resumptions, "dispatched": [],
                                       "deployed": False, "deployed_this_cycle": False,
                                       "delivery_wait": True, "recovery": self._recovery_summary()})
        if self.config.resume_only:
            return self._finish_cycle({"ts": datetime.now(timezone.utc).isoformat(), "mode": "resume_only",
                                       "resumptions": resumptions, "dispatched": [],
                                       "deployed": deployed_resume})
        reconciled = self._reconcile_stale_active()
        self.recovery = self.admission_store.recovery_state() if self.admission_store is not None and hasattr(self.admission_store, "recovery_state") else {
            "active": True, "cause": "admission_store_unavailable", "fingerprint": ""}
        if self.recovery.get("active"):
            for job in self.in_flight.values():
                if job.recovery_token and not self.admission_store.heartbeat_recovery(job.recovery_token):
                    logger.error("reserva global de recovery perdeu fencing para %s", job.ticket_id)
        now = self.clock()
        try:
            demands = self.demands_loader()
        except Exception as exc:  # noqa: BLE001
            demands = []
            load_error = str(exc)
        else:
            load_error = ""
        self._apply_reconciled_queue(demands, reconciled)
        busy = ({j.ticket_id for j in self.in_flight.values()} | set(self.busy_ids_loader())
                | {r["ticket"] for r in resumptions if "ticket" in r})
        self._main_sha = run_outcome.main_sha(PROJECT_ROOT)
        queue, skipped = select_tickets(demands, busy_ids=busy, admission_store=self.admission_store,
                                        main_sha=self._main_sha, now=now)
        self._pending_skip_reasons(resumptions, demands, skipped, now)
        terminal = {str(d.get("id")) for d in demands if d.get("status") in _TERMINAL_STATUSES}
        recovery_list = self._recovery_list_status(terminal, busy, queue, now)
        queue, preempt_skips = order_fleet_queue(
            queue, demands,
            recovery_tickets=self.config.recovery_tickets,
            recovery_list_live=bool(recovery_list.get("live")),
            recovery_active=bool(self.recovery.get("active")),
            delivery_in_flight=bool(self.in_flight) or any(
                r["status"] in {"running", "merged_awaiting_deploy"} for r in resumptions
            ),
        )
        skipped.update(preempt_skips)
        dispatched: list[dict[str, str]] = []
        idle: dict[str, str] = {}
        lock_busy, self.lock_busy = self.lock_busy, {}
        orphan_hosts = self.busy_hosts_loader()
        reservation_state: dict[str, Any] = {"reservations": {}, "error": ""}
        try:
            reservation_state["reservations"] = self._read_canonical_reservations(self.clock())
        except Exception as exc:  # noqa: BLE001 - capacity cannot be announced from an unknown snapshot
            reservation_state["error"] = type(exc).__name__
            logger.error("canonical reservation snapshot failed (%s)", reservation_state["error"])
        self._dispatch_hosts(queue, demands, idle, skipped, dispatched, orphan_hosts, now, reservation_state)
        idle.update(lock_busy)  # trava de linha recusada no ciclo anterior tem precedencia
        host_policy, policy_snapshot = self._publish_cycle_policy(orphan_hosts)
        deployed = self._maybe_deploy()
        canonical_reservations = reservation_state["reservations"]
        reservation_error = reservation_state["error"]
        if reservation_error:
            for host in self.config.hosts:
                idle[host.id] = "canonical_reservations_unavailable"
        eligible_by_policy = {row["host_id"] for row in host_policy if row["eligible"]}
        return self._finish_cycle({
            "resumptions": resumptions,
            "ts": datetime.now(timezone.utc).isoformat(),
            "policy_snapshot": policy_snapshot,
            "free_hosts": [] if reservation_error else [
                h.id for h in self.config.hosts
                if h.id in eligible_by_policy
                and h.id not in self.in_flight and h.id not in orphan_hosts
                and h.id not in canonical_reservations
                and not idle.get(h.id, "").startswith("host_indisponivel:")
            ],
            "host_policy": host_policy,
            "canonical_reservations": {
                host_id: {
                    "ticket": reservation.ticket_id,
                    "run_id": reservation.run_id,
                    "status": reservation.status,
                    "expires_at": reservation.expires_at,
                }
                for host_id, reservation in canonical_reservations.items()
            },
            "reservation_error": reservation_error,
            "in_flight": {h: j.ticket_id for h, j in self.in_flight.items()},
            "dispatched": dispatched,
            "idle_reasons": idle,
            "skipped": skipped,
            "pending_tickets": [str(d["id"]) for d in demands if d.get("status") == "planned"],
            "queue_remaining": queue,
            "cooldown": sorted(tid for tid, reason in skipped.items() if reason == "backoff_infra"),
            "deploys_total": self.deploys,
            "deployed_this_cycle": deployed or deployed_resume,
            "demands_error": load_error,
            "recent_failures": self.failures,
            "deploy_deferral": self.deploy_deferral_snapshot(),
            "ticket_holds": self._ticket_holds(),
            "recovery_list": {k: v for k, v in recovery_list.items() if k != "live"},
            "recovery": self._recovery_summary(),
            "reconciled": reconciled,
        })

    def _recovery_summary(self) -> dict[str, Any]:
        return {"active": bool(self.recovery.get("active")),
                "cause": self.recovery.get("cause", ""),
                "fingerprint": self.recovery.get("fingerprint", ""),
                "successes": self.recovery.get("successes", 0),
                "receipt_cause": self.recovery.get("receipt_cause", ""),
                "receipt_digest": self.recovery.get("receipt_digest", ""),
                "recovered_candidate_digest": self.recovery.get("recovered_candidate_digest", "")}

    def _finish_cycle(self, cycle: dict[str, Any]) -> dict[str, Any]:
        self._record_queue_blockage(cycle)
        now = float(self.clock())
        progress_evidence: list[dict[str, Any]] = []
        if cycle.get("dispatched"):
            progress_evidence.append({"source": "dispatch", "at": now})
        for job in self.in_flight.values():
            candidates = [job.started_at]
            if job.log_path is not None:
                try:
                    candidates.append(job.log_path.stat().st_mtime)
                except OSError:
                    pass
            stamp = max((value for value in candidates if value and value > 0), default=0.0)
            if stamp:
                progress_evidence.append({"ticket": job.ticket_id, "source": "in_flight", "at": stamp})
        cycle["progress_evidence"] = progress_evidence
        activity, stages, last_progress = progress_activity(
            cycle,
            self.last_cycle.get("stages"),
            previous_progress_epoch=_progress_epoch(self.last_cycle.get("last_progress_epoch")),
            now=now,
            window_sec=self.config.progress_window_sec,
        )
        cycle["activity"] = activity
        cycle["stages"] = stages
        cycle["progress_window_sec"] = max(0.0, float(self.config.progress_window_sec))
        cycle["heartbeat_epoch"] = now
        cycle["last_progress_epoch"] = last_progress
        observed = self._read_checkout_sha()
        cycle["code_sha"] = observed
        cycle["code_sha_changed"] = _checkout_sha_changed(self._startup_sha, observed)
        cycle.setdefault("reconciled", [])
        self.last_cycle = cycle
        self._write_state()
        return cycle

    def _previous_cycle(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.config.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _record_queue_blockage(self, cycle: dict[str, Any]) -> None:
        pending = cycle.get("pending_tickets", [])
        skipped = cycle.get("skipped", {})
        causes: dict[str, int] = {}
        blockers: dict[str, str] = {}
        for ticket in pending:
            reason = str(skipped.get(ticket, ""))
            if reason.startswith("ticket_hold:"):
                key = "hold"
            elif reason in {"backoff_infra", "admission_store_unavailable"}:
                key = reason
            elif reason.startswith("pending_delivery:"):
                key = "delivery_failure"
            else:
                blockers = {}
                break
            blockers[str(ticket)] = reason
            causes[key] = causes.get(key, 0) + 1
        active = (bool(pending) and len(blockers) == len(pending) and cycle.get("free_hosts")
                  and not cycle.get("in_flight") and not cycle.get("dispatched"))
        fingerprint = hashlib.sha256(json.dumps(
            blockers, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest() if active else ""
        previous_count = self.last_cycle.get("blocked_cycles", 0)
        try:
            previous_count = max(0, int(previous_count))
        except (TypeError, ValueError):
            previous_count = 0
        same_blockage = (
            bool(fingerprint)
            and self.last_cycle.get("blocked_fingerprint") == fingerprint
            and self.last_cycle.get("blocked_ticket_reasons") == blockers
            and self.last_cycle.get("blocked_causes") == causes
        )
        cycle["blocked_cycles"] = previous_count + 1 if active and same_blockage else int(bool(active))
        cycle["blocked_causes"] = causes if active else {}
        cycle["blocked_ticket_reasons"] = blockers if active else {}
        cycle["blocked_fingerprint"] = fingerprint

    def _pending_skip_reasons(self, resumptions: list[dict[str, Any]], demands: list[dict[str, Any]],
                              skipped: dict[str, str], now: float) -> None:
        projects = {str(d.get("id")): str(d.get("project_id") or "darkfac") for d in demands}
        for row in resumptions:
            ticket = row.get("ticket")
            if ticket and row.get("status") not in {"running", "merged_awaiting_deploy", "terminal"}:
                try:
                    reason = self.admission_store.retry_reason(projects.get(ticket, "darkfac"), ticket,
                                                               main_sha=self._main_sha, now=now)
                except Exception:
                    reason = "admission_store_unavailable"
                skipped[ticket] = reason or f"pending_delivery:{row.get('cause') or row.get('status')}"

    def _ticket_holds(self) -> list[dict[str, Any]]:
        try:
            return self.admission_store.ticket_holds() if self.admission_store is not None else []
        except Exception:  # noqa: BLE001
            logger.exception("falha ao ler holds por ticket")
            return []

    def _write_state(self) -> None:
        try:
            self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.last_cycle, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.config.state_path)
        except OSError:
            logger.exception("falha ao gravar estado do despachante")

    def run_digest_hook(self) -> None:
        if self.digest_hook is None or self.dry_run:
            return
        try:
            self.digest_hook()
        except Exception:  # noqa: BLE001 - notificacao nunca derruba o ciclo
            logger.exception("gancho do resumo diario falhou")

    def run_sentinel_hook(self) -> None:
        if self.sentinel_hook is None or self.dry_run:
            return
        try:
            self.sentinel_hook()
        except Exception:  # noqa: BLE001 - sentinela nunca derruba o ciclo
            logger.exception("gancho do sentinela falhou")

    def run_orphan_worktree_cleanup_hook(self) -> None:
        if self.orphan_worktree_cleanup_hook is None or self.dry_run:
            return
        try:
            self.orphan_worktree_cleanup_hook()
        except Exception:  # noqa: BLE001 - manutencao nunca derruba o ciclo da frota
            logger.exception("gancho de limpeza de worktrees sem dono falhou")

    def publish_usage(self, now: Optional[float] = None) -> None:
        """Publica as cotas no Hub a cada 5 min, sem sonda paga; falha so registra em log."""
        current = time.time() if now is None else now
        if self.usage_hook is None or self.dry_run or current - self._usage_published_at < USAGE_PUBLISH_SEC:
            return
        self._usage_published_at = current
        try:
            result = self.usage_hook()
            if not result.ok:
                logger.warning("publicacao de cotas no Hub falhou: %s", result.error)
        except Exception:  # noqa: BLE001 - publicacao nunca derruba o ciclo
            logger.exception("publicacao de cotas no Hub falhou")

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            # --once nao entra aqui. Mudanca de SHA substitui antes do ciclo.
            # Import obsoleto substitui depois do ciclo, uma vez por geracao.
            if self._exec_if_checkout_sha_changed():
                return
            try:
                cycle = self.run_cycle()
            except Exception:  # noqa: BLE001 - o daemon nunca morre por um ciclo ruim
                logger.exception("ciclo do despachante falhou")
                cycle = None
            if self._exec_if_stale_dispatcher_import(cycle):
                return
            self.run_digest_hook()
            self.run_sentinel_hook()
            self.run_orphan_worktree_cleanup_hook()
            self.publish_usage()
            time.sleep(self.config.cycle_sec)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Despachante continuo da frota (USR-366)")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume-only", action="store_true",
                    default=os.environ.get("DARKFAC_FLEET_RESUME_ONLY") == "1",
                    help="Retoma esperas persistidas sem admitir novos BUILDs")
    ap.add_argument("--recovery-tickets", metavar="ID1,ID2", action="append", default=[],
                    help="Um unico valor CSV (IDs separados por virgulas); processa na ordem, uma correcao por vez")
    ap.add_argument("--cycle-sec", type=float, default=30.0)
    try:
        progress_window_default = max(
            0.0, float(os.environ.get("DARKFAC_FLEET_PROGRESS_WINDOW_SEC", DEFAULT_PROGRESS_WINDOW_SEC)),
        )
    except ValueError:
        progress_window_default = DEFAULT_PROGRESS_WINDOW_SEC
    ap.add_argument("--progress-window-sec", type=float, default=progress_window_default,
                    help="Idade maxima de evidencia de progresso para publicar activity=running")
    ap.add_argument("unexpected_args", nargs="*", metavar="ARG",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if len(args.recovery_tickets) > 1:
        ap.error("informe --recovery-tickets uma unica vez com todos os IDs em um valor CSV")
    if args.unexpected_args:
        ap.error("argumentos posicionais inesperados; passe --recovery-tickets como um unico valor separado por virgulas")
    log_dir = Path(os.environ.get("DARKFAC_FLEET_LOG_DIR", str(LOG_DIR)))
    configure_logging(log_dir)
    from core.line.sentinel import maybe_run_sentinel
    from core.notifications.daily_digest import maybe_send_due
    from core.usage.sync_task import publish_once

    recovery_value = args.recovery_tickets[0] if args.recovery_tickets else ""
    recovery = tuple(t.strip() for t in recovery_value.split(",") if t.strip())
    from core.line.fleet_policy import DEFAULT_CONFIG_PATH
    d = FleetDispatcher(DispatcherConfig(cycle_sec=args.cycle_sec,
                                        progress_window_sec=max(0.0, args.progress_window_sec),
                                        resume_only=args.resume_only and not recovery,
                                        recovery_tickets=recovery,
                                        policy_path=DEFAULT_CONFIG_PATH), dry_run=args.dry_run,
                        digest_hook=None if args.resume_only else maybe_send_due,
                        sentinel_hook=None if args.resume_only else maybe_run_sentinel,
                        orphan_worktree_cleanup_hook=scheduled_cleanup_hook(log_dir),
                        usage_hook=publish_once,
                        busy_ids_loader=running_fleet_tickets, busy_hosts_loader=running_fleet_hosts,
                        launch_argv=list(sys.argv[1:] if argv is None else argv),
                        demands_store_factory=daemon_demands_store)
    if args.loop and not args.once:
        d.run_forever()
        return 0
    print(json.dumps(d.run_cycle(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
