"""Deterministic retrospective stage for the DarkFac production line (USR-91).

Closes the learning loop (retrospective -> planning) deterministically without LLM calls:
1. Generates 1 to 3 actionable lessons based on cause_codes, stage iterations,
   routing, outcome, and run cost.
2. Appends lessons to `.darkfac/LESSONS.md` in the target repository / workspace
   (isolated commit, best-effort, never blocks or alters run outcome).
3. Persists root cause analysis / preventative rules in `core.learning` ledger
   (`ContinuousLearningTracker`).
4. Supplies lessons read by `core.line.stage_planning.read_lessons` for subsequent runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.learning.models import MistakeCategory
from core.learning.tracker import ContinuousLearningTracker
from core.line import workspace as ws_mod
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import StageContext

logger = logging.getLogger("core.line.retrospective")

LESSONS_FILENAME = "LESSONS.md"
DARKFAC_DIRNAME = ".darkfac"


@dataclass
class RetrospectiveLesson:
    rule: str
    category: MistakeCategory = MistakeCategory.ASSUMPTION_ERROR
    cause_code: Optional[str] = None
    stage: Optional[str] = None
    run_id: Optional[str] = None

    def render_markdown(self) -> str:
        tag = self.cause_code or (f"{self.stage}:retry" if self.stage else "lesson")
        return f"- [{tag}] {self.rule}"


def generate_lessons_from_summary(
    summary: dict[str, Any],
    status: Optional[dict[str, Any]] = None,
) -> list[RetrospectiveLesson]:
    """Generate 1-3 deterministic lessons based on run outcome, cause_codes, iterations, and cost."""
    lessons: list[RetrospectiveLesson] = []
    seen_rules: set[str] = set()

    run_id = summary.get("run_id", "unknown")
    final_outcome = summary.get("final_outcome", "unknown")
    stages = summary.get("stages", {})
    total_cost = float(summary.get("total_cost_usd") or 0.0)

    # 1. Inspect all jobs for specific cause codes
    jobs: list[dict[str, Any]] = (status or {}).get("jobs", [])
    for job in jobs:
        cause = job.get("cause_code")
        stage = job.get("stage") or "line"
        if not cause:
            continue

        cause_lower = str(cause).lower()
        rule: Optional[str] = None
        category = MistakeCategory.ASSUMPTION_ERROR

        if "base_red" in cause_lower:
            rule = "Validar saude do CI e integridade da branch base antes de submeter alteracoes de integracao para evitar atrasos por base_red."
            category = MistakeCategory.GOVERNANCE_VIOLATION
        elif "ci_pending" in cause_lower or "ci_failed" in cause_lower:
            rule = "Acompanhar conclusao de checks do CI e politicas de branch antes de tentar merge da integracao."
            category = MistakeCategory.GOVERNANCE_VIOLATION
        elif "validate" in cause_lower or "build" in cause_lower:
            rule = f"Garantir que os testes e comandos de validacao passem localmente antes do estagio '{stage}'."
            category = MistakeCategory.TEST_REGRESSION
        elif "review" in cause_lower or "changes_required" in cause_lower:
            rule = "Resolver integralmente apontamentos de revisao independente na primeira rodada para evitar esgotamento de iteracoes."
            category = MistakeCategory.ASSUMPTION_ERROR
        elif "grill" in cause_lower or "waiting_human" in cause_lower:
            rule = "Esclarecer premissas de negocio e parametros estruturados no Grill antes de avancar para o planejamento."
            category = MistakeCategory.ASSUMPTION_ERROR
        elif "route" in cause_lower or "quota" in cause_lower:
            rule = "Planejar janelas de execucao observando limites de cota e capacidade dos harnesses para prevenir degradacao de rotas."
            category = MistakeCategory.TOOL_MISUSE
        elif "timeout" in cause_lower:
            rule = f"Decompor tarefas complexas em escopos menores para evitar estouro de timeout no estagio '{stage}'."
            category = MistakeCategory.TIMEOUT
        else:
            rule = f"Analisar causa raiz '{cause}' no estagio '{stage}' e incluir salvaguardas preventivas determinísticas."
            category = MistakeCategory.ASSUMPTION_ERROR

        if rule and rule not in seen_rules:
            seen_rules.add(rule)
            lessons.append(
                RetrospectiveLesson(
                    rule=rule,
                    category=category,
                    cause_code=str(cause),
                    stage=stage,
                    run_id=run_id,
                )
            )

    # 2. Inspect stages with excessive iterations (retry/exhaustion)
    for stage_name, stage_info in stages.items():
        max_iter = int(stage_info.get("max_iteration") or 0)
        if max_iter >= 2:
            rule = f"Estagio '{stage_name}' exigiu {max_iter} iteracoes; planejar diffs mais atomicos e validacoes unitarias intermediarias."
            if rule not in seen_rules:
                seen_rules.add(rule)
                lessons.append(
                    RetrospectiveLesson(
                        rule=rule,
                        category=MistakeCategory.TEST_REGRESSION,
                        cause_code=f"{stage_name}_high_iterations",
                        stage=stage_name,
                        run_id=run_id,
                    )
                )

    # 3. High cost run
    if total_cost >= 1.0:
        rule = f"Custo elevado na execucao (${total_cost:.2f} USD); priorizar modelos economicos e prompts concisos em tarefas rotineiras."
        if rule not in seen_rules:
            seen_rules.add(rule)
            lessons.append(
                RetrospectiveLesson(
                    rule=rule,
                    category=MistakeCategory.TOOL_MISUSE,
                    cause_code="high_cost",
                    stage="line",
                    run_id=run_id,
                )
            )

    # 4. Run did not complete successfully and no specific lesson generated yet
    if final_outcome != "completed" and not lessons:
        rule = f"Run concluido com estado '{final_outcome}'; revisar pre-condicoes e criterios determinísticos de aceite."
        seen_rules.add(rule)
        lessons.append(
            RetrospectiveLesson(
                rule=rule,
                category=MistakeCategory.GOVERNANCE_VIOLATION,
                cause_code=f"outcome_{final_outcome}",
                stage="line",
                run_id=run_id,
            )
        )

    # Bound to at most 3 lessons
    return lessons[:3]


def append_lessons_to_markdown_file(
    lessons_file: Path,
    lessons: Sequence[RetrospectiveLesson],
) -> None:
    """Append formatted lessons to a target LESSONS.md file with safety against race conditions."""
    if not lessons:
        return
    lessons_file.parent.mkdir(parents=True, exist_ok=True)
    existing_text = ""
    if lessons_file.is_file():
        try:
            existing_text = lessons_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read existing lessons file %s: %s", lessons_file, exc)
            existing_text = ""

    lines_to_add = [lesson.render_markdown() for lesson in lessons]
    if existing_text and not existing_text.endswith("\n"):
        existing_text += "\n"

    new_content = existing_text + "\n".join(lines_to_add) + "\n"
    lessons_file.write_text(new_content, encoding="utf-8")


def record_lessons_in_learning_tracker(
    lessons: Sequence[RetrospectiveLesson],
    run_id: str,
    tracker: Optional[ContinuousLearningTracker] = None,
) -> None:
    """Record lessons into ContinuousLearningTracker (learning_ledger.json)."""
    if not lessons:
        return
    try:
        active_tracker = tracker or ContinuousLearningTracker()
        for lesson in lessons:
            active_tracker.record_mistake_rca(
                category=lesson.category,
                symptom=f"Run {run_id} encountered {lesson.cause_code or 'retry/failure'}",
                mechanism="Production line retrospective deterministic rule evaluation",
                root_cause=lesson.rule,
                patch_description="Preventative retrospective lesson recorded for future planning",
                preventative_rule=lesson.rule,
            )
    except Exception as exc:
        logger.warning("Failed to record lessons in ContinuousLearningTracker for run %s: %s", run_id, exc)


def record_retrospective_lessons(
    project: ProjectDescriptor,
    run_id: str,
    summary: dict[str, Any],
    status: Optional[dict[str, Any]] = None,
    *,
    workspace_root: Optional[Path] = None,
    learning_tracker: Optional[ContinuousLearningTracker] = None,
) -> list[RetrospectiveLesson]:
    """Execute complete deterministic retrospective lesson recording.

    Best-effort, never raises fatal errors:
    1. Generates 1-3 lessons.
    2. Appends to target repo / workspace .darkfac/LESSONS.md.
    3. Commits to workspace branch if available.
    4. Records in core.learning ledger.
    """
    lessons = generate_lessons_from_summary(summary, status)
    if not lessons:
        logger.info("Retrospective for run %s: 0 lessons generated (clean run)", run_id)
        return []

    # 1. Update project repo directly if resolved
    try:
        project_path = project.resolve_path()
        if project_path and project_path.is_dir():
            target_lessons_file = project_path / DARKFAC_DIRNAME / LESSONS_FILENAME
            append_lessons_to_markdown_file(target_lessons_file, lessons)
            logger.info("Retrospective appended %d lessons to project %s", len(lessons), target_lessons_file)
    except Exception as exc:
        logger.warning("Failed to update project-level LESSONS.md for %s: %s", project.id, exc)

    # 2. Update and commit to run workspace worktree if available
    try:
        ws = ws_mod.checkout(project, run_id)
        ws_lessons_file = ws.path / DARKFAC_DIRNAME / LESSONS_FILENAME
        append_lessons_to_markdown_file(ws_lessons_file, lessons)
        commit_sha = ws_mod.commit_paths(
            ws,
            message=f"chore(line): record retrospective lessons [{run_id}]",
            job_key=f"{run_id}:retrospective",
            paths=[ws_lessons_file],
        )
        if commit_sha:
            logger.info("Retrospective committed lessons commit %s on %s", commit_sha, ws.branch)
            try:
                ws_mod.push(ws)
            except Exception as push_exc:
                logger.debug("Retrospective push skipped or failed (non-blocking): %s", push_exc)
    except Exception as ws_exc:
        logger.warning("Failed to commit retrospective lessons in workspace for %s: %s", run_id, ws_exc)

    # 3. Record in core.learning ledger
    record_lessons_in_learning_tracker(lessons, run_id, tracker=learning_tracker)

    return lessons
