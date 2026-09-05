#!/usr/bin/env python3
"""
Cross-Platform Deterministic Validation Harness Runner
Executes:
1. Static analysis (lint, types, syntax)
2. Unit test suite
3. Integration / Contract tests
4. E2E Headless (CLI, HTTP, or Library)
5. Holdout suite (if present)

Emits standard markers parsed by markers.py.
"""

import sys
import os
import json
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from markers import (
    MARKER_STEP_START,
    MARKER_STEP_PASS,
    MARKER_STEP_FAIL,
    MARKER_HARNESS_PASS,
    MARKER_HARNESS_FAIL,
    MARKER_TEST_COUNT
)

DEFAULT_CONFIG = {
    "steps": [
        {
            "name": "syntax_and_types",
            "cmd": "python -m py_compile core/router/model_router.py",
            "quick": True
        }
    ]
}

def run_step(step: Dict[str, Any]) -> bool:
    name = step["name"]
    cmd = step["cmd"]
    print(f"{MARKER_STEP_START} {name}")
    print(f"--> Executing: {cmd}")

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(PROJECT_ROOT),
            timeout=step.get("timeout_sec", 120)
        )
        print(proc.stdout)
        if proc.returncode == 0:
            print(f"{MARKER_STEP_PASS} {name}")
            return True
        else:
            print(f"{MARKER_STEP_FAIL} {name} (exit code: {proc.returncode})")
            return False
    except subprocess.TimeoutExpired:
        print(f"{MARKER_STEP_FAIL} {name} (timeout exceeded)")
        return False
    except Exception as e:
        print(f"{MARKER_STEP_FAIL} {name} (error: {e})")
        return False

def main():
    parser = argparse.ArgumentParser(description="Deterministic Validation Harness Runner")
    parser.add_argument("--config", default="harness.config.json", help="Path to harness config file")
    parser.add_argument("--quick", action="store_true", help="Run only quick/per-task validation steps")
    parser.add_argument("--holdout", action="store_true", help="Include holdout secret verification")
    args = parser.parse_args()

    steps = DEFAULT_CONFIG["steps"]
    config_path = Path(args.config)
    if not config_path.is_absolute() and not config_path.exists():
        config_path = PROJECT_ROOT / args.config

    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                steps = cfg.get("steps", steps)
        except Exception as e:
            print(f"[WARN] Failed to read {config_path}, using fallback: {e}")

    failed_any = False
    executed_count = 0

    print("==================================================")
    print(f"Starting Validation Harness (Quick Mode: {args.quick})")
    print("==================================================")

    for step in steps:
        if args.quick and not step.get("quick", False):
            continue
        if step.get("holdout", False) and not args.holdout:
            continue

        executed_count += 1
        success = run_step(step)
        if not success:
            failed_any = True
            break

    print(f"{MARKER_TEST_COUNT} count={executed_count}")

    if executed_count == 0:
        print("[ERROR] Zero checks executed. Empty is not a pass.")
        print(MARKER_HARNESS_FAIL)
        sys.exit(1)

    if failed_any:
        print("Validation Harness FAILED.")
        print(MARKER_HARNESS_FAIL)
        sys.exit(1)
    else:
        print("Validation Harness PASSED.")
        print(MARKER_HARNESS_PASS)
        sys.exit(0)

if __name__ == "__main__":
    main()
