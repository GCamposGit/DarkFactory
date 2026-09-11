# HF Wave 1 — Luna implementation report

## Identity and scope

- Owner/implementer: `/root/luna_wave1` (GPT-5.6 Luna, xhigh).
- Branch: `codex/hf-remediation-wave1-luna`.
- HEAD/base observed: `0f57e9c0cb3cf1a5d02d8e934c162617ebe71db9`.
- Approved handoff: `docs/handoffs/HF-CR-WAVE1-LUNA-2026-09-09.md`, SHA256 `b9f600ee26b3caf8477c138e04248825c6163e4124ef16f4b5c1fa77d6a3e929`.
- Product implementation/test paths limited to:
  `core/workflow/runtime.py`, `tests/test_workflow_runtime.py`,
  `spikes/runtime_choice/contracts.py`, `tests/test_runtime_spike_contracts.py`,
  `tests/test_runtime_spike_native.py`.
- `driver.py`, readiness, other contracts/tests, skills, Canaletto, and other work were left unchanged.

## Implemented changes

CR-09 now rejects local registration and transitions to `DELIVERED` before persistence, with the stable sanitized domain error `LOCAL_RUNTIME_DELIVERY_FORBIDDEN`. Reloading a legacy stored `DELIVERED` row is rejected before constructing a runtime record. Tests verify that rejected operations do not create a run, mutate state, or enqueue an outbox effect, including an independent SQLite inspection.

CR-01 preserves the Python API contract for `root_dir` (`Path` for a `Path` input and compatible `str` input) while making JSON validation return a serializable string. Invalid JSON roots (`bool`, object, and `null`) now become validation errors and the real CLI emits only `CONFIG_INVALID`. The subprocess test derives its project root from `__file__`, keeps stdin open until terminal state, checks protocol state/effect evidence without assuming event order, joins the reader before closing pipes, and shuts down only after terminal completion. `driver.py` was not changed.

## Validation evidence

| Unit | Phase/command | Result | Evidence |
| --- | --- | --- | --- |
| CR-09 | Red focused | 2 failed, 11 deselected; exit 1 | `cr09-red.log` (summary of the recovered red phase) |
| CR-09 | Green focused | 2 passed, 11 deselected; exit 0 | `cr09-green.log` |
| CR-09 | Focal `test_workflow_runtime.py` | 13 passed; exit 0 | `cr09-focal.log` |
| CR-09 | Mandatory harness rerun | 503 collected; 501 passed, 2 skipped; exit 0; `[HARNESS_PASS]` | `cr09-harness-rerun.raw.log`, summarized by `cr09-harness.log` |
| CR-01 | Red focused | 2 failed, 8 deselected; exit 1 | `cr01-red.log` (summary of the recovered red phase) |
| CR-01 | Green focused | 2 passed, 8 deselected; exit 0 | `cr01-green.log` |
| CR-01 | Focal contracts/native | 13 passed; exit 0 | `cr01-focal.log` |
| Wave 1 | Final mandatory harness | 508 collected; 506 passed, 2 skipped; exit 0; `[HARNESS_PASS]` | `cr01-harness.raw.log` |
| Wave 1 | Separate full pytest validation | 508 collected; 506 passed, 2 skipped; exit 0 | `cr01-pytest-suite.raw.log` |

The first CR-09 harness attempt in this resumed session reached the configured supervisor timeout and exited 124; it was a new execution, not a recovered receipt. No timeout or harness setting was changed. The subsequent rerun completed successfully and its raw output is preserved. The red/green records for the resumed work are compact summaries rather than raw receipts; they are reported with that limitation and are not presented as newly reconstructed transcripts.

The separate suite run after the implementer gates is the coordinator's joint validation of the wave. It is recorded above as the final joint result and is not claimed as a CR-09-only command by this implementer.

## Delta and provenance

The five implementation/test files were inherited as untracked baseline files. Therefore ordinary `git diff` does not represent their complete change. The coordinator captured the reviewed delta in:

- `.factory/reviews/hf-wave1-luna/reviewed-delta.json`
- `.factory/reviews/hf-wave1-luna/wave1-delta.patch`

The backup is `.factory/test_logs/hf-wave1-luna/before-files.zip` with the dispatch manifest in `.factory/test_logs/hf-wave1-luna/dispatch-manifest.json`. Compared with that backup, the captured line deltas are:

| Path | Added | Removed | Final SHA256 |
| --- | ---: | ---: | --- |
| `core/workflow/runtime.py` | 21 | 3 | `1ED7F86EFE4E22F5EDE6A0E49EBB92A2067FD27596C552D51D89A3801E5AE9C0` |
| `tests/test_workflow_runtime.py` | 53 | 0 | `ABB9ACF58CE553EC8CF77A8AE3757841F396DB01FA14F75BDBBDDA8082DDF20D` |
| `spikes/runtime_choice/contracts.py` | 5 | 4 | `13054603195F340CC0EB0D0D1BC692F6F7B30208A4B5BAD49CDBFE7187CACA1E` |
| `tests/test_runtime_spike_contracts.py` | 27 | 1 | `D34728CFFD1C9E4424C0754CA69C224A4A8ACF43FEA880F1859CCDA197EE0F58` |
| `tests/test_runtime_spike_native.py` | 115 | 0 | `BFC7491A18DD0C895B5086E87BA8D2A98D9A4F69C2888D707DE35D91E4FCCD12` |

No commit, push, pull request, remote integration, or inherited-file cleanup was performed. The branch still contains the pre-existing untracked and modified work from other fronts; this report covers only the approved wave delta.

## Limits

Two tests remained skipped by the host/environment: live audio is disabled unless explicitly enabled, and symlink creation is unavailable on this Windows host. No CR-10/CR-14 or other remediation unit was changed.
