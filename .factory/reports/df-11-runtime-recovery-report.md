# DF-11 — Runtime recovery report

## Resultado

Implementado o runtime recuperável local escolhido pelo spike: Python +
`sqlite3` da biblioteca padrão. O store mantém runs e checkpoints duráveis e
aplica leases atômicas por tarefa com `owner`, `token`, `fencing_token`,
`expires_at` e renovação.

Uma queda entre checkpoints deixa o run em `RUNNING`; depois da expiração,
outro owner recebe novo token/fencing e retoma do último `step_index` persistido.
O owner antigo não pode renovar, gravar checkpoint ou concluir o run.

## Arquivos do DF-11

- `core/orchestrator/store.py` — schema SQLite, transações `BEGIN IMMEDIATE`,
  runs, checkpoints, claims, fencing, renovação, conclusão e recuperação.
- `core/orchestrator/runtime.py` — execução sequencial de callbacks com
  renovação antes/depois da etapa e checkpoint pós-retorno.
- `tests/test_runtime_recovery.py` — exclusão mútua, fencing após expiração,
  renovação e recuperação após crash simulado.
- `docs/RUNTIME_DECISION.md` — spike, comparação de alternativas, contrato,
  limites e compatibilidade.
- `.factory/reports/df-11-runtime-recovery-report.md` — esta evidência.

Canaletto não foi alterado. `core/orchestrator/state.py` (DF-01) e
`core/usage/store.py` (DF-10) não foram alterados; o banco novo é separado.

## Decisões técnicas

- SQLite é o store do runtime porque oferece transação local, portabilidade
  Windows/Linux, inspeção simples e zero dependências novas.
- Há uma lease corrente por `task_id`. Um claim vivo bloqueia o concorrente;
  claim expirado é substituído dentro da mesma transação.
- `token` é um nonce por posse e `fencing_token` é um contador monotônico por
  tarefa. Mutação sem identidade completa e lease não expirada falha fechada.
- Checkpoints são JSON e monotônicos por `step_index`; efeitos externos devem
  usar idempotência própria, pois o contrato é at-least-once dentro de uma
  etapa que ainda não confirmou checkpoint.

## Evidências

- `python -m pytest tests/test_runtime_recovery.py -v`: **4 passed**.
- `python core/harness/runner.py --quick`: **190 passed, 1 skipped**;
  `[HARNESS_PASS]` emitido; `syntax_and_types` e
  `unit_and_integration_tests` passaram.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py`:
  **190 passed, 1 skipped**.
- O foco inicial falhou na coleta antes da implementação com
  `ModuleNotFoundError: core.orchestrator.runtime`, confirmando a reprodução
  esperada; após o patch, todos os testes focais passaram.
- O `model-router` classificou a tarefa como coding/high e produziu orçamento
  modular; a sondagem OpenRouter estava indisponível por política de rede e o
  CLI usou o catálogo local, sem bloquear a implementação.

Os comandos foram executados com o Python 3.12 local porque `python` não
estava no `PATH` deste host; isso não adiciona dependência ao projeto.

## Estado de entrega

- Nenhum commit criado, conforme solicitado.
- Nenhuma alteração em `canaletto_gallery/`, `run_canaletto.py` ou
  `tests/test_canaletto.py`.
- Validações obrigatórias verdes.

