from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.harness import runner
from core.harness.models import HarnessStepConfig


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            ["python", "-m", "pytest", "-n", "auto", "tests"],
            ["python", "-m", "pytest", "-n", "auto", "tests", "--maxprocesses=4"],
        ),
        (["pytest", "-n=auto", "tests"], ["pytest", "-n=auto", "tests", "--maxprocesses=4"]),
        (
            ["pytest", "-n", "auto", "--maxprocesses=2", "tests"],
            ["pytest", "-n", "auto", "--maxprocesses=2", "tests"],
        ),
        (["pytest", "-n", "6", "tests"], ["pytest", "-n", "6", "tests"]),
    ],
)
def test_windows_auto_xdist_is_capped_at_four_workers(
    command: list[str], expected: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner.os, "cpu_count", lambda: 16)

    assert runner.normalize_xdist_workers(command) == expected


def test_timeout_saves_and_prints_partial_pytest_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)

    partial = b"collecting ... collected 42 items\ntests/test_slow.py::test_wait "

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output=partial)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    step = HarnessStepConfig(
        name="unit_and_integration_tests_parallel",
        cmd="python -m pytest tests -q -n auto",
        quick=True,
        kind="test",
        timeout_sec=1,
    )

    execution = runner.run_step(step)

    captured = capsys.readouterr().out
    assert execution.returncode == 124
    assert "[STEP_FAIL] unit_and_integration_tests_parallel (timeout exceeded)" in captured
    assert "collected 42 items" in captured
    assert "[STEP_TIMEOUT_LOG]" in captured
    log_path = next((tmp_path / runner.STEP_TIMEOUT_LOG_DIR).glob("*.log"))
    saved = log_path.read_text(encoding="utf-8")
    assert "elapsed_seconds:" in saved
    assert "collected 42 items" in saved
    assert "test_slow.py::test_wait" in saved
