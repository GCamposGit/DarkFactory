# HF-02-02 - contracts and process protocol

## Identity

- Ticket: `HF-02-02` (`user-demand`)
- Initial implementation owner: `/root/hf02_02_luna`
- Model: `gpt-5.6-luna`, reasoning effort `xhigh`
- Validation coordinator after the model quota failure: `/root`
- Source branch/worktree: `codex/hf-02-02-luna`, `C:\dev\DarkFac\.worktrees\hf-02-02-luna`
- Delivery branch/worktree: `codex/hf-02-02-delivery`, `C:\dev\DarkFac\.worktrees\hf-02-02-delivery`
- Delivery base SHA: `c592d1c88aba67d1944cf72e490749b2c37a3b20`
- Functional commits: `8c25d3d` (cherry-pick of `478d407`), `ddaac5e` (cherry-pick of `4249c57`)
- Functional delivery SHA: `ddaac5e`; `40c4259` is documentation-only evidence refresh
- State before remote delivery: `validated_pending_publish`

The implementation was produced in an independent clean clone because the shared
workspace had unrelated active changes. The final delivery checkout is based directly
on `origin/main` and contains only the HF-02-02 commit plus the separate Canaletto
scope correction required to restore clean-clone test discovery.

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
- Removed the shared test/report pair that imported the local-only `run_canaletto`
  module, keeping Canaletto outside the shared clone and harness as required by scope.
- PostgreSQL access is not required for this ticket and was not used.

## Files in the delivery diff

- Added: `spikes/__init__.py`
- Added: `spikes/runtime_choice/__init__.py`
- Added: `spikes/runtime_choice/contracts.py`
- Added: `tests/test_runtime_spike_contracts.py`
- Added: `.factory/reports/hf-02-02-report.md`
- Removed: `tests/test_ajustar_link_para_canalle.py`
- Removed: `.factory/reports/usr-14-canaletto-launcher-report.md`

## Validation

| Command | Result |
| --- | --- |
| `python core/harness/terminal_env.py --check` | exit 0; `[TERMINAL_ENV_PASS]` |
| `python -m pytest tests/test_runtime_spike_contracts.py -v` | exit 0; 13 passed in 0.15 s |
| `python core/harness/runner.py --quick` | exit 0; `[HARNESS_PASS]`, 451 collected, 449 passed, 2 skipped |
| `python -m pytest tests -v` | exit 0; 451 collected, 449 passed, 2 skipped in 63.07 s |
| `git diff --check` | exit 0 |
| independent public round-trip probe | exit 0; exact object round-trip passed |
| independent strict JSON probe | exit 1 as expected; boolean rejected for numeric field |
| independent decision-integrity probe | exit 1 as expected; self-selected runtime rejected |

The initial gate failure was reproduced before the correction: collection found 460
items but aborted on `tests/test_ajustar_link_para_canalle.py` importing the absent
local-only `run_canaletto`. The separate scope correction removed that shared test and
its local-only report. The final gate collected 451 core tests without the
Canaletto collection error.

## Adversarial review

Verdict: `no_actionable_findings_in_reviewed_scope`.

Reviewed the public exports, JSON round-trip boundary, strict boolean handling, secret
and reference filters, duplicate result identity, and the rule forbidding a comparison
from selecting a runtime. Limits: no DBOS/PostgreSQL integration was exercised because
those dependencies and access are explicitly outside HF-02-02.

## Agent and delivery state

The Luna implementation task ended with an account usage-limit error after producing
the implementation files. The coordinator preserved the isolated delta, performed the
Canaletto scope correction, created the clean delivery branch from `origin/main`, and
completed the mandatory gates. The delivery branch is clean after the evidence refresh;
the functional delivery SHA is `ddaac5e`; the report refresh is documentation-only and
does not alter the implementation or test behavior.

Remote delivery remains pending: push, PR creation, required checks/reviews, merge,
and remote-main reachability verification are still required before declaring HF-02-02
complete.
