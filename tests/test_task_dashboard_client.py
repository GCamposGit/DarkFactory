"""Browser-side task polling contract, exercised with Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_task_dashboard_visible_polling_and_failure_cache() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed on this runner")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", str(root / "tests" / "js" / "task_dashboard.test.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
