"""Despachante continuo da frota (USR-366).

Roda sem sessao de chat (tarefa agendada) e mantem grokbox, Notebook e Desktop ocupados: a cada
ciclo le ``demands.json`` de ``origin/main``, escolhe tickets ``planned`` executaveis e despacha UM
por host livre, na ordem de ``line_fleet.json`` (grokbox > notebook > desktop). Cada despacho e um
``run_ticket.py --fleet --host-id <host>`` (que escolhe o harness por cota e usa a reserva por host
de ``line_lock``); hosts remotos fixam o worker do portao via ``DARKFAC_TEST_WORKERS`` +
``DARKFAC_FLEET_GATE_REMOTE=1`` (``--remote-required``). Falha de infra reenfileira (o ticket segue
``planned`` e entra em cooldown); deploy pos-merge e deduplicado por janela de silencio.

Uso::

    python -m core.line.fleet_dispatcher --loop
    python -m core.line.fleet_dispatcher --once --dry-run
"""

from __future__ import annotations

import argparse
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
from typing import Any, Callable, Mapping, Optional, Sequence
from core.paths import state_root

logger = logging.getLogger("darkfac.line.fleet_dispatcher")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = state_root() / "runs" / "fleet_dispatcher_state.json"
LOG_DIR = state_root() / "runs" / "fleet_dispatcher_logs"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

EXCLUDED_TAGS = frozenset({"congelado-v0", "not-authorized-for-execution", "planning-only", "waiting_human"})
MIN_NOTEBOOK_WORKERS = 1
INFRA_EXIT_CODES = frozenset({75})
LOCK_MARKERS = ("[TRAVA DE LINHA]", "[RESERVA DE FROTA]", "ja possui PR ativo")
INFRA_MARKERS = ("[REMOTE_JOB_LOST]", "--remote-required:", "RemoteRequiredError", "harness_infra_error")
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
FAILURE_HISTORY_LIMIT = 20


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


@dataclass
class HostSpec:
    id: str
    priority: int
    worker_url: Optional[str] = None  # None = executa localmente (Notebook)


def default_hosts() -> list[HostSpec]:
    """Ordem e URLs: prioridade de ``line_fleet.json``; URLs sobrescrevem por ``DARKFAC_FLEET_URL_<ID>``.

    ``DARKFAC_FLEET_DISABLED_HOSTS`` (ids separados por virgula) tira hosts da frota sem editar o
    config versionado, ex.: Desktop ocupado com outra carga.
    """
    from core.line import fleet_policy

    defaults = {"grokbox": "http://100.77.130.5:8080", "desktop": "http://100.78.181.90:8080"}
    disabled = {x.strip().lower() for x in os.environ.get("DARKFAC_FLEET_DISABLED_HOSTS", "").split(",") if x.strip()}
    hosts: list[HostSpec] = []
    for h in fleet_policy.load_fleet_config().sorted_hosts():
        if h.id.lower() in disabled:
            continue
        url = os.environ.get(f"DARKFAC_FLEET_URL_{h.id.upper()}", defaults.get(h.id))
        hosts.append(HostSpec(h.id, h.priority, url))
    return hosts


@dataclass
class DispatcherConfig:
    resume_only: bool = False
    recovery_tickets: tuple[str, ...] = ()
    hosts: list[HostSpec] = field(default_factory=default_hosts)
    cycle_sec: float = 30.0
    stable_probes: int = 3
    requeue_cooldown_sec: float = 600.0
    lock_requeue_cooldown_sec: float = 30.0  # host ocupado/trava: reenfileira quase de imediato
    deploy_window_sec: float = 180.0
    recovery_failure_threshold: int = 3
    recovery_list_ttl_sec: float = 43200.0
    state_path: Path = STATE_PATH


@dataclass
class InFlight:
    ticket_id: str
    host_id: str
    handle: Any  # tem .poll() -> int | None
    log_path: Optional[Path] = None
    started_at: float = 0.0
    operational_checkout: Optional[Path] = None
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


# --- selecao pura ---------------------------------------------------------------------------------


