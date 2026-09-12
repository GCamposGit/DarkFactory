# HF-02 Runtime Spike Comparison Report

- Code SHA: `3ba5bdb3e291e4bcc7b0fbb1c807a85cc6174e12`
- Baseline Snapshot: `1f931fcccc2f4a54cde1f0ba09825094dbe429af`
- Decision Status: `pending_architect_review`
- Environment Ref: `docs/handoffs/HF-02-ENVIRONMENT.md`

## Capability Matrix

| Capability | Native SQLite | DBOS PostgreSQL |
| --- | --- | --- |
| Durable Steps | True | True |
| Resume After Crash | True | True |
| Durable Wait | False | True |
| Deduplicated Intake | False | True |
| Cancel Before Next Step | False | True |
| Version Isolation | False | True |
| Bounded Concurrency | False | True |

## Scenario Results

| Scenario | Runtime | Status | Duration (ms) | Recovery (ms) | Peak RSS (MiB) | Effects |
| --- | --- | --- | --- | --- | --- | --- |
| R01 | native_sqlite | pass | 390.0 | - | 34.2 | 1 |
| R02 | native_sqlite | pass | 1657.0 | 1391.0 | 34.1 | 1 |
| R03 | native_sqlite | pass | 1750.0 | 1375.0 | 34.1 | 1 |
| R04 | native_sqlite | unsupported | 218.0 | - | 23.7 | 0 |
| R05 | native_sqlite | unsupported | 454.0 | - | 31.3 | 1 |
| R06 | native_sqlite | unsupported | 218.0 | - | 24.4 | 0 |
| R07 | native_sqlite | unsupported | 219.0 | - | 24.9 | 0 |
| R08 | native_sqlite | unsupported | 203.0 | - | 24.2 | 0 |
| R09 | native_sqlite | pass | 188.0 | - | - | 0 |
| R10 | native_sqlite | unsupported | 218.0 | - | 23.9 | 0 |
| R11 | native_sqlite | pass | 1.0 | - | - | 0 |
| R12 | native_sqlite | pass | 375.0 | - | 34.2 | 1 |
| R01 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R02 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R03 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R04 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R05 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R06 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R07 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R08 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R09 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R10 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R11 | dbos_postgres | blocked | 0.0 | - | - | 0 |
| R12 | dbos_postgres | blocked | 0.0 | - | - | 0 |
