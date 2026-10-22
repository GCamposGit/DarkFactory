# development attempt log

- iteration: 1
- harness: codex
- model: -
- error_kind: -
- duration_s: 1568.829
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (2 failed, 4823 passed, 37 skipped in 1568.83s)

Test failures detected:
- tests/test_remote_environment_isolation.py:31 [_]: Command '['/usr/local/bin/python', '-m', 'pytest', '-q', '--confcutdir', '/tmp/pytest-of-darkfac/pytest-30/popen-gw1/test_polluted_worker_environme0', '-p', 'tests.conftest', '/tmp/pytest-of-darkfac/pytest-30/popen-gw1/test_polluted_worker_environme0/test_environment.py']' timed out after 30 seconds
- tests/test_control_job_board.py:111 [AssertionError]: +      where stat = PosixPath('/tmp/pytest-of-darkfac/pytest-30/popen-gw0/test_sqlite_board_reads_withou0/control.db').stat
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 23040 chars total] ...
1] tests/test_demand_id_allocator.py:110: Windows Git fallback
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
2 failed, 4823 passed, 37 skipped, 5 warnings in 848.90s (0:14:08)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 855.0s
[TEST_COUNT] count=4862
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"bdf98aa43832af1a289b1019b8e568ab311da29d","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4862,"passed_count":4823,"skipped_count":37,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/workspaces/darkfac/runs/run-c3b71384f7bd/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 858.1, "host": "36e2621fa4b3", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 245.5, "no_cache": false, "path": "local_after_remote", "quick": true, "remote_attempts": [{"elapsed_sec": 0.0, "outcome": "unstable", "worker_url": "http://100.77.130.5:8080"}, {"elapsed_sec": 0.0, "outcome": "unstable", "worker_url": "http://100.78.181.90:8080"}], "remote_lost_sec": 0, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 1561.7, "tree_hash": "af93850491b87fdd4e452d7ff15f963c55ec000f", "ts": "2026-10-22T17:40:20Z", "worker_job": false}
```

## Remote dispatch lines (redacted)

```text
[line_validate] clean tree: running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 not stable (1/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.77.130.5:8080 did not stay stable across consecutive /health checks; trying next option
[REMOTE] http://100.78.181.90:8080 not stable (1/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 not stable (0/3 consecutive /health checks): restarting count
[REMOTE] http://100.78.181.90:8080 did not stay stable across consecutive /health checks; trying next option
[REMOTE] no remote worker used; running the suite locally on this host
```
