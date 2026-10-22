# development attempt log

- iteration: 1
- harness: codex
- model: -
- error_kind: -
- duration_s: 1646.572
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (1 failed, 4845 passed, 37 skipped in 1646.57s)

Test failures detected:
- tests/test_control_job_board.py:111 [AssertionError]: +      where stat = PosixPath('/tmp/pytest-of-darkfac/pytest-2/popen-gw0/test_sqlite_board_reads_withou0/control.db').stat
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 17295 chars total] ...
ente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
SKIPPED [1] tests/test_demand_id_allocator.py:110: Windows Git fallback
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
FAILED tests/test_control_job_board.py::test_sqlite_board_reads_without_writing
1 failed, 4845 passed, 37 skipped, 5 warnings in 723.39s (0:12:03)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 726.2s
[TEST_COUNT] count=4883
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"e8a9c5afeaaf654bc45758e9e65dbbc3afb4004c","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4883,"passed_count":4845,"skipped_count":37,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/tmp/line-validate-wt-_xcrudd1/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 728.5, "host": "12dbcaf806e8", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 0.0, "no_cache": false, "path": "local_after_remote", "quick": true, "remote_attempts": [{"elapsed_sec": 900.8, "outcome": "busy_timeout", "worker_url": "http://100.78.181.90:8080"}], "remote_lost_sec": 900.8, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 1643.3, "tree_hash": "854161fb90fee2a0e46dfee298145599957affc6", "ts": "2026-10-22T21:14:50Z", "worker_job": false}
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot e8a9c5afeaaf checked out at /tmp/line-validate-wt-_xcrudd1/tree; running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 still busy after 900s (or vanished)
[REMOTE] no remote worker used; running the suite locally on this host
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```
