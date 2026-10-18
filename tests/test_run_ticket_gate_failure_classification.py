"""Gate-failure classification that stops pointless agent reinvocations (USR-308).

Two real failures on 18/10/2026: (a) Grokbox killed the tests step with SIGTERM after 0.9 s
(`discovered_count=0`, exit -15) and the Claude agent was called to "fix" an untested candidate;
(b) the gate of USR-271 failed only in `tests/test_clock_bomb_guard.py` because of a file inherited from
main, and the self-correction edited that unrelated test to make it pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import run_ticket

# Real HarnessResult of case (a) (candidate/config fields trimmed).
GROKBOX_RESULT = {
    "required_steps": ["syntax_and_types", "unit_and_integration_tests_parallel"],
    "started_steps": ["syntax_and_types", "unit_and_integration_tests_parallel"],
    "passed_steps": ["syntax_and_types"],
    "failed_steps": ["unit_and_integration_tests_parallel"],
    "discovered_count": 0,
    "passed_count": 0,
    "skipped_count": 0,
    "exit_codes": {"syntax_and_types": 0, "unit_and_integration_tests_parallel": -15},
}

CLOCK_BOMB_FAILED = (
    "FAILED tests/test_clock_bomb_guard.py::"
    "test_no_unpinned_fixed_2026_dates_outside_the_exceptions_list - AssertionError"
)


def _result_output(payload: dict) -> str:
    return f"[TEST_COUNT] count={payload['discovered_count']}\n[HARNESS_RESULT] {json.dumps(payload)}\n[HARNESS_FAIL]\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_sigterm_with_zero_tests_is_infra() -> None:
    assert run_ticket._gate_failure_is_infra_no_tests(_result_output(GROKBOX_RESULT)) is True


def test_zero_tests_with_positive_exit_is_a_real_failure() -> None:
    payload = {**GROKBOX_RESULT, "exit_codes": {"syntax_and_types": 1}, "failed_steps": ["syntax_and_types"]}
    assert run_ticket._gate_failure_is_infra_no_tests(_result_output(payload)) is False


def test_tests_step_exit_zero_without_tests_is_infra() -> None:
    payload = {**GROKBOX_RESULT, "exit_codes": {"syntax_and_types": 0, "unit_and_integration_tests_parallel": 0}}
    assert run_ticket._gate_failure_is_infra_no_tests(_result_output(payload)) is True


def test_discovered_tests_failing_is_not_infra() -> None:
    payload = {
        **GROKBOX_RESULT,
        "discovered_count": 120,
        "exit_codes": {"syntax_and_types": 0, "unit_and_integration_tests_parallel": 1},
    }
    assert run_ticket._gate_failure_is_infra_no_tests(_result_output(payload)) is False


def test_missing_or_garbled_result_is_not_infra() -> None:
    assert run_ticket._gate_failure_is_infra_no_tests("[HARNESS_FAIL]") is False
    assert run_ticket._gate_failure_is_infra_no_tests("[HARNESS_RESULT] {not json") is False


def test_inherited_failure_outside_the_diff_is_base_red(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "def test_x():\n    pass\n")
    diff = ["core/line/other_feature.py", "tests/test_other_feature.py"]
    assert run_ticket._gate_failure_is_base_red(CLOCK_BOMB_FAILED, diff, tmp_path) is True


def test_failed_test_file_in_the_diff_is_a_real_failure(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "def test_x():\n    pass\n")
    diff = ["tests/test_clock_bomb_guard.py"]
    assert run_ticket._gate_failure_is_base_red(CLOCK_BOMB_FAILED, diff, tmp_path) is False


def test_failed_test_importing_a_changed_module_is_a_real_failure(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "from core.line import other_feature\n")
    diff = ["core/line/other_feature.py"]
    assert run_ticket._gate_failure_is_base_red(CLOCK_BOMB_FAILED, diff, tmp_path) is False


def test_undeterminable_failures_keep_reinvoking(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "def test_x():\n    pass\n")
    diff = ["core/line/other_feature.py"]
    # No FAILED lines at all (e.g. syntax error), or a line without a parseable file, or a missing file.
    assert run_ticket._gate_failure_is_base_red("SyntaxError: invalid syntax", diff, tmp_path) is False
    assert run_ticket._gate_failure_is_base_red("FAILED some-weird-id", diff, tmp_path) is False
    missing = "FAILED tests/test_gone.py::test_a"
    assert run_ticket._gate_failure_is_base_red(missing, diff, tmp_path) is False
    assert run_ticket._gate_failure_is_base_red(CLOCK_BOMB_FAILED, [], tmp_path) is False


def test_global_test_config_in_the_diff_is_never_base_red(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "def test_x():\n    pass\n")
    assert run_ticket._gate_failure_is_base_red(CLOCK_BOMB_FAILED, ["tests/conftest.py"], tmp_path) is False


def test_mixed_failures_with_one_in_the_diff_are_real(tmp_path: Path) -> None:
    _write(tmp_path, "tests/test_clock_bomb_guard.py", "def test_x():\n    pass\n")
    _write(tmp_path, "tests/test_mine.py", "def test_y():\n    pass\n")
    output = CLOCK_BOMB_FAILED + "\nFAILED tests/test_mine.py::test_y - assert 0\n"
    assert run_ticket._gate_failure_is_base_red(output, ["tests/test_mine.py"], tmp_path) is False
