# DF-01 — State contract

## Resultado

Implementada máquina de estados tipada e fail-closed. Transições fora da tabela são rejeitadas, metadata livre não pode sobrescrever campos de controle e `MERGED` exige evidência vinculada aos SHAs candidato e de merge.

## Arquivos

- `core/orchestrator/state.py`
- `core/orchestrator/models.py`
- `tests/test_orchestrator_state.py`

## Validação

- Baseline: novos testes falharam por ausência do contrato.
- `python -m pytest tests/test_orchestrator_state.py -v`: 10 passed.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 167 passed no lote integrado.
