"""Versioned registry of test files allowed to carry ``pytest.mark.serial`` (USR-263).

The official gate runs ``-m serial`` as a separate, single-process step with a 300 s limit
(``harness.config.json``: ``unit_and_integration_tests_serial``). Everything marked serial is
paid for in wall-clock time on one core, after the ~24 min parallel step; a file that does not
need shared state must stay in the parallel step instead. USR-257 was rejected three times
because ``tests/test_git_autonomy_ci_gate.py`` (60+ real-git tests, no shared state) sat there.

A file may only use ``pytest.mark.serial`` (decorator or ``pytestmark``) if it is listed below with
the concrete reason it cannot run under xdist. Adding a file here is a deliberate, reviewed act.
"""

from __future__ import annotations

SERIAL_ALLOWED: dict[str, str] = {
    "test_harness_affected.py": (
        "test_smoke_real_repo_json_is_valid_and_fast asserts an absolute latency budget on the real repo; "
        "CPU contention from xdist workers would make it flaky."
    ),
    "test_remote_dispatch.py": (
        "test_end_to_end_loopback_dispatch_real_server binds a real uvicorn server on a loopback port "
        "(live, opt-in); fixed port cannot be shared across workers."
    ),
    "test_roadmap_scale.py": (
        "test_dense_graph_500_items_1500_relations_latency_budget asserts a 500 ms compile latency budget "
        "that parallel CPU contention would break."
    ),
}
