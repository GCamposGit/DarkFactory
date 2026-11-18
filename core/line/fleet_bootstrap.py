"""Deterministic, harness-neutral entry point for the fleet dispatcher."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
import sys

from core.line.operating_harness import (
    declare_headless_process,
    interactive_identity_env_names,
    resolve_process_identity,
)

logger = logging.getLogger(__name__)

_INTERACTIVE_HARNESS_ENV = interactive_identity_env_names()

# USR-471 (T4): a `fleet_dispatcher_state.json` written more than this long ago is a dead cycle
# (the dispatcher crashed or was stopped), not an active run; the reinstaller must not block on it
# forever.
ACTIVE_RUN_STALENESS = timedelta(minutes=5)


def check_active_run(
    state_root: Path, *, now: datetime | None = None, staleness: timedelta = ACTIVE_RUN_STALENESS,
) -> tuple[bool, str]:
    """Whether the dispatcher's last written cycle (`fleet_dispatcher_state.json`) shows a ticket
    still in flight on some host, and a human-readable reason either way.

    Used by `scripts/install_fleet_dispatcher.ps1` to abort a reinstall instead of redefining the
    scheduled task under a run (AGENTS.md: never restart a host with active work). A missing,
    unreadable or empty `in_flight` map is "no active run"; a stale `ts` (older than `staleness`,
    the dispatcher cycles every 30s) is treated as a dead cycle rather than a live one, so a crashed
    dispatcher never blocks its own reinstall.
    """
    state_path = state_root / "runs" / "fleet_dispatcher_state.json"
    if not state_path.exists():
        return False, "sem estado anterior do despachante; nenhum run ativo"
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"estado do despachante ilegivel ({type(exc).__name__}); considerado sem run ativo"
    in_flight = data.get("in_flight")
    if not isinstance(in_flight, dict) or not in_flight:
        return False, "nenhum ticket em voo no ultimo ciclo do despachante"

    effective_now = now or datetime.now(timezone.utc)
    ts = _parse_iso(data.get("ts"))
    if ts is not None and (effective_now - ts) > staleness:
        return False, f"estado do despachante obsoleto (ultimo ciclo {ts.isoformat()}); considerado sem run ativo"

    tickets = ", ".join(f"{host}={ticket}" for host, ticket in sorted(in_flight.items()))
    return True, f"run ativo em voo ({tickets}); preserve a fila e o SHA, nao reinstale agora"


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    """Set stable roots and clear interactive identity before importing dispatcher code."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--check-active-run", action="store_true")
    args, dispatcher_args = parser.parse_known_args(argv)

    project_value = args.project_root.expanduser()
    state_value = args.state_root.expanduser()
    if not project_value.is_absolute() or not state_value.is_absolute():
        parser.error("--project-root e --state-root devem ser caminhos absolutos")
    try:
        project_root = project_value.resolve(strict=True)
        state_root = state_value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        parser.error(f"root inexistente ou invalido: {exc}")
    if not project_root.is_dir():
        parser.error(f"--project-root nao e um diretorio: {project_root}")
    if not (project_root / "core" / "line" / "fleet_bootstrap.py").is_file():
        parser.error(f"--project-root nao contem o bootstrap da frota: {project_root}")
    if not (project_root / "core" / "line" / "fleet_dispatcher.py").is_file():
        parser.error(f"--project-root nao contem o despachante da frota: {project_root}")
    if not state_root.is_dir():
        parser.error(f"--state-root nao e um diretorio existente: {state_root}")

    if args.check_active_run:
        active, reason = check_active_run(state_root)
        print(reason)
        return 1 if active else 0

    declare_headless_process(
        os.environ,
        project_root=project_root,
        state_root=state_root,
        fleet=True,
    )
    added_project_path = str(project_root) not in sys.path
    if added_project_path:
        sys.path.insert(0, str(project_root))

    # USR-471: the dispatcher's own identity, resolved the same way every headless executor does
    # (never a parent chat's preference, which `declare_headless_process` above already scrubbed).
    identity = resolve_process_identity(
        execution_context="headless", project_root=project_root, state_root=state_root,
    )
    identity_record = {
        "event": "dispatcher_process_identity",
        "harness": identity.harness,
        "origin": identity.origin,
        "process_harness": identity.process_harness,
        "process_harness_origin": identity.process_harness_origin,
        "mode": identity.mode,
        "write_capable": identity.write_capable,
        "execution_context": identity.execution_context,
        "project_root": str(identity.project_root),
        "state_root": str(identity.state_root),
        # The dispatcher itself only starts workers; each ticket stage selects a model later.
        "model": None,
    }
    # The dispatcher configures its file logger after bootstrap returns. Emit the
    # identity now so it remains observable in the service's stdout/task history too.
    print(json.dumps(identity_record, ensure_ascii=False, sort_keys=True))

    sys.argv = ["core.line.fleet_dispatcher", *dispatcher_args]
    try:
        runpy.run_module("core.line.fleet_dispatcher", run_name="__main__")
    finally:
        if added_project_path and sys.path and sys.path[0] == str(project_root):
            sys.path.pop(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
