# development attempt log

- iteration: 3
- harness: codex
- model: -
- error_kind: -
- duration_s: 1726.55
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (1 failed, 4983 passed, 25 skipped in 1726.55s)

Test failures detected:
- tests/test_model_id_ratchet_usr185.py:38 [AssertionError]: 2 literal model ids (baseline 0). Use an alias via core.models....line 504. Use an alias via core.models.registry (resolve/id_of/gateway_id), or mark a wire id with 'census: wire-id'."]
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 17139 chars total] ...
ostgres_real_integration.py:389: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_postgres_real_integration.py:402: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_postgres_real_integration.py:414: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_postgres_real_integration.py:448: DARKFAC_TEST_POSTGRES_DSN not set: real-PostgreSQL integration tests are opt-in
SKIPPED [1] tests/test_remote_worker_process_tree.py:128: O worker de testes roda so no Windows (Desktop). Num Linux o neto de um runner que ja saiu e reparentado ao init antes do primeiro snapshot da arvore e deixa de ser descendente, entao o patch nao o encontra (USR-251).
SKIPPED [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
SKIPPED [1] tests/test_demand_id_allocator.py:110: Windows Git fallback
SKIPPED [1] tests/test_ticket_workspace.py:634: read-only attribute semantics of Windows
SKIPPED [1] tests/test_ticket_workspace_cwd.py:115: the cwd handle only blocks removal on Windows
FAILED tests/test_model_id_ratchet_usr185.py::test_literal_model_ids_do_not_exceed_baseline
1 failed, 4983 passed, 25 skipped, 9 warnings in 805.29s (0:13:25)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 808.7s
[TEST_COUNT] count=5009
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"e1b3f87c914dbebb815ca9b025c82272c80ad98d","config_hash":"5fbdfdca494daf34f2b15dcffc07c5df48933fd0135b57d5a469775621fb14ac","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":5009,"passed_count":4983,"skipped_count":25,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/tmp/line-validate-wt-f6x55fcg/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[RUN_TIMING] {"exec_sec": 811.4, "host": "f53f0dd8e1d1", "local_only": false, "lock_timed_out": false, "lock_wait_sec": 0.0, "no_cache": false, "path": "local_after_remote", "quick": true, "remote_attempts": [{"elapsed_sec": 900.8, "outcome": "busy_timeout", "worker_url": "http://100.78.181.90:8080"}], "remote_lost_sec": 900.8, "schema": 1, "steps_timeout_sum_sec": 3420.0, "success": false, "total_sec": 1723.3, "tree_hash": "10d836decad6a5d08ca52b045c6c0e5728df89c1", "ts": "2026-10-22T11:07:01Z", "worker_job": false}
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot e1b3f87c914d checked out at /tmp/line-validate-wt-f6x55fcg/tree; running the official harness (--quick)
[REMOTE] http://100.77.130.5:8080 unreachable (worker offline or port blocked)
[REMOTE] http://100.78.181.90:8080 still busy after 900s (or vanished)
[REMOTE] no remote worker used; running the suite locally on this host
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```