def select_tickets(
    demands: Sequence[Mapping[str, Any]],
    *,
    busy_ids: set[str],
    cooldown_ids: set[str] = frozenset(),  # type: ignore[assignment]
) -> tuple[list[str], dict[str, str]]:
    """Tickets ``planned`` executaveis, em ordem estavel (id), mais o motivo de cada exclusao."""
    done = {d["id"] for d in demands if d.get("status") == "completed"}
    eligible: list[tuple[str, str]] = []
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
        elif tid in cooldown_ids:
            skipped[tid] = "cooldown_infra"
        elif [x for x in (d.get("dependencies") or []) if x not in done]:
            skipped[tid] = "dependencia_aberta"
        else:
            eligible.append((str(d.get("created_at", "")), tid))
    return [t for _, t in sorted(eligible)], skipped


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
            workers = gate_ram.measure_workers().workers
        except Exception as exc:  # noqa: BLE001
            return HostProbe(False, f"probe_local_falhou:{type(exc).__name__}", f"{host.id}:probe_error:{type(exc).__name__}")
        return HostProbe(workers >= MIN_NOTEBOOK_WORKERS, f"workers={workers}", f"{host.id}:workers={workers}")
    from core.harness import remote_dispatch as rd

    for _ in range(max(1, stable_probes)):
        health = rd.probe_health(host.worker_url)
        if health is None:
            return HostProbe(False, "health_indisponivel", f"{host.id}:health_unavailable")
        if health.get("busy"):
            return HostProbe(False, "worker_ocupado", f"{host.id}:busy")
    # Exclude observation timestamps: polling a healthy worker must not create a new resource version.
    identity = {
        key: health.get(key) for key in ("worker_id", "workers", "busy", "status", "version", "machine_id")
        if key in health
    }
    from core.line.admission_store import safe_digest
    return HostProbe(True, "saudavel", f"{host.id}:{safe_digest(identity)}")


def _git_checked(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run git and turn source/control failures into visible launch errors."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=120, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} falhou (rc={result.returncode}): {detail}")
    return result


def _create_operational_checkout(ticket_id: str, host_id: str, demand: Mapping[str, Any]) -> Path:
    """Create a clean launcher checkout and seed only the selected ticket into its ledger."""
    from core.demands.models import UserTicket
    from core.demands.store import DemandsStore
    from core.git import ticket_workspace

    main_root = ticket_workspace.main_checkout_root(PROJECT_ROOT)
    _git_checked(["fetch", "origin", "main", "-q"], main_root)
    stamp = f"{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
    path = main_root / ".worktrees" / f"fleet-dispatch-{ticket_id.lower()}-{host_id}-{stamp}"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # `worktree add` can create its admin entry/directory before returning an
        # error. Keep it inside the cleanup scope so a failed launch cannot leave
        # an orphan that later collides with workspace discovery.
        _git_checked(["worktree", "add", "--detach", str(path), "origin/main"], main_root)
        ticket = UserTicket.model_validate(dict(demand))
        if ticket.id != ticket_id:
            raise ValueError(f"registro selecionado tem id {ticket.id}, esperado {ticket_id}")
        DemandsStore(path / ".factory" / "demands" / "demands.json").save_ticket(ticket)
        status = _git_checked(["status", "--porcelain=v1", "--untracked-files=all"], path).stdout.strip()
        changed = [line[2:].strip() for line in status.splitlines() if len(line) >= 4]
        if changed and changed != [".factory/demands/demands.json"]:
            raise RuntimeError(f"checkout operacional recebeu arquivos inesperados: {changed}")
    except Exception:
        _remove_operational_checkout(path, main_root=main_root)
        raise
    return path


def _remove_operational_checkout(path: Optional[Path], *, main_root: Optional[Path] = None) -> None:
    if path is None:
        return
    try:
        from core.git import ticket_workspace

        root = main_root or ticket_workspace.main_checkout_root(PROJECT_ROOT)
        result = ticket_workspace.cleanup(path, root)
        if result.method == "preserved":
            logger.warning(
                "checkout operacional preservado: path=%s holder=%s; recuperacao possivel "
                "apos o processo encerrar, repetindo cleanup pelo despachante",
                path, result.detail,
            )
        elif not result.removed:
            logger.error(
                "nao foi possivel remover checkout operacional: path=%s metodo=%s detalhe=%s; "
                "recuperacao possivel repetindo cleanup apos liberar os handles",
                path, result.method, result.detail or "; ".join(result.errors),
            )
        else:
            logger.info("checkout operacional removido: path=%s metodo=%s", path, result.method)
    except Exception:
        logger.exception("falha ao remover checkout operacional %s", path)


