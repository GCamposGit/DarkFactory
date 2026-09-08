# DF-18 — Agent evaluation corpus and runner

## Entrega

- `evals/tasks.jsonl`: 24 tarefas versionadas, distribuídas em 8 bugfixes, 6 features, 4 refactors, 3 security e 3 recovery.
- `evals/graders.py`: contratos fechados, materialização isolada, aplicação de patch exato, allow-list, execução sem shell, timeout e limite de saída.
- `evals/runner.py`: modos de evidência separados (`smoke` determinístico e `real` via `ModelProvider`), loader JSONL e CLI.
- `tests/test_agent_evals.py`: cobertura do corpus, referências, defeitos conhecidos, isolamento e falha fechada do modo real.

O smoke executa cada referência e espera aprovação; em seguida executa o defeito conhecido e espera reprovação. Assim, um corpus com referências frouxas ou defeitos que passam não pode declarar sucesso. O modo real não reutiliza o resultado do smoke: solicita um patch ao provider e avalia somente o candidato produzido naquela rodada.

## Validação

- `python -m pytest tests/test_agent_evals.py -v` — **5 passed**.
- `python -m evals.runner --mode smoke` — **[EVAL_PASS]**, 24/24 tarefas.
- `python core/harness/runner.py --quick` — **[HARNESS_PASS]**, 348 passed, 1 skipped.
- `python -m pytest tests -v --ignore=tests/test_canaletto.py` — 347 passed, 1 skipped, 1 falha no teste de latência RM-08 já existente; a mesma verificação passou isoladamente.
- `python -m pytest tests/test_roadmap_scale.py::test_dense_graph_500_items_1500_relations_latency_budget -vv` — **1 passed** isoladamente.
- `git diff --check` — avisos apenas em arquivos modificados antes do DF-18; nenhum arquivo do ticket foi alterado para ocultá-los.

## RCA

- `rca_ac2580b0`: resposta de provider inválida vazava a mensagem bruta do decoder; o parser agora classifica explicitamente `provider response is not valid JSON`.
- `rca_193b826f`: a falha de 219–224 ms do warm-cache RM-08 ocorre na suíte completa, mas não isoladamente; nenhuma mudança do DF-18 toca esse módulo.

Nenhum arquivo do experimento Canaletto foi alterado.
