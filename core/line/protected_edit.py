"""Protected-path edits that a headless agent is not allowed to make (USR-205).

``core/orchestrator/guard.py`` lists the paths only the owner may commit
(``PROTECTED_PATTERNS``, including ``core/harness/*``). A headless Grok run
that calls an editor on one of those paths is cancelled (``stopReason``
``cancelled``, exit 0). That is not a transient crash: repeating the same
prompt, or handing the same scope to another harness, cancels again.

This module is the one place that:

* reads the cancelled tool call (Grok session ``chat_history.jsonl``) and
  compares its path with ``PROTECTED_PATTERNS``;
* tells the development prompt to deliver a proposal under ``docs/proposals``
  instead of editing;
* registers the owner action the launcher shows in the DarkHub.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from core.orchestrator.guard import audit_paths

logger = logging.getLogger(__name__)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,128}$")
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
_CANCEL_PHRASES = (
    "user cancelled the execution",
    "tool execution cancelled",
    "execution cancelled",
    "tool call cancelled",
    "tool call was cancelled",
)
_MAX_HISTORY_BYTES = 8 * 1024 * 1024
_MAX_PATH_CHARS = 1024


class CancelledEditDiagnosis:
    """How a Grok ``stopReason=cancelled`` run should be reported."""

    __slots__ = ("error_kind", "text", "protected_path")

    def __init__(self, error_kind: str, text: str, protected_path: str | None) -> None:
        self.error_kind = error_kind
        self.text = text
        self.protected_path = protected_path


def protected_paths_notice() -> str:
    """Tell a development agent which paths it must not edit.

    The list is ``PROTECTED_PATTERNS`` itself, so the prompt cannot drift from
    the guard. The delivery shape is the one already used for harness changes
    (``docs/proposals/USR-141-147-harness-resilience.md`` plus the patch).
    """

    from core.orchestrator.guard import PROTECTED_PATTERNS

    listing = "\n".join(f"- `{pattern}`" for pattern in PROTECTED_PATTERNS)
    return (
        "CAMINHOS PROTEGIDOS: so o owner pode alterar os padroes abaixo "
        "(core/orchestrator/guard.py). Nao use search_replace, write nem outro editor neles.\n"
        f"{listing}\n"
        "Se o ticket precisar mudar um desses caminhos, NAO edite o arquivo. "
        "Entregue a proposta e o patch em docs/proposals/ "
        "(modelo: docs/proposals/USR-141-147-harness-resilience.md e o .patch ao lado) "
        "e implemente normalmente apenas o que estiver fora desses caminhos.\n\n"
    )


def grok_sessions_root() -> Path:
    """Directory that holds one folder per Grok cwd.

    ``DARKFAC_GROK_SESSIONS_DIR`` overrides the location (tests). Otherwise the
    CLI store is ``$GROK_HOME/sessions`` or ``~/.grok/sessions``.
    """

    override = os.environ.get("DARKFAC_GROK_SESSIONS_DIR", "").strip()
    if override:
        return Path(override)
    home = os.environ.get("GROK_HOME", "").strip()
    base = Path(home) if home else Path.home() / ".grok"
    return base / "sessions"


def grok_session_dirname(cwd: Path | str) -> str:
    """Encode ``cwd`` the way Grok names the session directory on this host."""

    return quote(str(Path(cwd)), safe="")


def load_grok_chat_rows(cwd: Path | str, session_id: str | None) -> list[dict[str, Any]]:
    """Rows of ``chat_history.jsonl`` for this run, or ``[]`` when unknown.

    A missing or unreadable session is not an error: the caller keeps the
    ordinary permission-cancel classification.
    """

    if not session_id or not _SESSION_ID.match(session_id):
        return []
    path = _chat_history_path(Path(cwd), session_id)
    if path is None:
        return []
    try:
        if path.stat().st_size > _MAX_HISTORY_BYTES:
            logger.warning("Grok chat history %s is larger than %s bytes; ignoring it", path, _MAX_HISTORY_BYTES)
            return []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read Grok chat history %s: %s", path, exc)
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def cancelled_protected_edit_paths(
    rows: Sequence[Mapping[str, Any]],
    cwd: Path | str | None,
) -> list[str]:
    """Repo-relative protected paths whose edit call was cancelled.

    The match uses ``audit_paths`` (the guard's own ``fnmatch`` rules). Absolute
    paths are reduced to a repo-relative suffix before the comparison. A read
    of a protected file does not count, and a later "cancelled because of an
    earlier cancellation" of an ordinary file does not hide the protected edit.
    """

    calls, order = _index_tool_calls(rows)
    results = _index_tool_results(rows)
    root = Path(cwd) if cwd is not None else None
    found: list[str] = []
    seen: set[str] = set()

    def consider(call: Mapping[str, Any]) -> None:
        name = str(call.get("name") or "").strip().lower()
        if name not in _EDIT_TOOLS:
            return
        for raw in _paths_from_arguments(call.get("arguments")):
            display = protected_display_path(raw, root)
            if display and display not in seen:
                seen.add(display)
                found.append(display)

    for call_id in order:
        content = results.get(call_id)
        if content is not None and _is_cancel_result(content):
            consider(calls[call_id])
    if found:
        return found

    # The process stopped before the tool result was written. The last edit
    # that never received a result is the cancelled call.
    for call_id in reversed(order):
        if call_id in results:
            continue
        consider(calls[call_id])
        if found:
            return found
    return found


def protected_display_path(path: str, cwd: Path | str | None) -> str | None:
    """Shortest form of ``path`` that ``PROTECTED_PATTERNS`` matches, or None."""

    root = Path(cwd) if cwd is not None else None
    candidates = _path_candidates(path, root)
    hits = audit_paths(candidates)
    if not hits:
        return None
    return min(hits, key=len)


def diagnose_grok_cancellation(
    *,
    cwd: Path | str,
    session_id: str | None,
    permission_mode: str,
    agent_text: str,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> CancelledEditDiagnosis:
    """Classify ``stopReason=cancelled`` as ``protected_path`` or a plain ``crash``.

    ``rows``, when given, are used instead of reading the session (tests and a
    payload that already carries the transcript). Any failure while reading the
    session stays a ``crash``: a broken log must not change the retry policy.
    """

    protected: list[str] = []
    try:
        transcript = list(rows) if rows is not None else load_grok_chat_rows(cwd, session_id)
        protected = cancelled_protected_edit_paths(transcript, cwd)
    except Exception as exc:  # a diagnosis must not take down the runner
        logger.warning("Could not classify a cancelled Grok edit: %s", exc)
        protected = []
    if not protected:
        return CancelledEditDiagnosis("crash", _crash_text(permission_mode, agent_text), None)
    listed = ", ".join(protected)
    detail = (
        f"Grok stopped with stopReason=cancelled (permission mode '{permission_mode}'): "
        f"edit of protected path {listed} was cancelled (error_kind=protected_path). "
        "This is a human block: do not retry and do not edit the file. "
        "Deliver the proposal and patch under docs/proposals."
    )
    text = f"{detail} {agent_text}".strip()
    return CancelledEditDiagnosis("protected_path", text, protected[0])


def register_protected_path_owner_action(
    *,
    ticket_id: str,
    protected_path: str,
    workspace_path: Path | str | None = None,
    checkout_root: Path | str | None = None,
    store: Any = None,
    notifier: Any = None,
) -> Any:
    """Append ``OA-NNN`` for this block, or return the open item that already covers it.

    The owner applies a patch from ``docs/proposals`` on the shared checkout.
    The protected file is not edited here. Notification of a new high-priority
    item is best-effort and never raises.
    """

    from core.owner_actions.models import ActionPriority, OwnerActionDraft
    from core.owner_actions.notify import notify_owner_action, should_notify
    from core.owner_actions.store import OwnerActionStore

    active = store if store is not None else OwnerActionStore()
    path_label = protected_path.strip() or "caminho protegido"
    for existing in active.list_actions(include_done=False):
        blob = f"{existing.title}\n{existing.why}"
        if ticket_id in existing.blocks and path_label in blob:
            return existing

    workspace = Path(workspace_path) if workspace_path is not None else None
    checkout = Path(checkout_root) if checkout_root is not None else None
    draft = OwnerActionDraft(
        title=f"Commit humano em {path_label} ({ticket_id})",
        priority=ActionPriority.HIGH,
        why=(
            f"O agente foi cancelado ao editar {path_label}, caminho protegido por "
            "core/orchestrator/guard.py (so o owner commita). Repetir o mesmo escopo, "
            "no mesmo harness ou em outro, cancela de novo e nao produz o patch. "
            f"A entrega e uma proposta em docs/proposals, no formato de "
            "docs/proposals/USR-141-147-harness-resilience.md."
        ),
        blocks=[ticket_id],
        steps=_owner_steps(ticket_id, path_label, workspace, checkout),
        verify=(
            f"Depois do push, `python { _checkout(checkout, workspace) / 'core' / 'orchestrator' / 'guard.py' } "
            f"origin/main` imprime [GUARD PASS], o arquivo {path_label} esta no main com a mudanca, "
            f"e o item OA deste ticket esta marcado como feito."
        ),
        created_by="run_ticket",
    )
    action = active.add(draft)
    if should_notify(action):
        send = notifier if notifier is not None else notify_owner_action
        try:
            send(action)
        except Exception as exc:  # notification must not hide the registered item
            logger.warning("Owner action %s registered but notification failed: %s", action.id, exc)
    return action


def _owner_steps(
    ticket_id: str,
    protected_path: str,
    workspace: Path | None,
    checkout: Path | None,
) -> list[Any]:
    from core.owner_actions.models import ActionStep

    root = _checkout(checkout, workspace)
    proposal = f"docs/proposals/{ticket_id}-protected-path"
    worktree = str(workspace) if workspace is not None else "(worktree nao informada)"
    apply_check = f"git -C {root} apply --check {root / proposal}.patch"
    apply_commit = "\n".join([
        f"git -C {root} apply {root / proposal}.patch",
        f"git -C {root} add -- {protected_path} {proposal}.md {proposal}.patch",
        f'git -C {root} commit -m "fix: aplica patch em caminho protegido [{ticket_id}]"',
        f"git -C {root} push origin main",
    ])
    return [
        ActionStep(
            text=(
                f"Nao peca a outro harness para editar {protected_path}. O guard cancela essa edicao "
                f"e o lancador nao repete. A worktree preservada do agente e: {worktree}. "
                "Abra essa pasta e confira se ja existe uma proposta em docs/proposals."
            ),
        ),
        ActionStep(
            text=(
                f"Se a proposta ainda nao existir, peca um agente para escrever SOMENTE "
                f"{worktree}\\{proposal}.md e {worktree}\\{proposal}.patch, sem editar {protected_path}. "
                "Use como modelo docs/proposals/USR-141-147-harness-resilience.md e o .patch ao lado. "
                "O restante do ticket, fora dos caminhos protegidos, pode ir no commit normal da fabrica."
            ),
        ),
        ActionStep(
            text=(
                "No checkout compartilhado (onde o main vive; nao na worktree do ticket), "
                "atualize o main. Se o patch ainda so existe na worktree, copie os dois arquivos "
                f"de docs/proposals para {root}\\docs\\proposals antes deste comando."
            ),
            command=f"git -C {root} pull --ff-only origin main",
        ),
        ActionStep(
            text=(
                f"Confira que o patch aplica limpo. O arquivo esperado e {root}\\{proposal}.patch. "
                "Se o nome for outro, troque o caminho no comando pelo arquivo real."
            ),
            command=apply_check,
        ),
        ActionStep(
            text=(
                "Aplique o patch e faca o commit direto no main, sem abrir PR: o CI reprova "
                f"qualquer PR que altere {protected_path}. Inclua no commit o caminho protegido, "
                "a proposta e o patch. Nao use git add -A."
            ),
            command=apply_commit,
        ),
        ActionStep(
            text=(
                "No DarkHub, menu Acoes do Owner, marque este item como feito. "
                "No PowerShell, o mesmo efeito (troque OA-NNN pelo id mostrado no passo anterior da fila):"
            ),
            command=f"python {root / 'scripts' / 'owner_action.py'} list",
        ),
    ]


def _checkout(checkout: Path | None, workspace: Path | None) -> Path:
    return checkout or workspace or Path(".")


def _chat_history_path(cwd: Path, session_id: str) -> Path | None:
    root = grok_sessions_root()
    if not root.is_dir():
        return None
    names = [grok_session_dirname(cwd)]
    try:
        resolved = grok_session_dirname(cwd.resolve())
    except OSError:
        resolved = names[0]
    if resolved not in names:
        names.append(resolved)
    for name in names:
        candidate = root / name / session_id / "chat_history.jsonl"
        if candidate.is_file():
            return candidate
    matches = [
        path
        for path in root.glob(f"*/{session_id}/chat_history.jsonl")
        if path.is_file() and _SESSION_ID.match(path.parent.name)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "Several Grok sessions match %s; not guessing which cancelled call to read", session_id
        )
    return None


def _index_tool_calls(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    calls: dict[str, Mapping[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if row.get("type") != "assistant":
            continue
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
    return calls, order


def _index_tool_results(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    results: dict[str, str] = {}
    for row in rows:
        if row.get("type") != "tool_result":
            continue
        call_id = str(row.get("tool_call_id") or "")
        if call_id:
            results[call_id] = str(row.get("content") or "")
    return results


def _paths_from_arguments(arguments: Any) -> list[str]:
    payload: Any = arguments
    if isinstance(arguments, str):
        stripped = arguments.strip()
        if not stripped:
            return []
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, Mapping):
        return []
    found: list[str] = []
    for key in _PATH_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned and cleaned not in found:
                found.append(cleaned)
    return found


def _path_candidates(path: str, cwd: Path | None) -> list[str]:
    raw = path.strip().strip("'\"")
    if not raw or "\n" in raw or "\r" in raw or len(raw) > _MAX_PATH_CHARS:
        return []
    norm = raw.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    out: list[str] = []

    def add(value: str) -> None:
        cleaned = value.strip().strip("/")
        if cleaned and cleaned not in out:
            out.append(cleaned)

    add(norm)
    if cwd is not None:
        cwd_norm = str(cwd).replace("\\", "/").rstrip("/")
        if norm.lower().startswith(cwd_norm.lower() + "/"):
            add(norm[len(cwd_norm) + 1 :])
    parts = [part for part in norm.split("/") if part]
    for index in range(len(parts)):
        add("/".join(parts[index:]))
    return out


def _is_cancel_result(content: str) -> bool:
    collapsed = " ".join(content.lower().split())
    if not collapsed or len(collapsed) > 500:
        return False
    return any(phrase in collapsed for phrase in _CANCEL_PHRASES)


def _crash_text(permission_mode: str, agent_text: str) -> str:
    detail = (
        f"Grok stopped with stopReason=cancelled (permission mode '{permission_mode}'): "
        "a tool call that needs approval is cancelled in headless runs."
    )
    return f"{detail} {agent_text}".strip()
