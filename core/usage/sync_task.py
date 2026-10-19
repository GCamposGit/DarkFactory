"""Decisao pura de criar/manter/atualizar a tarefa agendada DarkFac-Usage-Cloud-Sync (USR-236).

Sem I/O e sem dependencia de Windows: o instalador em
``scripts/install_usage_sync_task.py`` coleta o estado e aplica a decisao.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath

TASK_NAME = "DarkFac-Usage-Cloud-Sync"
WORKTREE_MARKERS: tuple[str, ...] = (".worktrees", ".claude/worktrees", ".codex/worktrees")


class SyncTaskAction(str, Enum):
    CREATE = "create"
    KEEP = "keep"
    UPDATE = "update"
    REFUSE = "refuse"


@dataclass(frozen=True)
class SyncTaskState:
    """Estado observado da tarefa: script e diretorio de trabalho registrados."""

    script_path: str
    working_directory: str = ""


@dataclass(frozen=True)
class SyncTaskDecision:
    action: SyncTaskAction
    reason: str


def normalize_path(path: str) -> str:
    """Normaliza para comparacao: barras `/`, sem aspas nem barra final, minusculas."""
    cleaned = path.strip().strip('"').replace("\\", "/")
    while len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    return cleaned.lower()


def is_worktree_path(path: str) -> bool:
    """Indica se o caminho passa por uma worktree (`.worktrees`, `.claude|.codex/worktrees`)."""
    norm = "/" + normalize_path(path) + "/"
    return any(f"/{marker}/" in norm for marker in WORKTREE_MARKERS)


def expected_script_path(root: str) -> str:
    return str(PureWindowsPath(root) / "scripts" / "sync_usage_to_cloud.py")


def decide_sync_task(
    current: SyncTaskState | None, desired_root: str, execution_root: str
) -> SyncTaskDecision:
    """Decide a acao sobre a tarefa; recusa quando a raiz de execucao ou a desejada e worktree."""
    if is_worktree_path(execution_root):
        return SyncTaskDecision(
            SyncTaskAction.REFUSE,
            f"a raiz de execucao '{execution_root}' e uma worktree; rode a partir do checkout principal",
        )
    if is_worktree_path(desired_root):
        return SyncTaskDecision(
            SyncTaskAction.REFUSE, f"a raiz desejada '{desired_root}' e uma worktree"
        )
    if current is None:
        return SyncTaskDecision(SyncTaskAction.CREATE, "tarefa ausente")

    if is_worktree_path(current.script_path) or is_worktree_path(current.working_directory):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"tarefa aponta para worktree ('{current.script_path}')",
        )
    wanted = expected_script_path(desired_root)
    if normalize_path(current.script_path) != normalize_path(wanted):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"caminho atual '{current.script_path}' difere do esperado '{wanted}'",
        )
    if current.working_directory and normalize_path(current.working_directory) != normalize_path(
        desired_root
    ):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"diretorio de trabalho '{current.working_directory}' difere de '{desired_root}'",
        )
    return SyncTaskDecision(SyncTaskAction.KEEP, "tarefa ja aponta para o checkout principal")
