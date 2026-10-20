"""Deterministic qualification battery for model candidates (USR-185, slice 6).

Stages: ``contract`` (JSON reply format), ``tools`` (simulated tool call) and ``reference`` (factory reference
tasks with exact-match oracles). The model executor is injectable; tests use ``RecordedExecutor`` (no network, no
cost). A budget (USD and/or tokens) stops the battery and fails the candidate when exceeded. Evidence is persisted
as one JSON file per candidate and summarised in the registry entry (``qualification``). Promotion is slice 7.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Union

from core.models.registry import DEFAULT_REGISTRY_PATH, REPO_ROOT, parse_registry

logger = logging.getLogger(__name__)

BATTERY_PATH = REPO_ROOT / ".factory" / "config" / "qualification_battery.json"
EVIDENCE_DIR = REPO_ROOT / ".factory" / "models" / "qualification"


@dataclass(frozen=True)
class ExecResult:
    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ModelExecutor(Protocol):
    def __call__(self, model_id: str, prompt: str, tools: Optional[list[dict[str, Any]]] = None) -> ExecResult: ...


class RecordedExecutor:
    """Replays recorded answers keyed by step id (``contract``, ``tools``, ``reference:<task>``)."""

    def __init__(self, recorded: dict[str, ExecResult]) -> None:
        self.recorded = recorded
        self._step = ""

    def for_step(self, step_id: str) -> "RecordedExecutor":
        self._step = step_id
        return self

    def __call__(self, model_id: str, prompt: str, tools: Optional[list[dict[str, Any]]] = None) -> ExecResult:
        if self._step not in self.recorded:
            raise KeyError(f"no recorded answer for step {self._step!r}")
        return self.recorded[self._step]


@dataclass(frozen=True)
class Budget:
    max_usd: float = 0.0
    max_tokens: Optional[int] = None

    def exceeded(self, usd: float, tokens: int) -> bool:
        return usd > self.max_usd + 1e-12 or (self.max_tokens is not None and tokens > self.max_tokens)


@dataclass
class StepResult:
    step: str
    passed: bool
    detail: str = ""
    cost_usd: float = 0.0
    tokens: int = 0


@dataclass
class QualificationResult:
    model_id: str
    battery_version: str
    qualified_at: str
    approved: bool
    budget_exceeded: bool
    cost_usd: float
    tokens: int
    steps: list[StepResult] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "approved" if self.approved else "failed"

    def to_json(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "battery_version": self.battery_version,
            "qualified_at": self.qualified_at,
            "status": self.status,
            "budget_exceeded": self.budget_exceeded,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "steps": [vars(s) for s in self.steps],
        }


def load_battery(path: Optional[Path] = None) -> dict[str, Any]:
    return json.loads((path or BATTERY_PATH).read_text(encoding="utf-8"))


def _check_contract(spec: dict[str, Any], res: ExecResult) -> tuple[bool, str]:
    try:
        data = json.loads(res.text.strip())
    except ValueError:
        return False, "reply is not valid JSON"
    if not isinstance(data, dict):
        return False, "reply is not a JSON object"
    missing = [k for k in spec["required_keys"] if k not in data]
    if missing:
        return False, f"missing keys {missing}"
    return (data.get("ok") is True), "ok" if data.get("ok") is True else "ok is not true"


def _check_tools(spec: dict[str, Any], res: ExecResult) -> tuple[bool, str]:
    want = spec["expected_call"]
    for call in res.tool_calls:
        if call.get("name") == want["name"] and call.get("arguments") == want["arguments"]:
            return True, "ok"
    return False, "expected tool call not found"


def _check_reference(task: dict[str, Any], res: ExecResult) -> tuple[bool, str]:
    got = res.text.strip().strip("`\"'").lower()
    ok = got == str(task["expected"]).strip().lower()
    return ok, "ok" if ok else f"expected {task['expected']!r}"


def run_battery(
    model_id: str,
    executor: ModelExecutor,
    *,
    now: datetime,
    budget: Optional[Budget] = None,
    battery: Optional[dict[str, Any]] = None,
) -> QualificationResult:
    """Run every stage; stop and fail as soon as the budget is exceeded. ``now`` is injected (no wall clock)."""
    spec = battery if battery is not None else load_battery()
    if budget is None:
        b = spec.get("default_budget", {})
        budget = Budget(float(b.get("max_usd", 0.0)), b.get("max_tokens"))
    steps: list[tuple[str, str, Optional[list[dict[str, Any]]], Callable[[ExecResult], tuple[bool, str]]]] = [
        ("contract", spec["contract"]["prompt"], None, lambda r: _check_contract(spec["contract"], r)),
        ("tools", spec["tools"]["prompt"], [spec["tools"]["tool_schema"]], lambda r: _check_tools(spec["tools"], r)),
    ]
    for task in spec["reference_tasks"]:
        steps.append((f"reference:{task['id']}", task["prompt"], None, lambda r, t=task: _check_reference(t, r)))
    out = QualificationResult(model_id, str(spec["battery_version"]), now.isoformat(), False, False, 0.0, 0)
    for step_id, prompt, tools, check in steps:
        if isinstance(executor, RecordedExecutor):
            executor.for_step(step_id)
        try:
            res = executor(model_id, prompt, tools)
        except Exception as exc:  # an executor failure fails the step, never the process
            out.steps.append(StepResult(step_id, False, f"executor error: {exc}"))
            break
        tokens = res.input_tokens + res.output_tokens
        out.cost_usd += res.cost_usd
        out.tokens += tokens
        if budget.exceeded(out.cost_usd, out.tokens):
            out.budget_exceeded = True
            out.steps.append(StepResult(step_id, False, "budget exceeded", res.cost_usd, tokens))
            break
        passed, detail = check(res)
        out.steps.append(StepResult(step_id, passed, detail, res.cost_usd, tokens))
        if not passed:
            break
    out.approved = (not out.budget_exceeded) and len(out.steps) == len(steps) and all(s.passed for s in out.steps)
    return out


def _safe_name(model_id: str) -> str:
    return model_id.replace("/", "__").replace("\\", "__")


def persist_evidence(result: QualificationResult, evidence_dir: Optional[Path] = None) -> Path:
    target_dir = evidence_dir or EVIDENCE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{_safe_name(result.model_id)}.json"
    path.write_text(json.dumps(result.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return path


def record_in_registry(
    result: QualificationResult,
    evidence_path: Union[str, Path],
    registry_path: Optional[Path] = None,
) -> None:
    """Write the qualification summary on the candidate entry. Never changes ``status`` (promotion is slice 7)."""
    target = registry_path or DEFAULT_REGISTRY_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    for model in raw["models"]:
        if model["id"] == result.model_id:
            if model.get("status") != "candidate":
                raise ValueError(f"{result.model_id!r} is not a candidate (status={model.get('status')})")
            try:
                shown = Path(evidence_path).resolve().relative_to(REPO_ROOT).as_posix()
            except ValueError:
                shown = Path(evidence_path).as_posix()
            model["qualification"] = {
                "status": result.status,
                "battery_version": result.battery_version,
                "qualified_at": result.qualified_at,
                "cost_usd": result.cost_usd,
                "evidence_path": shown,
            }
            parse_registry(raw)  # fail before writing anything invalid
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            os.replace(tmp, target)
            return
    raise KeyError(f"unknown model {result.model_id!r}")