def default_launch(ticket_id: str, host: HostSpec, demand: Mapping[str, Any], *,
                   run_id: Optional[str] = None, recovery: bool = False,
                   candidate_digest: str = "unknown") -> InFlight:
    env = dict(os.environ)
    # The launcher runs from a temporary operational checkout, but its mutable
    # control store is shared with the dispatcher and the other workers. Resolve
    # the parent's effective root before changing cwd; this also makes a relative
    # DARKFAC_STATE_ROOT override stable in the child process.
    from core.paths import state_root

    env["DARKFAC_STATE_ROOT"] = str(state_root().resolve())
    env["DARKFAC_HOST_ID"] = host.id
    if run_id:
        env["DARKFAC_RUN_ID"] = run_id
    if recovery:
        env["DARKFAC_RECOVERY_MODE"] = "1"
    env["DARKFAC_CANDIDATE_DIGEST"] = candidate_digest
    if host.worker_url:
        env["DARKFAC_TEST_WORKERS"] = host.worker_url
        env["DARKFAC_REMOTE_HARNESS"] = "required"
        env["DARKFAC_FLEET_GATE_REMOTE"] = "1"
    else:
        env["DARKFAC_REMOTE_HARNESS"] = "off"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{ticket_id}-{host.id}.log"
    operational_checkout = _create_operational_checkout(ticket_id, host.id, demand)
    cmd = [sys.executable, str(operational_checkout / "run_ticket.py"), ticket_id, "--fleet", "--host-id", host.id]
    if run_id:
        cmd.extend(["--run-id", run_id])
    if recovery:
        cmd.append("--recovery")
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(cmd, cwd=operational_checkout, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=CREATE_NO_WINDOW)
    except Exception:
        _remove_operational_checkout(operational_checkout)
        raise
    return InFlight(ticket_id, host.id, proc, log_path, time.time(), operational_checkout)


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


def default_deploy() -> int:
    return subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "dokploy_redeploy.py")],
                          cwd=PROJECT_ROOT, creationflags=CREATE_NO_WINDOW).returncode


def is_lock_refusal(log_tail: str) -> bool:
    return any(m in log_tail for m in LOCK_MARKERS)


def is_infra_failure(rc: int, log_tail: str) -> bool:
    return rc in INFRA_EXIT_CODES or any(m in log_tail for m in INFRA_MARKERS)


def failure_reason(rc: int, log_tail: str, lines: int = 8, chars: int = 800) -> str:
    """Motivo da falha: ultimas linhas do log (sem segredo); nunca vazio."""
    from core.line.agent_cli import redact_secrets

    kept = chr(10).join(redact_secrets(log_tail or "").strip().splitlines()[-lines:])[-chars:]
    return kept or f"sem saida no log; exit_code={rc}"


_CAUSE_HUMAN_RE = re.compile(r"\[ERRO NA ENTREGA\][^\n]*?causa:\s*([a-z0-9_]+)")


