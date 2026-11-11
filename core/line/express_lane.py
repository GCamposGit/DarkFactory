"""Via expressa quando o chat nega a edicao do motor (USR-494).

Self-Modification, ou permission denied num caminho do motor, grava a causa
``self_modification`` e a tag ``line-block`` no control store. A mesma gravacao
nao duplica tag nem causa. ``protected_path`` e permission denied de banco ou
ACL ficam de fora. O run que recebeu a negacao nao relanca essa edicao.

A causa abre uma acao do owner com um unico comando (T3). O comando lista
todas as falhas do host e, com o host valido, executa o ticket uma vez.

A segunda ocorrencia da mesma causa alerta o owner uma vez (T4), no canal
ALL, com a causa e esse comando. A primeira so registra o episodio. Entrega
concluida, ou outra causa classificada, encerra o episodio. O alerta nao liga
a recuperacao global.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.demands.models import TAG_LINE_BLOCK, UserTicket

logger = logging.getLogger(__name__)

CAUSE_SELF_MODIFICATION = "self_modification"

_ENGINE_DIRS = ("core/line", "core/harness")
_ENGINE_FILES = ("run_ticket.py", "scripts/line_validate.py")
_EDIT_TOOLS = frozenset({
    "search_replace",
    "write",
    "edit",
    "str_replace",
    "create_file",
    "apply_patch",
    "multiedit",
})
_PATH_KEYS = ("file_path", "target_file", "path", "filepath", "file")
_SELF_MOD = re.compile(r"self-modification", re.IGNORECASE)
_PERMISSION_DENIED = re.compile(r"permission denied", re.IGNORECASE)
_DB_OR_ACL = re.compile(
    r"permission denied for (?:schema|table|relation|sequence|database|column)|"
    r"\binsufficient privilege\b|\bacl\b|must be owner",
    re.IGNORECASE,
)
_PATH_TOKEN = re.compile(r"[A-Za-z0-9_.:\\/-]+")
_MAX_PATH_CHARS = 1024


@dataclass(frozen=True)
class ExpressMark:
    """Resultado de uma gravacao da via expressa."""

    ticket_id: str
    cause: str
    tagged: bool
    written: bool
    causes: tuple[str, ...] = ()


class TicketPort(Protocol):
    """Leitura e gravacao de um ticket no control store."""

    def get(self, ticket_id: str) -> UserTicket | None: ...

    def save(self, ticket: UserTicket) -> None: ...


def official_cause_line(ticket_id: str, detail: str = "negacao ao alterar o motor") -> str:
    """Linha oficial de entrega. O classificador nao usa a cauda do log."""

    safe = " ".join(str(detail).split()) or "negacao ao alterar o motor"
    return f"[ERRO NA ENTREGA] causa: {CAUSE_SELF_MODIFICATION}; erro: {safe} ticket {ticket_id}"


def is_engine_path(path: str) -> bool:
    """True quando ``path`` e o motor: core/line, core/harness, run_ticket.py ou line_validate."""

    for candidate in _path_suffixes(path):
        folded = candidate.casefold()
        if folded in _ENGINE_FILES or folded in _ENGINE_DIRS:
            return True
        if any(folded.startswith(directory + "/") for directory in _ENGINE_DIRS):
            return True
    return False


def classify_engine_denial(
    text: str,
    *,
    path: str | None = None,
    error_kind: str | None = None,
) -> str | None:
    """Devolve ``self_modification`` ou None.

    Banco, ACL e o diagnostico ``protected_path`` nao entram nessa causa.
    """

    blob = text or ""
    if _is_protected_diagnosis(blob, error_kind) or _DB_OR_ACL.search(blob):
        return None
    if _SELF_MOD.search(blob):
        return CAUSE_SELF_MODIFICATION
    if _PERMISSION_DENIED.search(blob) is None:
        return None
    if path and is_engine_path(path):
        return CAUSE_SELF_MODIFICATION
    if _text_has_engine_path(blob):
        return CAUSE_SELF_MODIFICATION
    return None


def classify_result(result: Any) -> str | None:
    """Causa da via expressa num resultado de agente, ou None."""

    if getattr(result, "ok", False):
        return None
    kind = getattr(result, "error_kind", None)
    if kind == "protected_path":
        return None
    if kind == CAUSE_SELF_MODIFICATION:
        return CAUSE_SELF_MODIFICATION
    text = f"{getattr(result, 'text', '') or ''}\n{getattr(result, 'stderr_tail', '') or ''}"
    return classify_engine_denial(text, path=getattr(result, "protected_path", None), error_kind=kind)


def transcript_denial(rows: Sequence[Mapping[str, Any]] | None) -> str | None:
    """Causa quando um editor do motor foi negado no transcript do chat."""

    if not rows:
        return None
    calls, order, results = _index_edits(rows)
    for call_id in order:
        content = results.get(call_id)
        if content is None:
            continue
        call = calls[call_id]
        name = str(call.get("name") or "").strip().lower()
        if name and name not in _EDIT_TOOLS:
            continue
        path = _first_argument_path(call.get("arguments"))
        if classify_engine_denial(content, path=path) == CAUSE_SELF_MODIFICATION:
            return CAUSE_SELF_MODIFICATION
    return None


def cancelled_edit_kind(
    *,
    diagnosis_kind: str,
    diagnosis_text: str,
    stderr: str = "",
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Mantem ``protected_path``. Uma negacao do motor vira ``self_modification``."""

    if diagnosis_kind == "protected_path":
        return diagnosis_kind
    if (
        classify_engine_denial(diagnosis_text) == CAUSE_SELF_MODIFICATION
        or classify_engine_denial(stderr) == CAUSE_SELF_MODIFICATION
        or transcript_denial(rows) == CAUSE_SELF_MODIFICATION
    ):
        return CAUSE_SELF_MODIFICATION
    return diagnosis_kind


