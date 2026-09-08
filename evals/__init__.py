"""Deterministic and live evaluation harness for agent tasks."""

__all__ = [
    "EvalMode",
    "EvalSuiteResult",
    "EvalTask",
    "GradeResult",
    "TaskCategory",
    "load_tasks",
    "run_suite",
]


def __getattr__(name: str):
    """Expose the public API lazily so ``python -m evals.runner`` stays clean."""

    if name in {"EvalTask", "GradeResult", "TaskCategory"}:
        from evals.graders import EvalTask, GradeResult, TaskCategory

        return {"EvalTask": EvalTask, "GradeResult": GradeResult, "TaskCategory": TaskCategory}[name]
    if name in {"EvalMode", "EvalSuiteResult", "load_tasks", "run_suite"}:
        from evals.runner import EvalMode, EvalSuiteResult, load_tasks, run_suite

        return {
            "EvalMode": EvalMode,
            "EvalSuiteResult": EvalSuiteResult,
            "load_tasks": load_tasks,
            "run_suite": run_suite,
        }[name]
    raise AttributeError(name)
