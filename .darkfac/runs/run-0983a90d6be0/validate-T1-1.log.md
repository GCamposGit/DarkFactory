# development attempt log

- iteration: 1
- harness: codex
- model: -
- error_kind: -
- duration_s: 733.479
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (1 failed, 4841 passed, 38 skipped in 733.48s)

Test failures detected:
- tests/test_control_job_board.py:111 [AssertionError]: +      where stat = PosixPath('/tmp/pytest-of-darkfac/pytest-3/popen-gw0/test_sqlite_board_reads_withou0/control.db').stat
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 17154 chars total] ...
 [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
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
1 failed, 4841 passed, 38 skipped, 5 warnings in 706.13s (0:11:46)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 709.8s
[TEST_COUNT] count=4880
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"f9c5581917cddf64e556ad6d27f1b6d4c6824dc0","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4880,"passed_count":4841,"skipped_count":38,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/workspaces/darkfac/runs/run-0983a90d6be0/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 710.5, "host": "a81443ecdc45", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 0.0, "no_cache": false, "path": "local", "quick": true, "remote_attempts": [], "remote_lost_sec": 0, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 730.8, "tree_hash": "c669a1e105e3f8723e2559183be969b6cd9cff85", "ts": "2026-10-21T14:31:50Z", "worker_job": false}
```

## Remote dispatch lines (redacted)

```text
[line_validate] clean tree: running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)
[REMOTE] no remote worker used; running the suite locally on this host
```