def apply_mark(ticket: UserTicket) -> tuple[UserTicket, bool]:
    """Acrescenta a tag e a causa se ainda nao estiverem no ticket."""

    tags = list(ticket.tags)
    causes = list(ticket.express_causes)
    changed = False
    if TAG_LINE_BLOCK not in tags:
        tags.append(TAG_LINE_BLOCK)
        changed = True
    if CAUSE_SELF_MODIFICATION not in causes:
        causes.append(CAUSE_SELF_MODIFICATION)
        changed = True
    if not changed:
        return ticket, False
    updated = ticket.model_copy(update={
        "tags": tags,
        "express_causes": causes,
        "updated_at": datetime.now(timezone.utc),
    })
    return updated, True


def record_self_modification(
    ticket_id: str,
    *,
    project_id: str = "darkfac",
    port: TicketPort | None = None,
) -> ExpressMark:
    """Grava a causa e a tag ``line-block`` no control store, sem duplicar.

    ``project_id`` fica no ticket ja carregado. Ticket ausente nao e inventado.
    """

    store = port or default_ticket_port()
    current = store.get(ticket_id)
    if current is None:
        logger.warning(
            "via expressa: ticket %s projeto %s ausente no control store", ticket_id, project_id,
        )
        return ExpressMark(ticket_id=ticket_id, cause=CAUSE_SELF_MODIFICATION, tagged=False, written=False)
    updated, changed = apply_mark(current)
    if changed:
        store.save(updated)
    return ExpressMark(
        ticket_id=ticket_id,
        cause=CAUSE_SELF_MODIFICATION,
        tagged=TAG_LINE_BLOCK in updated.tags,
        written=changed,
        causes=tuple(updated.express_causes),
    )


class QueueTicketPort:
    """Control store da fila. O ledger versionado so entra na leitura."""

    def get(self, ticket_id: str) -> UserTicket | None:
        from core.demands.queue_store import get_ticket

        found = get_ticket(ticket_id)
        if found is not None:
            return found
        return _ledger_ticket(ticket_id)

    def save(self, ticket: UserTicket) -> None:
        from core.demands.queue_store import register_ticket

        register_ticket(ticket)


def default_ticket_port() -> TicketPort:
    return QueueTicketPort()


