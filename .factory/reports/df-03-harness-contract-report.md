# DF-03 — Harness evidence contract

## Resultado

Configuração ausente ou inválida falha fechada. Marcadores exigem correspondência entre início e conclusão, contagem positiva e um resultado estruturado ligado ao SHA candidato e ao hash da configuração. Marcadores emitidos por subprocessos são neutralizados.

## Arquivos

- `core/harness/markers.py`
- `core/harness/runner.py`
- `core/harness/models.py`
- `tests/test_harness_contract.py`

## Validação

- Baseline: novos testes falharam por ausência do contrato.
- `python -m pytest tests/test_harness_contract.py -v`: 9 passed.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 167 passed, resultado ligado ao SHA `de6442408427adaaf336a122c6dd7c4dbe2e9311`.
