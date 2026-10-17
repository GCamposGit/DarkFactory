"""Partial stdout/stderr survives a focal pre-check timeout (USR-249).

`TestSubagentEngine.execute_local` (core/harness/test_subagent.py) runs pytest with
`subprocess.run(..., text=True, timeout=...)`. On a timeout, CPython raises `TimeoutExpired` with the
RAW BYTES buffered so far on `.stdout`/`.stderr` -- the text decoding normally happens inside
`Popen.communicate()`, which never runs to completion when the call times out. The pre-patch code
guarded with `isinstance(exc.stdout, str)` and silently discarded the bytes, so a frozen/contended
machine (the USR-239 incident: a 3000s gate step and a 600s focal pre-check that both produced an empty,
21-byte "[TIMEOUT EXPIRED]" log) left no partial output at all -- no way to tell whether pytest hung on
collection, on one slow test, or never started.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from core.harness.test_subagent import TestExecutionInstruction, TestSubagentEngine


def test_timeout_with_bytes_partial_output_is_decoded_not_discarded(tmp_path: Path) -> None:
    """The bug: `exc.stdout`/`exc.stderr` arrive as bytes on a real timeout; the old
    `isinstance(..., str)` guard turned that into an empty string instead of decoding it."""
    engine = TestSubagentEngine(project_root=tmp_path)
    instruction = TestExecutionInstruction(target="tests/", timeout_seconds=1)

    partial_stdout = b"collecting ... collected 42 items\ntests/test_slow.py::test_one "
    partial_stderr = b"Warning: worker under memory pressure\n"

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output=partial_stdout, stderr=partial_stderr)

    with patch("core.harness.test_subagent.subprocess.run", side_effect=fake_run):
        report = engine.execute_local(instruction)

    assert report.exit_code == 124
    assert report.success is False
    raw_log = Path(report.raw_log_path).read_text(encoding="utf-8")
    assert "collected 42 items" in raw_log, "partial stdout must survive the timeout, not be discarded"
    assert "worker under memory pressure" in raw_log, "partial stderr must survive the timeout too"
    assert "[TIMEOUT EXPIRED]" in raw_log


def test_timeout_with_str_partial_output_still_works(tmp_path: Path) -> None:
    """Control: when `exc.stdout`/`.stderr` already come back as `str` (e.g. a mocked subprocess in
    another test), behavior is unchanged -- this is not a regression for the common/mocked case."""
    engine = TestSubagentEngine(project_root=tmp_path)
    instruction = TestExecutionInstruction(target="tests/", timeout_seconds=1)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="partial text stdout", stderr="partial text stderr")

    with patch("core.harness.test_subagent.subprocess.run", side_effect=fake_run):
        report = engine.execute_local(instruction)

    assert report.exit_code == 124
    raw_log = Path(report.raw_log_path).read_text(encoding="utf-8")
    assert "partial text stdout" in raw_log
    assert "partial text stderr" in raw_log


def test_timeout_with_none_partial_output_yields_empty_string(tmp_path: Path) -> None:
    """Control: no output captured at all (e.g. the process never wrote anything) must not crash."""
    engine = TestSubagentEngine(project_root=tmp_path)
    instruction = TestExecutionInstruction(target="tests/", timeout_seconds=1)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output=None, stderr=None)

    with patch("core.harness.test_subagent.subprocess.run", side_effect=fake_run):
        report = engine.execute_local(instruction)

    assert report.exit_code == 124
    raw_log = Path(report.raw_log_path).read_text(encoding="utf-8")
    assert "[TIMEOUT EXPIRED]" in raw_log