def _ledger_ticket(ticket_id: str) -> UserTicket | None:
    from core.demands.store import DemandsStore
    from core.paths import project_root

    path = project_root() / ".factory" / "demands" / "demands.json"
    if not path.is_file():
        return None
    try:
        return DemandsStore(path).get_ticket(ticket_id)
    except Exception as exc:  # noqa: BLE001 - a fila segue; a gravacao nao inventa ticket
        logger.warning("via expressa: ledger ilegivel para %s (%s)", ticket_id, type(exc).__name__)
        return None


def _is_protected_diagnosis(text: str, error_kind: str | None) -> bool:
    if error_kind == "protected_path":
        return True
    folded = text.casefold()
    return "error_kind=protected_path" in folded or "error_kind: protected_path" in folded


def _text_has_engine_path(text: str) -> bool:
    return any(is_engine_path(token) for token in _PATH_TOKEN.findall(text or ""))


def _path_suffixes(path: str) -> list[str]:
    raw = str(path or "").strip().strip("'\"")
    if not raw or "\n" in raw or "\r" in raw or len(raw) > _MAX_PATH_CHARS:
        return []
    norm = raw.replace("\\", "/")
    norm = re.sub(r":\d+(?::\d+)?$", "", norm)
    norm = norm.strip("()[]<>'\"")
    norm = norm.rstrip(".,;")
    while norm.startswith("./"):
        norm = norm[2:]
    parts = [part for part in norm.split("/") if part not in {"", "."}]
    if parts and len(parts[0]) == 2 and parts[0][1] == ":":
        parts = parts[1:]
    return ["/".join(parts[index:]) for index in range(len(parts))]


def _index_edits(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], list[str], dict[str, str]]:
    calls: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("type") == "assistant":
            tool_calls = row.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                if not call_id:
                    continue
                calls[call_id] = call
                order.append(call_id)
        elif row.get("type") == "tool_result":
            call_id = str(row.get("tool_call_id") or "")
            if call_id:
                results[call_id] = str(row.get("content") or "")
    return calls, order, results


# --- comando unico do owner (USR-494 T3) -----------------------------------------------------------

_IN_FLIGHT_STATUSES = frozenset({"implementing", "validating"})
_TERMINAL_STATUSES = frozenset({"completed", "cancelled", "canceled"})
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass(frozen=True)
class HostGap:
    """Um requisito do host que impede o lancamento."""

    code: str
    detail: str


def express_run_command(ticket_id: str) -> str:
    """O unico comando da acao do owner para este ticket."""

    return f"python -m core.line.express_lane run --ticket {ticket_id.strip()}"


def quota_floor_gap() -> HostGap | None:
    """Piso de cota: algum harness de escrita precisa estar acima de 15%."""

    try:
        from core.line.feeder_cycle import default_quota_gate

        ok, detail = default_quota_gate()
    except Exception as exc:  # a sonda entra na lista; nao esconde as outras falhas
        return HostGap("quota_floor", f"sonda falhou ({type(exc).__name__})")
    if ok:
        return None
    text = str(detail or "").strip()
    if text.lower().startswith("quota_floor:"):
        text = text.split(":", 1)[1].strip()
    return HostGap("quota_floor", text or "nenhum harness de escrita acima do piso de 15%")


def default_host_checks() -> list[Callable[[], HostGap | None]]:
    """Requisitos avaliados por completo. A primeira falha nao interrompe as outras."""

    return [_python_gap, _git_gap, _run_ticket_gap, quota_floor_gap]


def collect_host_gaps(
    checks: Sequence[Callable[[], HostGap | None]] | None = None,
) -> list[HostGap]:
    """Todas as falhas do host, na ordem das sondas. Uma sonda quebrada vira falha e segue."""

    gaps: list[HostGap] = []
    for check in default_host_checks() if checks is None else checks:
        try:
            gap = check()
        except Exception as exc:  # noqa: BLE001 - a lista completa vale mais do que a primeira excecao
            gaps.append(HostGap("host_check", f"sonda falhou ({type(exc).__name__})"))
            continue
        if gap is not None:
            gaps.append(gap)
    return gaps


