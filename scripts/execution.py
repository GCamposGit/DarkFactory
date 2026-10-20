#!/usr/bin/env python3
"""CLI of the unified execution registry (USR-208). Interactive sessions and subagents call it (USR-209).

    python C:\\dev\\DarkFac\\scripts\\execution.py start --source claude_session --harness claude --stage development --ticket USR-XX
    python C:\\dev\\DarkFac\\scripts\\execution.py heartbeat EXECUTION_ID
    python C:\\dev\\DarkFac\\scripts\\execution.py finish EXECUTION_ID --status succeeded --evidence "PR #123"

``start`` prints the new execution id (or JSON with ``--json``). The command NEVER fails the caller because of
the registry: an unreachable store only queues the event for a later resend; exit code 0 except for a malformed
command line (2).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.workflow import execution_registry  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="execution.py", description="Registro unico de execucoes (Hub Live).")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="registra uma execucao e imprime o execution_id")
    start.add_argument("--source", required=True, help="origem (claude_session, subagent, codex, grok, ...)")
    start.add_argument("--harness", default=None)
    start.add_argument("--stage", required=True)
    start.add_argument("--ticket", default=None)
    start.add_argument("--parent", default=None, help="execution_id da execucao pai")
    start.add_argument("--model", default=None)
    start.add_argument("--title", default="")
    start.add_argument("--json", action="store_true")

    beat = sub.add_parser("heartbeat", help="prova de vida (a cada ~60 s; sem ela por 3 min a execucao fica 'perdida')")
    beat.add_argument("execution_id")

    end = sub.add_parser("finish", help="encerra a execucao")
    end.add_argument("execution_id")
    end.add_argument("--status", default="succeeded", help="succeeded | failed | cancelled")
    end.add_argument("--evidence", default="")
    return parser


def main(argv: Sequence[str] | None = None, *, registry: execution_registry.ExecutionRegistry | None = None) -> int:
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    reg = registry or execution_registry.get_registry()
    try:
        if args.command == "start":
            eid = reg.start(
                args.source, args.harness, args.stage, args.ticket, args.parent, model=args.model, title=args.title
            )
            print(json.dumps({"execution_id": eid}) if args.json else eid)
        elif args.command == "heartbeat":
            reg.heartbeat(args.execution_id)
        else:
            reg.finish(args.execution_id, args.status, args.evidence)
    except Exception:  # noqa: BLE001 - the session is never failed by the board
        pass
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(main())
