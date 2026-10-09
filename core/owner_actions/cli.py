"""CLI for agents to keep the owner action backlog current (USR-190).

    python C:\\dev\\DarkFac\\scripts\\owner_action.py add --title "..." --priority high --why "..." ^
        --blocks USR-162 --step "Abra o PR" --cmd "gh pr view 230" --step "Faca o merge" --verify "PR MERGED"
    python C:\\dev\\DarkFac\\scripts\\owner_action.py list
    python C:\\dev\\DarkFac\\scripts\\owner_action.py done OA-001
    python C:\\dev\\DarkFac\\scripts\\owner_action.py answer OA-005 --option A --note "ok"

Validates the schema, assigns sequential ``OA-NNN`` ids and never prints anything credential-shaped
(inputs that look like tokens are rejected; output goes through ``redact``). Creating a critical or
high item notifies the owner on Telegram, best effort: a delivery failure never fails the command.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from core.owner_actions.models import (
    ActionKind,
    ActionPriority,
    ActionStatus,
    ActionStep,
    DecisionOption,
    OwnerAction,
    OwnerActionDraft,
    redact,
)
from core.owner_actions.notify import notify_owner_action, should_notify
from core.owner_actions.propagate import annotate_blocked_tickets
from core.owner_actions.store import (
    DEFAULT_OVERLAY_PATH,
    OwnerActionError,
    OwnerActionStore,
    blocked_by,
)

Notifier = Callable[[OwnerAction], dict[str, Any]]


class _OrderedStepAction(argparse.Action):
    """Collect ``--step`` / ``--cmd`` in command-line order so a command binds to its step."""

    def __call__(self, parser, namespace, values, option_string=None):  # type: ignore[no-untyped-def]
        ordered = list(getattr(namespace, "ordered_steps", None) or [])
        ordered.append((self.dest, values))
        namespace.ordered_steps = ordered


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="owner_action", description="Backlog de acoes humanas do owner (DarkHub).")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="Registra uma acao ou decisao para o owner")
    add.add_argument("--title", required=True)
    add.add_argument("--kind", choices=[k.value for k in ActionKind], default=ActionKind.ACTION.value)
    add.add_argument("--priority", choices=[p.value for p in ActionPriority], default=ActionPriority.MEDIUM.value)
    add.add_argument("--why", default="")
    add.add_argument("--blocks", nargs="*", default=[], metavar="TICKET")
    add.add_argument("--depends-on", dest="depends_on", nargs="*", default=[], metavar="OA-ID")
    add.add_argument("--step", action=_OrderedStepAction, dest="step", metavar="TEXTO", help="passo (repetivel)")
    add.add_argument("--cmd", action=_OrderedStepAction, dest="cmd", metavar="COMANDO", help="comando do passo anterior")
    add.add_argument(
        "--option", action="append", default=[], metavar="ID=ROTULO[|DETALHE]", help="opcao de decisao (repetivel)"
    )
    add.add_argument("--verify", default="")
    add.add_argument("--created-by", dest="created_by", default="claude-code")
    add.add_argument("--from-json", dest="from_json", metavar="ARQUIVO", help="rascunho completo em JSON (- = stdin)")
    add.add_argument("--no-notify", dest="no_notify", action="store_true", help="nao avisar no Telegram")
    add.add_argument("--hub-url", dest="hub_url", default=None)
    add.add_argument("--json", dest="as_json", action="store_true")

    ls = sub.add_parser("list", help="Lista o backlog")
    ls.add_argument("--status", choices=["open", "done", "all"], default="open")
    ls.add_argument("--priority", choices=[p.value for p in ActionPriority], default=None)
    ls.add_argument("--json", dest="as_json", action="store_true")

    done = sub.add_parser("done", help="Marca uma acao como feita")
    done.add_argument("id")
    done.add_argument("--note", default="")
    done.add_argument("--json", dest="as_json", action="store_true")

    answer = sub.add_parser("answer", help="Registra a resposta do owner a uma decisao")
    answer.add_argument("id")
    answer.add_argument("--option", required=True)
    answer.add_argument("--note", default="")
    answer.add_argument("--json", dest="as_json", action="store_true")
    return parser


def _parse_options(raw: Sequence[str]) -> list[DecisionOption]:
    options: list[DecisionOption] = []
    for item in raw:
        ident, sep, rest = item.partition("=")
        if not sep or not ident.strip() or not rest.strip():
            raise OwnerActionError(f"--option invalida {item!r}; use ID=ROTULO ou ID=ROTULO|DETALHE")
        label, _, detail = rest.partition("|")
        options.append(DecisionOption(id=ident.strip(), label=label.strip(), detail=detail.strip()))
    return options


def _draft_from_args(args: argparse.Namespace) -> OwnerActionDraft:
    if args.from_json:
        text = sys.stdin.read() if args.from_json == "-" else Path(args.from_json).read_text(encoding="utf-8-sig")
        try:
            return OwnerActionDraft.model_validate_json(text)
        except ValueError as exc:
            raise OwnerActionError(f"rascunho JSON invalido: {redact(str(exc))}") from exc
    steps: list[dict[str, Any]] = []
    for kind, value in getattr(args, "ordered_steps", None) or []:
        if kind == "step":
            steps.append({"text": value, "command": None})
        elif not steps:
            raise OwnerActionError("--cmd precisa vir depois de um --step")
        else:
            steps[-1]["command"] = value if not steps[-1]["command"] else f"{steps[-1]['command']}\n{value}"
    try:
        return OwnerActionDraft(
            kind=ActionKind(args.kind),
            title=args.title,
            priority=ActionPriority(args.priority),
            why=args.why,
            blocks=args.blocks,
            depends_on=args.depends_on,
            steps=[ActionStep(**s) for s in steps],
            options=_parse_options(args.option),
            verify=args.verify,
            created_by=args.created_by,
        )
    except ValueError as exc:
        raise OwnerActionError(redact(str(exc))) from exc


def _line(action: OwnerAction, pending: list[str]) -> str:
    flag = f" [bloqueada por {', '.join(pending)}]" if pending and action.status is not ActionStatus.DONE else ""
    blocks = f" bloqueia: {', '.join(action.blocks)}" if action.blocks else ""
    return f"{action.id}  {action.priority.value:<8} {action.status.value:<7} {action.kind.value:<8} {action.title}{blocks}{flag}"


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    store: Optional[OwnerActionStore] = None,
    notifier: Optional[Notifier] = None,
    demands_store: Any = None,
    out: Optional[TextIO] = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    stream = out or sys.stdout
    active = store or OwnerActionStore(overlay_path=DEFAULT_OVERLAY_PATH)

    def emit(text: str) -> None:
        stream.write(redact(text) + "\n")

    try:
        if args.command == "add":
            action = active.add(_draft_from_args(args))
            result: dict[str, Any] = {"ok": True, "id": action.id, "notified": False}
            if not args.no_notify and should_notify(action):
                try:
                    send = notifier or (lambda a: notify_owner_action(a, hub_base_url=args.hub_url))
                    result["notified"] = bool(send(action).get("sent", False))
                except Exception:  # noqa: BLE001 - notification must never fail the command
                    result["notified"] = False
            if args.as_json:
                emit(json.dumps({**result, "action": action.to_record()}, ensure_ascii=False))
            else:
                emit(f"[ok] {action.id} registrado ({action.priority.value}); aparece no DarkHub apos o deploy do merge.")
                if should_notify(action) and not args.no_notify:
                    emit("[ok] owner avisado no Telegram." if result["notified"] else "[aviso] Telegram nao enviou (item registrado mesmo assim).")
            return 0

        if args.command == "list":
            loaded = active.load()
            for warning in loaded.warnings:
                sys.stderr.write(redact(f"[AVISO] {warning}") + "\n")
            by_id = {a.id: a for a in loaded.actions}
            rows = [
                a for a in loaded.actions
                if (args.status == "all" or (args.status == "done") == (a.status is ActionStatus.DONE))
                and (args.priority is None or a.priority.value == args.priority)
            ]
            rows.sort(key=lambda a: (a.status is ActionStatus.DONE, list(ActionPriority).index(a.priority), a.id))
            if args.as_json:
                emit(json.dumps([a.to_record() for a in rows], ensure_ascii=False, indent=2))
            elif not rows:
                emit("Nenhum item.")
            else:
                for a in rows:
                    emit(_line(a, blocked_by(a, by_id)))
            return 0

        if args.command == "done":
            action = active.resolve(args.id, note=args.note)
            emit(json.dumps(action.to_record(), ensure_ascii=False) if args.as_json else f"[ok] {action.id} marcado como feito.")
            return 0

        if args.command == "answer":
            action = active.resolve(args.id, option_id=args.option, note=args.note)
            annotated = annotate_blocked_tickets(action, demands_store or _default_demands_store())
            if args.as_json:
                emit(json.dumps({"action": action.to_record(), "annotated_tickets": annotated}, ensure_ascii=False))
            else:
                emit(f"[ok] {action.id} respondida com a opcao {args.option}.")
                emit(f"[ok] decisao anexada a: {', '.join(annotated)}." if annotated else "[aviso] nenhum ticket aberto recebeu a decisao.")
            return 0
    except OwnerActionError as exc:
        sys.stderr.write(redact(f"[ERRO] {exc}") + "\n")
        return 1
    return 2  # pragma: no cover - argparse enforces a command


def _default_demands_store() -> Any:
    from core.demands.store import DemandsStore

    return DemandsStore(Path(__file__).resolve().parents[2] / ".factory" / "demands" / "demands.json")


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    return run(argv)


__all__ = ["main", "run"]