def register_express_owner_action(
    ticket_id: str,
    *,
    store: Any = None,
    notifier: Any = None,
) -> Any:
    """Abre a acao com um unico comando, ou devolve a acao aberta que ja cobre o ticket."""

    from core.owner_actions.models import ActionPriority, ActionStep, OwnerActionDraft
    from core.owner_actions.notify import notify_owner_action, should_notify
    from core.owner_actions.store import OwnerActionStore

    active = store if store is not None else OwnerActionStore()
    command = express_run_command(ticket_id)
    for existing in active.list_actions(include_done=False):
        if _is_express_action(existing, ticket_id, command):
            return existing
    draft = OwnerActionDraft(
        title=f"Via expressa: executar {ticket_id} neste host",
        priority=ActionPriority.HIGH,
        why=(
            f"O chat negou a edicao do motor do ticket {ticket_id} (causa self_modification). "
            "Um unico comando lista todos os requisitos ausentes do host e, com o host valido, "
            "executa o ticket uma vez."
        ),
        blocks=[ticket_id],
        steps=[
            ActionStep(
                text=(
                    "No PowerShell, no checkout da fabrica, rode o comando abaixo. "
                    "Ele lista todas as falhas do host. Com o host valido, executa o ticket uma vez. "
                    "Se o ticket ja estiver em voo ou terminal, termina sem criar outro processo."
                ),
                command=command,
            ),
        ],
        verify=(
            f"A saida lista os requisitos ausentes, ou mostra run_ticket.py {ticket_id} "
            "--fleet --host-id <host>, ou informa que o ticket ja estava em voo ou terminal."
        ),
        created_by="express_lane",
    )
    action = active.add(draft)
    if should_notify(action):
        send = notifier if notifier is not None else notify_owner_action
        try:
            send(action)
        except Exception as exc:  # o item ja esta no backlog; o aviso nao pode esconde-lo
            logger.warning("acao %s registrada; aviso falhou (%s)", action.id, type(exc).__name__)
    return action


def launch_argv(ticket_id: str, host_id: str, *, run_ticket: Path | None = None) -> list[str]:
    """Argv exato do lancamento: ``run_ticket.py <id> --fleet --host-id <host>``."""

    from core.paths import project_root

    script = run_ticket if run_ticket is not None else project_root() / "run_ticket.py"
    return [sys.executable, str(script), ticket_id, "--fleet", "--host-id", host_id]


