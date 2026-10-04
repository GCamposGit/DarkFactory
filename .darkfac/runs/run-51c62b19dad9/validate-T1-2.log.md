# development attempt log

- iteration: 2
- harness: codex
- model: -
- error_kind: -
- duration_s: 1186.281
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (2 failed, 3003 passed, 5 skipped in 1186.28s)

Test failures detected:
- tests/line/test_bounded_runner.py:102 [AssertionError]: +    where <function pid_exists at 0x736f819b9300> = psutil.pid_exists
- tests/line/test_routing.py:84 [AssertionError]: assert ('claude', 'sonnet') is None
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 13387 chars total] ...
leted_run_branch_is_recreated_and_reintegrates
5.36s call     tests/line/test_stage_integration.py::test_red_ci_feedback_reaches_development_then_green_ci_merges
5.33s call     tests/test_usage_monitor.py::test_independent_processes_serialize_usage_transactions
5.14s call     tests/test_suite_lock.py::test_two_real_processes_contend_for_the_single_slot
4.72s call     tests/test_hf08_demand_intake_grill_bootstrap.py::test_cli_headless_hf08_commands
4.67s call     tests/test_model_benchmark.py::test_model_router_recommend_integration
4.49s call     tests/test_public_autonomous_intake.py::test_cli_create_subprocess_autonomous_and_replay
4.33s call     tests/line/test_stage_integration_base_red.py::test_the_job_iteration_is_the_conservative_fallback_without_a_counter
4.02s call     tests/test_integration_handler.py::test_reconciliation_external_merge_blocked_when_checks_fail_or_stale
3.97s call     tests/test_speculative_racing.py::test_model_router_speculative_fields
3.91s call     tests/test_git_autonomy_ci_gate.py::test_only_shared_ci_module_invokes_pr_checks
3.80s call     tests/test_ai_account_monitor_e_moni.py::test_sync_usage_client_script_dry_run
3.77s call     tests/line/test_stage_integration.py::test_merge_conflict_without_route_uses_route_waiter_and_no_marker
3.76s call     tests/test_provider_integration.py::test_strict_architectural_decoupling_core_never_imports_hub
=========================== short test summary info ============================
SKIPPED [1] tests/line/test_grok_runner.py:537: opt-in live smoke test: set DARKFAC_LIVE_GROK_TEST=1 with the Grok Build CLI on PATH
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
SKIPPED [1] tests/test_node_sync.py:270: opt-in live node probe
SKIPPED [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
FAILED tests/line/test_bounded_runner.py::test_bounded_runner_terminates_grandchild_and_returns_under_15s
FAILED tests/line/test_routing.py::test_pick_unknown_quota_counts_as_ineligible_fail_closed
2 failed, 3003 passed, 5 skipped, 3 warnings in 589.11s (0:09:49)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 592.0s
[TEST_COUNT] count=3010
[ERROR] Candidate worktree is dirty; commit or use a clean checkout before running the official harness
[HARNESS_FAIL]
[line_validate] runner failed and left 4 dirty path(s) in the checkout:
[line_validate]   ?? "E:\\DarkFac\\Backups/auto-test/snp3t_auto-test_20261004_170129_c4a0a3.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/cron-cli-test/snp3t_cron-cli-test_20261004_170129_b1641b.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/fail-test/snp3t_fail-test_20261004_170129_a60092.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/proj-cli/snp3t_proj-cli_20261004_170129_bfc978.tar.gz.enc"
```
