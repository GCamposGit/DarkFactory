"""`diagnostics.validation_failure_refs`: what a failed validate run says on the live board (USR-62 pilot)."""

from __future__ import annotations

from core.line import diagnostics

_OUTPUT = """\
=========================== short test summary info ============================
SKIPPED [1] tests/test_a.py:1: opt-in
FAILED tests/test_run_ticket_routing.py::test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness
FAILED tests/test_executor_contract.py::test_process_sandbox_kills_descendant_after_leader_exits - OSError: [Errno 2]
ERROR tests/test_collect.py::test_import
FAILED tests/test_run_ticket_routing.py::test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness
2 failed, 3141 passed, 5 skipped in 450.79s (0:07:30)
"""


def test_failed_test_ids_are_distinct_ordered_and_skip_non_failures() -> None:
    assert diagnostics.failed_test_ids(_OUTPUT) == [
        "tests/test_run_ticket_routing.py::test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness",
        "tests/test_executor_contract.py::test_process_sandbox_kills_descendant_after_leader_exits",
        "tests/test_collect.py::test_import",
    ]


def test_refs_carry_the_tests_the_exit_code_and_the_log_name() -> None:
    refs = diagnostics.validation_failure_refs(_OUTPUT, exit_code=1, log_name="validate-T1-3.log.md")

    assert refs[:3] == [
        "validate_failed:tests/test_run_ticket_routing.py::test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness",
        "validate_failed:tests/test_executor_contract.py::test_process_sandbox_kills_descendant_after_leader_exits",
        "validate_failed:tests/test_collect.py::test_import",
    ]
    assert refs[3:] == ["validate_exit:1", "validate_log:validate-T1-3.log.md"]


def test_refs_are_capped_with_a_more_marker_so_the_board_never_overflows() -> None:
    output = "\n".join(f"FAILED tests/test_many.py::test_{i}" for i in range(20))

    refs = diagnostics.validation_failure_refs(output, exit_code=1, log_name="validate-T1-1.log.md")

    assert refs[: diagnostics.MAX_FAILURE_TEST_REFS] == [
        f"validate_failed:tests/test_many.py::test_{i}" for i in range(diagnostics.MAX_FAILURE_TEST_REFS)
    ]
    assert refs[diagnostics.MAX_FAILURE_TEST_REFS] == f"validate_failed:+{20 - diagnostics.MAX_FAILURE_TEST_REFS} more"
    assert len(refs) <= 12  # core.workflow.line_live.MAX_EVIDENCE_REFS


def test_output_without_pytest_failures_still_names_the_exit_code() -> None:
    refs = diagnostics.validation_failure_refs("[ERROR] Candidate worktree is dirty", exit_code=1)

    assert refs == ["validate_exit:1"]


def test_secrets_never_reach_the_refs() -> None:
    secret = "sk-ant-" + "A" * 24

    refs = diagnostics.validation_failure_refs(f"FAILED tests/test_x.py::test_{secret}", exit_code=1)

    assert all(secret not in ref for ref in refs)
