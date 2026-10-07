# development attempt log

- iteration: 2
- harness: claude
- model: sonnet
- error_kind: -
- duration_s: 889.42
- note: validate passed (exit 0)

## Output (redacted, first 2000 chars)

```text
verdict=PASSED (3144 passed, 5 skipped in 889.42s)

All tests passed successfully.
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 10581 chars total] ...
very_in_fresh_process_prevents_stale_module_value_error
5.68s call     tests/line/test_stage_integration_base_red.py::test_the_job_iteration_is_the_conservative_fallback_without_a_counter
5.59s call     tests/test_run_ticket.py::test_queue_only_registers_ticket_without_running_agent
=========================== short test summary info ============================
SKIPPED [1] tests/line/test_grok_runner.py:537: opt-in live smoke test: set DARKFAC_LIVE_GROK_TEST=1 with the Grok Build CLI on PATH
SKIPPED [1] tests/test_node_sync.py:270: opt-in live node probe
SKIPPED [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
3144 passed, 5 skipped, 4 warnings in 430.71s (0:07:10)
[STEP_PASS] unit_and_integration_tests_parallel
[STEP_TIME] unit_and_integration_tests_parallel 434.2s
[STEP_START] unit_and_integration_tests_serial
[WORKERS] allowed 2 workers (override PYTEST_XDIST_AUTO_NUM_WORKERS=2; available RAM: 0.8 GiB)
........................................................s.               [100%]
=============================== warnings summary ===============================
../../../usr/local/lib/python3.12/site-packages/fastapi/testclient.py:1
  /usr/local/lib/python3.12/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
SKIPPED [1] tests/test_remote_dispatch.py:639: ensaio live desabilitado; use --run-live-audio explicitamente
57 passed, 1 skipped, 3149 deselected, 1 warning in 59.61s
[STEP_PASS] unit_and_integration_tests_serial
[STEP_TIME] unit_and_integration_tests_serial 63.4s
[TEST_COUNT] count=3207
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"1e904b669603e550d6c2c97d64da019b956e5f0a","config_hash":"50ec7d2a443fb107451c52d2781efd8c5ef997cd8903d3098676385ae248dc4a","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"passed_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"failed_steps":[],"discovered_count":3207,"passed_count":3201,"skipped_count":6,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":0,"unit_and_integration_tests_serial":0},"artifact_refs":["/tmp/line-validate-wt-2qo62avn/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_PASS]
[HUB] Benchmark refresh triggered → https://darkhub.ggcampos.com (200)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot 1e904b669603 checked out at /tmp/line-validate-wt-2qo62avn/tree; running the official harness (--quick)
[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)
[REMOTE] no remote worker used; running the suite locally on this host
```
