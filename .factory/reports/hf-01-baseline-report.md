# HF-01 — baseline end-to-end report

## Identity

- Ticket: `HF-01-03` → `HF-01-06`
- Origin: `user-demand`
- Base Git SHA observed: `9c5ffd9fe5521cc00fac3df2cf3d3251caa35f78`
- Working checkout: `C:\dev\DarkFac` on `main`
- Final baseline run: `hf01-20260909T000103Z-1e8bf83f`
- Snapshot: `.factory/baselines/HF-01/hf01-20260909T000103Z-1e8bf83f/snapshot.json`
- Source fingerprint: `1e8bf83f` (full fingerprint is recorded in `snapshot.json`)

## Delivered

- Pure reconciliation of declarations, evidence dimensions and dependency graph.
- Sanitized, opt-in GET probes with explicit status classification and Git receipt parsing.
- CLI with atomic UTF-8 outputs, generated run IDs, overwrite protection and replay verification.
- Markdown audit projection that keeps implementation, integration and operation separate.
- Optional evidence fields `environment` and `validation_mode`.
- Catalog corrections for the two source schemas that had drifted from the checked-in documents.

The real collection read 55 sources and reconciled 61 items. It ended with
`completeness=complete`, `hf02_readiness=ready`, 19 non-blocking audit issues and
no blocker codes. The issues preserve source conflicts, partial DF-23 coverage,
the generic n8n URL and the owner-only PostgreSQL claim; none is silently promoted
to operational proof.

## Validation

| Command | Result |
| --- | --- |
| `python core/harness/terminal_env.py --check` | `[TERMINAL_ENV_PASS]`, exit 0 |
| `python -m pytest tests/test_baseline_catalog.py tests/test_baseline_sources.py tests/test_baseline_reconcile.py tests/test_baseline_probes.py tests/test_baseline_cli.py -v` | 32 passed, 1 skipped, exit 0 |
| `python core/harness/runner.py --quick` | `[HARNESS_PASS]`, 457 collected, 455 passed, 2 skipped, exit 0 |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | 457 collected, 455 passed, 2 skipped, exit 0 |
| `python -m core.planning.baseline_cli verify --snapshot .factory/baselines/HF-01/hf01-20260909T000103Z-1e8bf83f/snapshot.json --root .` | `[BASELINE_VERIFY_PASS]`, 55 sources, 61 items, exit 0 |
| `git diff --check` | clean |

The two skipped tests are the existing live-audio opt-in and Windows symlink
permission cases. No Canaletto file was changed.

## Runtime and delivery residuals

- HF-02 can start from this local readiness result, but its spike/ADR is not part of this ticket.
- The model router's Antigravity probe returned `WinError 5`; implementation stayed on the deterministic local route and made no paid/external call.
- Commit/push/PR/merge remain pending because this session's `.git` directory is read-only (`git add` failed with permission denied) and no remote delivery was executed. The implementation is therefore locally validated, not remotely delivered.

## Evidence

The machine-readable evidence manifest is
`.factory/runs/hf01-20260909T000103Z-1e8bf83f/evidence.json`.
