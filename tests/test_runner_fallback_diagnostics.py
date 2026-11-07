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


def test_windows_runner_worker_environment_uses_the_same_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.os, "name", "nt")

    assert runner.bounded_xdist_worker_count(7) == 4
    assert runner.bounded_xdist_worker_count(2) == 2


def test_non_windows_runner_worker_environment_keeps_detected_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.os, "name", "posix")

    assert runner.bounded_xdist_worker_count(7) == 7


def test_run_step_caps_windows_gate_workers_before_spawning_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from core.line import gate_ram

    monkeypatch.setattr(runner.os, "name", "nt")
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "_xdist_available", lambda: True)
    monkeypatch.setattr(
        gate_ram,
        "wait_for_gate_ram",
        lambda **_kwargs: gate_ram.RamWaitResult(True, gate_ram.RamReading(16.0, 7), 0.0),
    )
    captured_command: list[str] | None = None
    captured_env: dict[str, str] | None = None

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_command, captured_env
        captured_command = command
        captured_env = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "1 passed in 0.01s\n")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    step = HarnessStepConfig(
        name="unit_and_integration_tests_parallel",
        cmd="python -m pytest tests -q -n auto",
        quick=True,
        kind="test",
        timeout_sec=30,
    )

    result = runner.run_step(step)

    assert result.returncode == 0
    assert captured_env is not None
    assert captured_env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"
    assert captured_command is not None and "--maxprocesses=4" in captured_command
    assert "[GATE_CAPACITY] workers=4" in capsys.readouterr().out


def test_timeout_saves_and_prints_partial_pytest_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    from core.line import gate_ram
    monkeypatch.setattr(gate_ram, "measure_workers", lambda: gate_ram.RamReading(4.1, 1))

    partial = b"collecting ... collected 42 items\ntests/test_slow.py::test_wait "

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 2  # same suite, one worker gets both calibrated shares
        assert kwargs["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "1"
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output=partial)

    monkeypatch.setattr(runner.subprocess, "run", timeout)
    step = HarnessStepConfig(
        name="unit_and_integration_tests_parallel",
        cmd="python -m pytest tests -q -n auto",
        quick=True,
        kind="test",
        timeout_sec=1,
    )

    from core.harness import remote_worker
    from core.harness.models import HarnessConfig
    config_path = tmp_path / "harness.config.json"
    config_path.write_text(HarnessConfig(steps=[step]).model_dump_json(), encoding="utf-8")
    assert remote_worker._job_timeout_sec(tmp_path, quick=True, include_holdout=False) == (
        2 + remote_worker.JOB_TIMEOUT_BUFFER_SEC
    )
    monkeypatch.setattr(gate_ram, "measure_workers", lambda: gate_ram.RamReading(8.1, 3))
    assert runner.effective_step_timeout(step, workers=3) == 1
    monkeypatch.setattr(gate_ram, "measure_workers", lambda: gate_ram.RamReading(4.1, 1))
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
    assert "timeout_sec=2" in captured
    monkeypatch.setattr(gate_ram, "measure_workers", lambda: gate_ram.RamReading(2.1, 0))
    assert runner.run_step(step).returncode == 75  # no subprocess call and no duplicated RAM predicate
