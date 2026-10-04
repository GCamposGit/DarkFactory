# development attempt log

- iteration: 1
- harness: claude
- model: sonnet
- error_kind: -
- duration_s: 1268.1
- note: validate failed (exit 1)

## Output (redacted, first 2000 chars)

```text
verdict=FAILED (2 failed, 3001 passed, 5 skipped in 1268.10s)

Test failures detected:
- tests/line/test_bounded_runner.py:102 [AssertionError]: +    where <function pid_exists at 0x7718e4189300> = psutil.pid_exists
- tests/line/test_routing.py:84 [AssertionError]: assert ('claude', 'sonnet') is None
```

## Raw output tail (redacted, last 3000 chars)

```text
[truncated, 13357 chars total] ...
ntegration.py::test_red_ci_feedback_reaches_development_then_green_ci_merges
5.43s call     tests/line/test_stage_integration.py::test_no_workflow_merges_only_after_grace_without_sleeping
5.14s call     tests/test_suite_lock.py::test_two_real_processes_contend_for_the_single_slot
4.96s call     tests/test_hf08_demand_intake_grill_bootstrap.py::test_cli_headless_hf08_commands
4.67s call     tests/test_git_autonomy_ci_gate.py::test_only_shared_ci_module_invokes_pr_checks
4.65s call     tests/test_public_autonomous_intake.py::test_cli_create_subprocess_autonomous_and_replay
4.63s call     tests/line/test_stage_integration_base_red.py::test_the_job_iteration_is_the_conservative_fallback_without_a_counter
4.40s call     tests/test_dependencies_contract.py::test_production_imports_covered_by_requirements
4.26s call     tests/test_ai_account_monitor_e_moni.py::test_usage_sync_endpoint_ingests_and_updates_reports
4.16s call     tests/test_model_benchmark.py::test_model_router_recommend_integration
4.13s call     tests/test_speculative_racing.py::test_model_router_speculative_fields
4.11s call     tests/test_run_ticket.py::test_queue_only_fails_when_queue_delivery_fails
3.88s call     tests/test_integration_handler.py::test_reconciliation_external_merge_blocked_when_checks_fail_or_stale
3.85s call     tests/test_usage_monitor.py::test_independent_processes_serialize_usage_transactions
=========================== short test summary info ============================
SKIPPED [1] tests/line/test_grok_runner.py:537: opt-in live smoke test: set DARKFAC_LIVE_GROK_TEST=1 with the Grok Build CLI on PATH
SKIPPED [1] tests/test_audio_transcriber.py:147: ensaio live desabilitado; use --run-live-audio explicitamente
SKIPPED [1] tests/test_bug_em_inicializa_o_do_te.py:117: PowerShell script test applicable on Windows
SKIPPED [1] tests/test_node_sync.py:270: opt-in live node probe
SKIPPED [1] tests/test_reusable_pilots.py:454: Historical experiment results.json not found
FAILED tests/line/test_bounded_runner.py::test_bounded_runner_terminates_grandchild_and_returns_under_15s
FAILED tests/line/test_routing.py::test_pick_unknown_quota_counts_as_ineligible_fail_closed
2 failed, 3001 passed, 5 skipped, 3 warnings in 576.81s (0:09:36)
[STEP_FAIL] unit_and_integration_tests_parallel (exit code: 1)
[STEP_TIME] unit_and_integration_tests_parallel 579.8s
[TEST_COUNT] count=3008
[ERROR] Candidate worktree is dirty; commit or use a clean checkout before running the official harness
[HARNESS_FAIL]
[line_validate] runner failed and left 4 dirty path(s) in the checkout:
[line_validate]   ?? "E:\\DarkFac\\Backups/auto-test/snp3t_auto-test_20261004_162629_210513.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/cron-cli-test/snp3t_cron-cli-test_20261004_162629_240e27.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/fail-test/snp3t_fail-test_20261004_162629_20cbba.tar.gz.enc"
[line_validate]   ?? "E:\\DarkFac\\Backups/proj-cli/snp3t_proj-cli_20261004_162629_edf0f4.tar.gz.enc"
```
