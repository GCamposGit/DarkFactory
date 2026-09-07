# DF-02 — Governance guard

## Resultado

O guardrail agora bloqueia quando Git está indisponível, quando a referência-base é inválida ou quando a saída é malformada. A enumeração usa diff contra o commit-base validado, detecção de renames e arquivos untracked.

## Arquivos

- `core/orchestrator/guard.py`
- `tests/test_governance_guard.py`

## Validação

- Baseline: seis casos adversariais reproduziram os falsos verdes do guard antigo.
- `python -m pytest tests/test_governance_guard.py -v`: 6 passed.
- `python core/harness/runner.py --quick`: `[HARNESS_PASS]`, 167 passed no lote integrado.
