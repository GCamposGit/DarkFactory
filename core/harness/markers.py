#!/usr/bin/env python3
"""Parser for supervisor-owned validation markers and structured evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.paths import project_root

PROJECT_ROOT = project_root()

from core.harness.models import HarnessResult

MARKER_STEP_START = "[STEP_START]"
MARKER_STEP_PASS = "[STEP_PASS]"
MARKER_STEP_FAIL = "[STEP_FAIL]"
MARKER_HARNESS_PASS = "[HARNESS_PASS]"
MARKER_HARNESS_FAIL = "[HARNESS_FAIL]"
MARKER_TEST_COUNT = "[TEST_COUNT]"
MARKER_HARNESS_RESULT = "[HARNESS_RESULT]"

SUPERVISOR_MARKERS = (
    MARKER_STEP_START,
    MARKER_STEP_PASS,
    MARKER_STEP_FAIL,
    MARKER_HARNESS_PASS,
    MARKER_HARNESS_FAIL,
    MARKER_TEST_COUNT,
    MARKER_HARNESS_RESULT,
)


def _payload(line: str, marker: str) -> str | None:
    prefix = f"{marker} "
    return line[len(prefix) :].strip() if line.startswith(prefix) else None


def _invalid(reason: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"valid": False, "reason": reason, **details}


def parse_harness_output(output_lines: Sequence[str]) -> dict[str, Any]:
    """Validate marker ordering and bind it to one structured result record."""
    started: list[str] = []
    passed: list[str] = []
    failed: list[str] = []
    counts: list[int] = []
    verdicts: list[bool] = []
    results: list[HarnessResult] = []
    parse_errors: list[str] = []

    for raw_line in output_lines:
        line = raw_line.strip()
        if (value := _payload(line, MARKER_STEP_START)) is not None:
            started.append(value)
        elif (value := _payload(line, MARKER_STEP_PASS)) is not None:
            passed.append(value)
        elif (value := _payload(line, MARKER_STEP_FAIL)) is not None:
            failed.append(value.split(" (", 1)[0])
        elif (value := _payload(line, MARKER_TEST_COUNT)) is not None:
            if value.startswith("count=") and value[6:].isdigit():
                counts.append(int(value[6:]))
            else:
                parse_errors.append("Malformed TEST_COUNT marker")
        elif (value := _payload(line, MARKER_HARNESS_RESULT)) is not None:
            try:
                results.append(HarnessResult.model_validate_json(value))
            except (ValidationError, json.JSONDecodeError) as exc:
                parse_errors.append(f"Invalid structured result: {exc}")
        elif line == MARKER_HARNESS_PASS:
            verdicts.append(True)
        elif line == MARKER_HARNESS_FAIL:
            verdicts.append(False)

    details: dict[str, Any] = {
        "harness_verdict": verdicts[0] if len(verdicts) == 1 else None,
        "is_empty": not passed and not failed,
        "steps_started": started,
        "steps_passed": passed,
        "steps_failed": failed,
        "total_tests_run": counts[0] if len(counts) == 1 else 0,
        "candidate_sha": results[0].candidate_sha if len(results) == 1 else None,
        "config_hash": results[0].config_hash if len(results) == 1 else None,
    }
    if parse_errors:
        return _invalid(parse_errors[0], details)
    if len(results) != 1:
        return _invalid("Missing or duplicate structured HARNESS_RESULT evidence", details)
    if len(counts) != 1 or counts[0] <= 0:
        return _invalid("Test count must be positive and emitted exactly once", details)
    if len(verdicts) != 1 or verdicts[0] is not True:
        return _invalid("Missing or conflicting HARNESS_PASS marker", details)
    if len(started) != len(set(started)) or len(passed + failed) != len(set(passed + failed)):
        return _invalid("Step markers must be unique", details)
    if any(step not in started for step in passed + failed):
        return _invalid("Every completed step must have been started", details)
    if set(started) != set(passed + failed) or failed:
        return _invalid("Started steps must terminate successfully", details)

    result = results[0]
    expected_exit_codes = set(started)
    structured_matches = (
        result.required_steps == started
        and result.started_steps == started
        and result.passed_steps == passed
        and result.failed_steps == failed
        and result.discovered_count == counts[0]
        and set(result.exit_codes) == expected_exit_codes
        and all(result.exit_codes[step] == 0 for step in passed)
        and 0 < result.passed_count <= result.discovered_count
        and result.skipped_count <= result.discovered_count
    )
    if not structured_matches:
        return _invalid("Structured result does not match supervisor markers", details)

    return {"valid": True, "reason": "OK", **details}


if __name__ == "__main__":
    parsed = parse_harness_output(sys.stdin.readlines())
    print(f"Validation Result: {'PASS' if parsed['valid'] else 'FAIL'}")
    print(f"Details: {parsed['reason']}")
    print(
        f"Passed: {len(parsed['steps_passed'])}, "
        f"Failed: {len(parsed['steps_failed'])}, Tests: {parsed['total_tests_run']}"
    )
    sys.exit(0 if parsed["valid"] else 1)
