"""Browser-side task polling contract, exercised with Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


# O teste JS e deterministico (timers falsos, flush por setImmediate) e leva ~1 s;
# o tempo gasto e quase todo boot do node. Em runners Windows sob carga (suite
# paralela de ~4000 testes) o boot pode passar de 30 s, entao o limite do
# subprocess e 6x maior (partida a frio medida em 72 s). Continua finito: um travamento real ainda falha.
NODE_SUBPROCESS_TIMEOUT_SECONDS = 180


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
        timeout=NODE_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
