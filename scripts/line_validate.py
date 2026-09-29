#!/usr/bin/env python3
"""Validate command the production line runs for the `darkfac` project itself.

The official gate (`core/harness/runner.py --quick`) refuses to run on a dirty
worktree ("Candidate worktree is dirty"), because its verdict is bound to a
commit. The line's development stage validates BEFORE it commits the ticket, so
calling the runner directly there would fail every iteration. This wrapper picks
the right mode:

- clean tree (e.g. the ValidationStage's fresh clone of the pushed branch): run
  the official runner, which keeps its verdict cache and remote test-worker
  dispatch (DARKFAC_TEST_WORKERS);
- dirty tree (development loop): run the same `quick` steps of
  `harness.config.json` directly, each with its own timeout, stopping at the
  first failure. No drift is possible: the steps are read from the config.

Exit code 0 only when everything passed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.harness import runner  # noqa: E402


def is_clean_tree(root: Path) -> bool:
    """True only when `git status` succeeds and reports nothing (fail-closed: unknown -> dirty)."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


def run_steps_directly(root: Path, config_path: Path) -> int:
    config, _config_hash = runner.load_config(config_path)
    steps = runner._selected_steps(config, quick=True, include_holdout=False)
    if not steps:
        print("[line_validate] no quick steps selected; refusing to pass on an empty selection")
        return 1
    env = dict(os.environ, PYTHONPATH=str(root))
    for step in steps:
        print(f"[line_validate] step {step.name}: {step.cmd}", flush=True)
        try:
            proc = subprocess.run(
                runner.resolve_command(step.cmd), cwd=root, env=env, timeout=step.timeout_sec, check=False
            )
        except subprocess.TimeoutExpired:
            print(f"[line_validate] step {step.name} timed out after {step.timeout_sec}s")
            return 124
        except OSError as exc:
            print(f"[line_validate] step {step.name} failed to launch: {exc}")
            return 127
        if proc.returncode != 0:
            print(f"[line_validate] step {step.name} failed with exit code {proc.returncode}")
            return proc.returncode
    print("[line_validate] all quick steps passed")
    return 0


def main(root: Path = REPO_ROOT) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config_path = root / "harness.config.json"
    if is_clean_tree(root):
        print("[line_validate] clean tree: running the official harness (--quick)", flush=True)
        return subprocess.call(
            [sys.executable, str(root / "core" / "harness" / "runner.py"), "--quick"], cwd=root
        )
    print("[line_validate] dirty tree: running the harness quick steps directly", flush=True)
    return run_steps_directly(root, config_path)


if __name__ == "__main__":
    sys.exit(main())
