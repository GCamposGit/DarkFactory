"""DF-18 task corpus loader and smoke/live evaluation runner."""

from __future__ import annotations

import argparse
import json
import re
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from core.execution.contracts import UnknownCostPolicy
from core.execution.providers import ModelProvider, OllamaModelProvider, OpenRouterModelProvider
from evals.graders import EvalTask, GradeResult, TaskCategory, grade_known_defect, grade_patch, grade_reference


DEFAULT_TASKS_PATH = Path(__file__).with_name("tasks.jsonl")


class EvalMode(str, Enum):
    SMOKE = "smoke"
    REAL = "real"


class EvalTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    mode: EvalMode
    passed: bool
    reference: GradeResult | None = None
    known_defect: GradeResult | None = None
    candidate: GradeResult | None = None
    error: str | None = None


class EvalSuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    mode: EvalMode
    task_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    results: list[EvalTaskResult] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.failed_count == 0 and self.task_count > 0


def load_tasks(path: Path | str = DEFAULT_TASKS_PATH) -> list[EvalTask]:
    """Load and validate the versioned JSONL corpus, rejecting duplicates."""

    tasks: list[EvalTask] = []
    seen: set[str] = set()
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                task = EvalTask.model_validate_json(line)
            except Exception as exc:
                raise ValueError(f"invalid eval task at {source}:{line_number}: {exc}") from exc
            if task.task_id in seen:
                raise ValueError(f"duplicate eval task id: {task.task_id}")
            seen.add(task.task_id)
            tasks.append(task)
    if not tasks:
        raise ValueError(f"eval corpus is empty: {source}")
    return tasks


def _parse_patch_response(text: str) -> dict[str, str]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("provider response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("provider response must be a JSON patch object")
    return payload


def _real_candidate(task: EvalTask, provider: ModelProvider, model: str) -> GradeResult:
    files = "\n\n".join(f"FILE {name}\n{content}" for name, content in task.fixture.items())
    prompt = (
        "Return only a JSON object with path, old_text and new_text.\n"
        f"Task: {task.objective}\nAcceptance criteria: {task.acceptance_criteria}\n"
        f"Allowed paths: {task.allowed_paths}\n{files}"
    )
    response = provider.generate(
        prompt,
        model=model,
        temperature=0.0,
        unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
    )
    from evals.graders import PatchSpec

    patch = PatchSpec.model_validate(_parse_patch_response(response.text))
    return grade_patch(task, patch, candidate=f"model:{response.model}")


def run_task(task: EvalTask, *, mode: EvalMode | str = EvalMode.SMOKE, provider: ModelProvider | None = None, model: str = "") -> EvalTaskResult:
    """Run one task in exactly one evidence mode."""

    selected_mode = EvalMode(mode)
    try:
        if selected_mode is EvalMode.SMOKE:
            reference = grade_reference(task)
            known_defect = grade_known_defect(task)
            return EvalTaskResult(
                task_id=task.task_id,
                mode=selected_mode,
                passed=(
                    reference.patch_applied
                    and reference.passed
                    and known_defect.patch_applied
                    and not known_defect.passed
                ),
                reference=reference,
                known_defect=known_defect,
            )
        if provider is None:
            raise ValueError("real evaluation requires a ModelProvider")
        candidate = _real_candidate(task, provider, model or "eval-agent")
        return EvalTaskResult(
            task_id=task.task_id,
            mode=selected_mode,
            passed=candidate.passed,
            candidate=candidate,
        )
    except Exception as exc:
        return EvalTaskResult(task_id=task.task_id, mode=selected_mode, passed=False, error=str(exc))


def run_suite(
    tasks: Iterable[EvalTask],
    *,
    mode: EvalMode | str = EvalMode.SMOKE,
    provider: ModelProvider | None = None,
    model: str = "",
    task_ids: set[str] | None = None,
) -> EvalSuiteResult:
    selected_mode = EvalMode(mode)
    selected = [task for task in tasks if task_ids is None or task.task_id in task_ids]
    results = [run_task(task, mode=selected_mode, provider=provider, model=model) for task in selected]
    passed_count = sum(result.passed for result in results)
    return EvalSuiteResult(
        mode=selected_mode,
        task_count=len(results),
        passed_count=passed_count,
        failed_count=len(results) - passed_count,
        results=results,
    )


def _provider_from_name(name: str) -> ModelProvider:
    if name == "ollama":
        return OllamaModelProvider()
    if name == "openrouter":
        return OpenRouterModelProvider()
    raise ValueError(f"unsupported real provider: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DF-18 agent evaluation runner")
    parser.add_argument("--mode", choices=[mode.value for mode in EvalMode], default=EvalMode.SMOKE.value)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    parser.add_argument("--task-id", action="append", dest="task_ids")
    parser.add_argument("--provider", choices=["ollama", "openrouter"])
    parser.add_argument("--model", default="eval-agent")
    args = parser.parse_args(argv)
    provider = _provider_from_name(args.provider) if args.mode == EvalMode.REAL.value and args.provider else None
    if args.mode == EvalMode.REAL.value and provider is None:
        parser.error("--provider is required for --mode real")
    result = run_suite(
        load_tasks(args.tasks),
        mode=args.mode,
        provider=provider,
        model=args.model,
        task_ids=set(args.task_ids) if args.task_ids else None,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    print(f"[EVAL_{'PASS' if result.passed else 'FAIL'}] mode={result.mode.value} tasks={result.task_count} passed={result.passed_count}")
    return 0 if result.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke
    sys.exit(main())
