# Token Stress Routing — Implementation Report

## Resultado

O roteador agora prevê consumo por tarefa, avalia o headroom horário de todas as contas, alterna para uma conta mais saudável, preserva uma reserva de assinatura usando API paga quando configurada e degrada esforço/tamanho dos blocos sob pressão.

## Arquivos da mudança

- `core/router/token_budget.py`: previsor determinístico e política de pressão.
- `core/router/model_router.py`: integração programática e CLI com scan de contas por padrão.
- `tests/test_token_budget_router.py`: previsão, failover, gateway pago e degradação modular.
- `tests/test_token_budget_offline.py`: contrato do modo offline.
- `.agents/skills/03-model-router/`: política canônica e referência operacional.
- `.claude/skills/03-model-router/`: espelho sincronizado.

## Validação executada

- `python -m py_compile core/router/token_budget.py core/router/model_router.py`
- `python -m pytest tests/test_token_budget_router.py tests/test_token_budget_offline.py -v` — 6 passed.
- `python C:/Users/guigc/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/03-model-router` — skill válida.
- `python core/harness/runner.py --quick` — `[HARNESS_PASS]`; 133 testes passaram.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — 133 testes passaram.
