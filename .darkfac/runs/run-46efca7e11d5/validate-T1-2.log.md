# development attempt log

- iteration: 2
- harness: claude
- model: sonnet
- error_kind: -
- duration_s: 443.825
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (1 failed, 3141 passed, 5 skipped in 443.82s)

Test failures detected:
- tests/test_run_ticket_routing.py:293 [AssertionError]: Use -v to get more diff
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 14559 chars total] ...
n_ci_merges
7.31s call     tests/line/test_stage_integration.py::test_no_workflow_merges_only_after_grace_without_sleeping
7.20s call     tests/test_speculative_racing.py::test_model_router_speculative_fields
6.90s call     tests/test_hf08_demand_intake_grill_bootstrap.py::test_cli_headless_hf08_commands
6.78s call     tests/test_run_ticket_delivery.py::test_delivery_in_fresh_process_prevents_stale_module_value_error
6.04s call     tests/line/test_stage_integration_base_red.py::test_the_job_iteration_is_the_conservative_fallback_without_a_counter
5.64s call     tests/test_dependencies_contract.py::test_production_imports_covered_by_requirements
5.62s call     tests/test_run_ticket.py::test_queue_only_registers_ticket_without_running_agent
5.59s call     tests/test_provider_integration.py::test_strict_architectural_decoupling_core_never_imports_hub
5.27s call     tests/test_run_ticket.py::test_queue_only_fails_when_queue_delivery_fails
5.01s call     tests/test_target_journey_rollback.py::test_workflow_contracts_collection_preflight_passes
4.97s call     tests/test_usage_monitor.py::test_independent_processes_serialize_usage_transactions
=========================== short test summary info ============================
SKIPPED [1] tests/line/test_grok_runner.py:537: opt-in live smoke test: set DARKFAC_LIVE_GROK_TEST=1 with the Grok Build CLI on PATH
SKIPPED [1] tests/test_node_sync.py:270: opt-in live node probe
SKIPPED [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
FAILED tests/test_run_ticket_routing.py::test_a_final_failure_prints_harness_model_kind_exit_code_duration_and_stderr_and_exits_nonzero
FAILED tests/test_run_ticket_routing.py::test_an_auto_routed_ticket_falls_back_to_the_next_healthy_harness
2 failed, 3141 passed, 5 skipped, 4 warnings in 431.67s (0:07:11)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 435.2s
[TEST_COUNT] count=3148
[HARNESS_RESULT] {"schema_version":"1","candidate_sha":"30a7478950be57fa3d5f0e7e124ba12bb94b8ec4","config_hash":"50ec7d2a443fb107451c52d2781efd8c5ef997cd8903d3098676385ae248dc4a","required_steps":["syntax_and_types","unit_and_integration_tests_parallel","unit_and_integration_tests_serial"],"started_steps":["syntax_and_types","unit_and_integration_tests_parallel"],"passed_steps":["syntax_and_types"],"failed_steps":["unit_and_integration_tests_parallel"],"discovered_count":3148,"passed_count":3141,"skipped_count":5,"exit_codes":{"syntax_and_types":0,"unit_and_integration_tests_parallel":1},"artifact_refs":["/tmp/line-validate-wt-i9n902z9/tree/harness.config.json"],"reused_from":null,"executed_on":null}
[HARNESS_FAIL]
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```

## Remote dispatch lines (redacted)

```text
[line_validate] dirty tree: validating a snapshot commit of the working tree
[line_validate] snapshot 30a7478950be checked out at /tmp/line-validate-wt-i9n902z9/tree; running the official harness (--quick)
[REMOTE] http://100.78.181.90:8080 unreachable (worker offline or port blocked)
[REMOTE] no remote worker used; running the suite locally on this host
[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)
```
