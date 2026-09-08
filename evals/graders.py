"""Independent graders for the DF-18 agent evaluation corpus.

The grader owns the fixture directory, applies exactly one declared patch, and
executes the acceptance command without a shell.  Agent output is therefore
never treated as evidence of success: only the candidate process exit code and
its bounded output are used for the verdict.
"""

from __future__ import annotations

import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.execution.sandbox import PathContainment, ProcessSandbox, SandboxSecurityError


class TaskCategory(str, Enum):
    BUGFIX = "bugfix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    SECURITY = "security"
    RECOVERY = "recovery"


class PatchSpec(BaseModel):
    """A small exact replacement patch used by the offline corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("patch paths must be relative and cannot traverse parents")
        return normalized

    @field_validator("new_text")
    @classmethod
    def require_change(cls, value: str, info: Any) -> str:
        if info.data.get("old_text") == value:
            raise ValueError("patch must change the target text")
        return value


class ResourceLimits(BaseModel):
    """Per-task limits enforced by the independent evaluator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    max_output_bytes: int = Field(default=64_000, ge=1_024, le=2_000_000)


class EvalTask(BaseModel):
    """Versioned task contract stored one record per JSONL line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="1", pattern="^1$")
    task_id: str = Field(min_length=1)
    category: TaskCategory
    objective: str = Field(min_length=1)
    fixture: dict[str, str] = Field(min_length=1)
    allowed_paths: list[str] = Field(min_length=1)
    acceptance_command: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    test_count: int = Field(default=1, ge=1)
    reference_patch: PatchSpec
    known_defect: PatchSpec
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)

    @field_validator("fixture", "allowed_paths", "acceptance_command", "acceptance_criteria")
    @classmethod
    def require_nonblank_values(cls, value: Any) -> Any:
        values = value.values() if isinstance(value, Mapping) else value
        if any(not str(item).strip() for item in values):
            raise ValueError("task collections cannot contain blank values")
        return value

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            path = Path(value.replace("\\", "/").strip())
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("allowed paths must be relative and cannot traverse parents")
            normalized.append(path.as_posix())
        return normalized

    @property
    def base(self) -> dict[str, str]:
        """Compatibility name for consumers that call fixtures a base."""

        return self.fixture


class GradeResult(BaseModel):
    """Bounded, reproducible result from one candidate patch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    candidate: str
    passed: bool
    patch_applied: bool
    discovered_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float = Field(ge=0.0)
    output_tail: str = ""
    error: str | None = None


class PatchApplicationError(ValueError):
    """Raised when a candidate patch is malformed or does not match its base."""


def materialize_fixture(task: EvalTask, workdir: Path) -> None:
    """Write only the task-owned fixture files into an empty work directory."""

    # The corpus, not the candidate, owns fixture creation.  Candidate writes
    # are constrained separately by ``allowed_paths`` in ``apply_patch``.
    containment = PathContainment(workdir)
    for relative_path, content in task.fixture.items():
        target = containment.assert_path_allowed(relative_path, for_write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def apply_patch(task: EvalTask, patch: PatchSpec, workdir: Path) -> None:
    """Apply one exact replacement within the task allow-list."""

    containment = PathContainment(workdir, allowed_paths=task.allowed_paths)
    try:
        target = containment.assert_path_allowed(patch.path, for_write=True)
    except SandboxSecurityError as exc:
        raise PatchApplicationError(str(exc)) from exc
    if not target.is_file():
        raise PatchApplicationError(f"patch target does not exist: {patch.path}")
    current = target.read_text(encoding="utf-8")
    occurrences = current.count(patch.old_text)
    if occurrences != 1:
        raise PatchApplicationError(
            f"patch context for {patch.path!r} matched {occurrences} times; expected exactly one"
        )
    target.write_text(current.replace(patch.old_text, patch.new_text, 1), encoding="utf-8")


def _command_for_task(task: EvalTask) -> list[str]:
    import sys

    return [sys.executable if part == "{python}" else part for part in task.acceptance_command]


def _count_from_output(task: EvalTask, output: str, *, succeeded: bool) -> tuple[int, int]:
    match = re.search(r"\[EVAL_TESTS\]\s+discovered=(\d+)\s+passed=(\d+)", output)
    if match:
        return int(match.group(1)), int(match.group(2))
    return task.test_count, task.test_count if succeeded else 0


def grade_patch(task: EvalTask, patch: PatchSpec, *, candidate: str, workdir: Path | None = None) -> GradeResult:
    """Materialize, patch and independently execute one task candidate."""

    owned_temp = tempfile.TemporaryDirectory(prefix=f"df18-{task.task_id.lower()}-") if workdir is None else None
    target_dir = Path(owned_temp.name) if owned_temp is not None else Path(workdir)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        materialize_fixture(task, target_dir)
        try:
            apply_patch(task, patch, target_dir)
        except PatchApplicationError as exc:
            return GradeResult(
                task_id=task.task_id,
                candidate=candidate,
                passed=False,
                patch_applied=False,
                discovered_count=0,
                passed_count=0,
                duration_seconds=0.0,
                error=str(exc),
            )

        sandbox = ProcessSandbox(
            working_dir=target_dir,
            path_containment=PathContainment(target_dir, allowed_paths=task.allowed_paths),
            default_timeout_seconds=task.resource_limits.timeout_seconds,
        )
        result = sandbox.run_command(
            _command_for_task(task),
            timeout_seconds=task.resource_limits.timeout_seconds,
            shell=False,
        )
        combined = f"{result.stdout}\n{result.stderr}"
        if len(combined.encode("utf-8")) > task.resource_limits.max_output_bytes:
            return GradeResult(
                task_id=task.task_id,
                candidate=candidate,
                passed=False,
                patch_applied=True,
                discovered_count=task.test_count,
                passed_count=0,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_seconds=result.duration_seconds,
                output_tail=combined[-4_000:],
                error="acceptance output exceeded resource limit",
            )
        succeeded = result.exit_code == 0 and not result.timed_out
        discovered, passed = _count_from_output(task, combined, succeeded=succeeded)
        return GradeResult(
            task_id=task.task_id,
            candidate=candidate,
            passed=succeeded,
            patch_applied=True,
            discovered_count=discovered,
            passed_count=passed,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
            output_tail=combined[-4_000:],
            error=None if succeeded else "acceptance command failed",
        )
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()


def grade_reference(task: EvalTask, *, workdir: Path | None = None) -> GradeResult:
    return grade_patch(task, task.reference_patch, candidate="reference", workdir=workdir)


def grade_known_defect(task: EvalTask, *, workdir: Path | None = None) -> GradeResult:
    return grade_patch(task, task.known_defect, candidate="known_defect", workdir=workdir)
