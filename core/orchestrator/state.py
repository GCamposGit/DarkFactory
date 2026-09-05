#!/usr/bin/env python3
"""
Autonomous Factory State Machine
Enforces deterministic lifecycle transitions:
TRIAGED -> PLANNED -> IMPLEMENTING -> VALIDATING -> REVIEWING -> READY_TO_MERGE -> MERGED

Priority Dispatch Order:
1. Fix PR that failed review or tests
2. Validate PR waiting for check
3. Implement accepted / planned issue
4. Triage untriaged incoming issues
"""

import sys
import os
import json
from enum import Enum
from typing import Dict, Any, List, Optional
from pathlib import Path

# Ensure root directory in sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.benchmarks.fetcher import ensure_daily_benchmark

STATE_FILE = ".factory/state.json"

class TaskStatus(str, Enum):
    UNSET = "UNSET"
    TRIAGED = "TRIAGED"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    REVIEWING = "REVIEWING"
    NEEDS_FIX = "NEEDS_FIX"
    READY_TO_MERGE = "READY_TO_MERGE"
    MERGED = "MERGED"
    FAILED = "FAILED"

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"tasks": {}, "history": []}

def save_state(state: Dict[str, Any]):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def update_task_status(task_id: str, status: TaskStatus, metadata: Optional[Dict[str, Any]] = None):
    state = load_state()
    if task_id not in state["tasks"]:
        state["tasks"][task_id] = {"id": task_id, "created_at": None, "history": []}

    entry = state["tasks"][task_id]
    entry["status"] = status.value
    entry["updated_at"] = None
    if metadata:
        entry.update(metadata)
    entry["history"].append({"status": status.value})
    save_state(state)
    print(f"[STATE] Task '{task_id}' -> {status.value}")

def get_next_dispatchable_task() -> Optional[Dict[str, Any]]:
    """Strict priority ordering: finish in-flight work first."""
    # Ensure daily benchmark is up to date (<1ms if already run today)
    try:
        ensure_daily_benchmark()
    except Exception as exc:
        print(f"[WARN] Daily benchmark check skipped: {exc}")

    state = load_state()
    tasks = list(state["tasks"].values())

    # Priority 1: NEEDS_FIX
    for t in tasks:
        if t.get("status") == TaskStatus.NEEDS_FIX.value:
            return t

    # Priority 2: VALIDATING / REVIEWING
    for t in tasks:
        if t.get("status") in [TaskStatus.VALIDATING.value, TaskStatus.REVIEWING.value]:
            return t

    # Priority 3: PLANNED (Ready to implement)
    for t in tasks:
        if t.get("status") == TaskStatus.PLANNED.value:
            return t

    # Priority 4: TRIAGED (Needs planning)
    for t in tasks:
        if t.get("status") == TaskStatus.TRIAGED.value:
            return t

    return None

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "next":
        task = get_next_dispatchable_task()
        print(json.dumps(task, indent=2) if task else "No tasks pending.")
    else:
        st = load_state()
        print(json.dumps(st, indent=2))
