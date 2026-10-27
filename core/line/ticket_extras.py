"""RESEARCH, docs-sync and learning pack of a `run_ticket` run (USR-376).

Helpers without launcher globals, wired by `run_ticket.py`:
- `run_research`: policy + Knowledge Ledger of the MAIN checkout; without a deterministic web search in
  this process the stage records `research_skipped` (never invents sources).
- `docs_sync_feedback`: the manual-sync check over the ticket diff.
- `static_checks_feedback` (USR-398): complexity ratchet and `lint-imports` over the worktree, as CI runs them.
- `pre_review_feedback`: the combined output of both checks.
- `build_learning` / `publish_learning`: learning pack of the delivery, stored where the Hub and the
  daily digest look for it (`LearningPackStore`, `packs_index.json`). Failures are logged, never raised.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from core.line import ticket_engine, workspace
from core.research.policy import ResearchArtifact, run_research_stage

logger = logging.getLogger(__name__)

NO_WEB_REASON = "busca web indisponivel neste host"
LEARNING_FILE = "learning_pack.json"


class WebSearchUnavailable(RuntimeError):
    """No deterministic web search exists in this process."""


def unavailable_web_search(_query: str) -> list[Mapping[str, Any]]:
    raise WebSearchUnavailable(NO_WEB_REASON)


def research_ledger_root(main_root: Path) -> Path:
    return Path(main_root) / ".factory" / "research"


def run_research(
    ticket: Any, rws: workspace.RunWorkspace, main_root: Path, web_search: Any = None
) -> ResearchArtifact:
    """RESEARCH stage: artifact in the run workspace (and, when searched or reused, in the ledger)."""
    data = ticket.model_dump(mode="json") if hasattr(ticket, "model_dump") else dict(ticket)
    try:
        art = run_research_stage(data, research_ledger_root(main_root), web_search or unavailable_web_search)
    except WebSearchUnavailable:
        art = ResearchArtifact(kind="research_skipped", ticket_id=str(data["id"]), reason=NO_WEB_REASON)
    ticket_engine.write_research(rws, art)
    return art


def legacy_research(ticket_id: str) -> ResearchArtifact:
    """Artifact of an operator's resume of a worktree cut before USR-376 (no research was recorded)."""
    return ResearchArtifact(
        kind="research_skipped", ticket_id=ticket_id, reason="worktree anterior ao USR-376: sem pesquisa registrada"
    )


def docs_sync_feedback(repo: Path, base: str = "origin/main", head: str = "HEAD") -> Optional[str]:
    """None when the manual is in sync; otherwise the message for the development agent.

    Raises RuntimeError when git cannot answer (infra problem, not a violation).
    """
    from core.harness import docs_sync

    try:
        violations = docs_sync.check(Path(repo), base, head)
    except RuntimeError:
        raise
    except (OSError, TypeError, ValueError) as exc:  # unreadable git output is infra, not a violation
        raise RuntimeError(f"docs-sync sem leitura do git: {type(exc).__name__}: {exc}") from exc
    if not violations:
        return None
    lines = [f"- {v.covered_file} -> {v.page}" for v in violations]
    return (
        "docs-sync: arquivo coberto alterado sem alterar a pagina do manual. Atualize a pagina "
        "(docs/manual/) no mesmo diff, ou use o trailer 'Docs-Unchanged: <motivo>'.\n" + "\n".join(lines)
    )


# (rotulo, comando, arquivo que precisa existir no repo do ticket): projeto sem a ferramenta pula a checagem.
_STATIC_CHECKS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("complexity_ratchet", (sys.executable, "scripts/complexity_ratchet.py"), "scripts/complexity_ratchet.py"),
    ("lint-imports", (sys.executable, "-m", "importlinter.cli"), ".importlinter"),
)


def static_checks_feedback(repo: Path) -> Optional[str]:
    """None when the complexity ratchet and lint-imports pass; otherwise their combined output.

    Raises RuntimeError when a check cannot run (infra problem, not a violation).
    """
    failures: list[str] = []
    for label, cmd, required in _STATIC_CHECKS:
        if not (repo / required).is_file():
            continue
        try:
            done = subprocess.run(
                cmd, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"{label} nao executou: {type(exc).__name__}: {exc}") from exc
        if done.returncode != 0:
            output = (done.stdout + done.stderr).strip()[-3000:]
            failures.append(f"{label} reprovou (exit {done.returncode}):\n{output}")
    return "\n\n".join(failures) or None


def pre_review_feedback(repo: Path) -> Optional[str]:
    """Combined docs-sync, ratchet and lint-imports feedback for the agent; None when all pass."""
    parts = [f for f in (docs_sync_feedback(repo), static_checks_feedback(repo)) if f]
    return "\n\n".join(parts) or None


def build_learning(repo: Path, ticket_id: str, base: str, head: str = "HEAD") -> Any:
    """Learning pack of base..head, or None (logged) when it cannot be generated."""
    try:
        from core.learning_pack.delivery import build_for_delivery

        return build_for_delivery(repo, base, head, ticket_id)
    except Exception as exc:  # noqa: BLE001 - a pack never blocks a delivery
        logger.warning("learning pack de %s nao gerado: %s", ticket_id, exc)
        return None


def _session_pack(art: Any) -> Any:
    from core.learning_pack.models import (
        ConceptCategory,
        ExplanationTier,
        LearningConcept,
        SessionLearningPack,
    )

    paths = [f.path for f in art.files]
    tiers = ExplanationTier(
        pitch_30s=art.layman, staff_architect=art.dev,
        under_the_hood="\n".join(f"{f.path} (+{f.added}/-{f.removed})" for f in art.files) or "(sem arquivos)",
    )
    concept = LearningConcept(
        concept_id=f"{art.ticket}-delivery", name=f"Entrega {art.ticket}",
        category=ConceptCategory.ARCHITECTURE, mental_anchor="", tiers=tiers,
        code_anchor=paths[0] if paths else None,
    )
    return SessionLearningPack(
        pack_id=f"lp-{art.ticket.lower()}", session_id=art.ticket,
        timestamp=datetime.now(timezone.utc).isoformat(), title=f"Entrega {art.ticket}",
        executive_summary=art.layman, concepts=[concept], files_analyzed=paths,
        metrics={"symbols": len(art.symbols), "tests_changed": len(art.tests_changed),
                 "rejected_claims": len(art.rejected_claims)},
    )


def publish_learning(rws: workspace.RunWorkspace, art: Any, store_dir: Optional[Path] = None) -> bool:
    """Record the pack in the run workspace and in the store/index the Hub reads; False when it failed."""
    if art is None:
        return False
    try:
        from core.learning_pack.storage import LearningPackStore

        workspace.write_context(rws, LEARNING_FILE, art.model_dump_json(indent=2))
        LearningPackStore(store_dir).save_pack(_session_pack(art))
        return True
    except Exception as exc:  # noqa: BLE001 - the delivery is already merged
        logger.warning("learning pack de %s nao gravado: %s", getattr(art, "ticket", "?"), exc)
        return False
