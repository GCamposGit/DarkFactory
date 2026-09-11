# HF-02-02 - contracts and process protocol

## Identity

- Ticket: `HF-02-02`
- Initial implementation owner: `/root/hf02_02_luna`
- Model: `gpt-5.6-luna`, reasoning effort `xhigh`
- Validation coordinator after the model quota failure: `/root`
- Branch: `codex/hf-02-02-luna`
- Isolated checkout: `C:\dev\DarkFac\.worktrees\hf-02-02-luna`
- Base SHA: `0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9`
- State: `blocked_validation`; implementation candidate complete, not remotely delivered

The implementation ran in an independent clean clone because the repository root had
active unrelated changes. No file in the shared root was modified by this ticket.

## Scope

- Added dependency-light `spikes` and `spikes.runtime_choice` packages without DBOS or
  PostgreSQL imports during common test collection.
- Added strict Pydantic v2 contracts for lab configuration, scenarios, capabilities,
  JSONL commands/events, results, comparisons and stable CLI exit codes.
- Added explicit evidence origin fields: `environment_ref`, `validation_mode`,
  `target_differences` and sanitized artifact references.
- Added public-path JSON round-trip tests, closed enum/extra-field tests, root path
  containment, duplicate identity rejection, secret/DSN rejection and distinct
  `pass`, `unsupported` and `blocked` status coverage.
- PostgreSQL access is not required for this ticket and was not used.

## Files

- `spikes/__init__.py`
- `spikes/runtime_choice/__init__.py`
- `spikes/runtime_choice/contracts.py`
- `tests/test_runtime_spike_contracts.py`
- `.factory/reports/hf-02-02-report.md`

## Validation

| Command | Result |
| --- | --- |
| `python -m pytest tests/test_runtime_spike_contracts.py -v` | exit 0; 13 passed |
| `python core/harness/runner.py --quick` | exit 1; syntax passed, suite collection blocked by pre-existing `tests/test_ajustar_link_para_canalle.py` importing local-only `run_canaletto` absent from a clean clone |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py --ignore=tests/test_ajustar_link_para_canalle.py` | exit 1; 457 passed, 2 skipped, one unrelated scale-latency assertion failed at 545.60 ms versus 500 ms |
| `python -m pytest tests/test_roadmap_scale.py::test_dense_graph_500_items_1500_relations_latency_budget -q` | exit 0; 1 passed in 0.15 s on immediate isolated retry |

The official repository gate remains red because its configuration collects a test
that imports a file intentionally excluded from the shared repository. The timing
failure was not reproducible in isolation. Neither failure originates in or is fixed
by HF-02-02; Canaletto and roadmap performance are outside this ticket's ownership.

## Agent and delivery state

The Luna implementation task ended with an account usage-limit error after producing
the four code/test files. The coordinator preserved the isolated delta and completed
the validation above. The clone's `origin` points to the dirty local checkout, so no
push was attempted. There is no PR, merge receipt or remote-main reachability evidence.

HF-02-02 must not be marked delivered until the clean-clone harness contract is
reconciled, the mandatory gates pass, and the selective commit is published, reviewed,
merged and verified against the remote `main`.
