# DF-05 — Racing evidence separation

## Resultado

Corridas agora distinguem `synthetic_simulation`, `validator_only` e `live`. Todo histórico é preservado, mas Pass@1, custo, latência, tokens e Elo live só recebem tentativas com inferência real executada. Ledgers legados mistos são colocados em quarentena.

## Arquivos

- `core/benchmarks/models.py`
- `core/benchmarks/racing.py`
- `tests/test_speculative_racing.py`

## Validação

- `python -m pytest tests/test_speculative_racing.py -v`: 11 passed no lote integrado.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 167 passed.
