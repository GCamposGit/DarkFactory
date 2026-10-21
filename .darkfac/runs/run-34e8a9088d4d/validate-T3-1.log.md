# development attempt log

- iteration: 1
- harness: codex
- model: -
- error_kind: -
- duration_s: 1370.368
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (3 failed, 4837 passed, 37 skipped in 1370.37s)

Test failures detected:
- tests/test_remote_environment_isolation.py:31 [_]: Command '['/usr/local/bin/python', '-m', 'pytest', '-q', '--confcutdir', '/tmp/pytest-of-darkfac/pytest-37/popen-gw1/test_polluted_worker_environme0', '-p', 'tests.conftest', '/tmp/pytest-of-darkfac/pytest-37/popen-gw1/test_polluted_worker_environme0/test_environment.py']' timed out after 30 seconds
- tests/test_clock_bomb_guard.py:104 [AssertionError]: 30:00+00:00'")]
- tests/test_control_job_board.py:111 [AssertionError]: +      where stat = PosixPath('/tmp/pytest-of-darkfac/pytest-37/popen-gw0/test_sqlite_board_reads_withou0/control.db').stat
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 22822 chars total] ...
ED [1] tests/test_demand_id_allocator.py:110: Windows Git fallback
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:42: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [6] tests/test_watchdog_suite_lock_wait.py:48: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:63: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:76: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:84: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:95: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:176: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_watchdog_suite_lock_wait.py:181: USR-253 patch (watchdog suite_lock deadline extension) not applied yet
SKIPPED [1] tests/test_ticket_workspace.py:634: read-only attribute semantics of Windows
SKIPPED [1] tests/test_ticket_workspace_cwd.py:115: the cwd handle only blocks removal on Windows
FAILED tests/test_remote_environment_isolation.py::test_polluted_worker_environment_is_removed_before_child_tests_run
FAILED tests/test_clock_bomb_guard.py::test_no_unpinned_fixed_2026_dates_outside_the_exceptions_list
FAILED tests/test_control_job_board.py::test_sqlite_board_reads_without_writing
3 failed, 4837 passed, 37 skipped, 5 warnings in 747.65s (0:12:27)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 751.0s
[TEST_COUNT] count=4877
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"f19d49c9835529454e30ef18478e2c9c6ab6326d","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4877,"passed_count":4837,"skipped_count":37,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/tmp/line-validate-wt-34ra02_x/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 754.5, "host": "6c6dff9177c3", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 590.8, "no_cache": false, "path": "local", "quick": true, "remote_attempts": [], "remote_lost_sec": 0, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 1365.5, "tree_hash": "aad38c67afd8de9ac92366eebc49309e8b377b81", "ts": "2026-10-20T21:05:42Z", "worker_job": false}
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot f19d49c98355 checked out at /tmp/line-validate-wt-34ra02_x/tree; running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)
[REMOTE] no remote worker used; running the suite locally on this host
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```
