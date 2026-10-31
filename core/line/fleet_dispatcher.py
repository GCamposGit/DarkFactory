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

logger = logging.getLogger("darkfac.line.fleet_dispatcher")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = PROJECT_ROOT / ".factory" / "runs" / "fleet_dispatcher_state.json"
LOG_DIR = PROJECT_ROOT / ".factory" / "runs" / "fleet_dispatcher_logs"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

EXCLUDED_TAGS = frozenset({"congelado-v0", "not-authorized-for-execution", "planning-only", "waiting_human"})
MIN_NOTEBOOK_WORKERS = 2
INFRA_EXIT_CODES = frozenset({75})
LOCK_MARKERS = ("[TRAVA DE LINHA]",)
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
    hosts: list[HostSpec] = field(default_factory=default_hosts)
    cycle_sec: float = 30.0
    stable_probes: int = 3
    requeue_cooldown_sec: float = 600.0
    lock_requeue_cooldown_sec: float = 30.0  # host ocupado/trava: reenfileira quase de imediato
    deploy_window_sec: float = 180.0
    state_path: Path = STATE_PATH


@dataclass
class InFlight:
    ticket_id: str
    host_id: str
    handle: Any  # tem .poll() -> int | None
    log_path: Optional[Path] = None
    started_at: float = 0.0
    operational_checkout: Optional[Path] = None


@dataclass(frozen=True)
class HostProbe:
    free: bool
    reason: str = ""


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
                return HostProbe(False, "line_lock_ativo")
            workers = gate_ram.measure_workers().workers
        except Exception as exc:  # noqa: BLE001
            return HostProbe(False, f"probe_local_falhou:{exc}")
        return HostProbe(workers >= MIN_NOTEBOOK_WORKERS, f"workers={workers}")
    from core.harness import remote_dispatch as rd

    for _ in range(max(1, stable_probes)):
        health = rd.probe_health(host.worker_url)
        if health is None:
            return HostProbe(False, "health_indisponivel")
        if health.get("busy"):
            return HostProbe(False, "worker_ocupado")
    return HostProbe(True, "saudavel")


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


def default_launch(ticket_id: str, host: HostSpec, demand: Mapping[str, Any]) -> InFlight:
    env = dict(os.environ)
    # The launcher runs from a temporary operational checkout, but its mutable
    # control store is shared with the dispatcher and the other workers. Resolve
    # the parent's effective root before changing cwd; this also makes a relative
    # DARKFAC_STATE_ROOT override stable in the child process.
    from core.paths import state_root

    env["DARKFAC_STATE_ROOT"] = str(state_root().resolve())
    env["DARKFAC_HOST_ID"] = host.id
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


