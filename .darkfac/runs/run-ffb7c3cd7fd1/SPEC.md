# SPEC

## Objetivo
Corrigir a resolução de projeto no planning da esteira line para que runs com project_id canônico (ex.: darkfac) e ticket USR-315 não caiam em UnknownProjectError, preservando isolamento multi-projeto, idempotência e evidência de entrega já existente.

## Fora de escopo
- Relaxar piso de cota, Gate G1 ou isolamento de projetos.
- Recriar tickets já entregues ou reexecutar trabalho já concluído.
- Alterar runs.project_id na linha runs (já confirmado como darkfac).
- Retomada operacional real do run durante esta etapa de planejamento.

## Desenho
Investigar a cadeia de propagação do project_id até o StageContext, o lookup no store (open_line_store/get_run_status) e a versão do worker efetivamente carregada. Reproduzir o cenário planning com project_id=darkfac e ticket USR-315, cobrindo também o fallback de core/line/bindings.py que usa job_key.ticket_id quando o contexto não tem project_id. A correção deve: (1) usar runs.project_id como identidade canônica; (2) nunca inferir projeto a partir do prefixo do ticket (USR); (3) falhar de forma estruturada e explícita quando a identidade do projeto estiver ausente/desconhecida, sem fallback global para darkfac; (4) reconciliar USR-315 como completed sem duplicar entrega. Testes cobrem contexto darkfac/USR-315, projeto distinto e identidade ausente/desconhecida, além de idempotência e isolamento.

## Arquivos a tocar
- core/line/bindings.py
- core/line/store.py
- core/line/worker.py
- scripts/line_validate.py
- tests/line/test_bindings.py
- tests/line/test_store.py
- tests/line/test_worker.py

## Riscos
- Fallback de job_key.ticket_id em bindings.py mascarar a causa raiz e ser mantido por engano.
- Worker antigo em execução carregar código desatualizado e invalidar a reprodução.
- Reconciliação de USR-315 duplicar ticket ou perder evidência de entrega.
- Correção introduzir vazamento de contexto entre projetos distintos.
- Falha estruturada para identidade ausente ser confundida com UnknownProjectError genérico.
