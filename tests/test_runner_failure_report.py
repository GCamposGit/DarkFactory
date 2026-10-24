from __future__ import annotations

from pathlib import Path

import pytest

from core.harness import runner

OUTPUT = """\
=================================== FAILURES ===================================
____________________________ test_alpha ____________________________
    assert 1 == 2
E   assert 1 == 2
=========================== short test summary info ============================
FAILED tests/test_x.py::test_alpha - assert 1 == 2
ERROR tests/test_y.py::test_beta - OSError: boom
== 1 failed, 1 error, 5 passed in 1.00s ==
"""


def test_extract_failure_report_keeps_ids_and_traceback() -> None:
    report = runner.extract_failure_report(OUTPUT)
    assert "FAILED tests/test_x.py::test_alpha" in report
    assert "ERROR tests/test_y.py::test_beta" in report
    assert "assert 1 == 2" in report


def test_extract_failure_report_falls_back_to_tail() -> None:
    assert "no FAILED/ERROR" in runner.extract_failure_report("boom\nbang\n")


def test_record_step_failure_writes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runner, "PROJECT_ROOT", tmp_path)
    step = runner.HarnessStepConfig(name="unit", cmd="python -m pytest", timeout_sec=10)
    runner._record_step_failure(step, OUTPUT, 1)
    files = list((tmp_path / runner.STEP_FAILURE_LOG_DIR).glob("*.log"))
    assert len(files) == 1
    assert "tests/test_x.py::test_alpha" in files[0].read_text(encoding="utf-8")
    assert "[FAILED_TEST] FAILED tests/test_x.py::test_alpha" in capsys.readouterr().out