def classify_log_cause(log_tail: str) -> str:
    """The official delivery line takes precedence over nested gate output."""
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
            return str(payload["cause"])
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
        clock: Callable[[], float] = time.time,
        busy_ids_loader: Callable[[], set[str]] = lambda: set(),
        busy_hosts_loader: Callable[[], set[str]] = lambda: set(),
        dry_run: bool = False,
        digest_hook: Optional[Callable[[], Any]] = None,
        sentinel_hook: Optional[Callable[[], Any]] = None,
        admission_store: Any = None,
    ) -> None:
        self.config = config or DispatcherConfig()
        self.demands_loader = demands_loader
        self.probe = probe or (lambda h: default_probe(h, self.config.stable_probes))
        self.launch = launch
        self.deploy = deploy
        self.clock = clock
        self.busy_hosts_loader = busy_hosts_loader  # hosts com run --fleet vivo fora deste processo
        self.busy_ids_loader = busy_ids_loader  # tickets com run/worktree de entrega fora deste processo
        self.dry_run = dry_run
        self.digest_hook = digest_hook  # resumo diario ao owner (USR-376); idempotente por dia
        self.sentinel_hook = sentinel_hook  # sentinela da linha a cada 45m (USR-439); idempotente
        if admission_store is None:
            from core.line.admission_store import AdmissionStore
            from core.paths import state_root

            try:
                admission_store = AdmissionStore(state_root() / "admission.sqlite3")
            except Exception as exc:  # noqa: BLE001 - run_cycle reports a structured fail-closed cause
                logger.error("admission store indisponivel na inicializacao (%s)", type(exc).__name__)
                admission_store = None
        self.admission_store = admission_store
        self.in_flight: dict[str, InFlight] = {}  # trava por host: no maximo um ticket por host
        self.cooldown: dict[str, float] = {}
        self.lock_busy: dict[str, str] = {}  # host -> motivo (trava de linha recusada), vai a idle_reasons
        self.last_merge_at: Optional[float] = None
        self.pending_deploy = False
        self.pending_release_path = self.config.state_path.with_name("pending_recovery_releases.json")
        self.pending_releases = json.loads(self.pending_release_path.read_text(encoding="utf-8")) if self.pending_release_path.exists() else []
        if self.pending_releases:
            self.pending_deploy = True
            self.last_merge_at = self.clock() - self.config.deploy_window_sec
        self.deploys = 0
        self.last_cycle: dict[str, Any] = {}
        self.failures: list[dict[str, Any]] = []  # ultimas falhas: ticket, host, exit_code, motivo, classe
        self.recovery = self.admission_store.recovery_state() if self.admission_store is not None and hasattr(self.admission_store, "recovery_state") else {
            "active": True, "cause": "admission_store_unavailable", "fingerprint": ""}

    def _record_failure(self, job: InFlight, rc: int, kind: str, tail: str) -> None:
        reason = failure_reason(rc, tail)
        self.failures = (self.failures + [{
            "ts": datetime.now(timezone.utc).isoformat(), "ticket": job.ticket_id, "host": job.host_id,
            "exit_code": rc, "kind": kind, "reason": reason,
            "log": str(job.log_path) if job.log_path else None,
        }])[-FAILURE_HISTORY_LIMIT:]
        logger.error("%s falhou em %s: exit_code=%s kind=%s motivo=%s", job.ticket_id, job.host_id, rc, kind,
                     " | ".join(reason.splitlines()))
        try:
            from core.line.admission_store import TICKET_CAUSES, make_fingerprint

            cause = classify_log_cause(tail) if kind == "failure" else ""
            ticket_cause = cause if cause in TICKET_CAUSES else ""
            fingerprint = make_fingerprint(
                project_id=job.project_id, ticket_id=job.ticket_id,
                candidate_digest=job.candidate_digest, context_digest=job.context_digest,
                error=(f"{kind}:{tail}" if kind == "recovery_receipt" and tail else
                       kind if kind != "failure" else (cause or kind)),
                # causa de ticket nao depende do host: soma entre hosts
                resource_version="ticket_cause" if ticket_cause else job.resource_version,
            )
            occurrences = self.admission_store.record_failure(
                fingerprint, initial_backoff_sec=int(self.config.requeue_cooldown_sec))
            # repeticao e do mesmo ticket (o fingerprint inclui ticket_id): segura so ele; 75 e trava ocupada = espera
            if occurrences >= max(1, self.config.recovery_failure_threshold) and (
                    ticket_cause or (rc != 75 and kind != "lock")):
                self.admission_store.set_ticket_hold(fingerprint, occurrences)
                logger.error("%s em hold: causa=%s ocorrencias=%s", job.ticket_id,
                             ticket_cause or fingerprint.error_class, occurrences)
        except Exception:  # noqa: BLE001 - no admission can be trusted after persistence failure
            logger.exception("falha ao persistir circuit breaker de %s", job.ticket_id)

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
            _remove_operational_checkout(job.operational_checkout)
            tail = _tail(job.log_path) if rc != 0 else ""
            if rc == 0:
                tail = _tail(job.log_path)
                in_recovery = bool(self.admission_store and self.admission_store.recovery_state().get("active"))
                if _delivery_evidence(tail):
                    self._release_reservation(job)
                    if in_recovery:
                        pending = {"ticket_id": job.ticket_id, "run_id": job.run_id}
                        if pending not in self.pending_releases:
                            self.pending_releases.append(pending)
                            self._save_pending_releases()
                        self.recovery = self.admission_store.record_recovery_receipt_cause("awaiting_deploy_proof")
                    self.last_merge_at = self.clock()
                    self.pending_deploy = True
                elif not in_recovery:
                    self._record_failure(job, rc, "no_delivery_evidence", tail)
                    self._release_reservation(job)
                else:
                    self._record_failure(job, rc, "no_delivery_evidence", tail)
                    self.recovery = self.admission_store.record_recovery_receipt_cause("no_delivery_evidence")
                    self._release_reservation(job)
                if not _delivery_evidence(tail):
                    self.cooldown[job.ticket_id] = self.clock() + self.config.requeue_cooldown_sec * 3
            elif is_lock_refusal(tail):
                self._record_failure(job, rc, "lock", tail)
                self._release_reservation(job)
                self.cooldown[job.ticket_id] = self.clock() + self.config.lock_requeue_cooldown_sec
                self.lock_busy[host_id] = f"host_ocupado_trava:{job.ticket_id}"
                logger.warning("trava de linha em %s; %s reenfileirado", host_id, job.ticket_id)
            elif is_infra_failure(rc, tail):
                self._record_failure(job, rc, "infra", tail)
                self._release_reservation(job)
                self.cooldown[job.ticket_id] = self.clock() + self.config.requeue_cooldown_sec
                logger.warning("infra em %s (%s); %s reenfileirado", host_id, rc, job.ticket_id)
            else:
                self._record_failure(job, rc, "failure", tail)
                self._release_reservation(job)
                self.cooldown[job.ticket_id] = self.clock() + self.config.requeue_cooldown_sec * 3

    def _maybe_deploy(self) -> bool:
        """Um deploy por lote: so apos a janela de silencio desde o ultimo merge."""
        if not self.pending_deploy or self.last_merge_at is None:
            return False
        if self.clock() - self.last_merge_at < self.config.deploy_window_sec:
            return False
        if self.dry_run:
            return False
        from core.demands.store import _PathMutex, DemandsLockTimeout
        try:
            with _PathMutex(state_root() / "runs" / "deploy", timeout=0.1):
                rc = self.deploy()
        except DemandsLockTimeout:
            rc = 75
        if rc != 0:
            self.failures = (self.failures + [{
                "ts": datetime.now(timezone.utc).isoformat(), "ticket": "deploy", "host": "vps",
                "exit_code": rc, "kind": "deploy", "reason": f"canonical_deploy_exit:{rc}", "log": None,
            }])[-FAILURE_HISTORY_LIMIT:]
            logger.error("Canonical deployment deferred: exit_code=%s", rc)
            self.last_merge_at = self.clock()
            self._write_state()
            return False
        if self.pending_releases and not self._complete_pending_releases():
            self.last_merge_at = self.clock()
            return False
        self.pending_deploy = False
        self.deploys += 1
        return True

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
            hold = self.admission_store.ticket_hold(
                project_id=project_id, ticket_id=tid, candidate_digest=candidate_key, context_digest=context_key)
            if hold:
                idle[host.id] = skipped[tid] = f"ticket_hold:{hold['cause']}"
                return  # so o ticket fica retido; o host tenta o proximo
            if self.admission_store.is_failure_open(
                project_id=project_id, ticket_id=tid, candidate_digest=candidate_key,
                context_digest=context_key, resource_version=resource_key,
                error_class=normalize_error(demand["error_class"]) if demand.get("error_class") else None,
            ):
                idle[host.id] = "identical_failure_open"
                skipped[tid] = "identical_failure_open"
                return  # fora da fila neste ciclo: o host tenta o proximo admissivel
            if self.recovery.get("active"):
                recovery_token = self.admission_store.reserve_recovery()
                if recovery_token is None:
                    idle[host.id] = "recovery_delivery_reserved"
                    skipped[tid] = "recovery_delivery_reserved"
                    queue.insert(0, tid)
                    return
            token = self.admission_store.reserve(
                project_id=project_id, ticket_id=tid, candidate_digest=candidate_key,
                context_digest=context_key, resource_version=resource_key,
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
            if self.launch is None:
                job = default_launch(tid, host, demand, run_id=run_id,
                                     recovery=bool(self.recovery.get("active")),
                                     candidate_digest=candidate)
            else:
                job = self._launch_with_contract(tid, host, run_id, candidate)
            self._register_flight(host.id, job, project_id, candidate, context, resource_version, token,
                                  run_id, recovery_token)
            dispatched.append({"ticket": tid, "host": host.id})
        except Exception as exc:  # noqa: BLE001 - falha de infra: ticket volta a fila
            self._record_launch_failure(
                host, tid, project_id, candidate, context, resource_version,
                candidate_key, context_key, resource_key, token, recovery_token, exc, idle, now,
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
        self.in_flight[host_id] = job

    def _record_launch_failure(
        self, host: HostSpec, tid: str, project_id: str, candidate: str, context: str,
        resource_version: str, candidate_key: str, context_key: str, resource_key: str,
        token: str, recovery_token: str | None, exc: Exception, idle: dict[str, str], now: float,
    ) -> None:
        from core.line.agent_cli import redact_secrets

        safe_error = " | ".join(redact_secrets(str(exc)).splitlines())[-800:]
        idle[host.id] = f"lancamento_falhou:{safe_error}"
        failed_job = InFlight(
            tid, host.id, None, project_id=project_id, candidate_digest=candidate,
            context_digest=context, resource_version=resource_version,
        )
        self._record_failure(failed_job, 1, "launch", str(exc))
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
        self.cooldown[tid] = now + self.config.requeue_cooldown_sec

    def _dispatch_hosts(
        self,
        queue: list[str],
        demands: list[dict[str, Any]],
        idle: dict[str, str],
        skipped: dict[str, str],
        dispatched: list[dict[str, str]],
        orphan_hosts: set[str],
        now: float,
    ) -> None:
        for host in sorted(self.config.hosts, key=lambda h: h.priority):
            if self.pending_releases:
                idle[host.id] = "recovery_awaiting_deploy_proof"
                continue
            if host.id in self.in_flight:
                continue
            if host.id in orphan_hosts:
                idle[host.id] = "run_orfao_em_execucao"
                continue
            if not queue:
                idle[host.id] = "sem_ticket_executavel"
                continue
            pr = self.probe(host)
            if not pr.free:
                idle[host.id] = f"host_indisponivel:{pr.reason}"
                continue
            if self.recovery.get("active") and dispatched:
                idle[host.id] = "recovery_single_delivery_reserved"
                continue
            while queue:
                tid = queue.pop(0)
                demand = next((row for row in demands if str(row.get("id", "")) == tid), None)
                if demand is None:
                    skipped[tid] = "demand_disappeared"
                    idle[host.id] = "demand_disappeared"
                    break
                self._dispatch_ticket(host, pr, tid, demand, queue, idle, skipped, dispatched, now)
                if skipped.get(tid) != "identical_failure_open" and not skipped.get(tid, "").startswith("ticket_hold:"):
                    break
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

    def run_cycle(self) -> dict[str, Any]:
        self._reap()
        from core.line.pending_delivery import tick, acknowledge_deploy
        resumptions = tick(PROJECT_ROOT) if self.launch is None and not self.dry_run else []
        if any(r.get("status") == "dispatcher_busy" for r in resumptions):
            self.last_cycle = {"ts": datetime.now(timezone.utc).isoformat(), "resumptions": resumptions,
                               "dispatched": [], "deployed": False}
            self._write_state()
            return self.last_cycle
        awaiting_deploy = any(r["status"] == "merged_awaiting_deploy" for r in resumptions)
        if awaiting_deploy and not self.pending_deploy:
            self.pending_deploy, self.last_merge_at = True, self.clock()
        deployed_resume = self._maybe_deploy() if awaiting_deploy else False
        if deployed_resume:
            acknowledge_deploy(PROJECT_ROOT, resumptions)
        if self.config.resume_only:
            self.last_cycle = {"ts": datetime.now(timezone.utc).isoformat(), "mode": "resume_only",
                               "resumptions": resumptions, "dispatched": [], "deployed": deployed_resume}
            self._write_state()
            return self.last_cycle
        self.recovery = self.admission_store.recovery_state() if self.admission_store is not None and hasattr(self.admission_store, "recovery_state") else {
            "active": True, "cause": "admission_store_unavailable", "fingerprint": ""}
        if self.recovery.get("active"):
            for job in self.in_flight.values():
                if job.recovery_token and not self.admission_store.heartbeat_recovery(job.recovery_token):
                    logger.error("reserva global de recovery perdeu fencing para %s", job.ticket_id)
        now = self.clock()
        self.cooldown = {t: u for t, u in self.cooldown.items() if u > now}
        try:
            demands = self.demands_loader()
        except Exception as exc:  # noqa: BLE001
            demands = []
            load_error = str(exc)
        else:
            load_error = ""
        busy = ({j.ticket_id for j in self.in_flight.values()} | set(self.busy_ids_loader())
                | {r["ticket"] for r in resumptions if "ticket" in r})
        queue, skipped = select_tickets(demands, busy_ids=busy, cooldown_ids=set(self.cooldown))
        terminal = {str(d.get("id")) for d in demands if d.get("status") in _TERMINAL_STATUSES}
        recovery_list = self._recovery_list_status(terminal, busy, queue, now)
        if recovery_list.get("live"):
            allowed = [tid for tid in self.config.recovery_tickets if tid in queue]
            rest = [tid for tid in queue if tid not in allowed]
            if not self.recovery.get("active"):
                queue = allowed + rest  # lista so prioriza: todo host livre e usado
            else:
                active_resume = any(r["status"] in {"running", "merged_awaiting_deploy"} for r in resumptions)
                if active_resume or self.in_flight:
                    skipped.update({tid: "recuperacao_prioritaria" for tid in rest})
                    queue = []
                elif allowed:
                    queue = allowed + rest
        dispatched: list[dict[str, str]] = []
        idle: dict[str, str] = {}
        lock_busy, self.lock_busy = self.lock_busy, {}
        orphan_hosts = self.busy_hosts_loader()
        self._dispatch_hosts(queue, demands, idle, skipped, dispatched, orphan_hosts, now)
        idle.update(lock_busy)  # trava de linha recusada no ciclo anterior tem precedencia
        deployed = self._maybe_deploy()
        self.last_cycle = {
            "resumptions": resumptions,
            "ts": datetime.now(timezone.utc).isoformat(),
            "free_hosts": [h.id for h in self.config.hosts if h.id not in self.in_flight],
            "in_flight": {h: j.ticket_id for h, j in self.in_flight.items()},
            "dispatched": dispatched,
            "idle_reasons": idle,
            "skipped": skipped,
            "queue_remaining": queue,
            "cooldown": sorted(self.cooldown),
            "deploys_total": self.deploys,
            "deployed_this_cycle": deployed,
            "demands_error": load_error,
            "recent_failures": self.failures,
            "ticket_holds": self._ticket_holds(),
            "recovery_list": {k: v for k, v in recovery_list.items() if k != "live"},
            "recovery": {"active": bool(self.recovery.get("active")),
                         "cause": self.recovery.get("cause", ""),
                         "fingerprint": self.recovery.get("fingerprint", ""),
                         "successes": self.recovery.get("successes", 0),
                         "receipt_cause": self.recovery.get("receipt_cause", ""),
                         "receipt_digest": self.recovery.get("receipt_digest", ""),
                         "recovered_candidate_digest": self.recovery.get("recovered_candidate_digest", "")},
        }
        self._write_state()
        return self.last_cycle

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

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        while not stop():
            try:
                self.run_cycle()
            except Exception:  # noqa: BLE001 - o daemon nunca morre por um ciclo ruim
                logger.exception("ciclo do despachante falhou")
            self.run_digest_hook()
            self.run_sentinel_hook()
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

    recovery_value = args.recovery_tickets[0] if args.recovery_tickets else ""
    recovery = tuple(t.strip() for t in recovery_value.split(",") if t.strip())
    d = FleetDispatcher(DispatcherConfig(cycle_sec=args.cycle_sec, resume_only=args.resume_only and not recovery,
                                        recovery_tickets=recovery), dry_run=args.dry_run,
                        digest_hook=None if args.resume_only else maybe_send_due,
                        sentinel_hook=None if args.resume_only else maybe_run_sentinel,
                        busy_ids_loader=running_fleet_tickets, busy_hosts_loader=running_fleet_hosts)
    if args.loop and not args.once:
        d.run_forever()
        return 0
    print(json.dumps(d.run_cycle(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
