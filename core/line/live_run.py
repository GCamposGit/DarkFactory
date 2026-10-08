"""Live-board progress for interactive Claude Code sessions (USR-164).

``run_ticket.py`` publishes its phases to the live line board by itself. A ticket implemented *in the
chat* (subagents in worktrees, the 19-run-ticket skill driving git/gh by hand) never goes through that
launcher, so ``/live`` showed zero active runs while the work was happening. This module is the thin,
stable CLI such a session calls instead -- one local run per ticket, advanced by hand::

    python C:\\dev\\DarkFac\\scripts\\live_run.py open   --ticket USR-XX --title "Titulo" [--harness claude]
    python C:\\dev\\DarkFac\\scripts\\live_run.py phase  agent --ticket USR-XX --message "subagente sonnet"
    python C:\\dev\\DarkFac\\scripts\\live_run.py phase  gate  --ticket USR-XX --status failed --cause gate_red
    python C:\\dev\\DarkFac\\scripts\\live_run.py finish --ticket USR-XX [--failed --cause ci_red]
    python C:\\dev\\DarkFac\\scripts\\live_run.py cancel --ticket USR-XX

It reuses ``core.line.local_progress`` (same table, same sink resolution, same warning as the launcher),
so the Hub folds these runs exactly like launcher runs, with harness and machine on every phase.

Contract
--------
* One CLI process per call, so the run identity lives in ``<state root>/live_runs/<TICKET>.json``
  (run id, harness, machine, the phase in progress, whether the warning was already shown).
* Idempotent: ``open`` reuses a live run of the same ticket; repeating the same phase/status/message
  publishes nothing; ``finish``/``cancel`` without an open run are no-ops. A ``phase`` without ``open``
  opens the run implicitly.
* Starting a phase closes the previous one as ``succeeded``; ``finish`` closes the phase in progress
  (``succeeded``, or ``failed`` with ``--failed``); ``cancel`` records phase and run as ``cancelled``.
* It NEVER fails the session because of the board: an unreachable or unconfigured sink, an unwritable
  state directory or a corrupt state file only produce one ``[AVISO]`` on stderr per run. The exit code
  is 0 in all of those cases; only a malformed command line exits with 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from core.line.local_progress import (
    ABANDON_AFTER_SECONDS,
    ENV_RUN_ID,
    ENV_SWITCH,
    ENV_WARNED,
    PHASES,
    ProgressPublisher,
    ProgressSink,
    SqliteSink,
    open_progress,
)

logger = logging.getLogger("darkfac.live_run")

STATE_DIRNAME = "live_runs"
ENV_HARNESS = "DARKFAC_OPERATING_HARNESS"
DEFAULT_HARNESS = "claude"
PHASE_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "skipped")
_SAFE_TICKET = re.compile(r"[^A-Za-z0-9_.-]")


class LiveRunState(BaseModel):
    """What survives between two CLI calls of the same run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    ticket_id: str
    project_id: str = "darkfac"
    title: str = ""
    harness: str | None = None
    worker: str | None = None
    active_phase: str | None = None
    last_phase: str | None = None
    last_status: str | None = None
    last_message: str = ""
    warned: bool = False
    opened_at: str
    updated_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def state_directory(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    from core.paths import state_root

    return state_root() / STATE_DIRNAME


def _state_path(directory: Path, ticket_id: str) -> Path:
    return directory / f"{_SAFE_TICKET.sub('_', ticket_id.strip()) or '_'}.json"


def load_state(directory: Path, ticket_id: str) -> LiveRunState | None:
    """The live run of ``ticket_id`` or ``None`` (missing, corrupt, unreadable or abandoned)."""
    try:
        state = LiveRunState.model_validate_json(_state_path(directory, ticket_id).read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(state.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, ValidationError):
        return None
    if (_now() - updated).total_seconds() > ABANDON_AFTER_SECONDS:
        return None  # the Hub already shows it as abandoned: start a fresh run
    return state


def save_state(directory: Path, state: LiveRunState) -> bool:
    """Atomic write; returns False (never raises) when the directory is not writable."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = _state_path(directory, state.ticket_id)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".live_run_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(state.model_dump_json(indent=2))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return True
    except Exception as exc:  # noqa: BLE001 - the session must survive any state problem
        logger.warning("live_run: could not save state (%s)", type(exc).__name__)
        return False


def clear_state(directory: Path, ticket_id: str) -> None:
    try:
        _state_path(directory, ticket_id).unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("live_run: could not remove state (%s)", type(exc).__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="live_run.py",
        description="Publica o progresso de um ticket feito em sessao interativa na Esteira ao vivo (USR-164).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ticket", required=True, help="id do ticket (ex.: USR-164)")
        p.add_argument("--title", default="", help="titulo mostrado no quadro")
        p.add_argument("--project", default="darkfac", help="projeto (padrao: %(default)s)")
        p.add_argument("--harness", default=None, help=f"harness (padrao: ${ENV_HARNESS} ou {DEFAULT_HARNESS})")
        p.add_argument("--worker", default=None, help="maquina (padrao: nome do host)")
        p.add_argument("--json", action="store_true", help="saida JSON em vez de texto")

    opened = sub.add_parser("open", help="abre (ou reaproveita) o run do ticket")
    common(opened)
    opened.add_argument("--message", default="sessao interativa: cota, tamanho e roteamento")

    phase = sub.add_parser("phase", help="registra uma fase do ticket")
    phase.add_argument("phase", help="uma de: " + ", ".join(PHASES))
    common(phase)
    phase.add_argument("--status", default="running", choices=PHASE_STATUSES)
    phase.add_argument("--message", default="")
    phase.add_argument("--cause", default=None, help="codigo curto da causa (falhas)")

    finish = sub.add_parser("finish", help="encerra o run (sucesso, ou falha com --failed)")
    common(finish)
    finish.add_argument("--failed", action="store_true")
    finish.add_argument("--message", default="")
    finish.add_argument("--cause", default=None)

    cancel = sub.add_parser("cancel", help="registra o run como cancelado")
    common(cancel)
    cancel.add_argument("--message", default="")
    return parser


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname() or "unknown-host"
    except Exception:  # noqa: BLE001
        return "unknown-host"


def _publisher(
    state: LiveRunState,
    env: Mapping[str, str],
    sink: ProgressSink | None,
    warn: Callable[[str], None] | None | Literal["default"],
) -> ProgressPublisher:
    # One warning per run, not per CLI process: once shown, later calls stay quiet.
    channel: Callable[[str], None] | None | Literal["default"] = None if state.warned else warn
    return open_progress(
        state.ticket_id,
        state.project_id,
        state.title,
        sink=sink,
        environ=env,
        harness=state.harness,
        warn=channel,
        run_id=state.run_id,
        worker=state.worker,
        active_phase=state.active_phase,
    )


def _new_state(args: argparse.Namespace, env: Mapping[str, str], hostname: str) -> LiveRunState:
    moment = _now()
    run_id = f"local-{args.ticket}-{moment.strftime('%Y%m%dT%H%M%S')}"[:160]
    return LiveRunState(
        run_id=run_id,
        ticket_id=args.ticket,
        project_id=args.project or "darkfac",
        title=args.title or "",
        harness=args.harness or env.get(ENV_HARNESS, "").strip() or DEFAULT_HARNESS,
        worker=args.worker or hostname,
        opened_at=moment.isoformat(),
        updated_at=moment.isoformat(),
    )


def _emit(args: argparse.Namespace, payload: dict[str, Any], text: str) -> None:
    try:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(text)
    except Exception:  # noqa: BLE001 - a closed stdout must not matter
        pass


def _apply(
    args: argparse.Namespace,
    state: LiveRunState,
    pub: ProgressPublisher,
) -> str:
    """Publish the command's events through ``pub`` and update ``state``; returns a short description."""
    command = args.command
    if command == "open":
        pub.phase("preflight", "running", args.message)
        state.last_phase, state.last_status, state.last_message = "preflight", "running", args.message
        return "run aberto (preflight em andamento)"
    if command == "phase":
        name, status, message = args.phase, args.status, args.message
        if (state.last_phase, state.last_status, state.last_message) == (name, status, message):
            return f"fase {name} {status} ja registrada (nada a publicar)"
        previous = pub.active_phase
        if status == "running" and previous not in (None, name):
            pub.phase(previous, "succeeded", f"encerrada ao iniciar {name}")  # type: ignore[arg-type]
        pub.phase(name, status, message, cause=args.cause)  # type: ignore[arg-type]
        state.last_phase, state.last_status, state.last_message = name, status, message
        return f"fase {name} {status}"
    if command == "finish":
        active = pub.active_phase
        if args.failed:
            if active:
                pub.phase(active, "failed", args.message, cause=args.cause)
            pub.finish(False, args.message, cause=args.cause)
            return "run encerrado com falha"
        if active:
            pub.phase(active, "succeeded", "encerrada ao finalizar o run")
        pub.finish(True, args.message)
        return "run encerrado com sucesso"
    pub.cancel(args.message)
    return "run cancelado"


def run_command(
    args: argparse.Namespace,
    *,
    env: Mapping[str, str],
    sink: ProgressSink | None,
    directory: Path,
    hostname: str,
    warn: Callable[[str], None] | None | Literal["default"],
) -> dict[str, Any]:
    """Execute one parsed command; never raises. Returns the machine-readable outcome."""
    command = args.command
    state = load_state(directory, args.ticket)
    if state is None:
        if command in {"finish", "cancel"}:
            return {"ok": True, "action": command, "ticket_id": args.ticket, "run_id": None,
                    "published": False, "warning": None, "detail": "nenhum run aberto para este ticket (nada a fazer)"}
        state = _new_state(args, env, hostname)
    elif command == "open":
        # idempotent open: keep the run, refresh the descriptive fields the caller passed explicitly
        state.title = args.title or state.title
        state.harness = args.harness or state.harness
        state.worker = args.worker or state.worker
        state.updated_at = _now().isoformat()
        save_state(directory, state)
        return {"ok": True, "action": "open", "ticket_id": state.ticket_id, "run_id": state.run_id,
                "published": False, "warning": None, "detail": "run ja aberto: reaproveitado (nada a publicar)"}
    elif command == "phase" and (args.harness or args.worker or args.title):
        state.title = args.title or state.title
        state.harness = args.harness or state.harness
        state.worker = args.worker or state.worker

    detail = "falha inesperada ao publicar"
    pub: ProgressPublisher | None = None
    try:
        pub = _publisher(state, env, sink, warn)
        detail = _apply(args, state, pub)
    except Exception as exc:  # noqa: BLE001 - nothing here may fail the session
        logger.warning("live_run: %s failed (%s)", command, type(exc).__name__)
        detail = f"nao foi possivel publicar ({type(exc).__name__})"

    warning = pub.warning if pub is not None else None
    if warning:
        state.warned = True
    if command in {"finish", "cancel"}:
        clear_state(directory, state.ticket_id)
    else:
        state.active_phase = pub.active_phase if pub is not None else state.active_phase
        state.updated_at = _now().isoformat()
        save_state(directory, state)
    # "published" means visible to the cloud Hub: the machine's own control.db does not count.
    published = bool(
        pub is not None
        and pub.sink is not None
        and not isinstance(pub.sink, SqliteSink)
        and pub.failures == 0
    )
    return {"ok": True, "action": command, "ticket_id": state.ticket_id, "run_id": state.run_id,
            "harness": state.harness, "worker": state.worker, "published": published,
            "warning": warning, "detail": detail}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    sink: ProgressSink | None = None,
    state_dir: Path | None = None,
    hostname: str | None = None,
    warn: Callable[[str], None] | None | Literal["default"] = "default",
) -> int:
    """CLI entry point. Exit 0 whatever happens to the board; 2 only for a malformed command line."""
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.command == "phase" and args.phase not in PHASES:
        print(f"fase invalida '{args.phase}': use uma de {', '.join(PHASES)}", file=sys.stderr)
        return 2

    under_pytest_default = environ is None and bool(os.environ.get("PYTEST_CURRENT_TEST"))
    env = {k: v for k, v in (os.environ if environ is None else environ).items() if k not in (ENV_RUN_ID, ENV_WARNED)}
    if under_pytest_default and sink is None:
        env[ENV_SWITCH] = "off"  # tests never publish to a real database
    try:
        outcome = run_command(
            args,
            env=env,
            sink=sink,
            directory=state_directory(state_dir),
            hostname=hostname or _hostname(),
            warn=warn,
        )
    except Exception as exc:  # noqa: BLE001 - the last line of defence: the session is never failed
        logger.warning("live_run: unexpected failure (%s)", type(exc).__name__)
        outcome = {"ok": True, "action": args.command, "ticket_id": args.ticket, "run_id": None,
                   "published": False, "warning": None, "detail": f"falha inesperada ({type(exc).__name__})"}
    visibility = "publicado" if outcome["published"] else "NAO publicado"
    _emit(
        args,
        outcome,
        f"[live_run] {outcome['ticket_id']} {outcome['detail']} ({visibility})"
        + (f" run={outcome['run_id']}" if outcome.get("run_id") else ""),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
