# development attempt log

- iteration: 2
- harness: codex
- model: -
- error_kind: -
- duration_s: 1137.292
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (2 failed, 4822 passed, 23 skipped in 1137.29s)

Test failures detected:
- tests/test_safe_git_state.py [AssertionError]
- tests/test_stop_hook_artifacts.py:65 [AssertionError]: +  and   '' = CompletedProcess(args=['/usr/local/bin/python', '/tmp/line-validate-wt-8oo8yjir/tree/scripts/stop_hook_artifacts.py'], returncode=0, stdout='', stderr='').stderr
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 16930 chars total] ...
_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_postgres_real_integration.py:414: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_postgres_real_integration.py:448: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_remote_worker_process_tree.py:128: O worker de testes roda so no Windows (Desktop). Num Linux o neto de um runner que ja saiu e reparentado ao init antes do primeiro snapshot da arvore e deixa de ser descendente, entao o patch nao o encontra (USR-251).
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
SKIPPED [1] tests/test_demand_id_allocator.py:110: Windows Git fallback
SKIPPED [1] tests/test_ticket_workspace.py:677: read-only attribute semantics of Windows
FAILED tests/test_safe_git_state.py::test_no_shell_git_revision_path_commands_in_factory
FAILED tests/test_stop_hook_artifacts.py::test_allows_on_unexpected_failure
2 failed, 4822 passed, 23 skipped, 9 warnings in 1076.90s (0:17:56)
[STEP_FAILURE_LOG] /tmp/line-validate-wt-8oo8yjir/tree/.factory/test_logs/step-failures/20261102T085314Z-1793609594943722136-unit_and_integration_tests_parallel.log
[FAILED_TEST] FAILED tests/test_safe_git_state.py::test_no_shell_git_revision_path_commands_in_factory
[FAILED_TEST] FAILED tests/test_stop_hook_artifacts.py::test_allows_on_unexpected_failure
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 1081.4s
[TEST_COUNT] count=4847
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"f7db17a021994c44dfc3252776cc3990f0bd037c","config_hash":"e786c1a35812fcd11d02c51afba4e1704e8a28377a6a055ede3c8a49bf1bb84d","required_steps":["syntax_only","static_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_only","static_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_only","static_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":4847,"passed_count":4822,"skipped_count":23,"exit_codes":{"syntax_only":0,"static_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/tmp/line-validate-wt-8oo8yjir/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 1112.1, "host": "c73bfd84164c", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 0.0, "no_cache": false, "path": "local", "quick": true, "remote_attempts": [], "remote_lost_sec": 0, "schema": 1, "steps_timeout_sum_sec": 3540.0, "success": false, "total_sec": 1132.2, "tree_hash": "6fceee9c672820ec316723c130af996291f59980", "ts": "2026-11-02T08:53:14Z", "worker_job": false}
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot f7db17a02199 checked out at /tmp/line-validate-wt-8oo8yjir/tree; running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)
[REMOTE] no remote worker used; running the suite locally on this host
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```
