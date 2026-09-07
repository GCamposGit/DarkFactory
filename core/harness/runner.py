#!/usr/bin/env python3
"""Cross-platform validation runner that emits commit-bound evidence."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.paths import project_root

PROJECT_ROOT = project_root()

from core.harness.markers import (
    MARKER_HARNESS_FAIL,
    MARKER_HARNESS_PASS,
    MARKER_HARNESS_RESULT,
    MARKER_STEP_FAIL,
    MARKER_STEP_PASS,
    MARKER_STEP_START,
    MARKER_TEST_COUNT,
    SUPERVISOR_MARKERS,
)
from core.harness.models import HarnessConfig, HarnessResult, HarnessStepConfig


@dataclass(frozen=True)
class StepExecution:
    name: str
    returncode: int
    discovered_count: int
    passed_count: int
    skipped_count: int

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def load_config(config_path: Path) -> tuple[HarnessConfig, str]:
    """Load a config exactly as supplied; missing or invalid configs fail closed."""
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Invalid harness config {config_path}: {exc}") from exc
    try:
        config = HarnessConfig.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid harness config {config_path}: {exc}") from exc
    return config, hashlib.sha256(raw).hexdigest()


def sanitize_child_output(output: str) -> str:
    """Prevent a subprocess from emitting markers owned by the supervisor."""
    sanitized = output
    for marker in SUPERVISOR_MARKERS:
        sanitized = sanitized.replace(marker, marker.replace("[", "[CHILD_", 1))
    return sanitized


def resolve_command(command: str) -> list[str]:
    """Resolve an unqualified Python command to the runner's active interpreter."""

    arguments = shlex.split(command, posix=os.name != "nt")
    if arguments and arguments[0].casefold() in {"python", "python.exe"}:
        arguments[0] = sys.executable
    return arguments


def _pytest_counts(output: str) -> tuple[int, int, int]:
    collected = re.search(r"collected\s+(\d+)\s+items?", output)
    passed = re.findall(r"(?:^|\s)(\d+)\s+passed(?:,|\s|$)", output)
    skipped = re.findall(r"(?:^|\s)(\d+)\s+skipped(?:,|\s|$)", output)
    return (
        int(collected.group(1)) if collected else 0,
        int(passed[-1]) if passed else 0,
        int(skipped[-1]) if skipped else 0,
    )


def _is_test_step(step: HarnessStepConfig) -> bool:
    return step.kind == "test" or (step.kind is None and "pytest" in step.cmd.casefold())


def run_step(step: HarnessStepConfig) -> StepExecution:
    print(f"{MARKER_STEP_START} {step.name}")
    try:
        command = resolve_command(step.cmd)
        if not command:
            raise ValueError("empty command")
        process = subprocess.run(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=PROJECT_ROOT,
            timeout=step.timeout_sec,
            check=False,
        )
        output = process.stdout or ""
        if output:
            print(sanitize_child_output(output), end="" if output.endswith("\n") else "\n")
        discovered, passed, skipped = _pytest_counts(output) if _is_test_step(step) else (0, 0, 0)
        execution = StepExecution(step.name, process.returncode, discovered, passed, skipped)
        marker = MARKER_STEP_PASS if execution.passed else MARKER_STEP_FAIL
        suffix = "" if execution.passed else f" (exit code: {process.returncode})"
        print(f"{marker} {step.name}{suffix}")
        return execution
    except subprocess.TimeoutExpired:
        print(f"{MARKER_STEP_FAIL} {step.name} (timeout exceeded)")
        return StepExecution(step.name, 124, 0, 0, 0)
    except (OSError, ValueError) as exc:
        print(f"{MARKER_STEP_FAIL} {step.name} (error: {exc})")
        return StepExecution(step.name, 127, 0, 0, 0)


def _candidate_sha() -> str:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    sha = process.stdout.strip().lower()
    if process.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise RuntimeError(f"Unable to bind harness result to candidate SHA: {process.stderr.strip()}")
    return sha


def _selected_steps(
    config: HarnessConfig, *, quick: bool, include_holdout: bool
) -> list[HarnessStepConfig]:
    return [
        step
        for step in config.steps
        if (not quick or step.quick) and (include_holdout or not step.holdout)
    ]


def execute(
    config: HarnessConfig,
    *,
    config_hash: str,
    quick: bool,
    include_holdout: bool,
    config_path: Path,
) -> bool:
    steps = _selected_steps(config, quick=quick, include_holdout=include_holdout)
    if not steps:
        print("[ERROR] Zero checks selected. Empty is not a pass.")
        print(f"{MARKER_TEST_COUNT} count=0")
        print(MARKER_HARNESS_FAIL)
        return False

    executions: list[StepExecution] = []
    for step in steps:
        execution = run_step(step)
        executions.append(execution)
        if not execution.passed:
            break

    discovered_count = sum(item.discovered_count for item in executions)
    passed_count = sum(item.passed_count for item in executions)
    skipped_count = sum(item.skipped_count for item in executions)
    passed_steps = [item.name for item in executions if item.passed]
    failed_steps = [item.name for item in executions if not item.passed]
    print(f"{MARKER_TEST_COUNT} count={discovered_count}")

    result = HarnessResult(
        candidate_sha=_candidate_sha(),
        config_hash=config_hash,
        required_steps=[step.name for step in steps],
        started_steps=[item.name for item in executions],
        passed_steps=passed_steps,
        failed_steps=failed_steps,
        discovered_count=discovered_count,
        passed_count=passed_count,
        skipped_count=skipped_count,
        exit_codes={item.name: item.returncode for item in executions},
        artifact_refs=[str(config_path)],
    )
    print(f"{MARKER_HARNESS_RESULT} {result.model_dump_json()}")

    success = (
        not failed_steps
        and len(executions) == len(steps)
        and discovered_count > 0
        and passed_count > 0
    )
    print(MARKER_HARNESS_PASS if success else MARKER_HARNESS_FAIL)
    return success


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic validation harness runner")
    parser.add_argument("--config", default="harness.config.json", help="Harness config path")
    parser.add_argument("--quick", action="store_true", help="Run only quick steps")
    parser.add_argument("--holdout", action="store_true", help="Include holdout verification")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    try:
        config, config_hash = load_config(config_path)
        return 0 if execute(
            config,
            config_hash=config_hash,
            quick=args.quick,
            include_holdout=args.holdout,
            config_path=config_path,
        ) else 1
    except (RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        print(MARKER_HARNESS_FAIL)
        return 2


if __name__ == "__main__":
    sys.exit(main())
