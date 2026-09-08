"""Headless DF-15 factory vertical.

The vertical intentionally uses a tiny, deterministic patch protocol.  A
provider proposes a JSON patch, the supervisor applies it only inside the
declared allow-list, runs the candidate checks, and then invokes an
independent holdout verifier.  Durable checkpoints come from
``OrchestratorRuntime``; attempt cost and latency come from
``ExecutionBudgetManager``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

# Ensure repository root is on sys.path for direct CLI script execution
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.execution.budget import ExecutionBudgetManager
from core.execution.contracts import (
    AttemptOutcome,
    AttemptRecord,
    Budget,
    UnknownCostPolicy,
)
from core.execution.providers import MockModelProvider, ModelProvider
from core.execution.sandbox import PathContainment, ProcessSandbox, SandboxSecurityError
from core.orchestrator.runtime import OrchestratorRuntime, RuntimeContext, RuntimeResult
from core.orchestrator.store import OrchestratorStore

logger = logging.getLogger(__name__)

DEFAULT_PATCH_RESPONSE = json.dumps(
    {
        "path": "calculator.py",
        "old_text": "return left - right",
        "new_text": "return left + right",
        "rationale": "The issue asks for addition; subtraction is the defect.",
    },
    separators=(",", ":"),
)


class FactoryVerticalError(RuntimeError):
    """Base error for a DF-15 run."""


class InjectedFactoryCrash(FactoryVerticalError):
    """Deterministic crash used to prove recovery after a side effect."""


class FactoryVerticalConfig(BaseModel):
    """Closed configuration for one local factory task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    task_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    allowed_paths: list[str] = Field(min_length=1)
    candidate_command: list[str] = Field(min_length=1)
    holdout_module: str = Field(min_length=1)
    budget_ceiling: float = Field(gt=0.0)
    max_attempts: int = Field(ge=1, le=20)
    timeout_seconds: float = Field(gt=0.0, le=600.0)
    model: str = Field(default="mock-factory-agent", min_length=1)

    @field_validator("allowed_paths", "candidate_command")
    @classmethod
    def _require_nonblank_items(cls, values: list[str]) -> list[str]:
        if any(not item.strip() for item in values):
            raise ValueError("configuration lists cannot contain blank items")
        return values

    @field_validator("allowed_paths")
    @classmethod
    def _validate_relative_allowed_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = Path(value.replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("allowed paths must be relative and cannot traverse parents")
        return values

    @field_validator("holdout_module")
    @classmethod
    def _validate_relative_holdout_module(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("holdout module must be relative and cannot traverse parents")
        return normalized


class FactoryPatch(BaseModel):
    """Small, reviewable replacement patch emitted by the provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("patch path must be relative and cannot traverse parents")
        return normalized

    @field_validator("new_text")
    @classmethod
    def _different_replacement(cls, value: str, info: Any) -> str:
        old_text = info.data.get("old_text")
        if old_text is not None and value == old_text:
            raise ValueError("patch replacement must change the file")
        return value


class CandidateCheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: list[str]
    exit_code: int
    passed: bool
    discovered_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    output_sha256: str = Field(min_length=64, max_length=64)
    output_tail: str = ""


class HoldoutEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verifier: str
    verdict: Literal["PASS", "FAIL"]
    discovered_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class FactoryRunEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    task_id: str
    run_id: str
    recovered: bool
    recovery_count: int = Field(ge=0)
    patch_path: str
    issue_sha256: str = Field(min_length=64, max_length=64)
    patch_sha256: str = Field(min_length=64, max_length=64)
    candidate_sha256: str = Field(min_length=64, max_length=64)
    candidate_check: CandidateCheckEvidence
    holdout: HoldoutEvidence
    budget: dict[str, Any]
    telemetry: dict[str, Any]


class FactoryRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    run_id: str
    status: str
    recovered: bool
    recovery_count: int = Field(ge=0)
    patch_path: str
    evidence_path: str
    evidence: FactoryRunEvidence


def load_factory_config(path: Path | str, base_dir: Path | str | None = None) -> FactoryVerticalConfig:
    """Load and validate a closed DF-15 task configuration."""
    config_path = Path(path)
    if not config_path.is_file() and base_dir is not None:
        candidate = (Path(base_dir) / path).resolve()
        if candidate.is_file():
            config_path = candidate
    try:
        return FactoryVerticalConfig.model_validate_json(config_path.read_bytes())
    except OSError as exc:
        raise FactoryVerticalError(f"unable to read factory config: {config_path}") from exc


def init_demo_fixture(workdir: Path | str, config_target: Path | str | None = None) -> Path:
    """Bootstrap the DF15-CALCULATOR-ADD demo bug fixture into workdir."""
    target_dir = Path(workdir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    calc_path = target_dir / "calculator.py"
    if not calc_path.exists():
        calc_path.write_text(
            "def add(left: int, right: int) -> int:\n    return left - right\n",
            encoding="utf-8",
        )
    test_calc = target_dir / "test_calculator.py"
    if not test_calc.exists():
        test_calc.write_text(
            "from calculator import add\n\n\ndef test_issue_acceptance() -> None:\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8",
        )
    holdout_rel = Path(".factory/holdout/df15_factory_vertical.py")
    holdout_dest = target_dir / holdout_rel
    if not holdout_dest.exists():
        holdout_src = _REPO_ROOT / holdout_rel
        if holdout_src.exists():
            holdout_dest.parent.mkdir(parents=True, exist_ok=True)
            holdout_dest.write_text(holdout_src.read_text(encoding="utf-8"), encoding="utf-8")
    config_file = Path(config_target) if config_target else (target_dir / "factory_vertical.config.json")
    if not config_file.exists():
        src_config = _REPO_ROOT / "factory_vertical.config.json"
        if src_config.exists():
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(src_config.read_text(encoding="utf-8"), encoding="utf-8")
    return target_dir


def _json_from_provider(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise FactoryVerticalError("provider returned invalid patch JSON") from exc
    if not isinstance(value, dict):
        raise FactoryVerticalError("provider patch response must be a JSON object")
    return value


def _parse_pytest_counts(output: str) -> tuple[int, int]:
    collected = re.search(r"collected\s+(\d+)\s+items?", output)
    passed = re.findall(r"(?:^|\s)(\d+)\s+passed(?:,|\s|$)", output)
    discovered = int(collected.group(1)) if collected else (int(passed[-1]) if passed else 0)
    return discovered, int(passed[-1]) if passed else 0


class FactoryVertical:
    """Run one issue-to-patch-to-independent-verification task."""

    def __init__(
        self,
        workdir: Path | str,
        config: FactoryVerticalConfig,
        *,
        provider: ModelProvider | None = None,
        store: OrchestratorStore | None = None,
        budget_manager: ExecutionBudgetManager | None = None,
        owner: str = "df15-worker",
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.workdir = Path(workdir).resolve()
        self.config = config
        self.provider = provider or MockModelProvider(fixed_response=DEFAULT_PATCH_RESPONSE)
        self._clock = clock
        self.run_store = store or OrchestratorStore(
            self.workdir / ".factory" / "orchestrator.sqlite3", clock=clock
        )
        self.budget_manager = budget_manager or ExecutionBudgetManager(
            self.workdir / ".factory" / "budget.sqlite3", clock=clock
        )
        if self.budget_manager.get_budget(config.task_id) is None:
            self.budget_manager.register_budget(
                config.task_id,
                Budget(
                    currency="USD",
                    ceiling=config.budget_ceiling,
                    max_attempts=config.max_attempts,
                    concurrency_limit=1,
                    unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
                ),
            )
        self.runtime = OrchestratorRuntime(
            self.run_store,
            owner=owner,
            lease_seconds=lease_seconds,
        )

    def _artifact_dir(self, run_id: str) -> Path:
        path = self.workdir / ".factory" / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _issue_step(self, _context: RuntimeContext) -> dict[str, Any]:
        issue_hash = hashlib.sha256(self.config.issue.encode("utf-8")).hexdigest()
        return {"issue_sha256": issue_hash, "objective": self.config.objective}

    def _patch_step(
        self,
        context: RuntimeContext,
        *,
        crash_after_patch: bool,
    ) -> dict[str, Any]:
        artifact_dir = self._artifact_dir(context.run_id)
        marker_path = artifact_dir / "patch-attempt.json"
        if marker_path.exists():
            return json.loads(marker_path.read_text(encoding="utf-8"))

        prompt = (
            "Issue:\n"
            + self.config.issue
            + "\nObjective:\n"
            + self.config.objective
            + "\nAllowed paths:\n"
            + json.dumps(self.config.allowed_paths)
            + "\nReturn only JSON with path, old_text, new_text, rationale."
        )
        attempt_id = f"{context.run_id}:patch"
        reservation = self.budget_manager.reserve(
            self.config.task_id,
            attempt_id,
            round(self.config.budget_ceiling / self.config.max_attempts, 8),
        )
        committed = False
        try:
            response = self.provider.generate(
                prompt,
                model=self.config.model,
                max_tokens=512,
                temperature=0.0,
                unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
            )
            patch = FactoryPatch.model_validate(_json_from_provider(response.text))
            containment = PathContainment(self.workdir, self.config.allowed_paths)
            try:
                target = containment.assert_path_allowed(patch.path, for_write=True)
            except SandboxSecurityError as exc:
                raise FactoryVerticalError(str(exc)) from exc
            if not target.exists():
                raise FactoryVerticalError(f"target file for patch does not exist: {patch.path}")
            current = target.read_text(encoding="utf-8")
            if patch.old_text in current:
                updated = current.replace(patch.old_text, patch.new_text, 1)
                target.write_text(updated, encoding="utf-8")
            elif patch.new_text in current:
                updated = current
            else:
                raise FactoryVerticalError(
                    f"patch context not found in allowed file: {patch.path}"
                )

            patch_json = patch.model_dump(mode="json")
            patch_hash = hashlib.sha256(
                json.dumps(patch_json, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            attempt = AttemptRecord(
                attempt_id=attempt_id,
                invocation_id=f"{context.run_id}:provider",
                input_artifact_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                output_artifact_hash=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
                mode=(
                    "simulated"
                    if str(getattr(self.provider, "provider_id", "")).startswith("mock")
                    else ("live" if response.is_measured else "estimated")
                ),
                tokens=response.total_tokens,
                measured_cost=response.measured_cost,
                estimated_cost=response.estimated_cost,
                latency=response.latency_seconds,
                outcome=AttemptOutcome.SUCCEEDED,
                timestamp=datetime.now(UTC),
            )
            self.budget_manager.commit(reservation.reservation_id, attempt)
            committed = True
            result = {
                "path": patch.path,
                "patch_sha256": patch_hash,
                "attempt_id": attempt_id,
                "candidate_sha256": hashlib.sha256(updated.encode("utf-8")).hexdigest(),
            }
            marker_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
            if crash_after_patch:
                raise InjectedFactoryCrash("simulated crash after idempotent patch commit")
            return result
        except Exception:
            if not committed:
                try:
                    self.budget_manager.release(reservation.reservation_id)
                except Exception:
                    logger.exception("failed to release patch reservation")
            raise

    def _candidate_step(self, _context: RuntimeContext) -> dict[str, Any]:
        command = [sys.executable if value == "{python}" else value for value in self.config.candidate_command]
        containment = PathContainment(self.workdir, self.config.allowed_paths)
        result = ProcessSandbox(
            self.workdir,
            path_containment=containment,
            default_timeout_seconds=self.config.timeout_seconds,
        ).run_command(command, timeout_seconds=self.config.timeout_seconds)
        output = (result.stdout + result.stderr).strip()
        discovered, passed = _parse_pytest_counts(output)
        evidence = CandidateCheckEvidence(
            command=command,
            exit_code=result.exit_code,
            passed=result.exit_code == 0 and not result.timed_out,
            discovered_count=discovered,
            passed_count=passed,
            output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
            output_tail=output[-2000:],
        )
        if not evidence.passed:
            raise FactoryVerticalError(
                f"candidate check failed with exit code {result.exit_code} "
                f"(timed_out={result.timed_out}): {output[-500:]}"
            )
        return evidence.model_dump(mode="json")

    def _holdout_step(self, _context: RuntimeContext) -> dict[str, Any]:
        module_path = (self.workdir / self.config.holdout_module).resolve()
        if not module_path.is_file():
            repo_candidate = (_REPO_ROOT / self.config.holdout_module).resolve()
            if repo_candidate.is_file():
                module_path = repo_candidate
            else:
                raise FactoryVerticalError(f"holdout verifier is missing: {module_path}")
        spec = importlib.util.spec_from_file_location("df15_protected_holdout", module_path)
        if spec is None or spec.loader is None:
            raise FactoryVerticalError("unable to load holdout verifier")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        verify: Any = getattr(module, "verify", None)
        if not callable(verify):
            raise FactoryVerticalError("holdout verifier must export verify(workdir)")
        result = verify(self.workdir)
        evidence = HoldoutEvidence.model_validate(
            {"verifier": str(module_path), **result}
        )
        if evidence.verdict != "PASS":
            raise FactoryVerticalError(f"independent holdout rejected candidate: {evidence.details}")
        return evidence.model_dump(mode="json")

    def _evidence(self, runtime_result: RuntimeResult) -> FactoryRunEvidence:
        outputs = runtime_result.result if isinstance(runtime_result.result, dict) else {}
        patch = outputs.get("1", {})
        candidate = CandidateCheckEvidence.model_validate(outputs.get("2", {}))
        holdout = HoldoutEvidence.model_validate(outputs.get("3", {}))
        budget = self.budget_manager.get_budget(self.config.task_id)
        if budget is None:
            raise FactoryVerticalError("budget disappeared during run")
        attempts = self.budget_manager.list_attempts(self.config.task_id)
        telemetry = {
            "attempt_count": len(attempts),
            "tokens_total": sum(item.tokens for item in attempts),
            "latency_seconds_total": round(sum(item.latency for item in attempts), 6),
            "cost_usd_total": round(budget.spent, 8),
            "outcomes": [item.outcome.value for item in attempts],
            "checkpoint_step_index": runtime_result.step_index,
        }
        candidate_path = self.workdir / str(patch["path"])
        candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        return FactoryRunEvidence(
            task_id=self.config.task_id,
            run_id=runtime_result.run_id,
            recovered=runtime_result.recovered,
            recovery_count=runtime_result.recovery_count,
            patch_path=str(patch["path"]),
            issue_sha256=str(outputs.get("0", {}).get("issue_sha256", "")),
            patch_sha256=str(patch["patch_sha256"]),
            candidate_sha256=candidate_sha,
            candidate_check=candidate,
            holdout=holdout,
            budget=budget.model_dump(mode="json"),
            telemetry=telemetry,
        )

    def run(
        self,
        *,
        resume: bool = False,
        crash_after_step: int | None = None,
    ) -> FactoryRunResult:
        """Run or recover the vertical; crash injection is test-only behavior."""
        crash_once = {"enabled": crash_after_step == 1}

        def patch_step(context: RuntimeContext) -> dict[str, Any]:
            should_crash = crash_once["enabled"]
            crash_once["enabled"] = False
            return self._patch_step(context, crash_after_patch=should_crash)

        steps = (
            self._issue_step,
            patch_step,
            self._candidate_step,
            self._holdout_step,
        )
        runtime_result = self.runtime.resume(self.config.task_id, steps) if resume else self.runtime.start(
            self.config.task_id, steps
        )
        evidence = self._evidence(runtime_result)
        evidence_path = self._artifact_dir(runtime_result.run_id) / "evidence.json"
        evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
        return FactoryRunResult(
            task_id=self.config.task_id,
            run_id=runtime_result.run_id,
            status=runtime_result.status.value,
            recovered=runtime_result.recovered,
            recovery_count=runtime_result.recovery_count,
            patch_path=str((self.workdir / evidence.patch_path).resolve()),
            evidence_path=str(evidence_path.resolve()),
            evidence=evidence,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DF-15 autonomous factory vertical")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "init-fixture"])
    parser.add_argument("--config", default="factory_vertical.config.json")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--owner", default="df15-cli")
    parser.add_argument("--provider", default="mock", choices=["mock", "auto", "ollama", "openrouter"])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--init-fixture", action="store_true", help="Bootstrap demo fixture into workdir")
    parser.add_argument("--crash-after-step", type=int, choices=[1])
    args = parser.parse_args(argv)

    workdir = Path(args.workdir).resolve()
    if args.command == "init-fixture" or args.init_fixture:
        init_demo_fixture(workdir, args.config)
        if args.command == "init-fixture":
            print(f"[FACTORY_FIXTURE_INIT] Demo fixture prepared in {workdir}")
            return 0

    config = load_factory_config(args.config, base_dir=workdir)

    provider: ModelProvider | None = None
    if args.provider != "mock":
        from core.execution.providers import get_model_provider

        provider = get_model_provider(args.provider)

    result = FactoryVertical(workdir, config, provider=provider, owner=args.owner).run(
        resume=args.resume,
        crash_after_step=args.crash_after_step,
    )
    print(result.model_dump_json(indent=2))
    print("[FACTORY_PASS] df15_factory_vertical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
