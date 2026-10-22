# development attempt log

- iteration: 1
- harness: codex
- model: -
- error_kind: -
- duration_s: 1768.153
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (2 failed, 4827 passed, 37 skipped in 1768.15s)

Test failures detected:
- tests/test_remote_environment_isolation.py:31 [_]: Command '['/usr/local/bin/python', '-m', 'pytest', '-q', '--confcutdir', '/tmp/pytest-of-darkfac/pytest-38/popen-gw1/test_polluted_worker_environme0', '-p', 'tests.conftest', '/tmp/pytest-of-darkfac/pytest-38/popen-gw1/test_polluted_worker_environme0/test_environment.py']' timed out after 30 seconds
- tests/test_control_job_board.py:111 [AssertionError]: +      where stat = PosixPath('/tmp/pytest-of-darkfac/pytest-38/popen-gw0/test_sqlite_board_reads_withou0/control.db').stat
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 20925 chars total] ...
icializa_o_do_te.py:117: PowerShell script test applicable on Windows
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
FAILED tests/test_remote_environment_isolation.py::test_polluted_worker_environment_is_removed_before_child_tests_run
FAILED tests/test_control_job_board.py::test_sqlite_board_reads_without_writing
2 failed, 4827 passed, 37 skipped, 5 warnings in 846.64s (0:14:06)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 851.4s
[TEST_COUNT] count=4866
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"e562b65a394df9a8afd23d0f1e62db89b23d27ee","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4866,"passed_count":4827,"skipped_count":37,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/workspaces/darkfac/runs/run-294bd6f77095/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 854.2, "host": "36e2621fa4b3", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 0.0, "no_cache": false, "path": "local_after_remote", "quick": true, "remote_attempts": [{"elapsed_sec": 900.6, "outcome": "busy_timeout", "worker_url": "http://100.78.181.90:8080"}], "remote_lost_sec": 900.6, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 1765.7, "tree_hash": "eaa8af5e9bfa2a39918cdf02788696d5d9fba1d9", "ts": "2026-10-22T19:02:08Z", "worker_job": false}
```

## Remote dispatch lines (redacted)

```text
[line_validate] clean tree: running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 still busy after 900s (or vanished)
[REMOTE] no remote worker used; running the suite locally on this host
```
