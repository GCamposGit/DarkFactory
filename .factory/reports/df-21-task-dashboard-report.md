# Relatório de Execução: DF-21 — Painel operacional de tarefas

- **Ticket**: `DF-21`
- **Título**: Painel exibe fila, run, etapa, custo, evidência e exceções
- **Branch**: `codex/df-21-task-dashboard`
- **Base**: `f79f2812503c0064481f59d93ff266326cd5b62b`
- **Commit da implementação**: commit local que contém este relatório (`feat(df-21): add operational task dashboard`)
- **Escopo**: somente leitura; nenhum fluxo de mutação do orquestrador foi adicionado.

## Entrega

1. Projeção headless `TaskDashboardReport` consolida o ledger de estados, runs/checkpoints do SQLite e custo total do ledger de uso.
2. `GET /api/tasks/dashboard` expõe fila, posição, status, etapa, run, passo, custo, evidências, exceções, saúde das fontes e avisos; `/api/tasks` é alias compatível.
3. O DarkHub ganhou seção operacional com resumo, atualização manual, cards da fila, evidências e exceções escapadas contra XSS.
4. Ledgers ausentes ou corrompidos degradam para estado explícito (`missing`/`error`) sem derrubar a página.

## Evidência de validação

```text
node --check hub/frontend/tasks.js                         PASS
python -m compileall -q hub/backend                        PASS
python -m pytest tests/test_task_dashboard.py -v            3 passed
python -m pytest tests/test_task_dashboard.py tests/test_hub.py tests/test_hub_access.py tests/test_hub_browser.py -v
                                                             30 passed
python core/harness/runner.py --quick                       HARNESS_PASS; 385 passed, 1 skipped; 386 discovered
python -m pytest tests -v --ignore=tests/test_canaletto.py  385 passed, 1 skipped
```

The guided journey is covered by the focal API/static test: load `/api/tasks/dashboard`, verify the `/api/tasks` alias, serve `/`, locate `#tasks-dashboard-section` and `/static/tasks.js`, and render the queue projection from fixture ledgers.

## RCA / residual state

- O primeiro recorte do ticket já encontrou infraestrutura local de USR-15 modificando os mesmos arquivos do Hub. O commit DF-21 deve incluir apenas os hunks do painel e o novo teste/relatório; os hunks de infraestrutura permanecem fora do commit.
- O relatório foi produzido sem rede. Push, PR, checks remotos e merge dependem de autorização explícita para publicar código no GitHub.
- Alterações locais pré-existentes preservadas fora do commit: `.factory/demands/`, `.factory/infra/`, `.factory/learning/`, `core/infra/`, `core/learning/cli.py`, `hub/backend/main.py`, `hub/frontend/infra.js`, `tests/test_infra.py`, `tests/test_roadmap_scale.py`, `tests/test_adicionar_cards_de_infrae.py`.
