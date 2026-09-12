"""Selective context policy and compact assembly for the orchestrator (DF-19).

Adopts Anthropic Managed Agents and OpenAI Harness principles:
- Emits a concise operational summary instead of raw execution transcripts.
- Injects ONLY pertinent facts and promoted, verified ACTIVE rules.
- Replaces massive full-file dumps with structured file references/locators.
- Tracks durable progress markers from recoverable checkpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.execution.agent_executor import TaskSpec
from core.learning.models import PolicyStatus
from core.learning.promotion import LearningPromotionEngine


class FileReferenceSpec(BaseModel):
    """Structured locator pointing to a file without polluting context with raw code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    purpose: str = Field(default="")

    @model_validator(mode="after")
    def validate_line_order(self) -> "FileReferenceSpec":
        if self.line_start is not None and self.line_end is not None and self.line_end < self.line_start:
            raise ValueError("line_end must be greater than or equal to line_start")
        return self


class TaskContext(BaseModel):
    """Distilled, high-signal execution context for a task attempt."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    project_id: str = Field(default="default")
    operational_summary: str = Field(default="")
    pertinent_facts: list[str] = Field(default_factory=list)
    active_rules: list[str] = Field(default_factory=list)
    file_references: list[FileReferenceSpec] = Field(default_factory=list)
    durable_progress: list[str] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContextSelector:
    """Selects and compiles pertinent facts and verified active rules for a TaskSpec."""

    def __init__(self, promotion_engine: LearningPromotionEngine | None = None) -> None:
        self.promotion_engine = promotion_engine

    @staticmethod
    def _bounded_text(value: str, max_tokens: int) -> str:
        """Bound prompt text with a deterministic character approximation."""
        max_chars = max_tokens * 4
        if len(value) <= max_chars:
            return value
        suffix = " [truncated]"
        return value[: max(0, max_chars - len(suffix))] + suffix

    @staticmethod
    def _is_scope_relevant(candidate_scope: str, allowed_paths: list[str], objective: str) -> bool:
        """Determine whether candidate rule scope pertains to the task paths or objective."""
        norm_scope = candidate_scope.strip().lower()
        if norm_scope in ("global", "general", "*"):
            return True

        # Check against allowed paths
        for path in allowed_paths:
            norm_path = path.strip().lower()
            if norm_scope in norm_path or norm_path.startswith(norm_scope):
                return True

        # Check against objective tokens
        tokens = [t.strip().lower() for t in objective.split()]
        if any(token in norm_scope or norm_scope in token for token in tokens if len(token) > 3):
            return True

        return False

    @staticmethod
    def _is_project_match(item_project_id: str | None, target_project_id: str | None) -> bool:
        """Check project isolation: items belonging to another project cannot be injected."""
        if not target_project_id or not item_project_id:
            return True
        norm_item = item_project_id.strip().lower()
        norm_target = target_project_id.strip().lower()
        if norm_item in ("global", "general", "*"):
            return True
        return norm_item == norm_target

    def assemble_context(
        self,
        task_spec: TaskSpec,
        *,
        project_id: str | None = None,
        tracker: Any | None = None,
        promotion_engine: LearningPromotionEngine | None = None,
        checkpoint: dict[str, Any] | None = None,
        max_summary_tokens: int = 500,
    ) -> TaskContext:
        """Synthesize a compact TaskContext conforming to bounded context policy."""
        if max_summary_tokens < 1:
            raise ValueError("max_summary_tokens must be positive")

        # 1. Operational summary
        non_goals_str = f" | Non-goals: {', '.join(task_spec.non_goals)}" if task_spec.non_goals else ""
        criteria_str = f" | Acceptance: {'; '.join(task_spec.acceptance_criteria)}" if task_spec.acceptance_criteria else ""
        summary_lines = [
            f"Task: {task_spec.task_id}",
            f"Objective: {task_spec.objective}",
            f"risk_class: {task_spec.risk_class}",
            f"budget_ceiling: {task_spec.budget_ceiling}",
        ]
        if non_goals_str:
            summary_lines.append(non_goals_str)
        if criteria_str:
            summary_lines.append(criteria_str)
        operational_summary = self._bounded_text("\n".join(summary_lines), max_summary_tokens)

        # 2. Pertinent facts
        pertinent_facts: list[str] = [
            f"Allowed paths count: {len(task_spec.allowed_paths)}",
            f"Risk class: {task_spec.risk_class}",
            f"Max attempts: {task_spec.max_attempts}",
            f"Timeout seconds: {task_spec.timeout_seconds}",
        ]
        for idx, criterion in enumerate(task_spec.acceptance_criteria, start=1):
            pertinent_facts.append(f"Criterion {idx}: {criterion}")

        # 3. Active rules selection (strictly ACTIVE, project-isolated, and scoped)
        active_rules: list[str] = []
        engine = promotion_engine or self.promotion_engine
        if engine is not None:
            # Query active candidates
            all_active = engine.get_active_candidates()
            for candidate in all_active:
                if candidate.status != PolicyStatus.ACTIVE:
                    continue
                cand_project = candidate.metadata.get("project_id") if candidate.metadata else None
                if not self._is_project_match(cand_project, project_id):
                    continue
                if self._is_scope_relevant(candidate.scope, task_spec.allowed_paths, task_spec.objective):
                    content = candidate.rule_content or f"Rule [{candidate.rule_id}] scoped to {candidate.scope}"
                    active_rules.append(f"[{candidate.rule_id}][{candidate.scope}] {content}")

        if tracker is not None and hasattr(tracker, "ledger") and hasattr(tracker.ledger, "preferences"):
            for pref in tracker.ledger.preferences:
                if pref.status == PolicyStatus.ACTIVE and getattr(pref, "active", True):
                    pref_project = getattr(pref, "project_id", None)
                    if hasattr(pref, "metadata") and isinstance(pref.metadata, dict):
                        pref_project = pref.metadata.get("project_id", pref_project)
                    if not self._is_project_match(pref_project, project_id):
                        continue
                    category = getattr(pref.category, "value", str(pref.category))
                    if self._is_scope_relevant(category, task_spec.allowed_paths, task_spec.objective):
                        active_rules.append(f"[{pref.preference_id}][{category}] {pref.rule}")

        # 4. Structured file references
        file_references: list[FileReferenceSpec] = []
        for path in task_spec.allowed_paths:
            file_references.append(FileReferenceSpec(
                path=path,
                purpose="Allowed task implementation or test target",
            ))

        # 5. Durable progress from checkpoint
        durable_progress: list[str] = []
        if checkpoint:
            outputs = checkpoint.get("outputs", {})
            if isinstance(outputs, dict):
                for step_key in sorted(
                    outputs.keys(),
                    key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)),
                ):
                    val_str = str(outputs[step_key])[:60]
                    durable_progress.append(f"step {step_key}: {val_str}")
            if "last_step_index" in checkpoint:
                durable_progress.append(f"checkpoint last_step_index: {checkpoint['last_step_index']}")

        # 6. Token estimation (~4 characters per token heuristic)
        total_text = "\n".join([
            operational_summary,
            *pertinent_facts,
            *active_rules,
            *(f.path for f in file_references),
            *durable_progress,
        ])
        token_estimate = max(1, len(total_text) // 4)

        return TaskContext(
            task_id=task_spec.task_id,
            project_id=project_id or "default",
            operational_summary=operational_summary,
            pertinent_facts=pertinent_facts,
            active_rules=active_rules,
            file_references=file_references,
            durable_progress=durable_progress,
            token_estimate=token_estimate,
        )


__all__ = [
    "ContextSelector",
    "FileReferenceSpec",
    "TaskContext",
]
