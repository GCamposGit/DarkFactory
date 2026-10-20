# SPEC

## Objetivo
Corrigir a resolução de projeto no planning e reconciliar o run run-5c1eeae3564c sem reexecutar USR-315 já entregue.

## Fora de escopo
- Alterar piso de cota, Gate G1 ou isolamento de projetos.
- Usar prefixo de ticket como projeto, cadastrar projetos artificiais ou aplicar fallback para darkfac.
- Reimplementar USR-315 ou reabrir sua entrega.
- Alterar arquivos ou serviços nesta etapa de planejamento.

## Desenho
Investigar a partir da correção USR-303 (ed455155559894747804317c2fe8415002dfd3cd), comparando versão efetivamente carregada pelo worker, lookup do store e propagação até StageContext.project_id. O código consultado ainda permite que _resolve_project use JobKey.ticket_id quando project_id está ausente, enquanto project_id_for_run converte falhas de lookup em None; isso é uma hipótese reproduzível, não prova da causa operacional. Tornar runs.project_id a autoridade e distinguir lookup indisponível, run ausente e projeto não registrado com falhas estruturadas, sem inferência pelo ticket. Depois, verificar evidências de entrega de USR-315 e reconciliar o run obsoleto pelo mecanismo canônico, preservando tentativas e efeitos. Retomar execução somente para trabalho comprovadamente pendente.

## Arquivos a tocar
- core/line/bindings.py
- core/orchestrator/cloud_worker.py
- tests/line/test_bindings.py
- tests/test_postgres_real_integration.py
- core/line/recovery_context.py
- tests/line/test_recovery_context.py

## Riscos
- Worker antigo pode explicar o incidente mesmo com o checkout corrigido; HEAD ou /health isoladamente não comprovam a versão carregada.
- Falhas transitórias do store podem ser confundidas com projeto inexistente.
- Runs legados precisam resolver identidade pelo registro persistido, sem depender do formato de ticket_id.
- Reconciliar sem conferir entrega, leases e efeitos pode duplicar execução ou apagar evidências.
- A prova operacional exige acesso ao store e ao worker reais; testes locais não a substituem.
