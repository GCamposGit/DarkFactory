#!/usr/bin/env python3
"""
Harness Marker Contract & Validator
Enforces deterministic validation rules:
1. 'Empty is not a pass' - at least N tests/checks must be executed.
2. 'Zero discovered is a failure' - discovering 0 tests fails immediately.
3. Explicit marker tokens: [STEP_START], [STEP_PASS], [STEP_FAIL], [HARNESS_PASS], [HARNESS_FAIL]
"""

import sys
import re
from typing import List, Dict, Any

MARKER_STEP_START = "[STEP_START]"
MARKER_STEP_PASS = "[STEP_PASS]"
MARKER_STEP_FAIL = "[STEP_FAIL]"
MARKER_HARNESS_PASS = "[HARNESS_PASS]"
MARKER_HARNESS_FAIL = "[HARNESS_FAIL]"
MARKER_TEST_COUNT = "[TEST_COUNT]"

def parse_harness_output(output_lines: List[str]) -> Dict[str, Any]:
    """Parses harness log lines and returns deterministic audit results."""
    steps_started = []
    steps_passed = []
    steps_failed = []
    total_tests_run = 0
    harness_verdict = None

    for line in output_lines:
        line = line.strip()
        if MARKER_STEP_START in line:
            name = line.split(MARKER_STEP_START)[-1].strip()
            steps_started.append(name)
        elif MARKER_STEP_PASS in line:
            name = line.split(MARKER_STEP_PASS)[-1].strip()
            steps_passed.append(name)
        elif MARKER_STEP_FAIL in line:
            name = line.split(MARKER_STEP_FAIL)[-1].strip()
            steps_failed.append(name)
        elif MARKER_TEST_COUNT in line:
            m = re.search(r"count=(\d+)", line)
            if m:
                total_tests_run += int(m.group(1))
        elif MARKER_HARNESS_PASS in line:
            harness_verdict = True
        elif MARKER_HARNESS_FAIL in line:
            harness_verdict = False

    # Validation guards
    is_empty = len(steps_passed) == 0 and len(steps_failed) == 0
    has_failures = len(steps_failed) > 0

    valid = (harness_verdict is True) and (not is_empty) and (not has_failures)

    return {
        "valid": valid,
        "harness_verdict": harness_verdict,
        "is_empty": is_empty,
        "steps_started": steps_started,
        "steps_passed": steps_passed,
        "steps_failed": steps_failed,
        "total_tests_run": total_tests_run,
        "reason": "OK" if valid else ("No checks executed (empty is not pass)" if is_empty else ("Step failures detected" if has_failures else "Missing HARNESS_PASS marker"))
    }

if __name__ == "__main__":
    lines = sys.stdin.readlines()
    res = parse_harness_output(lines)
    print(f"Validation Result: {'PASS' if res['valid'] else 'FAIL'}")
    print(f"Details: {res['reason']}")
    print(f"Passed: {len(res['steps_passed'])}, Failed: {len(res['steps_failed'])}, Tests: {res['total_tests_run']}")
    sys.exit(0 if res["valid"] else 1)
