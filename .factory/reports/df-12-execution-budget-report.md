# DF-12 — Execution budget, concurrent reservations and quota windows

## Resultado

Implementado o subsistema transacional de controle orçamentário e reservas concorrentes (**DF-12**), atendendo à Fase 1 do Plano de Desenvolvimento da Dark Factory.

- `core/execution/contracts.py`: contratos Pydantic v2 para `Budget`, `BudgetWindow`, `ReservationRecord`, `AttemptRecord`, `UnknownCostPolicy`, `AttemptOutcome` e `ReservationStatus`.
- `core/execution/budget.py`: `ExecutionBudgetManager` gerenciando alocação atômica em SQLite com serialização estrita (`BEGIN IMMEDIATE` e lock de thread), garantindo que reservas concorrentes nunca excedam o teto, respeitem limites de concorrência simultânea, respeitem prazos (`deadline`), janelas curta/longa e apliquem a política de custo desconhecido (`REJECT`, `ESTIMATE`, `CONSERVATIVE_MAX`).
- `core/router/token_budget.py`: integração funcional permitindo derivar envelopes de `Budget` a partir de estimativas de tokens (`TaskTokenEstimate`) com conversão determinística em USD (`estimate_cost_from_tokens`).
- `tests/test_execution_budget.py`: suíte dedicada com 11 testes unitários e de concorrência com threads simultâneas.

## Arquivos

- `core/execution/__init__.py`
- `core/execution/contracts.py`
- `core/execution/budget.py`
- `core/router/token_budget.py`
- `tests/test_execution_budget.py`
- `.factory/reports/df-12-execution-budget-report.md`

## Validação

- `python -m pytest tests/test_execution_budget.py -v`: 11 passed.
- `python -m pytest tests/test_token_budget_router.py tests/test_token_budget_offline.py -v`: 6 passed.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 256 passed, 1 skipped.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: 256 passed, 1 skipped.