def run_express(
    ticket_id: str,
    *,
    port: TicketPort | None = None,
    checks: Sequence[Callable[[], HostGap | None]] | None = None,
    host_id: str | None = None,
    running: set[str] | None = None,
    spawn: Callable[[list[str]], Any] | None = None,
    run_ticket: Path | None = None,
) -> int:
    """Valida o host inteiro e executa o ticket uma vez.

    Requisitos ausentes sao impressos todos. Nenhum deles, inclusive o piso de
    cota, deixa o ticket ser lancado. Ticket em voo ou terminal termina sem
    criar outro processo.
    """

    ticket_id = ticket_id.strip()
    current = (port or default_ticket_port()).get(ticket_id)
    live, scan_failed = _running_ids(running)
    stop = _stop_message(current, ticket_id, live)
    if stop is not None:
        print(stop)
        return 0
    gaps = collect_host_gaps(checks)
    if scan_failed:
        gaps = [HostGap("processos", "nao foi possivel listar os processos da frota"), *gaps]
    if current is None:
        gaps = [*gaps, HostGap("ticket", f"ticket {ticket_id} ausente no control store")]
    if gaps:
        for gap in gaps:
            print(f"requisito ausente: {gap.code}: {gap.detail}")
        logger.info("via expressa: %s nao lancado; %s requisitos ausentes", ticket_id, len(gaps))
        return 1
    host = _this_host(host_id)
    argv = launch_argv(ticket_id, host, run_ticket=run_ticket)
    launcher = spawn if spawn is not None else _spawn_ticket
    try:
        launcher(argv)
    except Exception as exc:  # o owner ve a falha; nao ha segundo processo
        print(f"requisito ausente: lancamento: {type(exc).__name__}")
        logger.warning("via expressa: lancamento de %s falhou (%s)", ticket_id, type(exc).__name__)
        return 1
    print(f"lancado: run_ticket.py {ticket_id} --fleet --host-id {host}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``python -m core.line.express_lane run --ticket <ID>``."""

    parser = argparse.ArgumentParser(prog="python -m core.line.express_lane")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Valida o host e executa o ticket uma vez")
    run_parser.add_argument("--ticket", required=True)
    args = parser.parse_args(None if argv is None else list(argv))
    if args.command == "run":
        return run_express(str(args.ticket))
    return 2


def _python_gap() -> HostGap | None:
    if sys.executable:
        return None
    return HostGap("python", "interpretador python ausente")


def _git_gap() -> HostGap | None:
    if shutil.which("git"):
        return None
    return HostGap("git", "git ausente no PATH")


def _run_ticket_gap() -> HostGap | None:
    from core.paths import project_root

    if (project_root() / "run_ticket.py").is_file():
        return None
    return HostGap("run_ticket", "run_ticket.py ausente no checkout")


def _this_host(host_id: str | None) -> str:
    if host_id and host_id.strip():
        return host_id.strip()
    from core.line.line_lock import resolve_host_id

    return resolve_host_id()


def _running_ids(running: set[str] | None) -> tuple[set[str], bool]:
    """``(ids, scan_failed)``. Uma lista injetada nao consulta o sistema."""

    if running is not None:
        return set(running), False
    try:
        from core.line.fleet_dispatcher import running_fleet_tickets

        return set(running_fleet_tickets()), False
    except Exception as exc:  # sem a lista nao da para garantir que nao ha outro processo
        logger.warning("via expressa: lista de processos indisponivel (%s)", type(exc).__name__)
        return set(), True


def _stop_message(ticket: UserTicket | None, ticket_id: str, live: set[str]) -> str | None:
    if ticket is not None:
        status = str(getattr(ticket.status, "value", ticket.status)).strip().lower()
        if status in _TERMINAL_STATUSES:
            return f"ticket {ticket_id} ja terminal ({status}); nenhum processo novo"
        if status in _IN_FLIGHT_STATUSES or ticket_id in live:
            return f"ticket {ticket_id} ja em voo; nenhum processo novo"
    elif ticket_id in live:
        return f"ticket {ticket_id} ja em voo; nenhum processo novo"
    return None


def _is_express_action(action: Any, ticket_id: str, command: str) -> bool:
    if ticket_id not in list(getattr(action, "blocks", []) or []):
        return False
    commands = [step.command for step in action.steps if getattr(step, "command", None)]
    return commands == [command]


def _spawn_ticket(argv: list[str]) -> None:
    from core.paths import project_root

    subprocess.Popen(
        argv,
        cwd=str(project_root()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


# --- alerta unico da falha repetida (USR-494 T4) ---------------------------------------------------


@dataclass(frozen=True)
class ExpressEpisode:
    """Uma sequencia aberta de falhas da mesma causa classificada."""

    ticket_id: str
    cause: str
    occurrences: int
    alerted: bool


@dataclass(frozen=True)
class ExpressRepeatAlert:
    """Pedido de alerta da segunda ocorrencia. A cauda do log nao entra aqui."""

    ticket_id: str
    cause: str
    command: str


def _episode_key(ticket_id: str, cause: str) -> str:
    """Chave do episodio: ticket e causa classificada, sem texto de log."""

    return f"{ticket_id.strip()}|{cause.strip()}"


class ExpressEpisodeStore:
    """Episodios ao lado do estado do despachante. A chave nao inclui a cauda."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, ticket_id: str, cause: str) -> ExpressEpisode | None:
        row = self._load().get(_episode_key(ticket_id, cause))
        if not isinstance(row, dict):
            return None
        try:
            return ExpressEpisode(
                ticket_id=str(row.get("ticket_id") or ticket_id).strip(),
                cause=str(row.get("cause") or cause).strip(),
                occurrences=int(row.get("occurrences") or 0),
                alerted=bool(row.get("alerted")),
            )
        except (TypeError, ValueError):
            return None

    def bump(self, ticket_id: str, cause: str) -> ExpressEpisode:
        payload = self._load()
        current = self.get(ticket_id, cause)
        episode = ExpressEpisode(
            ticket_id=ticket_id,
            cause=cause,
            occurrences=1 if current is None else current.occurrences + 1,
            alerted=False if current is None else current.alerted,
        )
        payload[_episode_key(ticket_id, cause)] = {
            "ticket_id": episode.ticket_id,
            "cause": episode.cause,
            "occurrences": episode.occurrences,
            "alerted": episode.alerted,
        }
        self._write(payload)
        return episode

    def mark_alerted(self, ticket_id: str, cause: str) -> None:
        payload = self._load()
        key = _episode_key(ticket_id, cause)
        row = payload.get(key)
        if not isinstance(row, dict):
            return
        row["alerted"] = True
        payload[key] = row
        self._write(payload)

    def close(self, ticket_id: str, cause: str) -> bool:
        payload = self._load()
        key = _episode_key(ticket_id, cause)
        if key not in payload:
            return False
        del payload[key]
        self._write(payload)
        return True

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "episodio da via expressa ilegivel em %s (%s)", self.path.name, type(exc).__name__,
            )
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def observe_express_failure(
    ticket_id: str,
    cause: str,
    *,
    store: ExpressEpisodeStore,
    notify: Callable[[ExpressRepeatAlert], bool] | None = None,
) -> bool:
    """Registra a causa classificada e alerta so na segunda ``self_modification``.

    Causa vazia nao abre episodio. Outra causa classificada encerra o episodio
    deste ticket. A ocorrencia seguinte a alerta nao envia outro aviso.
    """

    ticket = str(ticket_id or "").strip()
    classified = str(cause or "").strip()
    if not ticket or not classified:
        return False
    if classified != CAUSE_SELF_MODIFICATION:
        store.close(ticket, CAUSE_SELF_MODIFICATION)
        return False
    episode = store.bump(ticket, classified)
    if episode.alerted or episode.occurrences != 2:
        return False
    alert = ExpressRepeatAlert(
        ticket_id=ticket,
        cause=classified,
        command=express_run_command(ticket),
    )
    sender = notify_repeated_self_modification if notify is None else notify
    try:
        sent = bool(sender(alert))
    except Exception as exc:  # a segunda ocorrencia ja foi a tentativa; nao repete o alerta
        logger.warning("via expressa: alerta de %s falhou (%s)", ticket, type(exc).__name__)
        return False
    if sent:
        store.mark_alerted(ticket, classified)
        logger.info("via expressa: alerta unico de %s causa %s", ticket, classified)
    return sent


def observe_express_success(ticket_id: str, *, store: ExpressEpisodeStore) -> bool:
    """Encerra o episodio quando a entrega da via conclui."""

    ticket = str(ticket_id or "").strip()
    if not ticket:
        return False
    return store.close(ticket, CAUSE_SELF_MODIFICATION)


def notify_repeated_self_modification(
    alert: ExpressRepeatAlert,
    *,
    service: Any = None,
) -> bool:
    """Um aviso no canal ALL com a causa e o comando unico. Nao liga recuperacao."""

    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    notifier = service if service is not None else NotificationService()
    event = notifier.notify(
        category=AlertCategory.SYSTEM_HEALTH,
        severity=AlertSeverity.CRITICAL,
        title="Falha repetida na via expressa",
        message=(
            f"O ticket {alert.ticket_id} repetiu a causa {alert.cause}. "
            f"Próximo passo: {alert.command}"
        ),
        channel=NotificationChannel.ALL,
        details={
            "ticket_id": alert.ticket_id,
            "cause": alert.cause,
            "command": alert.command,
            "finding": "express_repeat",
        },
        force=True,
    )
    return event is not None


def _first_argument_path(arguments: Any) -> str | None:
    payload: Any = arguments
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


if __name__ == "__main__":
    raise SystemExit(main())
