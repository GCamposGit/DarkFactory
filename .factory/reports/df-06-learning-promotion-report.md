# DF-06 — Learning promotion lifecycle

## Resultado

Preferências e RCAs usam o ciclo `proposed → evaluated → active → retired`. Regras sem evidência ou com gate reprovado não entram no contexto ativo. Preferências inferidas começam como candidatas. O benchmark de autoaprendizado e cada cenário são rotulados como evidência sintética.

## Arquivos

- `core/learning/models.py`
- `core/learning/tracker.py`
- `core/learning/benchmark.py`
- `core/learning/cli.py`
- `tests/test_learning_engine.py`
- `tests/test_self_learning_benchmark.py`

## Validação

- Primeiro gate integrado: quatro falhas detectaram a entrega parcial e geraram RCA no ledger.
- `python -m pytest tests/test_learning_engine.py tests/test_self_learning_benchmark.py -v`: 11 passed.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 167 passed após a correção.