def _tail(path: Optional[Path], n: int = 6000) -> str:
    try:
        if path and path.exists():
            return path.read_bytes()[-n:].decode("utf-8", "replace")
    except OSError:
        pass
    return ""


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
        self.in_flight: dict[str, InFlight] = {}  # trava por host: no maximo um ticket por host
        self.cooldown: dict[str, float] = {}
        self.lock_busy: dict[str, str] = {}  # host -> motivo (trava de linha recusada), vai a idle_reasons
        self.last_merge_at: Optional[float] = None
        self.pending_deploy = False
        self.deploys = 0
        self.last_cycle: dict[str, Any] = {}
        self.failures: list[dict[str, Any]] = []  # ultimas falhas: ticket, host, exit_code, motivo, classe

    def _record_failure(self, job: InFlight, rc: int, kind: str, tail: str) -> None:
        reason = failure_reason(rc, tail)
        self.failures = (self.failures + [{
            "ts": datetime.now(timezone.utc).isoformat(), "ticket": job.ticket_id, "host": job.host_id,
            "exit_code": rc, "kind": kind, "reason": reason,
            "log": str(job.log_path) if job.log_path else None,
        }])[-FAILURE_HISTORY_LIMIT:]
        logger.error("%s falhou em %s: exit_code=%s kind=%s motivo=%s", job.ticket_id, job.host_id, rc, kind,
                     " | ".join(reason.splitlines()))

    def _reap(self) -> None:
        for host_id, job in list(self.in_flight.items()):
            rc = job.handle.poll()
            if rc is None:
                continue
            del self.in_flight[host_id]
            _remove_operational_checkout(job.operational_checkout)
            tail = _tail(job.log_path) if rc != 0 else ""
            if rc == 0:
                self.last_merge_at = self.clock()
                self.pending_deploy = True
            elif is_lock_refusal(tail):
                self._record_failure(job, rc, "lock", tail)
                self.cooldown[job.ticket_id] = self.clock() + self.config.lock_requeue_cooldown_sec
                self.lock_busy[host_id] = f"host_ocupado_trava:{job.ticket_id}"
                logger.warning("trava de linha em %s; %s reenfileirado", host_id, job.ticket_id)
            elif is_infra_failure(rc, tail):
                self._record_failure(job, rc, "infra", tail)
                self.cooldown[job.ticket_id] = self.clock() + self.config.requeue_cooldown_sec
                logger.warning("infra em %s (%s); %s reenfileirado", host_id, rc, job.ticket_id)
            else:
                self._record_failure(job, rc, "failure", tail)
                self.cooldown[job.ticket_id] = self.clock() + self.config.requeue_cooldown_sec * 3

    def _maybe_deploy(self) -> bool:
        """Um deploy por lote: so apos a janela de silencio desde o ultimo merge."""
        if not self.pending_deploy or self.last_merge_at is None:
            return False
        if self.clock() - self.last_merge_at < self.config.deploy_window_sec:
            return False
        self.pending_deploy = False
        if not self.dry_run:
            self.deploy()
        self.deploys += 1
        return True

    def run_cycle(self) -> dict[str, Any]:
        self._reap()
        now = self.clock()
        self.cooldown = {t: u for t, u in self.cooldown.items() if u > now}
        try:
            demands = self.demands_loader()
        except Exception as exc:  # noqa: BLE001
            demands = []
            load_error = str(exc)
        else:
            load_error = ""
        busy = {j.ticket_id for j in self.in_flight.values()} | set(self.busy_ids_loader())
        queue, skipped = select_tickets(demands, busy_ids=busy, cooldown_ids=set(self.cooldown))
        dispatched: list[dict[str, str]] = []
        idle: dict[str, str] = {}
        lock_busy, self.lock_busy = self.lock_busy, {}
        orphan_hosts = self.busy_hosts_loader()
        for host in sorted(self.config.hosts, key=lambda h: h.priority):
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
            tid = queue.pop(0)
            if self.dry_run:
                dispatched.append({"ticket": tid, "host": host.id, "dry_run": "1"})
                continue
            try:
                if self.launch is None:
                    demand = next(row for row in demands if str(row.get("id", "")) == tid)
                    job = default_launch(tid, host, demand)
                else:
                    job = self.launch(tid, host)
                self.in_flight[host.id] = job
                dispatched.append({"ticket": tid, "host": host.id})
            except Exception as exc:  # noqa: BLE001 - falha de infra: ticket volta a fila
                from core.line.agent_cli import redact_secrets

                safe_error = " | ".join(redact_secrets(str(exc)).splitlines())[-800:]
                idle[host.id] = f"lancamento_falhou:{safe_error}"
                self._record_failure(InFlight(tid, host.id, None), 1, "launch", str(exc))
                self.cooldown[tid] = now + self.config.requeue_cooldown_sec
        idle.update(lock_busy)  # trava de linha recusada no ciclo anterior tem precedencia
        deployed = self._maybe_deploy()
        self.last_cycle = {
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
        }
        self._write_state()
        return self.last_cycle

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
    ap.add_argument("--cycle-sec", type=float, default=30.0)
    args = ap.parse_args(argv)
    log_dir = Path(os.environ.get("DARKFAC_FLEET_LOG_DIR", str(LOG_DIR)))
    configure_logging(log_dir)
    from core.line.sentinel import maybe_run_sentinel
    from core.notifications.daily_digest import maybe_send_due

    d = FleetDispatcher(DispatcherConfig(cycle_sec=args.cycle_sec), dry_run=args.dry_run, digest_hook=maybe_send_due,
                        sentinel_hook=maybe_run_sentinel,
                        busy_ids_loader=running_fleet_tickets, busy_hosts_loader=running_fleet_hosts)
    if args.loop and not args.once:
        d.run_forever()
        return 0
    print(json.dumps(d.run_cycle(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
