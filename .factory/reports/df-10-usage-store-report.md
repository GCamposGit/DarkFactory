# DF-10 — Durable usage store

## Resultado

O ledger de uso agora separa eventos recentes de um índice durável de invocações e executa toda mutação como transação atômica entre processos.

- `AtomicUsageStore` centraliza lock cross-process, leitura, migração e `os.replace`.
- `invocations` preserva a evidência necessária para deduplicar mesmo depois que o payload sai da janela de eventos recentes.
- JSON inválido é movido para `model_usage.corrupt-<timestamp>.json` e a operação falha com erro estruturado; não há reset silencioso.
- `recompute_aggregates()` reconstrói os totais a partir das invocações duráveis.
- O caminho legado fail-open foi removido do ledger.
- O harness passou a usar `compileall`, eliminando a lista manual que deixava módulos novos fora do gate sintático.

## Arquivos

- `core/usage/store.py`
- `core/usage/ledger.py`
- `tests/test_usage_monitor.py`
- `harness.config.json`

## Validação

- Baseline: a suíte não coletava porque o contrato `core.usage.store` ainda não existia.
- `python -m pytest tests/test_usage_monitor.py -v`: 12 passed.
- Casos novos: replay pós-retenção, quarentena de corrupção, recomputação e quatro processos concorrentes.
- `python core/harness/runner.py --quick`: 183 passed, `[HARNESS_PASS]`.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`: 183 passed.
- `python core/harness/runner.py | python core/harness/markers.py`: `Validation Result: PASS`, 183 testes.
