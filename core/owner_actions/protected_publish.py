r"""Ordered owner steps for publishing protected governance files by direct push (USR-313).

Incident (18/10/2026): a direct push of ``core/harness/*`` replaced the official gate by focused tests,
skipped PR CI and left ``main`` red. The steps built here make the full gate on the exact candidate commit
mandatory *before* the push and a ``main`` CI check *after* it.
"""

from __future__ import annotations

from typing import Sequence

from core.owner_actions.models import ActionPriority, ActionStep, OwnerActionDraft

GATE_COMMAND = r"python C:\dev\DarkFac\core\harness\runner.py --quick"
GATE_PASS_MARKER = "[HARNESS_PASS]"
FAST_GUARD_TESTS: tuple[str, ...] = (
    "tests/test_clock_bomb_guard.py",
    "tests/test_hub_coverage.py",
    "tests/test_safe_git_state.py",
    "tests/test_harness_contract.py",
)
CI_LIST_COMMAND = "gh run list --branch main --limit 3"
CI_WATCH_COMMAND = "gh run watch"


def build_protected_publish_steps(
    ticket_id: str, files: Sequence[str], *, candidate_worktree: str = r"C:\dev\DarkFac\.worktrees\<WORKTREE_DO_CANDIDATO>"
) -> list[ActionStep]:
    """Return the ordered steps: fast guards, full gate, direct push, main CI check (never skip the gate)."""
    shown = ", ".join(files) if files else "os arquivos protegidos"
    guards = " ".join(FAST_GUARD_TESTS)
    return [
        ActionStep(
            text=(
                f"Na worktree do commit candidato de {ticket_id} ({shown}), rode primeiro os guardas rapidos. "
                "Eles NAO substituem o portao completo."
            ),
            command=f"cd {candidate_worktree}\npython -m pytest {guards} -q",
        ),
        ActionStep(
            text=(
                "Rode o portao oficial COMPLETO no commit candidato exato (worker remoto ou local). "
                f"So continue se a saida contiver {GATE_PASS_MARKER}; qualquer outro resultado: NAO publique, "
                "avise o agente. Nunca troque o portao completo por testes focados."
            ),
            command=GATE_COMMAND,
        ),
        ActionStep(
            text=(
                f"Somente com {GATE_PASS_MARKER} no passo anterior, confirme que o HEAD e o commit candidato "
                "e publique por push direto na main."
            ),
            command="git rev-parse HEAD\ngit push origin HEAD:main",
        ),
        ActionStep(
            text=(
                "Depois do push confira o CI da main ate ficar verde. Se ficar vermelho, avise o agente: "
                "ele abre o hotfix (voce nao corrige)."
            ),
            command=f"{CI_LIST_COMMAND}\n{CI_WATCH_COMMAND}",
        ),
    ]


def build_protected_publish_draft(
    ticket_id: str, files: Sequence[str], *, created_by: str = "claude-code"
) -> OwnerActionDraft:
    """Build the OA draft for a protected-publication (direct push) task."""
    return OwnerActionDraft(
        title=f"Publicar arquivos protegidos de {ticket_id} por push direto (com portao completo)",
        priority=ActionPriority.HIGH,
        why=(
            "Arquivos protegidos so o owner publica, e o push direto na main pula o CI de PR. "
            "Por isso o portao oficial completo roda antes do push e o CI da main e conferido depois."
        ),
        blocks=[ticket_id],
        steps=build_protected_publish_steps(ticket_id, files),
        verify=f"{GATE_PASS_MARKER} no portao antes do push e ultimo run da main verde em {CI_LIST_COMMAND}.",
        created_by=created_by,
    )
