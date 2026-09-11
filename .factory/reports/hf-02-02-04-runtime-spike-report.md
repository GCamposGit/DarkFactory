# HF-02-02/03/04 — runtime spike implementation report

## Scope

- Ticket set: `HF-02-02`, `HF-02-03`, `HF-02-04`.
- Base checkout at implementation review: `af1c28df9b301f7f5adff49da9b6ec7919b514d3`.
- Branch: `codex/hf-01-baseline` (the workspace's `.git` directory is read-only, so a new branch could not be created).
- Canaletto: unchanged.
- PostgreSQL/DBOS real-lab access: still `waiting_access`; no DSN, secret, or external database was used.

## Delivered

### HF-02-02

- Added isolated packages under `spikes/` with no DBOS import at package initialization.
- Added strict Pydantic v2 contracts for lab configuration, scenarios, adapter capabilities, JSONL commands/events, scenario results, comparisons, and stable CLI exit codes.
- Closed enum vocabularies, root-directory validation, loopback effect URL validation, action-specific command requirements, duplicate scenario protection, and DSN/secret rejection are covered by four deterministic tests.

### HF-02-03

- Added `NativeEffectStore` backed by a separate SQLite database at `<lab-root>/effects/effects.sqlite3`.
- Added loopback-only `ThreadingHTTPServer` with a dynamic port, 16 KiB request limit, `/effects`, `/observations`, and `/approvals` endpoints, plus independent count/read queries.
- Effect operation keys are idempotent and conflict on changed identity; `commit_then_disconnect_once` commits before closing the connection and survives service restart.
- Added frozen R01–R12 catalogue. Exact catalogue SHA-256:
  `4839e911119d918dd08424829972701055feb0f32999f924010b142c51c403d9`.

### HF-02-04

- Added `NativeAdapter` using only the existing `core.orchestrator` APIs and an exclusive `<lab-root>/native/orchestrator.sqlite3` path.
- Added independent HTTP effect/observation client, threaded workflow execution, lease-based recovery, and JSONL process driver.
- Baseline gaps are explicit: durable wait, cancel-before-next-step, deduplicated intake, version isolation, and bounded concurrency return `CAPABILITY_UNSUPPORTED`; no parallel wrapper claims those capabilities.

## Validation

Commands executed:

```text
python -m pytest tests/test_runtime_spike_contracts.py -v
python -m pytest tests/test_runtime_spike_effects.py -v
python -m pytest tests/test_runtime_spike_native.py tests/test_runtime_recovery.py -v
python core/harness/runner.py --quick
python -m pytest tests -v --ignore=tests/test_canaletto.py
```

Results:

- Incremental spike tests: 11 passed.
- Harness: `[HARNESS_PASS]`, 477 collected, 475 passed, 2 skipped.
- Full required suite: 477 collected, 475 passed, 2 skipped.
- Skips are the existing live-audio probe and Windows symlink probe.
- `git diff --check`: passed.

## Remaining gate

HF-02-05/07 remain blocked until an authorized disposable PostgreSQL target and the environment variable reference are supplied. This implementation does not claim DBOS execution, production connectivity, or runtime selection.

