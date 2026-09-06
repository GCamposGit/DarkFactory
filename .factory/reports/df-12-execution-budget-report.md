# DF-12 - Execution budget report

## Identidade

- Ticket: `DF-12`
- Task/owner: `/root/df12_preflight`
- Lease: `fencing_token=1`, expira em `2026-09-06T12:55:35Z`
- Branch: `codex/df-12-execution-budget`
- Worktree/cwd: `C:\dev\DarkFac_worktrees\df12`
- SHA-base: `a4fd4817fe6f911d70bc9a45187d11ac7357940e`
- SHA final: commit seletivo que contém este relatório; o valor exato consta no
  contrato WT-08 porque um commit não pode conter o próprio hash.

## Resultado

Foi criado um envelope de execução tipado e fail-closed. Reservas de custo são
serializadas por lock no mesmo trecho crítico que confere teto, concorrência,
número de tentativas e deadline. Liquidação substitui reserva por custo medido;
liberação devolve capacidade sem apagar a tentativa.

Custo desconhecido é recusado por padrão. A política conservadora alternativa
reserva todo o saldo, impedindo outra admissão até a liquidação/liberação. O
roteador passou a considerar a janela de quota conhecida mais restritiva, de
modo que uma janela curta saudável não mascare uma janela longa esgotada.

## Arquivos

- `core/execution/__init__.py`
- `core/execution/contracts.py`
- `core/execution/budget.py`
- `core/router/token_budget.py`
- `tests/test_execution_budget.py`
- `.factory/reports/df-12-execution-budget-report.md`

## Evidências

- Test-first inicial: exit code `1`; coleta bloqueada como esperado por
  `ModuleNotFoundError: core.execution`.
- `python -m pytest tests/test_execution_budget.py tests/test_token_budget_router.py -q`:
  exit code `0`; **9 passed**.
- `python core/harness/runner.py --quick`: exit code `0`; **207 passed, 1 skipped**,
  208 coletados; `[HARNESS_PASS]`.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: exit code `0`;
  **207 passed, 1 skipped**, 208 coletados.
- `git diff --check`: exit code `0`.

Os basetemps e `.factory/usage/` regenerável criados pela validação foram
removidos por caminhos absolutos validados. Canaletto não foi alterado.

## Estado de entrega

- Heartbeat final: fase `commit`, cwd e branch acima, lease token `1`.
- Commit seletivo contém somente os seis caminhos autorizados.
- Estado residual esperado após o commit: limpo.
