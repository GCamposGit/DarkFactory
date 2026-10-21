# SPEC

## Objetivo
Corrigir a resolução de projeto no planning da esteira line para que runs com runs.project_id canônico (ex.: darkfac) e ticket com prefixo distinto (ex.: USR-315) não caiam em UnknownProjectError por fallback indevido ao ticket_id, preservando isolamento multi-projeto e idempotência de entregas.

## Fora de escopo
- Relaxar piso de cota, Gate G1 ou isolamento de projetos.
- Recriar tickets já entregues ou reexecutar trabalho já concluído (USR-315 permanece completed).
- Alterar a identidade canônica runs.project_id ou migrar dados de runs.
- Retomada operacional real do run-5c1eeae3564c nesta etapa de planejamento.

## Desenho
Investigar a propagação de project_id até o StageContext no caminho de planning, a consulta ao store (open_line_store/get_run_status) e a versão do worker efetivamente carregada. O ponto suspeito é core/line/bindings.py, onde há fallback de contexto sem project_id para job_key.ticket_id; esse fallback deve ser reproduzido em teste antes de qualquer correção. A correção deve: (1) usar runs.project_id como identidade canônica do projeto; (2) nunca inferir projeto a partir do prefixo do ticket_id; (3) falhar de forma estruturada (erro tipado, sem fallback global para darkfac) quando a identidade do projeto estiver ausente ou desconhecida; (4) reconciliar a entrega de USR-315 sem duplicar ticket nem reexecutar trabalho. Validação cobre contexto darkfac/USR-315, projeto distinto e identidade ausente/desconhecida, com evidência de run real respeitando leases e idempotência.

## Arquivos a tocar
- core/line/bindings.py
- core/line/store.py
- core/line/planning.py
- scripts/line_validate.py
- tests/line/test_bindings_project_resolution.py
- tests/line/test_planning_unknown_project.py

## Riscos
- Fallback em bindings.py mascarar a causa raiz e a correção não cobrir o caminho real do worker.
- Worker antigo em execução continuar usando código pré-correção e reproduzir o incidente.
- Reconciliação de USR-315 duplicar ticket ou perder evidência de entrega.
- Correção introduzir fallback implícito que quebre isolamento multi-projeto.
- Testes não reproduzirem o contexto exato (project_id darkfac + ticket USR-315) e passarem sem cobrir a falha.
