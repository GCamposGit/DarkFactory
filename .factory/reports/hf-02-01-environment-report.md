# HF-02-01 — environment freeze report

- Ticket: `HF-02-01`
- Origin: `user-demand` follow-up
- Owner: Codex local implementation
- Branch: `codex/hf-01-baseline`
- Worktree: `C:\dev\DarkFac`
- Base SHA: `1f931fcccc2f4a54cde1f0ba09825094dbe429af`
- Final implementation SHA: `f9cc002` (evidence-only amendments follow in the local history)
- Status: `waiting_access` for PostgreSQL; local implementation complete

## Scope delivered

- Added the isolated HF-02 dependency lock with the actual stable PyPI versions observed
  on 2026-09-09: DBOS 2.31.1, psycopg 3.3.5, psycopg-binary 3.3.5 and psutil 7.2.2.
- Added `versions.json` with Python/platform, API smoke, local tool discovery, validation,
  database naming and secret-reference metadata.
- Added the operator handoff for a disposable `darkfac_hf02_` database and a sanitized
  `SELECT 1` probe. No DSN or secret value is serialized.
- Kept DBOS out of default `requirements.txt`; only a reference comment was added.
- Added four deterministic tests protecting the lock, manifest state and isolation policy.

## Commands and results

| Command | Result |
| --- | --- |
| `python core/harness/terminal_env.py --check` | pass |
| `python -m pytest tests/test_hf02_environment.py -v` | 4 passed |
| dedicated venv `python -m pip check` | pass |
| second clean venv install from `requirements.lock.txt` | pass; 42 packages |
| second clean venv import/API smoke | pass |
| `python core/harness/runner.py --quick` | pass; 461 collected, 459 passed, 2 skipped |
| `python -m pytest tests -v --ignore=tests/test_canaletto.py` | pass; 459 passed, 2 skipped |

## External access boundary

The local machine has no Docker or `psql` executable and no PostgreSQL access variable.
No database, firewall, DNS, TLS, VPS or production service was mutated. The next operator
must inject `DARKFAC_HF02_DATABASE_URL` through the approved secret channel and run the
sanitized probe in `docs/handoffs/HF-02-ENVIRONMENT.md`. Until then, HF-02-05 and HF-02-07
remain blocked; HF-02-02/03/04 can proceed without the database.

## Residual delivery state

Remote publication/PR/merge was not attempted in this ticket. The repository policy still
requires explicit remote delivery before the ticket can be considered integrated.
